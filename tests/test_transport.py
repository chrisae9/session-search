import hashlib
import json
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from session_search.core.records import Event, SessionRevision
from session_search.interfaces.client import Client, RemoteError, validate_endpoint
from session_search.interfaces.server import create_app
from session_search.storage.catalog import Catalog


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "data"
    with Catalog(root):
        pass
    credentials = tmp_path / "devices.json"
    credentials.write_text(json.dumps({"device": hashlib.sha256(b"test-only").hexdigest()}))
    app = create_app(root, credentials)
    return TestClient(app), root, credentials


def payload(text="backup fix", expected=None):
    revision = SessionRevision("s1", (Event("e1", "user", text),))
    return {"request_id": revision.revision, "expected_revision": expected,
            "session": asdict(revision)}


HEADERS = {"Authorization": "Bearer test-only"}


def test_authentication_revocation_and_strict_validation(setup):
    client, _, credentials = setup
    assert client.get("/v1/status").status_code == 401
    assert client.get("/v1/status", headers=HEADERS).status_code == 200
    response = client.post("/v1/search", headers=HEADERS, json={"text": "hi", "unknown": True})
    assert response.status_code == 400
    credentials.write_text("{}")
    assert client.get("/v1/status", headers=HEADERS).status_code == 401


def test_durable_upload_retry_conflict_and_search(setup):
    client, _, _ = setup
    first = client.post("/v1/revisions", headers=HEADERS, json=payload())
    assert first.status_code == 200
    assert first.json()["status"] == "durable"
    assert client.post("/v1/revisions", headers=HEADERS, json=payload()).json()["duplicate"]
    assert client.post("/v1/revisions", headers=HEADERS, json=payload("changed")).status_code == 409
    new = payload("changed", first.json()["revision"])
    assert client.post("/v1/revisions", headers=HEADERS, json=new).status_code == 200
    assert client.post("/v1/revisions", headers=HEADERS, json=payload()).status_code == 200
    results = client.post("/v1/search", headers=HEADERS, json={"text": "changed"}).json()
    assert results["results"]


def test_standby_never_accepts_upload(setup):
    _, root, credentials = setup
    standby = TestClient(create_app(root, credentials, readonly=True))
    assert standby.post("/v1/revisions", headers=HEADERS, json=payload()).status_code == 409
    key = "a" * 64
    assert standby.get(f"/v1/revision-objects/{key}", headers=HEADERS).status_code == 409
    assert standby.put(f"/v1/revision-objects/{key}?offset=0&total=1",
                       headers=HEADERS, content=b"x").status_code == 409
    assert standby.post("/v1/revision-objects", headers=HEADERS,
                        json={"digest": key, "size": 1}).status_code == 409
    assert standby.post("/v1/migration-heads", headers=HEADERS,
                        json={"sessions": ["s1"]}).status_code == 409


def test_large_revision_limit_and_busy_admission_return_retryable_status(setup):
    import fcntl

    from session_search.core.protocol import MAX_REVISION_UPLOAD

    client, root, _ = setup
    assert client.post("/v1/revision-objects", headers=HEADERS,
        json={"digest": "a" * 64, "size": MAX_REVISION_UPLOAD + 1}).status_code == 413
    with (root / ".revision-ingest.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert client.post("/v1/revision-objects", headers=HEADERS,
            json={"digest": "a" * 64, "size": 1}).status_code == 503


def test_legacy_locator_expands_through_authenticated_context(setup):
    client, root, _ = setup
    revision = client.post("/v1/revisions", headers=HEADERS, json=payload()).json()["revision"]
    with Catalog(root) as catalog:
        with catalog.db:
            catalog.db.execute("INSERT INTO legacy_citations VALUES (?,?,?,?)",
                               ("codex:s1:turn:7", "s1", revision, "e1"))
    response = client.post("/v1/context", headers=HEADERS,
                           json={"citations": [{"legacy_locator": "codex:s1:turn:7"}]})
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["citation"]["revision"] == revision
    assert result["events"][0]["text"] == "backup fix"


def test_client_failover_is_read_only_and_auth_errors_do_not_retry(tmp_path, monkeypatch):
    client = Client("https://primary.example", tmp_path / "token", standby="https://standby.example")
    called = []

    def request(endpoint, route, payload):
        called.append(endpoint)
        if endpoint == client.primary:
            raise RemoteError(503)
        return {"version": 1, "status": "ok"}

    monkeypatch.setattr(client, "_request", request)
    assert client.read("status")["served_by"] == "standby"
    assert len(called) == 2
    called.clear()
    with pytest.raises(RemoteError):
        client.upload({})
    assert called == [client.primary]

    def unauthorized(*args):
        raise RemoteError(401)

    monkeypatch.setattr(client, "_request", unauthorized)
    with pytest.raises(RemoteError) as error:
        client.read("status")
    assert error.value.status == 401


def test_client_refuses_unencrypted_nonloopback_and_url_credentials():
    assert validate_endpoint("http://127.0.0.1:9000")
    with pytest.raises(ValueError):
        validate_endpoint("http://remote.example")
    with pytest.raises(ValueError):
        validate_endpoint("https://user:password@remote.example")


def test_upload_deadline_does_not_delay_search_failover(tmp_path):
    import io
    token = tmp_path / 'token'
    token.write_text('synthetic-token')
    client = Client('https://primary.example', token, standby='https://standby.example',
                    timeout=2, upload_timeout=45)
    observed = []

    class Transport:
        def open(self, request, *, timeout):
            observed.append((request.full_url, timeout))
            if request.full_url == 'https://primary.example/v1/search':
                raise TimeoutError
            return io.BytesIO(b'{"version":1}')

    client.opener = Transport()
    client._request(client.primary, '/v1/objects/hash?offset=0&total=1', b'x', method='PUT')
    client._request(client.primary, '/v1/revision-objects', {'digest': 'synthetic'})
    assert client.read('search', {'text': 'example'})['served_by'] == 'standby'
    assert [timeout for _, timeout in observed] == [45, 45, 2, 2]
    with pytest.raises(ValueError, match='upload timeout'):
        Client('https://primary.example', token, upload_timeout=61)
