"""Candidate prefilter must never exclude a baseline literal match."""

import importlib.util
from pathlib import Path
import random
import sqlite3


def test_trigram_candidates_preserve_ascii_substrings_with_unicode_and_controls():
    spec = importlib.util.spec_from_file_location('compare_literal', Path(__file__).parents[1] / 'benchmarks/compare_literal.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    randomizer = random.Random(731)
    corpus = ['prefix\0BACKUP suffix', 'Straße a"b', 'éßİΣ backup', 'a\nb\tc', '100%_backup']
    alphabet = 'abcABC xyz%_"\n\0éßİΣ'
    corpus += [''.join(randomizer.choice(alphabet) for _ in range(80)) for _ in range(250)]
    db = sqlite3.connect(':memory:')
    db.execute("CREATE TABLE original(text)")
    db.execute("CREATE VIRTUAL TABLE candidate USING fts5(text,tokenize='trigram',detail=none)")
    db.executemany('INSERT INTO original(text) VALUES (?)', [(text,) for text in corpus])
    db.execute("INSERT INTO candidate(rowid,text) SELECT rowid,replace(text,char(0),' ') FROM original")
    needles = ['backup', 'a"b', '100%_', 'Straße', 'a\nb', 'éßİ']
    for text in corpus:
        start = randomizer.randrange(len(text) - 4)
        needles.append(text[start:start + 5])
    for needle in needles:
        expression = module.terms(needle)
        if not expression:
            continue  # The production candidate path would fall back to a scan.
        baseline = db.execute('SELECT rowid FROM original WHERE instr(lower(text),lower(?))>0 ORDER BY rowid', (needle,)).fetchall()
        filtered = db.execute('SELECT o.rowid FROM original o JOIN candidate ON candidate.rowid=o.rowid WHERE candidate MATCH ? AND instr(lower(o.text),lower(?))>0 ORDER BY o.rowid', (expression, needle)).fetchall()
        assert filtered == baseline
    db.close()
