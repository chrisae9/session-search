import pytest

from session_search.capture.queue import UploadQueue
from session_search.core.records import Event, SessionRevision
from session_search.storage.chunks import CHUNK_SIZE, ChunkStore
from session_search.storage.objects import ObjectStore


def stage(queue, source, text):
    raw = ObjectStore(queue.root).put(source, chunked=True)
    session = SessionRevision('session', (Event(text, 'user', text),))
    queue.ingest(session, producer='device', request_id=text,
                 raw={**raw, 'path': str(source), 'fingerprint': text})
    return raw


class Receiver:
    def upload_raw_store(self, store, key):
        assert store.verify(key)

    def upload(self, payload):
        value = dict(payload['session'])
        value['events'] = tuple(Event(**event) for event in value['events'])
        return {'status': 'durable', 'revision': SessionRevision(**value).revision}


def test_acknowledgement_keeps_shared_pending_chunks_then_reclaims_all(tmp_path):
    source = tmp_path / 'native'
    prefix = b'a' * CHUNK_SIZE
    with UploadQueue(tmp_path / 'client') as queue:
        source.write_bytes(prefix + b'first')
        first = stage(queue, source, 'first')
        source.write_bytes(prefix + b'second')
        second = stage(queue, source, 'second')
        chunks = ChunkStore(queue.root)
        shared = chunks.recipe(first['digest'])['chunks'][0]['digest']
        assert chunks.recipe(second['digest'])['chunks'][0]['digest'] == shared
        result = queue.flush(Receiver())
        assert result['sent'] == 1
        assert result['chunk_cleanup']['removed_members'] == 2
        assert not chunks.path('recipes', first['digest']).exists()
        assert chunks.verify(second['digest'])
        assert chunks.path('chunks', shared).exists()
        result = queue.flush(Receiver())
        assert result['sent'] == 1
        assert result['chunk_cleanup']['removed_members'] == 3
        assert not list(chunks.root.glob('*/*/*'))
        assert source.read_bytes() == prefix + b'second'


def test_cleanup_retry_after_interrupted_recipe_removal(tmp_path, monkeypatch):
    source = tmp_path / 'native'
    source.write_bytes(b'retained original')
    with UploadQueue(tmp_path / 'client') as queue:
        raw = ObjectStore(queue.root).put(source, chunked=True)
        import session_search.storage.objects as objects
        original = objects.sync_directory
        monkeypatch.setattr(objects, 'sync_directory', lambda path: (_ for _ in ()).throw(OSError()))
        assert queue.flush(Receiver())['chunk_cleanup']['status'] == 'deferred'
        chunks = ChunkStore(queue.root)
        assert not chunks.path('recipes', raw['digest']).exists()
        assert list(chunks.root.glob('chunks/*/*'))
        monkeypatch.setattr(objects, 'sync_directory', original)
        assert queue.flush(Receiver())['chunk_cleanup']['status'] == 'complete'
        assert not list(chunks.root.glob('*/*/*'))
        assert source.read_bytes() == b'retained original'


@pytest.mark.parametrize('state', ['conflict', 'rejected'])
def test_cleanup_retains_all_pending_states_and_defers_on_missing_recipe(tmp_path, state):
    source = tmp_path / 'native'
    source.write_bytes(b'pending raw')
    with UploadQueue(tmp_path / 'client') as queue:
        raw = stage(queue, source, 'pending')
        with queue.db:
            queue.db.execute('UPDATE pending SET state=?', (state,))
        chunks = ChunkStore(queue.root)
        assert queue.flush(Receiver())['chunk_cleanup']['removed_members'] == 0
        assert chunks.verify(raw['digest'])
        source.write_bytes(b'orphan')
        orphan = ObjectStore(queue.root).put(source, chunked=True)
        chunks.path('recipes', raw['digest']).unlink()
        assert queue.flush(Receiver())['chunk_cleanup']['status'] == 'deferred'
        assert chunks.verify(orphan['digest'])


def test_cleanup_rejects_symlink_and_catalog_roots(tmp_path):
    source = tmp_path / 'native'
    source.write_bytes(b'raw')
    with UploadQueue(tmp_path / 'client') as queue:
        raw = ObjectStore(queue.root).put(source, chunked=True)
        chunks = ChunkStore(queue.root)
        (queue.root / 'catalog.sqlite3').touch()
        assert queue.flush(Receiver())['chunk_cleanup']['status'] == 'deferred'
        (queue.root / 'catalog.sqlite3').unlink()
        recipe = chunks.path('recipes', raw['digest'])
        recipe.unlink()
        recipe.symlink_to(source)
        assert queue.flush(Receiver())['chunk_cleanup']['status'] == 'deferred'
        assert source.read_bytes() == b'raw'
