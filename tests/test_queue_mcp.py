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


@pytest.mark.parametrize('operation', ['search', 'context'])
def test_both_servers_down_reports_local_pending_work_in_cli_and_mcp(tmp_path, monkeypatch, operation):
    from session_search.interfaces.client import Client
    from session_search.interfaces.cli import parser, remote_command
    client = Client('http://localhost:1', tmp_path / 'unused-token', standby='http://localhost:2')
    attempts = []
    def offline(endpoint, *args):
        attempts.append(endpoint)
        raise RemoteError(None)
    monkeypatch.setattr(client, '_request', offline)
    with UploadQueue(tmp_path) as queue:
        queue.ingest(revision('s', 'retained private evidence'), producer='d', request_id='one')
        arguments = ['search', 'restore'] if operation == 'search' else ['context', '[]']
        args = parser().parse_args(['--data-dir', str(tmp_path), *arguments])
        cli = remote_command(args, client)
        async def run():
            server = create_mcp(tmp_path, client)
            response = await server.call_tool(operation, {'text': 'restore'} if operation == 'search'
                                              else {'citations': []})
            return json.loads(response[0].text)
        mcp = asyncio.run(run())
        for response in (cli, mcp):
            assert response['status'] == 'unavailable'
            assert response['reason'] == 'search_servers_unreachable'
            assert 'results' not in response
            assert response['queue'] == {'pending': 1, 'states': {'pending': 1}}
            assert response['local_capture_sync']['status'] == 'not_observed'
            assert 'private evidence' not in json.dumps(response)
        assert attempts == [client.primary, client.standby] * 2
        assert queue.status()['pending'] == 1


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


def test_client_compaction_preserves_recovery_checkpoints_and_pending_work(tmp_path, monkeypatch):
    import hashlib
    from types import SimpleNamespace
    from session_search.capture import queue as queue_module
    from session_search.storage.objects import ObjectStore

    root = tmp_path / 'client'
    source = tmp_path / 'synthetic.jsonl'
    source.write_bytes(b'synthetic original')
    raw = {**ObjectStore(root).put(source), 'path': str(source), 'fingerprint': 'stable'}
    session = revision('a', 'retained evidence ' + 'x' * (2 * 1024 * 1024))

    class Online:
        def upload_raw(self, path, digest):
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest

        def upload(self, payload):
            return {'status': 'durable', 'revision': session.revision}

    with UploadQueue(root) as queue:
        queue.ingest(session, producer='device', request_id='one',
                     checkpoint=(str(source), 'stable'), raw=raw)
        for state in ('pending', 'conflict', 'rejected'):
            with queue.db:
                queue.db.execute('UPDATE pending SET state=?', (state,))
            assert queue.compact()['reason'] == 'pending_work'
            assert queue.status()['states'] == {state: 1}
        with queue.db:
            queue.db.execute("UPDATE pending SET state='pending'")
        assert queue.flush(Online())['sent'] == 1
        with queue.capture_guard():
            assert queue.compact()['reason'] == 'client_busy'
        with monkeypatch.context() as patch:
            patch.setattr(queue_module.shutil, 'disk_usage', lambda _: SimpleNamespace(free=0))
            assert queue.compact()['reason'] == 'insufficient_scratch_space'
        compacted = queue.compact()
        assert compacted['status'] == 'compacted'
        assert compacted['reclaimed_bytes'] > 1024 * 1024
    with UploadQueue(root) as reopened:
        assert reopened.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert reopened.fingerprint(str(source)) == 'stable'
        assert reopened.has_raw(str(source), 'stable')
        assert reopened.db.execute('SELECT revision FROM acknowledged').fetchone()[0] == session.revision
        assert dict(reopened.db.execute('SELECT * FROM raw_acknowledgements').fetchone()) == {
            'path': str(source), 'fingerprint': 'stable', 'digest': raw['digest'], 'size': raw['size'],
            'session_id': 'a', 'revision': session.revision}
        assert reopened.status()['pending'] == 0
        assert reopened.compact()['status'] == 'unchanged'
        # A later capture still chains to the acknowledged head after compaction.
        reopened.ingest(revision('a', 'later evidence'), producer='device', request_id='two')
        payload = json.loads(reopened.db.execute('SELECT payload FROM pending').fetchone()[0])
        assert payload['expected_revision'] == session.revision
        assert source.read_bytes() == b'synthetic original'


def test_raw_acknowledgement_is_atomic_and_tracks_only_durable_revisions(tmp_path):
    import sqlite3
    from session_search.storage.objects import ObjectStore
    root = tmp_path / 'client'
    source = tmp_path / 'synthetic.jsonl'
    source.write_bytes(b'first raw\n')
    first_raw = {**ObjectStore(root).put(source), 'path': str(source), 'fingerprint': 'first'}
    first = revision('a', 'first')
    second = revision('a', 'second')

    class Online:
        def upload_raw(self, *args):
            pass
        def upload(self, payload):
            text = payload['session']['events'][0]['text']
            return {'status': 'durable', 'revision': first.revision if text == 'first' else second.revision}

    with UploadQueue(root) as queue:
        queue.ingest(first, producer='device', request_id='one',
                     checkpoint=(str(source), 'first'), raw=first_raw)
        assert queue.db.execute('SELECT COUNT(*) FROM raw_acknowledgements').fetchone()[0] == 0
        queue.db.execute("CREATE TRIGGER fail_ack BEFORE INSERT ON raw_acknowledgements "
                         "BEGIN SELECT RAISE(ABORT, 'synthetic interruption'); END")
        queue.db.commit()
        with pytest.raises(sqlite3.IntegrityError, match='interruption'):
            queue.flush(Online())
        assert queue.status()['pending'] == 1
        assert queue.db.execute('SELECT COUNT(*) FROM acknowledged').fetchone()[0] == 0
        queue.db.execute('DROP TRIGGER fail_ack')
        queue.db.commit()
        assert queue.flush(Online())['sent'] == 1
        assert not ObjectStore(root).path(first_raw['digest']).exists()
        source.write_bytes(b'second raw\n')
        second_raw = {**ObjectStore(root).put(source), 'path': str(source), 'fingerprint': 'second'}
        queue.ingest(second, producer='device', request_id='two',
                     checkpoint=(str(source), 'second'), raw=second_raw)
        # A newer capture is not a newer durable acknowledgement.
        assert queue.db.execute('SELECT fingerprint FROM raw_acknowledgements').fetchone()[0] == 'first'
    with UploadQueue(root) as reopened:
        assert reopened.flush(Online())['sent'] == 1
        assert dict(reopened.db.execute('SELECT * FROM raw_acknowledgements').fetchone()) == {
            **second_raw, 'session_id': 'a', 'revision': second.revision}
        assert reopened.status()['pending'] == 0
        assert source.read_bytes() == b'second raw\n'


def test_old_queue_does_not_invent_raw_acknowledgement(tmp_path):
    with UploadQueue(tmp_path) as queue:
        queue.db.execute("INSERT INTO captured VALUES ('old-path','old-fingerprint')")
        queue.db.execute("INSERT INTO raw_captures VALUES ('old-path','old-fingerprint',?)", ('a' * 64,))
        queue.db.execute("INSERT INTO acknowledged VALUES ('session',?)", ('b' * 64,))
        queue.db.execute('DROP TABLE raw_acknowledgements')
        queue.db.commit()
    with UploadQueue(tmp_path) as reopened:
        assert reopened.db.execute('SELECT COUNT(*) FROM raw_acknowledgements').fetchone()[0] == 0
        assert reopened.has_raw('old-path', 'old-fingerprint')
        assert reopened.db.execute('SELECT revision FROM acknowledged').fetchone()[0] == 'b' * 64
