"""Compare scan and trigram candidate plans on a separate catalog copy."""

import argparse
import json
from pathlib import Path
import sqlite3
import time

from session_search.core.records import SearchQuery
from session_search.storage.catalog import Catalog


from session_search.storage.literal import terms


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    args.destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    path = args.destination / 'catalog.sqlite3'
    source = sqlite3.connect((args.source.resolve() / 'catalog.sqlite3').as_uri() + '?mode=ro', uri=True)
    db = sqlite3.connect(path)
    try:
        source.backup(db)
        before = path.stat().st_size
        start = time.monotonic()
        db.execute("CREATE VIRTUAL TABLE literal_probe USING fts5(text,content='',detail=none,columnsize=0,tokenize='trigram')")
        db.execute("INSERT INTO literal_probe(rowid,text) SELECT row_id,text FROM evidence")
        db.execute('CREATE TABLE literal_probe_nul(rowid INTEGER PRIMARY KEY)')
        db.execute('INSERT INTO literal_probe_nul SELECT row_id FROM evidence WHERE instr(text,char(0))>0')
        db.commit()
        report = {'index_build_seconds': round(time.monotonic() - start, 3),
                  'index_added_bytes': path.stat().st_size - before, 'queries': []}
    finally:
        source.close()
        db.close()
    with Catalog(args.destination, readonly=True) as catalog:
        for text in ('backup', 'pyproject.toml', 'sqlite_autoindex', 'restore snapshot'):
            cte, conditions, values = catalog.scope(SearchQuery(text, literal=True, limit=10))
            conditions.append('instr(lower(e.text),lower(?))>0')
            values.append(text)
            base = cte + ('SELECT e.row_id,e.session_id,e.event_id,e.ordinal FROM events e '
                'JOIN heads h ON h.session_id=e.session_id AND h.revision=e.revision '
                'JOIN revisions r ON r.session_id=e.session_id AND r.revision=e.revision ')
            suffix = ' ORDER BY e.timestamp DESC,e.session_id,e.ordinal LIMIT 11'
            scan = base + ' WHERE ' + ' AND '.join(conditions) + suffix
            indexed = (base + ' WHERE ' + ' AND '.join([*conditions,
                       'e.row_id IN (SELECT rowid FROM literal_probe WHERE literal_probe MATCH ? '
                       'UNION SELECT rowid FROM literal_probe_nul)']) + suffix)
            observed = {}
            expected = None
            for name, sql, arguments in [('scan', scan, values), ('trigram', indexed, [*values, terms(text)])]:
                times = []
                for _ in range(2):
                    start = time.monotonic()
                    rows = catalog.db.execute(sql, arguments).fetchall()
                    times.append(round(time.monotonic() - start, 5))
                evidence = [tuple(row) for row in rows]
                if expected is None:
                    expected = evidence
                else:
                    assert evidence == expected, 'candidate index changed ordered evidence'
                observed[name] = times
            report['queries'].append({'query': text, 'seconds': observed, 'same_ordered_evidence': True})
    (args.destination / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
