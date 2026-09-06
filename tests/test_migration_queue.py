import hashlib
import json

from fastapi.testclient import TestClient

from session_search.capture.queue import UploadQueue
from session_search.core.records import Event, SearchQuery, SessionRevision
from session_search.interfaces.client import Client, RemoteError
from session_search.interfaces.server import create_app
from session_search.storage.catalog import Catalog


def setup(tmp_path):
    root = tmp_path / 'primary'
    legacy = SessionRevision('s', (Event('old', 'user', 'original insight'),),
                             project='legacy-project', parser_version='legacy-archive-v1')
    with Catalog(root) as catalog:
        catalog.ingest(legacy, producer='legacy', request_id='import')
        cite = catalog.search(SearchQuery('insight'))['results'][0]['citation']
    registry = tmp_path / 'devices.json'
    registry.write_text(json.dumps({'d': hashlib.sha256(b'test-only').hexdigest()}))
    http = TestClient(create_app(root, registry))
    client = Client('http://127.0.0.1:8765', tmp_path / 'token')

    def request(endpoint, route, payload, **kwargs):
        result = http.post(route, json=payload, headers={'Authorization': 'Bearer test-only'})
        if result.status_code != 200:
            raise RemoteError(result.status_code)
        return result.json()

    client._request = request
    return root, client, cite


def test_explicit_bootstrap_preserves_imported_citation_and_queue_order(tmp_path):
    root, client, cite = setup(tmp_path)
    first = SessionRevision('s', (Event('new', 'user', 'native continuation'),),
                            project='/workspace/native-project')
    second = SessionRevision('s', (*first.events, Event('later', 'assistant', 'later evidence')),
                             project=first.project)
    with UploadQueue(tmp_path / 'queue') as queue:
        queue.ingest(first, producer='d', request_id='first')
        queue.ingest(second, producer='d', request_id='second')
        assert queue.flush(client)['failed'] == 1
        result = queue.flush(client, bootstrap_imports=True)
        assert result['bootstrapped'] == 1 and result['sent'] == 1
        assert queue.flush(client, bootstrap_imports=True)['sent'] == 1
    with Catalog(root, readonly=True) as catalog:
        assert catalog.context([cite])['results'][0]['events'][0]['text'] == 'original insight'
        assert catalog.search(SearchQuery('continuation', project='legacy-project'))['results']
        assert catalog.db.execute('SELECT revision FROM heads WHERE session_id=?', ('s',)).fetchone()[0] == second.revision


def test_intervening_native_writer_is_never_overwritten_by_bootstrap(tmp_path):
    root, client, _ = setup(tmp_path)
    original = client.migration_heads
    modern = SessionRevision('s', (Event('other', 'user', 'another client update'),))

    def race(sessions):
        heads = original(sessions)
        with Catalog(root) as catalog:
            catalog.ingest(modern, producer='other', request_id='other')
        return heads

    client.migration_heads = race
    with UploadQueue(tmp_path / 'queue') as queue:
        queue.ingest(SessionRevision('s', (Event('mine', 'user', 'local update'),)),
                     producer='d', request_id='mine')
        result = queue.flush(client, bootstrap_imports=True)
        assert result['bootstrapped'] == 1 and result['failed'] == 1
        assert queue.flush(client, bootstrap_imports=True)['bootstrapped'] == 0
        assert queue.status()['states'] == {'conflict': 1}
    with Catalog(root, readonly=True) as catalog:
        assert catalog.db.execute('SELECT revision FROM heads').fetchone()[0] == modern.revision


def test_bootstrap_does_not_adopt_an_existing_native_head(tmp_path):
    root, client, _ = setup(tmp_path)
    with Catalog(root) as catalog:
        catalog.ingest(SessionRevision('s', (Event('other', 'user', 'native head'),)),
                       producer='other', request_id='other')
    with UploadQueue(tmp_path / 'queue') as queue:
        queue.ingest(SessionRevision('s', (Event('mine', 'user', 'local update'),)),
                     producer='d', request_id='mine')
        result = queue.flush(client, bootstrap_imports=True)
        assert result['bootstrapped'] == 0 and result['failed'] == 1
