import fcntl
import hashlib
import os
import time

import pytest

from session_search.storage.catalog import Catalog
from session_search.storage.transfers import RawTransfers
from session_search.storage.transfer_maintenance import expire_transfers


def staged(root, namespace='raw', producer='device', data=b'expired synthetic data'):
    transfers = RawTransfers(root, namespace=namespace)
    key = hashlib.sha256(data).hexdigest()
    transfers.append(producer, key, 0, len(data), data[:5])
    partial, lock = transfers.paths(producer, key)
    old = time.time() - 10 * 86400
    os.utime(partial, (old, old))
    return partial, lock


def initialize(root):
    with Catalog(root):
        pass


def test_expiry_is_opt_in_preserves_locks_and_unmanaged_history(tmp_path):
    initialize(tmp_path)
    partial, lock = staged(tmp_path)
    normalized, normalized_lock = staged(tmp_path, 'revision-upload')
    inode = lock.stat().st_ino
    fresh, _ = staged(tmp_path, producer='fresh')
    os.utime(fresh, None)
    unmanaged = partial.parent / 'keep.part'
    unmanaged.write_bytes(b'not a managed transfer')
    native = tmp_path / 'native.jsonl'
    native.write_bytes(b'preserve native')
    before = (tmp_path / 'catalog.sqlite3').read_bytes()
    plan = expire_transfers(tmp_path, older_than_days=7)
    assert plan['status'] == 'planned' and plan['eligible_files'] == 2
    assert partial.exists() and normalized.exists()
    result = expire_transfers(tmp_path, older_than_days=7, apply=True)
    assert result['removed_files'] == 2 and result['removed_bytes'] == 10
    assert not partial.exists() and not normalized.exists()
    assert lock.stat().st_ino == inode and normalized_lock.exists()
    assert fresh.exists() and unmanaged.exists() and native.read_bytes() == b'preserve native'
    assert (tmp_path / 'catalog.sqlite3').read_bytes() == before
    assert expire_transfers(tmp_path, older_than_days=7, apply=True)['removed_files'] == 0


def test_active_writer_and_transfer_reader_defer_expiry(tmp_path):
    initialize(tmp_path)
    partial, lock = staged(tmp_path)
    with Catalog(tmp_path):
        result = expire_transfers(tmp_path, older_than_days=7, apply=True)
        assert result['reason'] == 'active_writer' and partial.exists()
    with lock.open('rb') as reader:
        fcntl.flock(reader, fcntl.LOCK_SH)
        assert expire_transfers(tmp_path, older_than_days=7, apply=True)['skipped_files'] == 1
        assert partial.exists()


def test_scan_budget_never_allows_partial_cleanup(tmp_path):
    initialize(tmp_path)
    partial, _ = staged(tmp_path)
    result = expire_transfers(tmp_path, older_than_days=7, apply=True, scan_limit=1)
    assert result['reason'] == 'scan_limit' and result['removed_files'] == 0
    assert partial.exists()


@pytest.mark.parametrize('marker', ['FENCED.json', 'manifest.json', 'CURRENT'])
def test_readonly_and_fenced_stores_are_unchanged(tmp_path, marker):
    initialize(tmp_path)
    partial, _ = staged(tmp_path)
    (tmp_path / marker).write_text('{}')
    before = set(tmp_path.iterdir())
    with pytest.raises(PermissionError):
        expire_transfers(tmp_path, older_than_days=7, apply=True)
    assert partial.exists() and set(tmp_path.iterdir()) == before


def test_special_files_and_hardlinks_are_not_removed(tmp_path):
    initialize(tmp_path)
    partial, lock = staged(tmp_path)
    target = tmp_path / 'keep'
    partial.rename(target)
    partial.symlink_to(target)
    assert expire_transfers(tmp_path, older_than_days=7, apply=True)['removed_files'] == 0
    partial.unlink()
    os.link(target, partial)
    assert expire_transfers(tmp_path, older_than_days=7, apply=True)['removed_files'] == 0
    partial.unlink()
    target.rename(partial)
    lock.unlink()
    os.mkfifo(lock)
    assert expire_transfers(tmp_path, older_than_days=7, apply=True)['removed_files'] == 0
    assert partial.exists()


@pytest.mark.parametrize('days', [0, -1, True, float('nan'), float('inf')])
def test_invalid_age(days, tmp_path):
    with pytest.raises(ValueError):
        expire_transfers(tmp_path, older_than_days=days)


@pytest.mark.parametrize('namespace', ['raw', 'revision-upload'])
def test_published_object_keeps_partial_even_if_corrupt(tmp_path, namespace):
    from session_search.storage.objects import ObjectStore
    initialize(tmp_path)
    partial, _ = staged(tmp_path, namespace)
    key = partial.name[65:-5]
    published = ObjectStore(tmp_path, namespace=namespace).path(key)
    published.parent.mkdir(parents=True)
    published.write_bytes(b'corrupt published object')
    assert expire_transfers(tmp_path, older_than_days=7, apply=True)['removed_files'] == 0
    assert partial.exists()


def test_missing_archived_object_still_keeps_recovery_partial(tmp_path):
    from session_search.core.records import Event, SessionRevision
    from session_search.storage.objects import ObjectStore
    initialize(tmp_path)
    source = tmp_path / 'source'
    source.write_bytes(b'exact archived raw bytes')
    key = hashlib.sha256(source.read_bytes()).hexdigest()
    with Catalog(tmp_path) as c:
        ObjectStore(tmp_path).put(source)
        c.ingest(SessionRevision('s', (Event('e', 'user', 'recovery'),)), producer='device',
                 request_id='1', raw={'path': str(source), 'fingerprint': 'f',
                                      'digest': key, 'size': source.stat().st_size})
    ObjectStore(tmp_path).path(key).unlink()
    partial, _ = staged(tmp_path, data=source.read_bytes())
    assert expire_transfers(tmp_path, older_than_days=7, apply=True)['removed_files'] == 0
    assert partial.exists()


def test_expiry_can_resume_after_process_exit(tmp_path):
    import subprocess
    import sys
    initialize(tmp_path)
    first, first_lock = staged(tmp_path)
    second, second_lock = staged(tmp_path, 'revision-upload')
    before = (tmp_path / 'catalog.sqlite3').read_bytes()
    script = '''
import os, sys
from pathlib import Path
from session_search.storage.transfer_maintenance import expire_transfers
original = os.unlink
def interrupted(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(73)
os.unlink = interrupted
expire_transfers(Path(sys.argv[1]), older_than_days=7, apply=True)
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                            capture_output=True, timeout=5)
    assert result.returncode == 73
    assert sum(p.exists() for p in (first, second)) == 1
    assert expire_transfers(tmp_path, older_than_days=7, apply=True)['removed_files'] == 1
    assert first_lock.exists() and second_lock.exists()
    assert (tmp_path / 'catalog.sqlite3').read_bytes() == before


def test_symlinked_directory_rejects_before_any_cleanup(tmp_path):
    root = tmp_path / 'data'
    initialize(root)
    partial, _ = staged(root)
    external = tmp_path / 'outside'
    external.mkdir()
    (root / 'revision-transfers').symlink_to(external, target_is_directory=True)
    with pytest.raises(OSError):
        expire_transfers(root, older_than_days=7, apply=True)
    assert partial.exists() and list(external.iterdir()) == []


def test_cli_plan_and_apply(tmp_path):
    import json
    import subprocess
    import sys
    initialize(tmp_path)
    partial, _ = staged(tmp_path)
    command = [sys.executable, '-m', 'session_search.interfaces.cli', '--data-dir', str(tmp_path),
               'expire-transfers', '--older-than-days', '7']
    planned = subprocess.run(command, check=True, capture_output=True, text=True, timeout=5)
    assert json.loads(planned.stdout)['eligible_files'] == 1 and partial.exists()
    applied = subprocess.run([*command, '--apply'], check=True, capture_output=True,
                             text=True, timeout=5)
    assert json.loads(applied.stdout)['removed_files'] == 1 and not partial.exists()
