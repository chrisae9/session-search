import json
import os
import shutil

import pytest

from session_search.core.records import Event, SearchQuery, SessionRevision
from session_search.storage.backups import ResticRepository, backup_all, restore_backup
from session_search.storage.catalog import Catalog
from session_search.storage.chunks import CHUNK_SIZE, ChunkStore
from session_search.storage.objects import ObjectStore
from session_search.storage.recovery import prepare_primary
from session_search.storage.snapshots import create_snapshot, verify_snapshot


@pytest.mark.skipif(shutil.which('restic') is None, reason='restic integration dependency missing')
def test_both_backups_restore_chunked_revisions_and_writable_replacement(tmp_path):
    root = tmp_path / 'catalog'
    source = tmp_path / 'original'
    prefix = os.urandom(CHUNK_SIZE)
    raws = []
    with Catalog(root) as catalog:
        for i, tail in enumerate((b'first', b'first and appended')):
            source.write_bytes(prefix + tail)
            raw = ObjectStore(root).put(source, chunked=True)
            raws.append((raw, tail))
            catalog.ingest(SessionRevision('s', (Event(str(i), 'user', f'recovery evidence {i}'),)),
                           producer='test', request_id=str(i),
                           raw={**raw, 'path': str(source), 'fingerprint': str(i)})
            if i == 0:
                citation = catalog.search(SearchQuery('recovery'))['results'][0]['citation']
        snapshot = tmp_path / 'snapshot'
        create_snapshot(catalog, snapshot)
    manifest = json.loads((snapshot / 'manifest.json').read_text())
    assert manifest['version'] == 2
    assert len(list((ChunkStore(snapshot).root / 'chunks').glob('*/*'))) == 3
    password = tmp_path / 'password'
    password.write_text('synthetic-password')
    password.chmod(0o600)
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    repositories = [ResticRepository(name, str(tmp_path / name), password, scratch) for name in ('one', 'two')]
    for repository in repositories:
        repository.initialize()
    receipt = backup_all(snapshot, repositories, tmp_path / 'receipt.json')
    source.unlink()
    shutil.rmtree(root)
    shutil.rmtree(snapshot)
    for repository, row in zip(repositories, receipt['receipts']):
        restored = tmp_path / (repository.name + '-restored')
        restore_backup(repository, row, restored)
        assert verify_snapshot(restored)['snapshot'] == row['snapshot']
        chunks = ChunkStore(restored)
        for raw, tail in raws:
            output = tmp_path / (repository.name + raw['digest'])
            chunks.restore(raw['digest'], output)
            assert output.read_bytes() == prefix + tail
        replacement = tmp_path / (repository.name + '-primary')
        prepare_primary(restored, replacement, expected_snapshot=row['snapshot'], old_primary_isolated=True)
        with Catalog(replacement) as catalog:
            assert catalog.context([citation])['results'][0]['events'][0]['text'] == 'recovery evidence 0'
            catalog.ingest(SessionRevision('new', (Event('one', 'user', 'continued'),)),
                           producer='test', request_id='new')
        assert len(list((ChunkStore(replacement).root / 'chunks').glob('*/*'))) == 3
        first_key = chunks.recipe(raws[0][0]['digest'])['chunks'][0]['digest']
        assert chunks.path('chunks', first_key).stat().st_ino != ChunkStore(replacement).path('chunks', first_key).stat().st_ino


def test_chunk_snapshot_rejects_old_version_label_and_corruption(tmp_path):
    source = tmp_path / 'original'
    source.write_bytes(b'exact original')
    root = tmp_path / 'catalog'
    with Catalog(root) as catalog:
        raw = ObjectStore(root).put(source, chunked=True)
        catalog.ingest(SessionRevision('s', (Event('e', 'user', 'evidence'),)), producer='d', request_id='one',
                       raw={**raw, 'path': str(source), 'fingerprint': 'one'})
        snapshot = tmp_path / 'snapshot'
        create_snapshot(catalog, snapshot)
    path = snapshot / 'manifest.json'
    manifest = json.loads(path.read_text())
    path.write_text(json.dumps({**manifest, 'version': 1}))
    with pytest.raises(ValueError, match='version 2'):
        verify_snapshot(snapshot)
    path.write_text(json.dumps(manifest))
    chunks = ChunkStore(snapshot)
    key = chunks.recipe(raw['digest'])['chunks'][0]['digest']
    chunks.path('chunks', key).write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='missing or corrupt'):
        verify_snapshot(snapshot)
