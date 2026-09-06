import hashlib
import json
import subprocess
import sys
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from session_search.core.records import Citation, Event, SearchQuery, SessionRevision
from session_search.interfaces.server import create_app
from session_search.storage.catalog import Catalog
from session_search.storage.fencing import WriteFenced, fence_primary, writer_lease
from session_search.storage.transfers import RawTransfers


def test_fence_waits_for_writers_and_preserves_reads(tmp_path):
    revision = SessionRevision('synthetic', (Event('e1', 'user', 'restore evidence'),))
    with Catalog(tmp_path) as catalog:
        catalog.ingest(revision, producer='test', request_id='first')
        citation = Citation(**catalog.search(SearchQuery('restore'))['results'][0]['citation'])
        assert fence_primary(tmp_path)['status'] == 'busy'
        assert not (tmp_path / 'FENCED.json').exists()
    with writer_lease(tmp_path):
        assert fence_primary(tmp_path)['status'] == 'busy'
    receipt = fence_primary(tmp_path)
    assert receipt['status'] == 'fenced'
    assert fence_primary(tmp_path) == receipt
    with pytest.raises(WriteFenced):
        Catalog(tmp_path)
    with Catalog(tmp_path, readonly=True) as catalog:
        assert catalog.status()['write_fenced']
        assert catalog.search(SearchQuery('restore'))['results']
        assert catalog.context([citation])['results'][0]['events'][0]['text'] == 'restore evidence'
    with pytest.raises(WriteFenced):
        RawTransfers(tmp_path).append('test', hashlib.sha256(b'x').hexdigest(), 0, 1, b'x')
    assert not (tmp_path / 'transfers').exists()


def test_crashed_writer_releases_fence_lease(tmp_path):
    with Catalog(tmp_path):
        pass
    code = ('import sys,time; from pathlib import Path; '
            'from session_search.storage.catalog import Catalog; '
            'c=Catalog(Path(sys.argv[1])); print("ready",flush=True); time.sleep(60)')
    process = subprocess.Popen([sys.executable, '-c', code, str(tmp_path)],
                               stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'ready'
        assert fence_primary(tmp_path)['status'] == 'busy'
    finally:
        process.kill()
        process.wait(timeout=10)
        process.stdout.close()
    assert fence_primary(tmp_path)['status'] == 'fenced'


def test_corrupt_fence_never_reopens_writes(tmp_path):
    with Catalog(tmp_path):
        pass
    fence_primary(tmp_path)
    (tmp_path / 'FENCED.json').write_text('{}')
    with pytest.raises(WriteFenced):
        Catalog(tmp_path)
    with pytest.raises(ValueError):
        fence_primary(tmp_path)


def test_http_fence_rejects_ingestion_but_keeps_search(tmp_path):
    root = tmp_path / 'data'
    with Catalog(root):
        pass
    registry = tmp_path / 'devices.json'
    registry.write_text(json.dumps({'test': hashlib.sha256(b'test-only').hexdigest()}))
    client = TestClient(create_app(root, registry))
    headers = {'Authorization': 'Bearer test-only'}
    fence_primary(root)
    revision = SessionRevision('synthetic', (Event('e1', 'user', 'restore'),))
    response = client.post('/v1/revisions', headers=headers,
                           json={'request_id': 'one', 'expected_revision': None, 'session': asdict(revision)})
    assert response.status_code == 503
    assert response.json()['status'] == 'primary_fenced'
    assert client.get('/v1/status', headers=headers).status_code == 200
    assert client.post('/v1/search', headers=headers, json={'text': 'restore'}).status_code == 200
    key = hashlib.sha256(b'x').hexdigest()
    assert client.put(f'/v1/objects/{key}?offset=0&total=1', headers=headers,
                      content=b'x').status_code == 503
