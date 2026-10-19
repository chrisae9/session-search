import json
import time

import numpy as np
import pytest

from session_search.core.deadlines import SearchDeadline
from session_search.core.records import Event, SearchQuery, SessionRevision
from session_search.storage.catalog import Catalog
from session_search.storage.semantic import hybrid_search, index_pending
from session_search.storage import vector_cache
from test_semantic import FakeProvider


def seed(catalog, sid='s', text='recovery retained evidence', source='codex'):
    catalog.ingest(SessionRevision(sid, (Event('e', 'user', text),), source=source),
                   producer='device', request_id=sid)


def test_cached_and_uncached_filters_citations_and_no_foreground_count(tmp_path, monkeypatch):
    with Catalog(tmp_path) as c:
        seed(c)
        seed(c, 'pi', 'recovery private experiment', 'pi')
        p = FakeProvider()
        index_pending(c, p)
        statements = []
        c.db.set_trace_callback(statements.append)
        cached = hybrid_search(c, SearchQuery('restore', sources=['codex']), p)
        c.db.set_trace_callback(None)
        assert cached['vector_engine'] == 'exact_mmap'
        assert not any('COUNT(DISTINCT a.event_row)' in sql for sql in statements)
        monkeypatch.setattr(vector_cache, 'scores', lambda *a, **k: None)
        uncached = hybrid_search(c, SearchQuery('restore', sources=['codex']), p)
        assert cached['results'] == uncached['results']
        assert c.context([cached['results'][0]['citation']])['results']


def test_cache_tail_insertion_without_rebuild_and_deleted_vector_invalidation(tmp_path):
    with Catalog(tmp_path) as c:
        seed(c)
        p = FakeProvider()
        index_pending(c, p)
        q = np.asarray([1, 0], dtype='<f4')
        before = vector_cache.scores(c, p.identity, q)
        assert len(before[0]) == 1
        with c.db:
            c.db.execute('INSERT INTO vectors VALUES (?,?,?)', (p.identity.key, 'new', q.tobytes()))
        tail = vector_cache.scores(c, p.identity, q)
        assert len(tail[0]) == 2 and list(tail[1]) == [1, 1]
        with c.db:
            c.db.execute('DELETE FROM vectors WHERE content_hash=?', ('new',))
        assert vector_cache.scores(c, p.identity, q) is None
        assert vector_cache.build(c, p.identity)['status'] == 'ready'
        assert len(vector_cache.scores(c, p.identity, q)[0]) == 1


def test_corrupt_cache_falls_back_to_sqlite_without_changing_results(tmp_path):
    with Catalog(tmp_path) as c:
        seed(c)
        p = FakeProvider()
        index_pending(c, p)
        expected = hybrid_search(c, SearchQuery('restore'), p)['results']
        root = c.root / 'vector-cache' / p.identity.key
        manifest = json.loads((root / 'manifest.json').read_text())
        path = root / manifest['segments'][0]['f32']
        path.chmod(0o600)
        path.write_bytes(np.asarray([0, 1], dtype='<f4').tobytes())
        result = hybrid_search(c, SearchQuery('restore'), p)
        assert result['vector_engine'] == 'sqlite'
        assert result['results'] == expected


def test_background_cache_is_incremental_and_search_never_creates_it(tmp_path):
    with Catalog(tmp_path) as c:
        seed(c)
        p = FakeProvider()
        index_pending(c, p)
        assert vector_cache.build(c, p.identity)['status'] == 'unchanged'
        root = c.root / 'vector-cache' / p.identity.key
        (root / 'manifest.json').unlink()
    with Catalog(tmp_path, readonly=True) as c:
        assert hybrid_search(c, SearchQuery('restore'), p)['vector_engine'] == 'sqlite'
        assert not (root / 'manifest.json').exists()
        with pytest.raises(PermissionError):
            vector_cache.build(c, p.identity)


def test_cache_deadline_is_controlled_not_an_unbounded_sqlite_retry(tmp_path, monkeypatch):
    with Catalog(tmp_path) as c:
        seed(c)
        p = FakeProvider()
        index_pending(c, p)
        with pytest.raises(SearchDeadline):
            vector_cache.scores(c, p.identity, np.asarray([1, 0], dtype='<f4'), deadline=time.monotonic() - 1)
        def expired(*args, **kwargs):
            raise SearchDeadline()
        monkeypatch.setattr(vector_cache, 'scores', expired)
        result = hybrid_search(c, SearchQuery('recovery'), p)
        assert result['mode'] == 'keyword' and result['results']
        assert result['degradation_reason'] == 'search_deadline'


def test_ranked_cache_preserves_exact_top_events_with_filters_and_score_ties(tmp_path, monkeypatch):
    class Graded(FakeProvider):
        def embed(self, text, *, query=False):
            if query:
                return [1, 0]
            number = int(text.split()[-1])
            return [1, (number % 83) / 83]
    with Catalog(tmp_path) as c:
        c.configure_sources({'pi': False})
        for number in range(12):
            c.ingest(SessionRevision(str(number), tuple(
                Event(str(event), 'user', f'recovery evidence {number * 100 + event}')
                for event in range(100)), source='pi' if number % 3 == 0 else 'codex',
                is_subagent=number % 4 == 0, parent_session_id='0' if number == 5 else None),
                producer='d', request_id=str(number))
        p = Graded()
        index_pending(c, p, limit=2000)
        for query in [SearchQuery('restore',limit=100), SearchQuery('restore',include_subagents=True),
                      SearchQuery('restore',exclude_sessions=('0',)), SearchQuery('restore',all_sources=True)]:
            cached = hybrid_search(c, query, p)
            with monkeypatch.context() as patch:
                patch.setattr(vector_cache, 'scores', lambda *a, **k: None)
                reference = hybrid_search(c, query, p)
            assert cached['vector_engine'] == 'exact_mmap_ranked'
            assert cached['results'] == reference['results']
            assert cached['more_matches'] == reference['more_matches']
