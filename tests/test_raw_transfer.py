import hashlib
import json

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
def test_remote_raw_capture_queues_exact_file_and_acknowledges_after_upload(tmp_path, chunk_raw):
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
        capture_file(queue, source, "device", archive_raw=True)
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


def test_large_normalized_upload_recovers_lost_ack_without_raw_archival(tmp_path):
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
    assert "offset=1048576&" in chunk_routes[1]
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
