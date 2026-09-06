import fcntl
import json
import shutil
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


def test_crash_after_complete_receipt_retries_without_new_snapshot(tmp_path, monkeypatch):
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
        if path.name == 'latest.json':
            raise OSError('simulated publication interruption')
        save(path, value)

    monkeypatch.setattr(module, 'save_receipt', interrupted)
    with pytest.raises(OSError):
        backup_cycle(catalog, outbox, repositories)
    assert len(list((outbox / 'receipts').iterdir())) == 1
    assert (outbox / 'pending').exists()
    monkeypatch.setattr(module, 'save_receipt', save)
    result = backup_cycle(catalog, outbox, repositories)
    assert result['status'] == 'restore_verified'
    assert calls == ['one', 'two']
    assert not (outbox / 'pending').exists()
    assert Path(result['receipt']).exists()


def test_backup_status_rejects_corrupt_or_unbounded_metadata(tmp_path):
    from session_search.storage.backup_cycle import cycle_status
    path = tmp_path / "backup-cycle-status.json"
    assert cycle_status(tmp_path, None) == "not_configured"
    for text in ("{}", "x" * 8193, "null"):
        path.write_text(text)
        assert cycle_status(tmp_path, None) == {"status": "unavailable"}


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
