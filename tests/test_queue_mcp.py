import asyncio
import json

import pytest

from session_search.capture.queue import UploadQueue
from session_search.core.records import Event, SessionRevision
from session_search.interfaces.client import RemoteError
from session_search.interfaces.credentials import update_device
from session_search.interfaces.mcp import create_mcp
from session_search.storage.catalog import Catalog


def revision(sid, text):
    return SessionRevision(sid, (Event("e1", "user", text),))


def test_pending_work_survives_reopen_and_outage(tmp_path):
    session = revision("a", "retained")
    with UploadQueue(tmp_path) as queue:
        queue.ingest(session, producer="device", request_id="one")

    class Offline:
        def upload(self, payload):
            raise RemoteError(None)

    with UploadQueue(tmp_path) as queue:
        assert queue.status()["pending"] == 1
        result = queue.flush(Offline(), now=100)
        assert result["failed"] == 1
        assert queue.status()["pending"] == 1
        assert queue.flush(Offline(), now=101)["failed"] == 0


def test_queue_orders_revisions_and_validates_acknowledgement(tmp_path):
    first = revision("a", "first")
    second = revision("a", "second")
    captured = []

    class Online:
        def upload(self, payload):
            captured.append(payload)
            rev = first.revision if len(captured) == 1 else second.revision
            return {"status": "durable", "revision": rev}

    with UploadQueue(tmp_path) as queue:
        queue.ingest(first, producer="device", request_id="one")
        queue.ingest(second, producer="device", request_id="two")
        assert queue.flush(Online())["sent"] == 1
        assert captured[0]["expected_revision"] is None
        assert queue.flush(Online())["sent"] == 1
        assert captured[1]["expected_revision"] == first.revision
        assert queue.status()["pending"] == 0


def test_conflicting_session_does_not_block_other_sessions(tmp_path):
    class Conflict:
        def upload(self, payload):
            raise RemoteError(409)

    with UploadQueue(tmp_path) as queue:
        for sid in ("a", "b"):
            queue.ingest(revision(sid, sid), producer="d", request_id=sid)
        assert queue.flush(Conflict())["failed"] == 2
        assert queue.status()["states"] == {"conflict": 2}


def test_device_token_is_private_and_not_stored_in_registry(tmp_path):
    registry = tmp_path / "registry.json"
    token = tmp_path / "token"
    update_device(registry, "device", token)
    assert token.stat().st_mode & 0o777 == 0o600
    assert token.read_text().strip() not in registry.read_text()
    with pytest.raises(ValueError):
        update_device(registry, "device", tmp_path / "other")
    update_device(registry, "device")
    assert json.loads(registry.read_text()) == {}


def test_mcp_has_exactly_three_readonly_tools_and_matches_catalog(tmp_path):
    with Catalog(tmp_path) as catalog:
        catalog.ingest(revision("a", "restore evidence"), producer="d", request_id="1")

    async def run():
        mcp = create_mcp(tmp_path)
        tools = await mcp.list_tools()
        assert {tool.name for tool in tools} == {"search", "context", "status"}
        assert all(tool.annotations.readOnlyHint for tool in tools)
        response = await mcp.call_tool("search", {"text": "restore"})
        result = json.loads(response[0].text)
        assert result["results"][0]["excerpt"] == "restore evidence"
        citation = result["results"][0]["citation"]
        expanded = await mcp.call_tool("context", {"citations": [citation]})
        assert json.loads(expanded[0].text)["results"][0]["events"][0]["text"] == "restore evidence"

    asyncio.run(run())
