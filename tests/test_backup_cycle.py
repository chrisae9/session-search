import fcntl
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from session_search.capture.local import capture_file
from session_search.storage.backup_cycle import backup_cycle
from session_search.storage.backups import ResticRepository, restore_backup
from session_search.storage.catalog import Catalog
from session_search.storage.objects import ObjectStore
from test_capture import rollout


def setup(tmp_path):
    source = tmp_path / 'session.jsonl'
    source.write_text(rollout('synthetic recurring backup evidence'))
    catalog = tmp_path / 'catalog'
    with Catalog(catalog) as c:
        capture_file(c, source, 'test', archive_raw=True)
    password = tmp_path / 'password'
    password.write_text('synthetic-password')
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    repositories = [ResticRepository(n, str(tmp_path / n), password, scratch) for n in ('one', 'two')]
    return source, catalog, repositories


@pytest.mark.skipif(shutil.which('restic') is None, reason='restic integration dependency missing')
def test_retry_partial_real_backup_then_recover_both(tmp_path, monkeypatch):
    source, catalog, repositories = setup(tmp_path)
    for r in repositories:
        r.initialize()
    outbox = tmp_path / 'outbox'
    original = ResticRepository.run
    calls = []
    fail = True

    def run(self, arguments, **kwargs):
        if arguments[0] == 'backup':
            calls.append(self.name)
            if self.name == 'two' and fail:
                raise RuntimeError('private repository diagnostics')
        return original(self, arguments, **kwargs)

    monkeypatch.setattr(ResticRepository, 'run', run)
    first = backup_cycle(catalog, outbox, repositories)
    assert first['status'] == 'partial'
    assert first['verified_destinations'] == ['one']
    assert 'private' not in json.dumps(first)
    assert not (outbox / 'latest.json').exists()
    assert (outbox / 'pending').exists()
    # Capture may continue while a destination is unavailable. Retry the old
    # immutable checkpoint rather than silently relabeling it as the latest one.
    from session_search.core.records import Event, SessionRevision
    with Catalog(catalog) as c:
        c.ingest(SessionRevision("newer", (Event("e", "user", "new evidence"),)),
                 producer="test", request_id="newer")
    fail = False
    second = backup_cycle(catalog, outbox, repositories)
    assert second['status'] == 'restore_verified'
    assert calls == ['one', 'two', 'two']
    assert all(r['snapshot'] == first['snapshot'] for r in second['receipts'])
    assert not (outbox / 'pending').exists()
    assert (outbox / 'latest.json').stat().st_mode & 0o777 == 0o600
    assert source.exists()
    with Catalog(catalog, readonly=True) as c:
        status = c.status()["backup"]
        assert status["status"] == "restore_verified"
        assert not status["matches_current"]
        assert status["verified_destinations"] == 2
        assert "password" not in json.dumps(status)
    for r, receipt in zip(repositories, second['receipts']):
        recovered = tmp_path / ('recovered-' + r.name)
        restore_backup(r, receipt, recovered)
        with Catalog(recovered, readonly=True) as c:
            assert c.status()['sessions'] == 1
            raw = c.db.execute('SELECT digest FROM raw_sources LIMIT 1').fetchone()[0]
            assert b''.join(ObjectStore(recovered).iter_bytes(raw)) == source.read_bytes()
    assert not list((tmp_path / 'scratch').iterdir())


def test_capacity_overlap_binding_and_interrupted_stage(tmp_path, monkeypatch):
    _, catalog, repositories = setup(tmp_path)
    outbox = tmp_path / 'outbox'
    monkeypatch.setattr('session_search.storage.backup_cycle.capacity',
                        lambda *a: {'status': 'deferred', 'reason': 'capacity'})
    assert backup_cycle(catalog, outbox, repositories)['stage'] == 'recovery_snapshot'
    assert not (outbox / 'pending').exists()
    with (outbox / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert backup_cycle(catalog, outbox, repositories)['status'] == 'coalesced'
    with pytest.raises(ValueError, match='configuration'):
        backup_cycle(catalog, outbox, list(reversed(repositories)))
    (outbox / '.snapshot-interrupted').mkdir()
    assert backup_cycle(catalog, outbox, repositories)['reason'] == 'interrupted_snapshot_staging'
    (catalog / 'manifest.json').write_text('{}')
    with pytest.raises(ValueError, match='authoritative'):
        backup_cycle(catalog, tmp_path / 'other-outbox', repositories)


@pytest.mark.parametrize("interruption", ["receipt", "cleanup"])
def test_crash_after_complete_receipt_retries_without_new_snapshot(tmp_path, monkeypatch, interruption):
    _, catalog, repositories = setup(tmp_path)
    outbox = tmp_path / 'outbox'
    calls = []

    def run(self, args):
        return json.dumps({'id': self.name * 20})

    def backup(self, path):
        from session_search.storage.snapshots import verify_snapshot
        calls.append(self.name)
        return {'version': 1, 'status': 'restore_verified', 'repository': self.name,
                'repository_id': self.name * 20, 'backup_id': 'a' * 64,
                'source_path': str(path), 'snapshot': verify_snapshot(path)['snapshot']}

    monkeypatch.setattr(ResticRepository, 'run', run)
    monkeypatch.setattr(ResticRepository, 'backup_and_verify', backup)
    from session_search.storage import backup_cycle as module
    save = module.save_receipt

    def interrupted(path, value):
        if path.name == 'latest.json' and interruption == 'receipt':
            raise OSError('simulated publication interruption')
        save(path, value)

    monkeypatch.setattr(module, 'save_receipt', interrupted)
    remove = module.shutil.rmtree

    def interrupted_cleanup(path, *args, **kwargs):
        if path.name == '.retired':
            (path / 'manifest.json').unlink()
            raise OSError('simulated cleanup interruption')
        return remove(path, *args, **kwargs)

    if interruption == 'cleanup':
        monkeypatch.setattr(module.shutil, 'rmtree', interrupted_cleanup)
    with pytest.raises(OSError):
        backup_cycle(catalog, outbox, repositories)
    assert len(list((outbox / 'receipts').iterdir())) == 1
    assert (outbox / ('pending' if interruption == 'receipt' else '.retired')).exists()
    monkeypatch.setattr(module.shutil, 'rmtree', remove)
    if interruption == 'cleanup':
        # Resume cleanup, but do not admit another snapshot under low capacity.
        monkeypatch.setattr(module, 'capacity', lambda *a: {'status': 'deferred', 'reason': 'capacity'})
    monkeypatch.setattr(module, 'save_receipt', save)
    result = backup_cycle(catalog, outbox, repositories)
    assert result['status'] == ('restore_verified' if interruption == 'receipt' else 'deferred')
    assert calls == ['one', 'two']
    assert not (outbox / 'pending').exists()
    assert not (outbox / '.retired').exists()
    if interruption == 'receipt':
        assert Path(result['receipt']).exists()
    else:
        with Catalog(catalog, readonly=True) as c:
            assert c.status()['backup']['verified_destinations'] == 0
        assert (outbox / 'latest.json').exists()


def test_backup_status_rejects_corrupt_or_unbounded_metadata(tmp_path):
    from session_search.storage.backup_cycle import cycle_status
    path = tmp_path / "backup-cycle-status.json"
    assert cycle_status(tmp_path, None) == "not_configured"
    for text in ("{}", "x" * 8193, "null"):
        path.write_text(text)
        assert cycle_status(tmp_path, None) == {"status": "unavailable"}


@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink", "dangling", "replaced"])
def test_backup_status_special_files_cannot_block_agent_status(tmp_path, kind):
    # Run in a killable process so a regression to blocking FIFO reads cannot
    # hang the test runner. Replacement exercises the open/fstat boundary.
    code = '''
import json, os, sys
from pathlib import Path
from session_search.storage.backup_cycle import cycle_status
root = Path(sys.argv[1])
kind = sys.argv[2]
path = root / "backup-cycle-status.json"
if kind == "fifo":
    os.mkfifo(path)
elif kind == "directory":
    path.mkdir()
elif kind in {"symlink", "dangling"}:
    target = root / "target"
    if kind == "symlink":
        target.write_text("{}")
    path.symlink_to(target)
else:
    path.write_text("{}")
    original = os.open
    def replace_before_open(name, flags, *args, **kwargs):
        if Path(name) == path:
            path.unlink()
            os.mkfifo(path)
        return original(name, flags, *args, **kwargs)
    os.open = replace_before_open
print(json.dumps(cycle_status(root, None)))
'''
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path), kind],
                            capture_output=True, text=True, timeout=5, check=True)
    assert json.loads(result.stdout) == {"status": "unavailable"}


def test_duplicate_repository_identity_never_completes_policy(tmp_path, monkeypatch):
    _, catalog, repositories = setup(tmp_path)
    outbox = tmp_path / "outbox"
    monkeypatch.setattr(ResticRepository, "run", lambda *args: json.dumps({"id": "a" * 64}))

    def backup(self, path):
        from session_search.storage.snapshots import verify_snapshot
        return {"version": 1, "status": "restore_verified", "repository": self.name,
                "repository_id": "a" * 64, "backup_id": "b" * 64,
                "source_path": str(path), "snapshot": verify_snapshot(path)["snapshot"]}

    monkeypatch.setattr(ResticRepository, "backup_and_verify", backup)
    result = backup_cycle(catalog, outbox, repositories)
    assert result["status"] == "partial"
    assert result["verified_destinations"] == ["one"]
    assert not (outbox / "latest.json").exists()


def test_changed_repository_does_not_count_cached_verification(tmp_path, monkeypatch):
    _, catalog, repositories = setup(tmp_path)
    identities = {"one": "a" * 64, "two": "b" * 64}
    unavailable = True

    def run(self, args):
        if self.name == "two" and unavailable:
            raise RuntimeError("unavailable")
        return json.dumps({"id": identities[self.name]})

    def backup(self, path):
        from session_search.storage.snapshots import verify_snapshot
        return {"version": 1, "status": "restore_verified", "repository": self.name,
                "repository_id": identities[self.name], "backup_id": "c" * 64,
                "source_path": str(path), "snapshot": verify_snapshot(path)["snapshot"]}

    monkeypatch.setattr(ResticRepository, "run", run)
    monkeypatch.setattr(ResticRepository, "backup_and_verify", backup)
    outbox = tmp_path / "outbox"
    assert backup_cycle(catalog, outbox, repositories)["verified_destinations"] == ["one"]
    unavailable = False
    identities["one"] = "d" * 64
    result = backup_cycle(catalog, outbox, repositories)
    assert result["status"] == "partial"
    assert result["verified_destinations"] == ["two"]
    assert not (outbox / "latest.json").exists()
    with Catalog(catalog, readonly=True) as c:
        assert c.status()["backup"]["verified_destinations"] == 1
