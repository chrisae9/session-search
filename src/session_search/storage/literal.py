"""Optional exact-substring candidates, built and maintained transactionally."""

import shutil
import sqlite3


def terms(text: str) -> str:
    triples = {}
    for i in range(len(text) - 2):
        value = text[i:i + 3]
        if all(32 <= ord(c) < 127 for c in value):
            triples[value.lower()] = None
            if len(triples) == 8:
                break
    return ' AND '.join('"' + value.replace('"', '""') + '"' for value in triples)


def ready(db) -> bool:
    names = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE name IN "
        "('literal_fts','literal_index_state','literal_fts_insert')")}
    if len(names) != 3:
        return False
    try:
        db.execute('SELECT rowid FROM literal_fts LIMIT 0')  # Check tokenizer availability.
        state = db.execute('SELECT version,high_water FROM literal_index_state WHERE id=1').fetchone()
        return bool(state and state[0] == 1 and state[1] == db.execute(
            'SELECT COALESCE(MAX(row_id),0) FROM evidence').fetchone()[0])
    except sqlite3.DatabaseError:
        return False


def build(catalog, *, reserve_bytes: int = 2 * 1024 ** 3) -> dict:
    if catalog.readonly:
        raise PermissionError('literal index construction requires a writable catalog')
    if type(reserve_bytes) is not int or reserve_bytes < 0:
        raise ValueError('index reserve must be nonnegative')
    db = catalog.db
    if ready(db):
        return {'version': 1, 'status': 'unchanged'}
    source_bytes = db.execute('SELECT COALESCE(SUM(length(CAST(text AS BLOB))),0) FROM evidence').fetchone()[0]
    required = 2 * source_bytes + reserve_bytes
    available = shutil.disk_usage(catalog.root).free
    if available < required:
        return {'version': 1, 'status': 'deferred', 'reason': 'insufficient_index_space',
                'required_bytes': required, 'available_bytes': available}
    db.execute('BEGIN IMMEDIATE')
    try:
        # Readers see the complete previous state until this transaction commits.
        db.execute('DROP TRIGGER IF EXISTS literal_fts_insert')
        db.execute('DROP TABLE IF EXISTS literal_fts')
        db.execute('DROP TABLE IF EXISTS literal_index_state')
        db.execute("CREATE VIRTUAL TABLE literal_fts USING fts5(text,content='',detail=none,columnsize=0,tokenize='trigram')")
        db.execute("INSERT INTO literal_fts(rowid,text) SELECT row_id,replace(text,char(0),' ') FROM evidence")
        db.execute('CREATE TABLE literal_index_state(id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL,high_water INTEGER NOT NULL)')
        db.execute('INSERT INTO literal_index_state SELECT 1,1,COALESCE(MAX(row_id),0) FROM evidence')
        db.execute('''CREATE TRIGGER literal_fts_insert AFTER INSERT ON evidence BEGIN
            INSERT INTO literal_fts(rowid,text) VALUES (NEW.row_id,replace(NEW.text,char(0),' '));
            UPDATE literal_index_state SET high_water=MAX(high_water,NEW.row_id) WHERE id=1;
        END''')
        catalog.bump_publication()
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return {'version': 1, 'status': 'ready', 'source_text_bytes': source_bytes}
