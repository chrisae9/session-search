import hashlib
import json
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient

from session_search.capture.local import capture_file
from session_search.capture.queue import UploadQueue
from session_search.core.records import SearchQuery
from session_search.interfaces.client import Client, RemoteError
from session_search.interfaces.server import create_app
from session_search.storage.catalog import Catalog
from test_capture import rollout


def setup(tmp_path):
    root = tmp_path / 'server'
    with Catalog(root):
        pass
    registry = tmp_path / 'registry'
    registry.write_text(json.dumps({'d': hashlib.sha256(b'test-only').hexdigest()}))
    http = TestClient(create_app(root, registry))
    client = Client('http://127.0.0.1:8765', tmp_path / 'token')

    def request(endpoint, route, payload, *, method=None):
        arguments = {'headers': {'Authorization': 'Bearer test-only'}}
        if isinstance(payload, bytes):
            arguments['content'] = payload
        elif payload is not None:
            arguments['json'] = payload
        response = http.request(method or ('POST' if payload is not None else 'GET'), route, **arguments)
        if response.status_code != 200:
            raise RemoteError(response.status_code)
        return response.json()

    client._request = request
    source = tmp_path / 'example.jsonl'
    source.write_text(rollout('original insight'))
    return root, client, source, registry


def append(source, text):
    with source.open('a') as stream:
        stream.write(json.dumps({'type': 'response_item', 'payload': {
            'type': 'message', 'role': 'assistant', 'content': [
                {'type': 'output_text', 'text': text}]}}) + '\n')


def test_lost_client_checkpoints_recover_proven_append_and_preserve_queue_order(tmp_path):
    root, client, source, _ = setup(tmp_path)
    with UploadQueue(tmp_path / 'lost-client') as queue:
        capture_file(queue, source, 'd', archive_raw=True)
        assert queue.flush(client)['sent'] == 1
    with Catalog(root, readonly=True) as catalog:
        cite = catalog.search(SearchQuery('original'))['results'][0]['citation']
    with UploadQueue(tmp_path / 'replacement-client') as queue:
        append(source, 'second insight')
        capture_file(queue, source, 'd', archive_raw=True)
        append(source, 'third insight')
        capture_file(queue, source, 'd', archive_raw=True)
        assert queue.flush(client)['failed'] == 1
        recovered = queue.flush(client, reconcile_raw_prefixes=True)
        assert recovered['reconciled'] == 1 and recovered['sent'] == 1
        assert queue.flush(client)['sent'] == 1
        assert queue.status()['pending'] == 0
    with Catalog(root, readonly=True) as catalog:
        assert catalog.search(SearchQuery('third'))['results']
        assert catalog.context([cite])['results'][0]['events'][0]['text'] == 'original insight'


@pytest.mark.parametrize('restore_empty', [False, True])
def test_forced_recapture_recovers_after_primary_rollback(tmp_path, restore_empty):
    root, client, source, _ = setup(tmp_path)
    backup = tmp_path / 'old-primary.sqlite3'
    with UploadQueue(tmp_path / 'client') as queue:
        if not restore_empty:
            capture_file(queue, source, 'd', archive_raw=True)
            assert queue.flush(client)['sent'] == 1
        with Catalog(root) as catalog, sqlite3.connect(backup) as destination:
            catalog.db.backup(destination)
        append(source, 'acknowledged after backup')
        capture_file(queue, source, 'd', archive_raw=True)
        assert queue.flush(client)['sent'] == 1
        # All API catalog connections are closed before replacing the synthetic
        # database. Raw objects remain available; backup integrity is tested separately.
        shutil.copyfile(backup, root / 'catalog.sqlite3')
        assert capture_file(queue, source, 'd', archive_raw=True)['status'] == 'unchanged'
        assert capture_file(queue, source, 'd', archive_raw=True, force=True)['status'] == 'captured'
        assert queue.flush(client)['failed'] == 1
        result = queue.flush(client, reconcile_raw_prefixes=True)
        assert result['reconciled'] == 1 and result['sent'] == 1
    with Catalog(root, readonly=True) as catalog:
        assert catalog.search(SearchQuery('acknowledged'))['results']


@pytest.mark.parametrize('archive_raw,diverge', [(False, False), (True, True)])
def test_missing_proof_or_divergent_raw_remains_conflicted(tmp_path, archive_raw, diverge):
    root, client, source, _ = setup(tmp_path)
    with Catalog(root) as catalog:
        first = capture_file(catalog, source, 'd', archive_raw=archive_raw)['revision']
    if diverge:
        source.write_text(rollout('diverged insight'))
    else:
        append(source, 'new insight')
    with UploadQueue(tmp_path / 'new-client') as queue:
        capture_file(queue, source, 'd', archive_raw=True)
        result = queue.flush(client, reconcile_raw_prefixes=True)
        assert result['reconciled'] == 0 and result['failed'] == 1
        assert queue.status()['states'] == {'conflict': 1}
    with Catalog(root, readonly=True) as catalog:
        assert catalog.db.execute('SELECT revision FROM heads').fetchone()[0] == first


def test_intervening_writer_wins_after_prefix_check(tmp_path):
    root, client, source, _ = setup(tmp_path)
    with Catalog(root) as catalog:
        capture_file(catalog, source, 'd', archive_raw=True)
    other = tmp_path / 'other' / source.name
    other.parent.mkdir()
    other.write_bytes(source.read_bytes())
    append(other, 'racing branch')
    append(source, 'local continuation')
    original = client.recovery_heads

    def race(sessions):
        heads = original(sessions)
        with Catalog(root) as catalog:
            capture_file(catalog, other, 'other', archive_raw=True)
        return heads

    client.recovery_heads = race
    with UploadQueue(tmp_path / 'client') as queue:
        capture_file(queue, source, 'd', archive_raw=True)
        result = queue.flush(client, reconcile_raw_prefixes=True)
        assert result['reconciled'] == 1 and result['failed'] == 1
        assert queue.status()['states'] == {'conflict': 1}
    with Catalog(root, readonly=True) as catalog:
        assert catalog.search(SearchQuery('racing'))['results']
        assert not catalog.search(SearchQuery('local continuation', literal=True))['results']


def test_recovery_metadata_is_primary_only_and_bounded(tmp_path):
    root, _, _, registry = setup(tmp_path)
    http = TestClient(create_app(root, registry, readonly=True))
    headers = {'Authorization': 'Bearer test-only'}
    assert http.post('/v1/recovery-heads', json={'sessions': ['example']}, headers=headers).status_code == 409
    http = TestClient(create_app(root, registry))
    assert http.post('/v1/recovery-heads', json={'sessions': ['example'] * 11}, headers=headers).status_code == 400


def test_shorter_client_file_cannot_roll_back_server_history(tmp_path):
    root, client, source, _ = setup(tmp_path)
    older = source.read_bytes()
    append(source, 'newer server evidence')
    with Catalog(root) as catalog:
        current = capture_file(catalog, source, 'd', archive_raw=True)['revision']
    source.write_bytes(older)
    with UploadQueue(tmp_path / 'older-client') as queue:
        capture_file(queue, source, 'd', archive_raw=True)
        result = queue.flush(client, reconcile_raw_prefixes=True)
        assert result['reconciled'] == 0 and result['failed'] == 1
        assert queue.status()['states'] == {'conflict': 1}
    with Catalog(root, readonly=True) as catalog:
        assert catalog.db.execute('SELECT revision FROM heads').fetchone()[0] == current
