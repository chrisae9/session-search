from dataclasses import replace
import hashlib
import sqlite3

import pytest

from session_search.core.embeddings import EmbeddingIdentity
from session_search.core.records import Event, SearchQuery, SessionRevision
from session_search.storage.catalog import Catalog
from session_search.storage.semantic import hybrid_search, index_pending
from session_search.storage.snapshots import create_snapshot, verify_snapshot


class Provider:
    identity = EmbeddingIdentity('test-project-alias', dimensions=2)

    def embed(self, text, *, query=False):
        return [1, 0]


def migrate(catalog):
    old = SessionRevision('s', (Event('old', 'user', 'recovery insight'),),
                          project='www-example', parser_version='legacy-archive-v1')
    catalog.ingest(old, producer='legacy', request_id='old')
    cite = catalog.search(SearchQuery('recovery'))['results'][0]['citation']
    native = SessionRevision('s', (Event('new', 'user', 'recovery insight'),),
                             project='/home/example/www/example')
    catalog.ingest(native, producer='client', request_id='native')
    return old, native, cite


@pytest.mark.parametrize('semantic', [False, True])
def test_alias_preserves_filters_without_leaking_across_sessions_or_project_moves(tmp_path, semantic):
    with Catalog(tmp_path) as catalog:
        old, native, cite = migrate(catalog)
        catalog.ingest(replace(native, session_id='other'), producer='client', request_id='other')
        provider = Provider()

        def search(**filters):
            query = SearchQuery('restore' if semantic else 'recovery', **filters)
            if semantic:
                index_pending(catalog, provider)
                return hybrid_search(catalog, query, provider)['results']
            return catalog.search(query)['results']

        hits = search(project='WWW-EXAMPLE', role='user')
        assert [h['citation']['session_id'] for h in hits] == ['s']
        assert hits[0]['citation']['revision'] == native.revision
        assert not search(project='www-example', role='assistant')
        assert not search(project='www-example', after='2099-01-01')
        assert not search(project='www-example', exclude_sessions=('s',))
        assert len(search(project=native.project)) == 2
        assert catalog.context([cite])['results'][0]['events'][0]['text'] == 'recovery insight'
        appended = replace(native, events=(*native.events, Event('later', 'assistant', 'recovery notes')))
        catalog.ingest(appended, producer='client', request_id='append')
        assert search(project='www-example')
        catalog.ingest(replace(appended, project='/different/project'), producer='client', request_id='move')
        assert not search(project='www-example')
        assert catalog.export_revision(old.session_id, old.revision)['project'] == old.project


def test_alias_transaction_failure_keeps_import_head(tmp_path):
    with Catalog(tmp_path) as catalog:
        catalog.db.execute("CREATE TRIGGER reject_alias BEFORE INSERT ON project_aliases "
                           "BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with pytest.raises(sqlite3.IntegrityError):
            migrate(catalog)
        assert catalog.db.execute('SELECT parser_version FROM revisions').fetchall()[0][0] == 'legacy-archive-v1'
        assert catalog.db.execute('SELECT COUNT(*) FROM revisions').fetchone()[0] == 1
        assert catalog.db.execute('SELECT COUNT(*) FROM project_aliases').fetchone()[0] == 0
        assert catalog.search(SearchQuery('recovery', project='www-example'))['results']


def test_old_readonly_catalog_without_alias_table_is_not_modified(tmp_path):
    with Catalog(tmp_path) as catalog:
        catalog.ingest(SessionRevision('s', (Event('e', 'user', 'recovery'),), project='old'),
                       producer='p', request_id='r')
        catalog.db.execute('DROP TABLE project_aliases')
    path = tmp_path / 'catalog.sqlite3'
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with Catalog(tmp_path, readonly=True) as catalog:
        assert not catalog.has_project_aliases
        assert catalog.search(SearchQuery('recovery', project='old'))['results']
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_snapshot_preserves_alias_and_rejects_invented_mapping(tmp_path):
    with Catalog(tmp_path / 'source') as catalog:
        migrate(catalog)
        create_snapshot(catalog, tmp_path / 'snapshot')
        assert verify_snapshot(tmp_path / 'snapshot')['status'] == 'verified'
        with Catalog(tmp_path / 'snapshot', readonly=True) as restored:
            assert restored.search(SearchQuery('recovery', project='www-example'))['results']
        catalog.db.execute("UPDATE project_aliases SET alias='invented'")
        catalog.db.commit()
        with pytest.raises(ValueError, match='project alias provenance'):
            create_snapshot(catalog, tmp_path / 'invalid')
        assert not (tmp_path / 'invalid').exists()
