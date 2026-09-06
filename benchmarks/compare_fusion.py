"""Compare fusion policies on an existing, exactly matched synthetic benchmark catalog.

This diagnostic does not change production ranking or rewrite the catalog.
"""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from session_search.core.embeddings import load_provider, validate_vector
from session_search.core.records import Event, SearchQuery, SessionRevision
from session_search.storage.catalog import Catalog


def fuse(lexical, semantic, *, constant=60, semantic_weight=1, lexical_window=50):
    scores = {}
    for candidates, weight in ((lexical[:lexical_window], 1), (semantic[:100], semantic_weight)):
        for rank, sid in enumerate(candidates, 1):
            scores[sid] = scores.get(sid, 0) + weight / (constant + rank)
    return sorted(scores, key=lambda sid: (-scores[sid], sid))[:10]


def compare(catalog, cases, provider):
    if not cases or len({case['id'] for case in cases}) != len(cases):
        raise ValueError('comparison cases require unique nonempty identities')
    expected = {case['id']: SessionRevision(case['id'],
                (Event('insight', 'assistant', case['evidence']),)).revision for case in cases}
    actual = dict(catalog.db.execute('SELECT session_id,revision FROM heads'))
    if actual != expected:
        raise ValueError('catalog must exactly match the synthetic fixture; native history is refused')
    if not catalog.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='vectors'").fetchone():
        raise ValueError('synthetic vector coverage must be complete')
    vectors = catalog.db.execute(
        'SELECT e.session_id,v.vector FROM events e JOIN heads h '
        'ON e.session_id=h.session_id AND e.revision=h.revision '
        'JOIN event_chunks ec ON e.row_id=ec.event_row JOIN vectors v '
        'ON ec.content_hash=v.content_hash WHERE v.identity=?', (provider.identity.key,),
    ).fetchall()
    if {row['session_id'] for row in vectors} != set(expected):
        raise ValueError('synthetic vector coverage must be complete')
    matrix = np.stack([np.frombuffer(row['vector'], dtype='<f4') for row in vectors])
    if matrix.shape[1] != provider.identity.dimensions or not np.isfinite(matrix).all():
        raise ValueError('synthetic vectors are incompatible or corrupt')
    policies = {'equal_60': {}, 'equal_20': {'constant': 20}, 'equal_10': {'constant': 10},
                'equal_1': {'constant': 1}, 'semantic_weight_2': {'semantic_weight': 2},
                'semantic_weight_3': {'semantic_weight': 3}, 'lexical_window_10': {'lexical_window': 10}}
    results = {name: [] for name in (*policies, 'keyword', 'vector')}
    for case in cases:
        lexical = [r['citation']['session_id'] for r in catalog.search(
            SearchQuery(case['query'], role='assistant', limit=50))['results']]
        vector = np.asarray(validate_vector(provider.embed(case['query'], query=True),
                                            provider.identity.dimensions), dtype='<f4')
        scores = {}
        for row, score in zip(vectors, matrix @ vector):
            sid = row['session_id']
            scores[sid] = max(scores.get(sid, -float('inf')), float(score))
        semantic = sorted(scores, key=lambda sid: (-scores[sid], sid))
        rankings = {name: fuse(lexical, semantic, **options) for name, options in policies.items()}
        rankings.update(keyword=lexical[:10], vector=semantic[:10])
        for name, ranked in rankings.items():
            rank = ranked.index(case['id']) + 1 if case['id'] in ranked else None
            results[name].append({'case': case['id'], 'rank': rank})
    return {name: {'recall_at_10': sum(r['rank'] is not None for r in rows) / len(rows),
                  'mrr_at_10': sum(1/r['rank'] if r['rank'] else 0 for r in rows) / len(rows),
                  'top_one': sum(r['rank'] == 1 for r in rows) / len(rows), 'cases': rows}
            for name, rows in results.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--cases', type=Path, default=Path(__file__).with_name('retrieval-cases.json'))
    parser.add_argument('--embedding-config', type=Path, required=True)
    parser.add_argument('--allow-remote-embeddings', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError('use a new report path')
    encoded = args.cases.read_bytes()
    cases = json.loads(encoded)
    provider = load_provider(args.embedding_config, allow_remote=args.allow_remote_embeddings)
    with Catalog(args.data_dir.resolve(), readonly=True) as catalog:
        result = {'version': 1, 'dataset_sha256': hashlib.sha256(encoded).hexdigest(),
                  'embedding_identity': provider.identity.key, 'policies': compare(catalog, cases, provider)}
    with args.output.open('x') as stream:
        args.output.chmod(0o600)
        json.dump(result, stream, indent=2)
    print(json.dumps({name: {key: value for key, value in result.items() if key != 'cases'}
                      for name, result in result['policies'].items()}))


if __name__ == '__main__':
    main()
