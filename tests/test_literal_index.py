import shutil
import sqlite3

import pytest

from session_search.core.records import Event, SearchQuery, SessionRevision
from session_search.storage.catalog import Catalog
from session_search.storage.literal import build, ready
from session_search.storage.snapshots import create_snapshot, verify_snapshot


def test_literal_index_preserves_filters_order_and_new_evidence(tmp_path):
    root = tmp_path / 'catalog'
    with Catalog(root) as catalog:
        for i in range(6):
            catalog.ingest(SessionRevision(str(i), (Event('e', 'user' if i % 2 else 'assistant',
                'prefixBACKUPsuffix a"b éßİ\0tail', timestamp=f'2026-01-0{i + 1}T00:00:00Z'),),
                project='alpha' if i < 3 else 'beta', is_subagent=i == 5, parent_session_id='0' if i == 5 else None),
                producer='first' if i < 3 else 'second', request_id=str(i))
        queries = [SearchQuery(text, literal=True, limit=2, **filters)
                   for text in ('backup', 'UPs', 'a"b', 'éßİ', '\0tail', 'a', 'no matches')
                   for filters in ({}, {'role': 'user'}, {'project': 'alpha'}, {'producer': 'second'},
                       {'after': '2026-01-03T00:00:00Z'}, {'before': '2026-01-04T00:00:00Z'},
                       {'session_id': '1'}, {'include_subagents': True}, {'exclude_sessions': ('0',)})]
        baseline = [catalog.search(query) for query in queries]
        publication = catalog.publication()
        assert build(catalog)['status'] == 'ready'
        assert catalog.publication() != publication
        assert ready(catalog.db)
        for query, expected in zip(queries, baseline):
            actual = catalog.search(query)
            assert actual['results'] == expected['results']
            assert actual['more_matches'] == expected['more_matches']
        assert build(catalog)['status'] == 'unchanged'
        citation = catalog.search(SearchQuery('backup', literal=True))['results'][0]['citation']
        catalog.ingest(SessionRevision('later', (Event('e', 'user', 'new\0backup insight',
                      timestamp='2026-02-01T00:00:00Z'),)), producer='first', request_id='later')
        assert ready(catalog.db)
        query = SearchQuery('backup', literal=True)
        indexed = catalog.search(query)
        catalog.db.execute('BEGIN')
        catalog.db.execute('DROP TRIGGER literal_fts_insert')
        assert not ready(catalog.db)
        assert catalog.search(query)['results'] == indexed['results']
        catalog.db.rollback()
        assert catalog.context([citation])['results'][0]['events']
        snapshot = tmp_path / 'snapshot'
        create_snapshot(catalog, snapshot)
    assert verify_snapshot(snapshot)['status'] == 'verified'
    with Catalog(snapshot, readonly=True) as catalog:
        assert ready(catalog.db)
        assert catalog.search(query)['results'] == indexed['results']
        with pytest.raises(PermissionError):
            build(catalog)


def test_interrupted_build_rolls_back_every_index_object(tmp_path):
    with Catalog(tmp_path / 'catalog') as catalog:
        catalog.ingest(SessionRevision('s', (Event('e', 'user', 'backup'),)),
                       producer='d', request_id='one')
        publication = catalog.publication()
        catalog.db.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY
                                  if action == sqlite3.SQLITE_CREATE_TRIGGER else sqlite3.SQLITE_OK)
        with pytest.raises(sqlite3.DatabaseError):
            build(catalog)
        catalog.db.set_authorizer(None)
        assert not catalog.db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'literal_%'").fetchall()
        assert catalog.publication() == publication
        assert catalog.search(SearchQuery('backup', literal=True))['results']
        assert build(catalog)['status'] == 'ready'


def test_low_capacity_does_not_create_partial_index(tmp_path, monkeypatch):
    with Catalog(tmp_path / 'catalog') as catalog:
        monkeypatch.setattr('session_search.storage.literal.shutil.disk_usage',
                            lambda root: shutil._ntuple_diskusage(100, 100, 0))
        assert build(catalog)['status'] == 'deferred'
        assert not ready(catalog.db)
