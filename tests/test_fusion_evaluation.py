import importlib.util
from pathlib import Path

import pytest

from session_search.core.records import Event, SearchQuery, SessionRevision
from session_search.storage.catalog import Catalog
from session_search.storage.semantic import hybrid_search, index_pending
from test_semantic import FakeProvider

spec = importlib.util.spec_from_file_location('fusion_comparison',
    Path(__file__).resolve().parents[1] / 'benchmarks' / 'compare_fusion.py')
fusion = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fusion)


def test_diagnostic_baseline_matches_production_and_rejects_other_history(tmp_path):
    cases = [{'id': 'a', 'query': 'recovery', 'evidence': 'recovery process'},
             {'id': 'b', 'query': 'pictures', 'evidence': 'unrelated pictures'}]
    provider = FakeProvider()
    with Catalog(tmp_path) as catalog:
        for case in cases:
            catalog.ingest(SessionRevision(case['id'], (Event('insight', 'assistant', case['evidence']),)),
                           producer='fixture', request_id=case['id'])
        with pytest.raises(ValueError, match='coverage'):
            fusion.compare(catalog, cases, provider)
        index_pending(catalog, provider)
        expected = []
        for case in cases:
            result = hybrid_search(catalog, SearchQuery(case['query'], role='assistant', limit=10), provider)
            rank = next(i for i, r in enumerate(result['results'], 1)
                        if r['citation']['session_id'] == case['id'])
            expected.append({'case': case['id'], 'rank': rank})
        assert fusion.compare(catalog, cases, provider)['equal_60']['cases'] == expected
        catalog.ingest(SessionRevision('unexpected', (Event('e', 'user', 'other evidence'),)),
                       producer='other', request_id='other')
        with pytest.raises(ValueError, match='synthetic fixture'):
            fusion.compare(catalog, cases, provider)
