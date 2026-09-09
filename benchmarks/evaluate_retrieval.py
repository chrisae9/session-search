"""Evaluate fixed synthetic memories without reading any native session directories."""

import argparse
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import statistics
import time

from session_search.core.embeddings import load_provider
from session_search.core.records import Event, SearchQuery, SessionRevision
from session_search.storage.catalog import Catalog
from session_search.storage.semantic import hybrid_search, index_pending


def summarize(rows):
    durations = sorted(row['milliseconds'] for row in rows)
    return {'queries': len(rows), 'recall_at_10': sum(r['rank'] is not None for r in rows) / len(rows),
            'mrr_at_10': sum(1 / r['rank'] if r['rank'] else 0 for r in rows) / len(rows),
            'top_one': sum(r['rank'] == 1 for r in rows) / len(rows),
            'latency_ms': {'median': statistics.median(durations),
                           'p95': durations[math.ceil(len(durations) * .95) - 1]}, 'cases': rows}


def evaluate(catalog, cases, provider=None):
    rows = []
    for case in cases:
        start = time.perf_counter()
        result = hybrid_search(catalog, SearchQuery(case['query'], role='assistant', limit=10), provider)
        elapsed = time.perf_counter() - start
        expected = case['id']
        rank = next((i for i, hit in enumerate(result['results'], 1)
                     if hit['citation']['session_id'] == expected), None)
        rows.append({'case': expected, 'rank': rank, 'milliseconds': round(elapsed * 1000, 3),
                     'mode': result.get('mode', 'keyword')})
    return summarize(rows)


def evaluate_vectors(catalog, cases, provider):
    """Small-corpus exact cosine baseline, separate from production rank merging."""
    import numpy as np
    from session_search.core.embeddings import validate_vector
    vectors = catalog.db.execute(
        'SELECT e.session_id,v.vector FROM events e JOIN heads h '
        'ON e.session_id=h.session_id AND e.revision=h.revision '
        'JOIN event_chunks ec ON e.row_id=ec.event_row JOIN vectors v '
        'ON ec.content_hash=v.content_hash WHERE v.identity=? AND e.role=?',
        (provider.identity.key, 'assistant'),
    ).fetchall()
    matrix = np.stack([np.frombuffer(r['vector'], dtype='<f4') for r in vectors])
    rows = []
    for case in cases:
        start = time.perf_counter()
        query = np.asarray(validate_vector(provider.embed(case['query'], query=True),
                                          provider.identity.dimensions), dtype='<f4')
        scores = {}
        for row, score in zip(vectors, matrix @ query):
            key = row['session_id']
            scores[key] = max(scores.get(key, -float('inf')), float(score))
        ranked = sorted(scores, key=lambda key: (-scores[key], key))[:10]
        rank = next((i for i, key in enumerate(ranked, 1) if key == case['id']), None)
        rows.append({'case': case['id'], 'rank': rank, 'mode': 'vector',
                     'milliseconds': round((time.perf_counter() - start) * 1000, 3)})
    return summarize(rows)


def keyword_regressions(report, baseline):
    """Protect previously retrieved cases; aggregate gains cannot hide a lost case."""
    if baseline.get('version') != 1 or baseline['dataset_sha256'] != report['dataset_sha256']:
        raise ValueError('keyword baseline dataset/version mismatch')
    expected = baseline['ranks']
    rows = report['keyword']['cases']
    if len(rows) != len(expected) or {row['case'] for row in rows} != set(expected):
        raise ValueError('keyword baseline case IDs mismatch')
    if not any(rank is not None for rank in expected.values()):
        raise ValueError('keyword baseline must protect at least one retrieved case')
    failures = []
    for row in rows:
        before, after = expected[row['case']], row['rank']
        if before is not None and (type(before) is not int or not 1 <= before <= 10):
            raise ValueError('invalid keyword baseline rank')
        if row['mode'] != 'keyword' or (before is not None and (after is None or after > before)):
            failures.append({'case': row['case'], 'expected_max_rank': before, 'actual_rank': after})
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True, help='new isolated benchmark catalog')
    parser.add_argument('--output', type=Path, required=True, help='new JSON report')
    parser.add_argument('--cases', type=Path, default=Path(__file__).with_name('retrieval-cases.json'))
    parser.add_argument('--embedding-config', type=Path)
    parser.add_argument('--allow-remote-embeddings', action='store_true')
    parser.add_argument('--baseline', type=Path, help='reviewed keyword rank regression baseline')
    args = parser.parse_args()
    if args.data_dir.exists() or args.output.exists():
        raise ValueError('benchmark catalog and output must not already exist')
    encoded = args.cases.read_bytes()
    cases = json.loads(encoded)
    if (not cases or len({c['id'] for c in cases}) != len(cases)
            or not all(c['query'] and c['evidence'] for c in cases)):
        raise ValueError('benchmark cases require unique IDs and nonempty queries and evidence')
    provider = load_provider(args.embedding_config, allow_remote=args.allow_remote_embeddings)
    with Catalog(args.data_dir.resolve()) as catalog:
        for case in cases:
            catalog.ingest(SessionRevision(case['id'], (Event('insight', 'assistant', case['evidence']),)),
                           producer='synthetic-benchmark', request_id=case['id'])
        result = {'version': 1, 'dataset_sha256': hashlib.sha256(encoded).hexdigest(),
                  'dataset': 'synthetic engineering memories; one designated relevant session per query',
                  'keyword': evaluate(catalog, cases)}
        if provider:
            start = time.perf_counter()
            total = 0
            while True:
                batch = index_pending(catalog, provider, limit=1000)
                if batch['failed'] or batch['status'] != 'ok':
                    raise RuntimeError('benchmark embedding failed; inspect provider before retrying')
                total += batch['embedded']
                if batch['embedded'] == 0:
                    break
            result['indexing'] = {'vectors': total, 'seconds': round(time.perf_counter() - start, 3)}
            result['embedding_identity'] = asdict(provider.identity)
            result['hybrid'] = evaluate(catalog, cases, provider)
            result['vector'] = evaluate_vectors(catalog, cases, provider)
            if any(row['mode'] != 'hybrid' for row in result['hybrid']['cases']):
                raise RuntimeError('hybrid benchmark fell back; do not report it as model quality')
    failures = keyword_regressions(result, json.loads(args.baseline.read_text())) if args.baseline else []
    if args.baseline:
        result['regression'] = {'status': 'failed' if failures else 'passed', 'failures': failures}
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with args.output.open('x') as output:
        args.output.chmod(0o600)
        json.dump(result, output, indent=2)
        output.write('\n')
    print(json.dumps({key: {k: v for k, v in result[key].items() if k != 'cases'}
                      for key in ('keyword', 'hybrid', 'vector') if key in result}))

    if failures:
        raise SystemExit(f'keyword retrieval regressed on {len(failures)} cases; see JSON report')


if __name__ == '__main__':
    main()
