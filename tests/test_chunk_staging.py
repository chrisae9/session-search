import pytest
import subprocess
import sys

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


@pytest.mark.parametrize('kind', ['chunks', 'recipes'])
@pytest.mark.parametrize('linked', [False, True])
def test_process_crash_temporary_cleanup_preserves_pending_raw(tmp_path, kind, linked):
    source = tmp_path / 'native'
    source.write_bytes(b'pending original')
    root = tmp_path / 'client'
    with UploadQueue(root) as queue:
        raw = stage(queue, source, 'pending')
        with queue.db:
            queue.db.execute("UPDATE pending SET state='conflict'")
    interrupted = tmp_path / 'interrupted'
    interrupted.write_bytes(b'interrupted capture')
    code = '''
import os, sys
from pathlib import Path
from session_search.capture.queue import UploadQueue
from session_search.storage.chunks import ChunkStore
original = os.link
def crash(source, destination):
    if sys.argv[3] in destination.parts:
        if sys.argv[4] == 'True':
            original(source, destination)
        os._exit(23)
    return original(source, destination)
os.link = crash
with UploadQueue(Path(sys.argv[1])) as queue, queue.capture_guard():
    ChunkStore(queue.root).put(Path(sys.argv[2]))
'''
    result = subprocess.run([sys.executable, '-c', code, str(root), str(interrupted),
                             kind, str(linked)], timeout=20)
    assert result.returncode == 23
    chunks = ChunkStore(root)
    assert list(chunks.root.glob('*/*/.chunk-*'))
    with UploadQueue(root) as queue:
        result = queue.flush(Receiver())['chunk_cleanup']
        assert result['status'] == 'complete'
        assert result['removed_temporaries'] == 1
        assert chunks.verify(raw['digest'])
        assert queue.status()['pending'] == 1
        assert not list(chunks.root.glob('*/*/.chunk-*'))
        assert queue.flush(Receiver())['chunk_cleanup']['removed_members'] == 0
    assert source.read_bytes() == b'pending original'


@pytest.mark.parametrize('problem', ['symlink', 'permissions', 'unknown_name', 'missing_recipe'])
def test_temporary_cleanup_defers_before_removal_on_invalid_state(tmp_path, problem):
    import os
    import tempfile
    source = tmp_path / 'native'
    source.write_bytes(b'pending original')
    with UploadQueue(tmp_path / 'client') as queue:
        raw = stage(queue, source, 'pending')
        with queue.db:
            queue.db.execute("UPDATE pending SET state='rejected'")
        chunks = ChunkStore(queue.root)
        recipe = chunks.path('recipes', raw['digest'])
        fd, name = tempfile.mkstemp(dir=recipe.parent, prefix='.chunk-')
        os.close(fd)
        from pathlib import Path
        temporary = Path(name)
        if problem == 'symlink':
            temporary.unlink()
            temporary.symlink_to(source)
        elif problem == 'permissions':
            temporary.chmod(0o644)
        elif problem == 'unknown_name':
            (recipe.parent / '.unknown').touch()
        else:
            recipe.unlink()
        assert queue.flush(Receiver())['chunk_cleanup']['status'] == 'deferred'
        assert temporary.exists()
        assert source.read_bytes() == b'pending original'


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
