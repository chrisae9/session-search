import hashlib
import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from session_search.capture.local import capture_file
from session_search.capture.queue import UploadQueue
from session_search.interfaces.client import Client
from session_search.interfaces.server import create_app
from session_search.storage.catalog import Catalog
from session_search.storage.objects import ObjectStore
from session_search.storage.transfers import OffsetConflict, RawTransfers
from test_capture import rollout


@pytest.mark.parametrize('storage', ['raw', 'chunks', 'revision-upload'])
def test_status_reclaims_published_partial_after_process_crash(tmp_path, storage):
    code = '''
import hashlib, os, sys
from pathlib import Path
from session_search.storage.transfers import RawTransfers
root, storage = Path(sys.argv[1]), sys.argv[2]
data = b'synthetic upload after publication crash'
key = hashlib.sha256(data).hexdigest()
transfers = RawTransfers(root, namespace='raw' if storage == 'chunks' else storage,
                        chunked=storage == 'chunks')
partial, _ = transfers.paths('device', key)
original = Path.unlink
def stop_before_unlink(path, *args, **kwargs):
    if path == partial:
        os._exit(72)
    return original(path, *args, **kwargs)
Path.unlink = stop_before_unlink
transfers.append('device', key, 0, len(data), data)
'''
    child = subprocess.run([sys.executable, '-c', code, str(tmp_path), storage], timeout=5)
    assert child.returncode == 72
    data = b'synthetic upload after publication crash'
    key = hashlib.sha256(data).hexdigest()
    transfers = RawTransfers(tmp_path, namespace='raw' if storage == 'chunks' else storage)
    partial, lock = transfers.paths('device', key)
    other, _ = transfers.paths('other', key)
    other.write_bytes(data[:10])
    assert partial.read_bytes() == data
    assert transfers.status('device', key) == {'version': 1, 'status': 'complete', 'offset': len(data)}
    assert not partial.exists()
    assert not lock.exists()
    assert other.read_bytes() == data[:10]
    assert transfers.objects.verify(key)
    assert transfers.status('device', key)['status'] == 'complete'


def test_status_cleanup_defers_for_active_transfer_or_fenced_writer(tmp_path):
    import fcntl
    data = b'synthetic completed evidence'
    key = hashlib.sha256(data).hexdigest()
    transfers = RawTransfers(tmp_path)
    transfers.append('device', key, 0, len(data), data)
    partial, lock = transfers.paths('device', key)
    partial.write_bytes(data)
    with lock.open('a') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        code = '''
import json, sys
from pathlib import Path
from session_search.storage.transfers import RawTransfers
print(json.dumps(RawTransfers(Path(sys.argv[1])).status('device', sys.argv[2])))
'''
        child = subprocess.run([sys.executable, '-c', code, str(tmp_path), key],
                               capture_output=True, text=True, check=True, timeout=5)
        assert json.loads(child.stdout)['status'] == 'complete'
        assert partial.exists()
    (tmp_path / 'FENCED.json').write_text('{}')
    assert transfers.status('device', key)['status'] == 'complete'
    assert partial.exists()
    assert transfers.objects.verify(key)


def test_status_cleanup_preserves_staging_when_completed_object_is_corrupt(tmp_path):
    data = b'synthetic completed evidence'
    key = hashlib.sha256(data).hexdigest()
    transfers = RawTransfers(tmp_path)
    transfers.append('device', key, 0, len(data), data)
    partial, _ = transfers.paths('device', key)
    partial.write_bytes(data)
    transfers.objects.path(key).write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='corrupt'):
        transfers.status('device', key)
    assert partial.read_bytes() == data


def test_interrupted_raw_transfer_resumes_at_durable_offset(tmp_path):
    data = b"synthetic raw session" * 200
    key = hashlib.sha256(data).hexdigest()
    transfers = RawTransfers(tmp_path)
    assert transfers.append("d", key, 0, len(data), data[:100])["offset"] == 100
    resumed = RawTransfers(tmp_path)
    assert resumed.status("d", key)["offset"] == 100
    with pytest.raises(OffsetConflict):
        resumed.append("d", key, 0, len(data), data)
    assert resumed.append("d", key, 100, len(data), data[100:])["status"] == "complete"
    assert ObjectStore(tmp_path).path(key).read_bytes() == data
    assert resumed.status("d", key)["status"] == "complete"


def test_corrupt_upload_is_not_published(tmp_path):
    key = hashlib.sha256(b"correct").hexdigest()
    with pytest.raises(ValueError, match="checksum"):
        RawTransfers(tmp_path).append("d", key, 0, 5, b"wrong")
    assert not ObjectStore(tmp_path).path(key).exists()


@pytest.mark.parametrize("chunk_raw", [False, True])
@pytest.mark.parametrize("chunk_staging", [False, True])
def test_remote_raw_capture_queues_exact_file_and_acknowledges_after_upload(tmp_path, chunk_raw, chunk_staging):
    server_root = tmp_path / "server"
    with Catalog(server_root):
        pass
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"device": hashlib.sha256(b"test-only").hexdigest()}))
    http = TestClient(create_app(server_root, credentials, chunk_raw=chunk_raw))
    client = Client("http://127.0.0.1:1234", tmp_path / "token")

    def request(endpoint, route, payload, *, method=None):
        kwargs = {"headers": {"Authorization": "Bearer test-only"}}
        if isinstance(payload, bytes):
            kwargs["content"] = payload
        elif payload is not None:
            kwargs["json"] = payload
        response = http.request(method or ("POST" if payload is not None else "GET"), route, **kwargs)
        assert response.status_code == 200, response.text
        return response.json()

    client._request = request
    source = tmp_path / "example.jsonl"
    source.write_text(rollout("exact raw upload"))
    with UploadQueue(tmp_path / "client") as queue:
        capture_file(queue, source, "device", archive_raw=True, chunk_raw=chunk_staging)
        assert queue.status()["pending"] == 1
        assert queue.flush(client)["sent"] == 1
    with Catalog(server_root, readonly=True) as catalog:
        row = catalog.db.execute("SELECT digest FROM raw_sources").fetchone()
        if chunk_raw:
            from session_search.storage.chunks import ChunkStore
            assert b"".join(ChunkStore(server_root).iter_bytes(row[0])) == source.read_bytes()
            assert ObjectStore(server_root).layout(row[0]) == "chunks-v1"
        else:
            assert ObjectStore(server_root).path(row[0]).read_bytes() == source.read_bytes()


def test_full_partial_file_can_finalize_after_restart(tmp_path):
    data = b"completed but not yet published"
    key = hashlib.sha256(data).hexdigest()
    transfers = RawTransfers(tmp_path)
    partial, _ = transfers.paths("d", key)
    partial.write_bytes(data)
    result = transfers.append("d", key, len(data), len(data), b"")
    assert result["status"] == "complete"


@pytest.mark.parametrize('expire_partial', [False, True])
def test_large_normalized_upload_recovers_lost_ack_without_raw_archival(tmp_path, expire_partial):
    from session_search.core.protocol import MAX_REQUEST
    from session_search.core.records import Event, SearchQuery, SessionRevision
    from session_search.interfaces.client import RemoteError

    root = tmp_path / "server"
    with Catalog(root):
        pass
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"d": hashlib.sha256(b"test-only").hexdigest()}))
    http = TestClient(create_app(root, credentials))
    client = Client("http://127.0.0.1:1234", tmp_path / "token")
    lost = False
    interrupted = False
    chunk_sizes = []
    chunk_routes = []

    def request(endpoint, route, payload, *, method=None):
        nonlocal lost, interrupted
        kwargs = {"headers": {"Authorization": "Bearer test-only"}}
        if isinstance(payload, bytes):
            kwargs["content"] = payload
            chunk_sizes.append(len(payload))
            chunk_routes.append(route)
        elif payload is not None:
            kwargs["json"] = payload
        response = http.request(method or ("POST" if payload is not None else "GET"), route, **kwargs)
        if response.status_code != 200:
            raise RemoteError(response.status_code)
        if method == "PUT" and not interrupted:
            interrupted = True
            if expire_partial:
                import os
                import time
                from session_search.storage.transfer_maintenance import expire_transfers
                key = route.split('?')[0].rsplit('/', 1)[1]
                partial, _ = RawTransfers(root, namespace='revision-upload').paths('d', key)
                old = time.time() - 10 * 86400
                os.utime(partial, (old, old))
                assert expire_transfers(root, older_than_days=7, apply=True)['removed_files'] == 1
            raise RemoteError(None)
        if route == "/v1/revision-objects" and not lost:
            lost = True
            raise RemoteError(None)
        return response.json()

    client._request = request
    session = SessionRevision("long", (Event("event", "user",
        "synthetic evidence " * (MAX_REQUEST // 18 + 1) + " finalmarker"),))
    with UploadQueue(tmp_path / "client") as queue:
        queue.ingest(session, producer="d", request_id="long-upload")
        assert queue.flush(client, now=0)["failed"] == 1
        assert queue.status()["pending"] == 1
        assert queue.flush(client, now=10)["failed"] == 1
        assert queue.flush(client, now=20)["sent"] == 1
        assert queue.status()["pending"] == 0
    assert max(chunk_sizes) <= 1024 * 1024
    assert ('offset=0&' if expire_partial else 'offset=1048576&') in chunk_routes[1]
    with Catalog(root, readonly=True) as catalog:
        assert catalog.db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 1
        assert catalog.db.execute("SELECT count(*) FROM raw_sources").fetchone()[0] == 0
        found = catalog.search(SearchQuery("finalmarker", literal=True))["results"][0]
        assert found["citation"]["offset"] > MAX_REQUEST
        context = catalog.context([found["citation"]], neighbors=0)
        assert "finalmarker" in context["results"][0]["events"][0]["text"]
    assert not (root / "objects/raw").exists()
    assert not [p for p in (root / "objects/revision-upload").rglob("*") if p.is_file()]


def test_ack_cleanup_cannot_remove_an_object_being_recaptured(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event as Signal
    from session_search.core.records import Event, SessionRevision

    root = tmp_path / 'client'
    source = tmp_path / 'source.jsonl'
    source.write_text(rollout('synthetic raw evidence'))
    raw = {**ObjectStore(root).put(source), 'path': str(source), 'fingerprint': 'synthetic'}
    session = SessionRevision('s', (Event('e', 'user', 'synthetic raw evidence'),))
    uploaded = Signal()

    class Online:
        def upload_raw(self, path, digest):
            assert path.read_bytes() == source.read_bytes()

        def upload(self, payload):
            uploaded.set()
            return {'status': 'durable', 'revision': session.revision}

    def flush():
        with UploadQueue(root) as worker:
            return worker.flush(Online())

    with ThreadPoolExecutor(max_workers=1) as pool, UploadQueue(root) as queue:
        queue.ingest(session, producer='d', request_id='first', raw=raw)
        with queue.capture_guard():
            future = pool.submit(flush)
            assert uploaded.wait(timeout=5)
            # Capture has reused the object but has not yet committed its queue
            # record. Acknowledgement cleanup must wait for this critical section.
            queue.ingest(session, producer='d', request_id='recaptured', raw=raw)
        assert future.result(timeout=5)['sent'] == 1
        assert ObjectStore(root).path(raw['digest']).exists()
        assert queue.flush(Online())['sent'] == 1
        assert not ObjectStore(root).path(raw['digest']).exists()
        assert source.exists()


@pytest.mark.parametrize("namespace", ["raw", "revision-upload"])
def test_completion_publishes_staging_inode_without_a_full_copy(tmp_path, monkeypatch, namespace):
    data = b"synthetic upload" * 100
    key = hashlib.sha256(data).hexdigest()
    transfers = RawTransfers(tmp_path, namespace=namespace)
    transfers.append("device", key, 0, len(data), data[:30])
    partial, _ = transfers.paths("device", key)
    inode = partial.stat().st_ino

    def unexpected_copy(*args, **kwargs):
        raise AssertionError("completion must reuse the verified staging inode")

    monkeypatch.setattr(ObjectStore, "put", unexpected_copy)
    result = transfers.append("device", key, 30, len(data), data[30:])
    assert result["status"] == "complete"
    assert transfers.objects.path(key).stat().st_ino == inode
    assert transfers.objects.path(key).read_bytes() == data
    assert not partial.exists()


def test_retry_after_publication_crash_keeps_exact_object_and_removes_staging(tmp_path, monkeypatch):
    from session_search.storage import transfers as module
    data = b"synthetic upload survives lost completion"
    key = hashlib.sha256(data).hexdigest()
    transfers = RawTransfers(tmp_path)
    link = module.os.link

    def publish_then_crash(source, destination):
        link(source, destination)
        raise RuntimeError("injected crash after object publication")

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "link", publish_then_crash)
        with pytest.raises(RuntimeError, match="injected crash"):
            transfers.append("device", key, 0, len(data), data)
    partial, _ = transfers.paths("device", key)
    assert partial.exists()
    inode = partial.stat().st_ino
    assert transfers.status("device", key)["status"] == "complete"
    assert transfers.append("device", key, len(data), len(data), b"")["status"] == "complete"
    assert not partial.exists()
    assert transfers.objects.path(key).stat().st_ino == inode
    assert transfers.objects.path(key).read_bytes() == data


def test_chunk_uploads_share_appended_prefix_and_recognize_retry_without_feature_flag(tmp_path):
    import os
    from session_search.storage.chunks import CHUNK_SIZE, ChunkStore
    prefix = os.urandom(CHUNK_SIZE)
    transfers = RawTransfers(tmp_path, chunked=True)
    keys = []
    for tail in (b"first", b"first and appended"):
        data = prefix + tail
        key = hashlib.sha256(data).hexdigest()
        keys.append(key)
        for offset in range(0, len(data), 1024 * 1024):
            response = transfers.append("device", key, offset, len(data), data[offset:offset+1024*1024])
        assert response["status"] == "complete"
        assert RawTransfers(tmp_path).status("device", key)["offset"] == len(data)
        assert b"".join(ChunkStore(tmp_path).iter_bytes(key)) == data
        assert not transfers.paths("device", key)[0].exists()
    chunks = ChunkStore(tmp_path)
    assert chunks.recipe(keys[0])["chunks"][0] == chunks.recipe(keys[1])["chunks"][0]
    assert len(list((chunks.root / "chunks").glob("*/*"))) == 3


def test_retry_after_chunk_recipe_publication_crash(tmp_path, monkeypatch):
    from session_search.storage.chunks import ChunkStore
    data = b"exact raw file"
    key = hashlib.sha256(data).hexdigest()
    transfers = RawTransfers(tmp_path, chunked=True)
    put = ChunkStore.put

    def publish_then_crash(store, *args, **kwargs):
        put(store, *args, **kwargs)
        raise RuntimeError("injected post-recipe crash")

    with monkeypatch.context() as patch:
        patch.setattr(ChunkStore, "put", publish_then_crash)
        with pytest.raises(RuntimeError):
            transfers.append("device", key, 0, len(data), data)
    assert transfers.status("device", key)["status"] == "complete"
    assert transfers.append("device", key, len(data), len(data), b"")["status"] == "complete"
    assert not transfers.paths("device", key)[0].exists()
    assert b"".join(ChunkStore(tmp_path).iter_bytes(key)) == data


@pytest.mark.parametrize('chunk_raw', [False, True])
@pytest.mark.parametrize('chunk_staging', [False, True])
def test_disappearing_partial_retries_without_losing_queued_raw(
    tmp_path, chunk_raw, chunk_staging,
):
    from session_search.interfaces.client import RemoteError
    from session_search.core.records import SearchQuery
    from session_search.storage.transfer_maintenance import expire_transfers
    import os
    import time

    root = tmp_path / 'server'
    with Catalog(root):
        pass
    credentials = tmp_path / 'credentials.json'
    credentials.write_text(json.dumps({'device': hashlib.sha256(b'test-only').hexdigest()}))
    http = TestClient(create_app(root, credentials, chunk_raw=chunk_raw))
    client = Client('http://127.0.0.1:1234', tmp_path / 'token')
    source = tmp_path / 'synthetic.jsonl'
    source.write_text(rollout('recover interrupted transfer', sid='synthetic'))
    original = source.read_bytes()
    key = hashlib.sha256(original).hexdigest()
    transfers = RawTransfers(root, chunked=chunk_raw)
    transfers.append('device', key, 0, len(original), original[:10])
    partial, lock = transfers.paths('device', key)
    removed = False

    def request(endpoint, route, payload, *, method=None):
        nonlocal removed
        kwargs = {'headers': {'Authorization': 'Bearer test-only'}}
        if isinstance(payload, bytes):
            kwargs['content'] = payload
        elif payload is not None:
            kwargs['json'] = payload
        response = http.request(method or ('POST' if payload is not None else 'GET'), route, **kwargs)
        if response.status_code != 200:
            raise RemoteError(response.status_code)
        result = response.json()
        if route == '/v1/objects/' + key and payload is None and not removed:
            assert result['offset'] == 10
            # Expiry after GET: the next PUT carries a now-stale offset.
            old = time.time() - 10 * 86400
            os.utime(partial, (old, old))
            assert expire_transfers(root, older_than_days=7, apply=True)['removed_files'] == 1
            removed = True
        return result

    client._request = request
    with UploadQueue(tmp_path / 'client') as queue:
        capture_file(queue, source, 'device', archive_raw=True, chunk_raw=chunk_staging)
        assert queue.flush(client, now=0)['failed'] == 1
        assert queue.db.execute('SELECT state FROM pending').fetchone()[0] == 'pending'
        assert queue.db.execute('SELECT COUNT(*) FROM raw_acknowledgements').fetchone()[0] == 0
        assert b''.join(ObjectStore(queue.root).iter_bytes(key)) == original
        with Catalog(root, readonly=True) as catalog:
            assert catalog.db.execute('SELECT COUNT(*) FROM revisions').fetchone()[0] == 0
        assert queue.flush(client, now=10)['sent'] == 1
        assert queue.status()['pending'] == 0
        assert queue.db.execute('SELECT COUNT(*) FROM raw_acknowledgements').fetchone()[0] == 1
    assert source.read_bytes() == original
    assert not lock.exists()
    assert b''.join(ObjectStore(root).iter_bytes(key)) == original
    with Catalog(root, readonly=True) as catalog:
        result = catalog.search(SearchQuery('recover interrupted transfer', literal=True))
        assert result['results']
        assert catalog.context([result['results'][0]['citation']])['status'] == 'ok'
