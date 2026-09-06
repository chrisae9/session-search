"""Optional covering index for ranking without reading transcript payloads."""

import shutil

NAME = 'search_evidence_metadata_v1'
COLUMNS = ('row_id', 'session_id', 'role', 'timestamp', 'origin')


def ready(db) -> bool:
    index = next((row for row in db.execute('PRAGMA index_list(evidence)')
                  if row[1] == NAME), None)
    return bool(index and not index[4] and tuple(
        row[2] for row in db.execute(f'PRAGMA index_info({NAME})')
    ) == COLUMNS)


def event_source() -> str:
    # Match the events view, projecting only fields needed by filters and rank.
    return (f'(SELECT b.row_id,b.session_id,b.role,b.timestamp,b.origin,h.revision,a.ordinal '
            f'FROM evidence b INDEXED BY {NAME} '
            'JOIN active_events a ON a.event_row=b.row_id '
            'JOIN heads h ON h.session_id=a.session_id) e')


def build(catalog, *, reserve_bytes: int = 2 * 1024 ** 3) -> dict:
    if catalog.readonly:
        raise PermissionError('metadata index construction requires a writable catalog')
    if type(reserve_bytes) is not int or reserve_bytes < 0:
        raise ValueError('index reserve must be nonnegative')
    db = catalog.db
    if ready(db):
        return {'version': 1, 'status': 'unchanged'}
    if db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (NAME,)).fetchone():
        raise ValueError('metadata index name has an incompatible definition')
    source_bytes = db.execute(
        'SELECT COALESCE(SUM(length(CAST(session_id AS BLOB))+'
        'length(CAST(role AS BLOB))+length(CAST(timestamp AS BLOB))+'
        'length(CAST(origin AS BLOB))+32),0) FROM evidence'
    ).fetchone()[0]
    required = 3 * source_bytes + reserve_bytes
    available = shutil.disk_usage(catalog.root).free
    if available < required:
        return {'version': 1, 'status': 'deferred', 'reason': 'insufficient_index_space',
                'required_bytes': required, 'available_bytes': available}
    db.execute('BEGIN IMMEDIATE')
    try:
        # Recheck after acquiring the writer lock: another builder may have won.
        if ready(db):
            db.commit()
            return {'version': 1, 'status': 'unchanged'}
        db.execute(f'CREATE INDEX {NAME} ON evidence({",".join(COLUMNS)})')
        catalog.bump_publication()
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return {'version': 1, 'status': 'ready', 'metadata_input_bytes': source_bytes}
