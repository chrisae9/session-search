import fcntl
import json

from session_search.capture.sync import sync_once
from session_search.capture.queue import UploadQueue
from session_search.interfaces.client import Client, RemoteError
from session_search.storage.catalog import Catalog
from test_capture import rollout
from test_raw_reconciliation import setup


def home(tmp_path):
    root = tmp_path / 'codex'
    sessions = root / 'sessions'
    sessions.mkdir(parents=True)
    path = sessions / 'example.jsonl'
    path.write_text(rollout('background insight'))
    return root, path


def test_remote_sync_captures_uploads_and_cleans_shared_staging(tmp_path):
    server, client, _, _ = setup(tmp_path)
    codex, source = home(tmp_path)
    root = tmp_path / 'client'
    result = sync_once(root, codex, 'd', client=client, max_bytes=1024 * 1024,
                       max_pending_bytes=1024 * 1024, archive_raw=True, chunk_raw=True)
    assert result['status'] == 'complete'
    assert result['flush_after']['sent'] == 1
    assert result['queue']['pending'] == 0
    assert source.exists()
    receipt = root / 'sync-status.json'
    assert json.loads(receipt.read_text()) == result
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert not list(root.glob('objects/chunked-raw/*/*/*'))
    with Catalog(server, readonly=True) as catalog:
        assert catalog.status()['sessions'] == 1


def test_outage_preserves_backlog_and_stops_new_admission(tmp_path):
    codex, source = home(tmp_path)
    root = tmp_path / 'client'
    client = Client('http://127.0.0.1:1', tmp_path / 'token')
    attempts = []

    def unavailable(*args, **kwargs):
        attempts.append(1)
        raise RemoteError(None)

    client._request = unavailable
    size = source.stat().st_size
    kwargs = dict(client=client, max_bytes=size, max_pending_bytes=size,
                  archive_raw=True, chunk_raw=True)
    first = sync_once(root, codex, 'd', **kwargs)
    assert first['status'] == 'partial' and first['queue']['pending'] == 1
    source.write_text(rollout('later background insight'))
    second = sync_once(root, codex, 'd', **kwargs)
    assert second['capture']['reason'] == 'backlog_byte_budget'
    assert second['queue']['pending'] == 1
    assert len(attempts) == 1


def test_local_sync_needs_no_server_and_overlapping_runs_coalesce(tmp_path):
    codex, _ = home(tmp_path)
    root = tmp_path / 'local'
    assert sync_once(root, codex, 'd', max_bytes=1024 * 1024)['status'] == 'complete'
    with (root / '.sync.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert sync_once(root, codex, 'd', max_bytes=1024)['status'] == 'coalesced'
    assert sync_once(root, codex, 'd', max_bytes=1024)['capture']['counts'] == {'unchanged': 1}


def test_sync_does_not_capture_during_another_flush(tmp_path):
    codex, _ = home(tmp_path)
    root = tmp_path / 'client'
    with UploadQueue(root):
        pass
    with (root / '.flush.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = sync_once(root, codex, 'd', client=object(), max_bytes=1024,
                           max_pending_bytes=1024)
        assert result['status'] == 'coalesced'
    with UploadQueue(root) as queue:
        assert queue.status()['pending'] == 0
        assert queue.db.execute('SELECT COUNT(*) FROM captured').fetchone()[0] == 0
