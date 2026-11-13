import json
import sqlite3
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from session_search.capture.queue import UploadQueue
from session_search.core.records import Event, SearchQuery, SessionRevision, digest
from session_search.interfaces.client import Client, RemoteError
from session_search.interfaces.server import create_app
from session_search.storage.catalog import Catalog
from session_search.storage.literal import build


def session(text):
    return SessionRevision(
        "hermes:device:profile:session",
        (Event("shared", "user", "unchanged setup"), Event("edited", "assistant", text)),
        source="hermes", parser_version="hermes-v1",
    )


def test_known_revision_reactivation_switches_search_without_rewriting_citations(tmp_path):
    a, b = session("original answer"), session("replacement answer")
    with Catalog(tmp_path) as catalog:
        catalog.ingest(a, producer="device", request_id="capture-1", reactivate=True)
        assert build(catalog)["status"] == "ready"
        a_cite = catalog.search(SearchQuery("original"))["results"][0]["citation"]
        catalog.ingest(b, producer="device", request_id="capture-2", reactivate=True)
        b_cite = catalog.search(SearchQuery("replacement"))["results"][0]["citation"]

        publication = catalog.publication()
        catalog.ingest(a, producer="device", request_id="capture-3", reactivate=True)
        assert catalog.publication() != publication
        for literal in (False, True):
            assert not catalog.search(SearchQuery("replacement", literal=literal))["results"]
            hits = catalog.search(SearchQuery("original", literal=literal))["results"]
            assert [hit["citation"] for hit in hits] == [a_cite]
            assert len(catalog.search(SearchQuery("unchanged", literal=literal))["results"]) == 1
        expanded = catalog.context([a_cite, b_cite], neighbors=0)["results"]
        assert [item["events"][0]["text"] for item in expanded] == [
            "original answer", "replacement answer",
        ]


def test_failed_reactivation_leaves_search_and_checkpoint_at_previous_revision(tmp_path):
    a, b = session("original answer"), session("replacement answer")
    with Catalog(tmp_path) as catalog:
        catalog.ingest(a, producer="device", request_id="capture-1", reactivate=True)
        catalog.ingest(b, producer="device", request_id="capture-2", reactivate=True,
                       checkpoint=("hermes-source", "before"))
        publication = catalog.publication()
        catalog.db.execute("CREATE TRIGGER reject_head BEFORE UPDATE ON heads BEGIN "
                           "SELECT RAISE(ABORT, 'injected crash'); END")
        with pytest.raises(sqlite3.IntegrityError):
            catalog.ingest(a, producer="device", request_id="capture-3", reactivate=True,
                           checkpoint=("hermes-source", "after"))
        assert not catalog.search(SearchQuery("original"))["results"]
        assert catalog.search(SearchQuery("replacement"))["results"]
        assert catalog.publication() == publication
        assert catalog.fingerprint("hermes-source") == "before"

        catalog.db.execute("DROP TRIGGER reject_head")
        catalog.ingest(a, producer="device", request_id="capture-3", reactivate=True,
                       checkpoint=("hermes-source", "after"))
        assert catalog.search(SearchQuery("original"))["results"]
        assert catalog.fingerprint("hermes-source") == "after"


def test_queued_hermes_reactivation_rejects_stale_transition_and_old_receipt_replay(tmp_path):
    a, b = session("original answer"), session("replacement answer")
    root = tmp_path / "catalog"
    with Catalog(root):
        pass
    credentials = tmp_path / "devices.json"
    credentials.write_text(json.dumps({"device": digest(b"test-only")}))
    headers = {"Authorization": "Bearer test-only"}
    client = Client("http://127.0.0.1:8765", tmp_path / "token")
    with TestClient(create_app(root, credentials)) as http:
        def request(endpoint, route, payload, **kwargs):
            response = http.post(route, json=payload, headers=headers)
            if response.status_code != 200:
                raise RemoteError(response.status_code)
            return response.json()

        client._request = request
        with UploadQueue(tmp_path / "queue") as queue:
            # Both occurrences of A can be pending at the same time. The queue
            # sends the oldest update first and chains expected head revisions.
            for index, revision in enumerate((a, b, a), start=1):
                queue.ingest(revision, producer="device", request_id=f"capture-{index}")
            for _ in range(3):
                assert queue.flush(client)["sent"] == 1
            assert queue.status()["pending"] == 0

        def post_revision(revision, request_id, expected):
            return http.post("/v1/revisions", headers=headers, json={
                "request_id": request_id, "expected_revision": expected,
                "session": asdict(revision),
            })

        assert post_revision(b, "stale", None).status_code == 409
        retry = post_revision(b, "capture-2", a.revision)
        assert retry.status_code == 200 and retry.json()["duplicate"]
        response = http.post("/v1/search", headers=headers, json={"text": "original"})
        assert response.status_code == 200
        assert response.json()["results"][0]["citation"]["revision"] == a.revision
        response = http.post("/v1/search", headers=headers, json={"text": "replacement"})
        assert response.status_code == 200 and not response.json()["results"]
