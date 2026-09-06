import hashlib
import json
import os

import pytest

from session_search.storage.chunks import CHUNK_SIZE, ChunkStore


def test_appended_raw_revisions_share_chunks_and_restore_exactly(tmp_path):
    store = ChunkStore(tmp_path / 'archive')
    source = tmp_path / 'raw'
    prefix = os.urandom(CHUNK_SIZE)
    source.write_bytes(prefix + b'first tail')
    first = store.put(source)
    source.write_bytes(prefix + b'first tail and appended evidence')
    second = store.put(source)
    assert first['digest'] != second['digest']
    assert store.recipe(first['digest'])['chunks'][0] == store.recipe(second['digest'])['chunks'][0]
    assert len(list((store.root / 'chunks').glob('*/*'))) == 3
    for result, expected in [(first, prefix + b'first tail'),
                             (second, prefix + b'first tail and appended evidence')]:
        output = tmp_path / result['digest']
        store.restore(result['digest'], output)
        assert output.read_bytes() == expected
        assert hashlib.sha256(expected).hexdigest() == result['digest']
        assert store.verify(result['digest'])
        with pytest.raises(FileExistsError):
            store.restore(result['digest'], output)
    assert store.put(source) == second
    assert len(list((store.root / 'chunks').glob('*/*'))) == 3


def test_corruption_never_publishes_a_restore(tmp_path):
    source = tmp_path / 'raw'
    source.write_bytes(b'recoverable original')
    store = ChunkStore(tmp_path / 'archive')
    result = store.put(source)
    key = result['digest']
    chunk = store.recipe(key)['chunks'][0]['digest']
    store.path('chunks', chunk).write_bytes(b'damaged')
    assert not store.verify(key)
    with pytest.raises(ValueError):
        store.restore(key, tmp_path / 'output')
    assert not (tmp_path / 'output').exists()
    assert not list(tmp_path.glob('.raw-restore-*'))
    with pytest.raises(ValueError, match='corrupt'):
        store.put(source)


def test_interrupted_publication_has_no_recipe_and_retry_reuses_chunks(tmp_path, monkeypatch):
    source = tmp_path / 'raw'
    source.write_bytes(b'exact raw bytes')
    store = ChunkStore(tmp_path / 'archive')
    key = hashlib.sha256(source.read_bytes()).hexdigest()
    publish = store._publish

    def fail_recipe(path, content):
        if 'recipes' in path.parts:
            raise OSError('injected interruption')
        publish(path, content)

    with monkeypatch.context() as patch:
        patch.setattr(store, '_publish', fail_recipe)
        with pytest.raises(OSError):
            store.put(source)
    assert not store.path('recipes', key).exists()
    assert not store.verify(key)
    assert store.put(source)['digest'] == key
    assert store.verify(key)


def test_empty_file_and_invalid_recipe(tmp_path):
    source = tmp_path / 'empty'
    source.write_bytes(b'')
    store = ChunkStore(tmp_path / 'archive')
    result = store.put(source)
    assert store.recipe(result['digest'])['chunks'] == []
    assert store.verify(result['digest'])
    recipe = store.path('recipes', result['digest'])
    value = json.loads(recipe.read_text())
    value['size'] = -1
    recipe.write_text(json.dumps(value))
    assert not store.verify(result['digest'])
    with pytest.raises(ValueError):
        store.put(source, expected_digest='../invalid')


def test_low_space_allows_verified_reuse_but_refuses_new_chunk(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from session_search.storage import chunks
    source = tmp_path / 'raw'
    source.write_bytes(b'existing raw file')
    store = ChunkStore(tmp_path / 'archive')
    first = store.put(source)
    monkeypatch.setattr(chunks.shutil, 'disk_usage', lambda path: SimpleNamespace(free=0))
    assert store.put(source) == first
    source.write_bytes(b'a changed raw file')
    new_key = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(OSError, match='insufficient space'):
        store.put(source)
    assert not store.path('recipes', new_key).exists()
    assert store.verify(first['digest'])
