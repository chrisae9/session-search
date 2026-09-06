from types import SimpleNamespace

import pytest

from session_search.core.records import Event, SearchQuery, SessionRevision
from session_search.core.embeddings import EmbeddingIdentity
from session_search.storage.catalog import Catalog
from session_search.storage import metadata_index
from session_search.storage.snapshots import create_snapshot
from session_search.storage.semantic import hybrid_search, index_pending


class Provider:
    identity = EmbeddingIdentity('test-metadata-index', dimensions=2)

    def embed(self, text, *, query=False):
        return [1, 0]


def populate(catalog):
    for i in range(80):
        events = (Event('u', 'user', 'recovery ' * (i + 1) + '🧠 metadata',
                        '2026-01-01', 'summary' if i % 4 == 0 else 'user'),
                  Event('a', 'assistant', 'backup ' + 'wide payload ' * 1500, '2026-02-01'))
        catalog.ingest(SessionRevision(str(i), events, project=f'project-{i % 3}',
                                      parent_session_id='0' if i else None, is_subagent=i % 5 == 0),
                       producer='device', request_id=str(i))


def test_index_preserves_entire_responses_filters_and_later_capture(tmp_path, monkeypatch):
    with Catalog(tmp_path / 'store') as c:
        populate(c)
        assert metadata_index.build(c, reserve_bytes=0)['status'] == 'ready'
        queries = [SearchQuery('recovery backup', limit=n, **filters)
                   for n in (1, 50, 100)
                   for filters in ({}, {'role': 'user'}, {'role': 'assistant'},
                                   {'project': 'project-1', 'producer': 'device'},
                                   {'after': '2026-01-15', 'before': '2026-03-01'},
                                   {'exclude_sessions': ('0',)},
                                   {'exclude_sessions': ('5',), 'include_subagents': True},
                                   {'session_id': '12'},
                                   {'include_subagents': True})]
        queries += [SearchQuery('"recovery" 🧠'), SearchQuery('not-present'),
                    SearchQuery('wide payload', literal=True)]
        for query in queries:
            with monkeypatch.context() as patch:
                patch.setattr(metadata_index, 'ready', lambda db: False)
                reference = c.search(query)
            assert c.search(query) == reference
        provider = Provider()
        index_pending(c, provider, limit=1000)
        for query in queries[:6]:
            with monkeypatch.context() as patch:
                patch.setattr(metadata_index, 'ready', lambda db: False)
                reference = hybrid_search(c, query, provider)
            assert hybrid_search(c, query, provider) == reference
        old = c.search(SearchQuery('recovery', session_id='12'))['results'][0]['citation']
        c.ingest(SessionRevision('12', (Event('new', 'user', 'new recovery insight'),), project='new'),
                 producer='device', request_id='later')
        query = SearchQuery('insight', project='new')
        with monkeypatch.context() as patch:
            patch.setattr(metadata_index, 'ready', lambda db: False)
            reference = c.search(query)
        assert c.search(query) == reference
        assert c.context([old])['status'] == 'ok'
        assert metadata_index.build(c)['status'] == 'unchanged'
        create_snapshot(c, tmp_path / 'snapshot')
    with Catalog(tmp_path / 'snapshot', readonly=True) as c:
        assert metadata_index.ready(c.db)
        assert c.search(query)['results']
        with pytest.raises(PermissionError):
            metadata_index.build(c)


def test_space_deferral_and_failed_build_preserve_existing_catalog(tmp_path, monkeypatch):
    with Catalog(tmp_path) as c:
        c.ingest(SessionRevision('s', (Event('e', 'user', 'recovery'),)), producer='p', request_id='r')
        before = c.publication()
        with monkeypatch.context() as patch:
            patch.setattr(metadata_index.shutil, 'disk_usage', lambda p: SimpleNamespace(free=0))
            assert metadata_index.build(c)['status'] == 'deferred'
        assert not metadata_index.ready(c.db) and c.publication() == before
        with monkeypatch.context() as patch:
            def fail():
                raise RuntimeError('injected publication failure')
            patch.setattr(c, 'bump_publication', fail)
            with pytest.raises(RuntimeError):
                metadata_index.build(c, reserve_bytes=0)
        assert not metadata_index.ready(c.db) and c.publication() == before
        assert c.search(SearchQuery('recovery'))['results']


def test_partial_or_incompatible_index_is_never_used(tmp_path):
    with Catalog(tmp_path) as c:
        c.ingest(SessionRevision('s', (Event('e', 'user', 'recovery'),)), producer='p', request_id='r')
        c.db.execute(f"CREATE INDEX {metadata_index.NAME} ON evidence(row_id,session_id,role,timestamp,origin) WHERE role='assistant'")
        assert not metadata_index.ready(c.db)
        assert c.search(SearchQuery('recovery'))['results']
        with pytest.raises(ValueError, match='incompatible definition'):
            metadata_index.build(c)


def test_metadata_ranking_retains_imported_project_aliases(tmp_path, monkeypatch):
    with Catalog(tmp_path) as c:
        old = SessionRevision('s', (Event('old', 'user', 'migration insight'),),
                              project='legacy-label', parser_version='legacy-archive-v1')
        c.ingest(old, producer='legacy', request_id='old')
        metadata_index.build(c, reserve_bytes=0)
        native = SessionRevision('s', (Event('new', 'user', 'migration insight'),),
                                 project='/workspace/project')
        c.ingest(native, producer='client', request_id='native')
        query = SearchQuery('insight', project='legacy-label', role='user')
        with monkeypatch.context() as patch:
            patch.setattr(metadata_index, 'ready', lambda db: False)
            reference = c.search(query)
        assert c.search(query) == reference
        assert reference['results'][0]['citation']['revision'] == native.revision
