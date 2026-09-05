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


def test_remote_raw_capture_queues_exact_file_and_acknowledges_after_upload(tmp_path):
    server_root = tmp_path / "server"
    with Catalog(server_root):
        pass
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"device": hashlib.sha256(b"test-only").hexdigest()}))
    http = TestClient(create_app(server_root, credentials))
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
