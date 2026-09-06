import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest

from session_search.interfaces.client import Client, RemoteError
from session_search.storage.chunks import CHUNK_SIZE, ChunkStore
from session_search.storage.objects import ObjectStore
from session_search.storage.transfers import MAX_CHUNK, RawTransfers


@pytest.mark.parametrize('chunked', [False, True])
@pytest.mark.parametrize('offset', [0, 123, CHUNK_SIZE + 17])
def test_resume_raw_layout_without_reconstruction(tmp_path, chunked, offset):
    source = tmp_path / 'source'
    content = b'a' * CHUNK_SIZE + b'b' * 100
    source.write_bytes(content)
    store = ObjectStore(tmp_path / 'client')
    raw = store.put(source, chunked=chunked)
    source.unlink()
    transfers = RawTransfers(tmp_path / 'server')
    for start in range(0, offset, MAX_CHUNK):
        transfers.append('device', raw['digest'], start, len(content),
                         content[start:min(start + MAX_CHUNK, offset)])
    client = Client('http://127.0.0.1:1234', tmp_path / 'token')
    sent = []

    def request(endpoint, route, payload, *, method=None):
        if payload is None:
            return transfers.status('device', raw['digest'])
        query = parse_qs(urlsplit(route).query)
        sent.append(len(payload))
        assert len(payload) <= MAX_CHUNK
        return transfers.append('device', raw['digest'], int(query['offset'][0]),
                                int(query['total'][0]), payload)

    client._request = request
    before = set(store.data_root.rglob('*'))
    assert client.upload_raw_store(store, raw['digest'])['status'] == 'complete'
    assert sum(sent) == len(content) - offset
    assert ObjectStore(tmp_path / 'server').path(raw['digest']).read_bytes() == content
    assert set(store.data_root.rglob('*')) == before
    assert client.upload_raw_store(store, raw['digest'])['status'] == 'complete'


def test_resume_verifies_skipped_chunks(tmp_path):
    source = tmp_path / 'source'
    source.write_bytes(b'a' * CHUNK_SIZE + b'tail')
    store = ObjectStore(tmp_path / 'client')
    raw = store.put(source, chunked=True)
    chunks = ChunkStore(store.data_root)
    key = chunks.recipe(raw['digest'])['chunks'][0]['digest']
    chunks.path('chunks', key).write_bytes(b'corrupt')
    client = Client('http://127.0.0.1:1234', tmp_path / 'token')
    calls = []

    def request(endpoint, route, payload, *, method=None):
        calls.append(payload)
        return {'status': 'pending', 'offset': CHUNK_SIZE}

    client._request = request
    with pytest.raises(ValueError, match='corrupt'):
        client.upload_raw_store(store, raw['digest'])
    assert calls == [None]


def test_premature_server_completion_is_rejected(tmp_path):
    source = tmp_path / 'source'
    source.write_bytes(b'x' * (MAX_CHUNK + 1))
    store = ObjectStore(tmp_path / 'client')
    raw = store.put(source, chunked=True)
    client = Client('http://127.0.0.1:1234', tmp_path / 'token')
    client._request = lambda endpoint, route, payload, **kwargs: (
        {'status': 'pending', 'offset': 0} if payload is None else
        {'status': 'complete', 'offset': len(payload)})
    with pytest.raises(RemoteError):
        client.upload_raw_store(store, raw['digest'])


def test_file_stream_detects_corruption(tmp_path):
    source = tmp_path / 'source'
    source.write_bytes(b'original')
    store = ObjectStore(tmp_path / 'client')
    raw = store.put(source)
    store.path(raw['digest']).write_bytes(b'changed!')
    with pytest.raises(ValueError, match='checksum'):
        list(store.iter_bytes(raw['digest']))
    assert raw['digest'] == hashlib.sha256(b'original').hexdigest()
