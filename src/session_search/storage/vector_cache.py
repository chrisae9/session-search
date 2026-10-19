"""Disposable, append-only float32 segments for bounded-memory exact retrieval.

Only background writers build this cache. Searches open immutable segments and
join scores to the authoritative filtered catalog snapshot. Updates/deletes of
vectors invalidate it; inserts after its watermark are scored from SQLite.
"""

import fcntl
import functools
import time
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from session_search.core.records import canonical_json
from session_search.storage.objects import sync_directory

SEGMENT_ROWS = 4096
NAME = re.compile(r'[0-9a-f]{64}\.(?:f32|i64)')


def epoch(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='vector_cache_state'").fetchone():
        return None
    return db.execute('SELECT epoch FROM vector_cache_state WHERE id=1').fetchone()[0]


def _manifest(root):
    path = root / 'manifest.json'
    if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('invalid vector cache manifest')
    value = json.loads(path.read_text())
    if value.get('version') != 1 or not isinstance(value.get('segments'), list):
        raise ValueError('invalid vector cache manifest')
    return value


def compatible(value, catalog, identity):
    publication = catalog.publication()
    return (isinstance(publication, str) and value.get('publication_identity') == publication.split(':')[0]
            and value.get('identity') == identity.key
            and value.get('dimensions') == identity.dimensions
            and value.get('epoch') == epoch(catalog.db)
            and type(value.get('watermark')) is int and value['watermark'] >= 0)


def build(catalog, identity, *, reserve_bytes=2 * 1024 ** 3):
    """Append new vectors without rewriting prior segments or touching evidence."""
    import numpy as np
    if catalog.readonly:
        raise PermissionError('vector cache construction requires a writable primary')
    if epoch(catalog.db) is None:
        return {'status': 'unavailable'}
    root = catalog.root / 'vector-cache' / identity.key
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / '.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'coalesced'}
        for pattern in ('.segment-*', '.manifest-*'):
            for temporary in root.glob(pattern):
                temporary.unlink()
        try:
            previous = _manifest(root)
        except (OSError, ValueError, KeyError, TypeError):
            previous = {}
        catalog.db.execute('BEGIN')
        try:
            valid = bool(previous) and compatible(previous, catalog, identity)
            low = previous['watermark'] if valid else 0
            high = catalog.db.execute('SELECT COALESCE(MAX(rowid),0) FROM vectors').fetchone()[0]
            if low > high:
                valid, low = False, 0
            if valid and high == low:
                return {'status': 'unchanged', 'watermark': high}
            if shutil.disk_usage(root).free < reserve_bytes + SEGMENT_ROWS * (identity.dimensions * 4 + 8):
                return {'status': 'deferred', 'reason': 'vector_cache_capacity'}
            segments = list(previous['segments']) if valid else []
            retained = {entry[key] for entry in previous.get('segments', []) for key in ('i64', 'f32')}
            for orphan in root.iterdir():
                if NAME.fullmatch(orphan.name) and orphan.name not in retained:
                    orphan.unlink()
            cursor = catalog.db.execute(
                'SELECT rowid,vector FROM vectors NOT INDEXED WHERE rowid>? AND rowid<=? '
                'AND identity=? ORDER BY rowid', (low, high, identity.key))
            while rows := cursor.fetchmany(SEGMENT_ROWS):
                if shutil.disk_usage(root).free < reserve_bytes + len(rows) * (identity.dimensions * 4 + 8):
                    return {'status': 'deferred', 'reason': 'vector_cache_capacity'}
                ids = np.asarray([r[0] for r in rows], dtype='<i8')
                matrix = np.stack([np.frombuffer(r[1], dtype='<f4') for r in rows])
                if matrix.shape[1] != identity.dimensions or not np.isfinite(matrix).all():
                    raise ValueError('stored embedding is incompatible or corrupt')
                entry = {'rows': len(rows), 'first': int(ids[0]), 'last': int(ids[-1])}
                for suffix, data in [('i64', ids.tobytes()), ('f32', matrix.tobytes())]:
                    name = hashlib.sha256(data).hexdigest() + '.' + suffix
                    path = root / name
                    if not path.exists():
                        fd, temporary = tempfile.mkstemp(prefix='.segment-', dir=root)
                        try:
                            with os.fdopen(fd, 'wb') as stream:
                                stream.write(data)
                                stream.flush()
                                os.fsync(stream.fileno())
                            os.chmod(temporary, 0o400)
                            os.replace(temporary, path)
                        finally:
                            Path(temporary).unlink(missing_ok=True)
                    entry[suffix] = name
                segments.append(entry)
            value = {'version': 1, 'publication_identity': catalog.publication().split(':')[0],
                     'identity': identity.key, 'dimensions': identity.dimensions,
                     'epoch': epoch(catalog.db), 'watermark': high, 'segments': segments}
        finally:
            catalog.db.rollback()
        # Publish only after every segment is durable. An interrupted build leaves
        # the preceding manifest usable; its missing tail is read from SQLite.
        fd, temporary = tempfile.mkstemp(prefix='.manifest-', dir=root)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(canonical_json(value))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, root / 'manifest.json')
            sync_directory(root)
        finally:
            Path(temporary).unlink(missing_ok=True)
        referenced = {s[k] for s in segments for k in ('i64', 'f32')}
        # Open readers retain their mmap/file handles across POSIX unlink.
        for path in root.iterdir():
            if NAME.fullmatch(path.name) and path.name not in referenced:
                path.unlink()
        return {'status': 'ready', 'watermark': high, 'segments': len(segments)}


@functools.lru_cache(maxsize=2048)
def _verified(path, inode, size, modified, changed):
    # Immutable content-addressed segments are verified once per inode/version,
    # rather than adding another full-vector hash scan to every request.
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() == Path(path).stem


def scores(catalog, identity, query_vector, *, deadline=None):
    """Return sorted row IDs and exact float32 scores, or no cache on any mismatch."""
    import numpy as np
    root = catalog.root / 'vector-cache' / identity.key
    try:
        value = _manifest(root)
        if not compatible(value, catalog, identity):
            return None
        all_ids, all_scores = [], []
        last = 0
        for segment in value['segments']:
            if deadline is not None and time.monotonic() > deadline:
                from session_search.core.deadlines import SearchDeadline
                raise SearchDeadline()
            rows = segment['rows']
            if type(rows) is not int or not 1 <= rows <= SEGMENT_ROWS:
                return None
            for suffix, size in [('i64', rows * 8), ('f32', rows * identity.dimensions * 4)]:
                name = segment[suffix]
                path = root / name
                if not NAME.fullmatch(name) or path.is_symlink():
                    return None
                info = path.stat()
                if info.st_size != size or not _verified(str(path), info.st_ino, info.st_size,
                                                         info.st_mtime_ns, info.st_ctime_ns):
                    return None
            ids = np.memmap(root / segment['i64'], dtype='<i8', mode='r', shape=(rows,))
            matrix = np.memmap(root / segment['f32'], dtype='<f4', mode='r', shape=(rows, identity.dimensions))
            if (int(ids[0]) <= last or not np.all(ids[1:] > ids[:-1])
                    or int(ids[-1]) > value['watermark'] or not np.isfinite(matrix).all()):
                return None
            last = int(ids[-1])
            all_ids.append(np.array(ids))
            all_scores.append(matrix @ query_vector)
        cursor = catalog.db.execute(
            'SELECT rowid,vector FROM vectors NOT INDEXED WHERE rowid>? AND identity=? ORDER BY rowid',
            (value['watermark'], identity.key))
        while rows := cursor.fetchmany(SEGMENT_ROWS):
            if deadline is not None and time.monotonic() > deadline:
                from session_search.core.deadlines import SearchDeadline
                raise SearchDeadline()
            ids = np.asarray([r[0] for r in rows], dtype='<i8')
            matrix = np.stack([np.frombuffer(r[1], dtype='<f4') for r in rows])
            if matrix.shape[1] != identity.dimensions or not np.isfinite(matrix).all():
                return None
            all_ids.append(ids)
            all_scores.append(matrix @ query_vector)
        return (np.concatenate(all_ids) if all_ids else np.empty(0, dtype='<i8'),
                np.concatenate(all_scores) if all_scores else np.empty(0, dtype='<f4'))
    except TimeoutError:
        raise
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return None
