"""Opt-in expiry of managed upload staging; never archive garbage collection."""

import fcntl
import math
import os
import re
import stat
import time
from contextlib import ExitStack
from pathlib import Path

PART = re.compile(r'[0-9a-f]{64}-[0-9a-f]{64}\.part')
DIRECTORIES = ('transfers', 'revision-transfers')


def expire_transfers(root: Path, *, older_than_days: float, apply: bool = False,
                     scan_limit: int = 10000) -> dict:
    if (isinstance(older_than_days, bool) or not isinstance(older_than_days, (int, float))
            or not math.isfinite(older_than_days) or older_than_days < 1):
        raise ValueError('transfer expiry must retain at least one day')
    if type(scan_limit) is not int or not 1 <= scan_limit <= 1000000:
        raise ValueError('scan limit must be between 1 and 1000000 entries')
    if type(apply) is not bool:
        raise ValueError('apply must be a boolean')
    root = root.resolve()
    if not (root / 'catalog.sqlite3').is_file():
        raise ValueError('transfer expiry requires an initialized primary catalog')
    markers = ('CURRENT', 'manifest.json', 'FENCED.json')
    if any((root / name).exists() or (root / name).is_symlink() for name in markers):
        raise PermissionError('transfer expiry requires an unfenced primary')
    result = {'version': 1, 'status': 'planned', 'scanned_entries': 0,
              'eligible_files': 0, 'eligible_bytes': 0, 'removed_files': 0,
              'removed_bytes': 0, 'skipped_files': 0}
    cutoff = time.time() - older_than_days * 86400
    with ExitStack() as stack:
        # Exclude cooperating capture, ingestion, and append writers for the
        # entire bounded scan. A long-running indexer makes maintenance defer.
        fd = os.open(root / '.writers.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        stack.callback(os.close, fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('invalid writer lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**result, 'status': 'deferred', 'reason': 'active_writer'}
        if any((root / name).exists() or (root / name).is_symlink() for name in markers):
            raise PermissionError('transfer expiry requires an unfenced primary')
        from session_search.storage.catalog import Catalog
        from session_search.storage.objects import ObjectStore
        from session_search.storage.chunks import ChunkStore
        catalog = stack.enter_context(Catalog(root, readonly=True))
        candidates = []
        for directory in DIRECTORIES:
            try:
                directory_fd = os.open(root / directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except FileNotFoundError:
                continue
            stack.callback(os.close, directory_fd)
            with os.scandir(directory_fd) as entries:
                for entry in entries:
                    if result['scanned_entries'] >= scan_limit:
                        return {**result, 'status': 'deferred', 'reason': 'scan_limit',
                                'eligible_files': 0, 'eligible_bytes': 0}
                    result['scanned_entries'] += 1
                    if not PART.fullmatch(entry.name):
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        result['skipped_files'] += 1
                        continue
                    if info.st_mtime < cutoff:
                        candidates.append((directory_fd, directory, entry.name, info))
        # No removal starts until the complete directory scan fits the budget.
        needed = {name[65:-5] for _, directory, name, _ in candidates if directory == 'transfers'}
        # Stream recovery references once, retaining only candidate keys rather
        # than rescanning the catalog once per partial or loading all history.
        referenced = ({row[0] for row in catalog.db.execute('SELECT digest FROM raw_sources')
                       if row[0] in needed} if needed else set())
        for directory_fd, directory, name, observed in candidates:
            key = name[65:-5]
            namespace = 'raw' if directory == 'transfers' else 'revision-upload'
            published = [ObjectStore(root, namespace=namespace).path(key)]
            if namespace == 'raw':
                published.append(ChunkStore(root).path('recipes', key))
            if (any(path.exists() or path.is_symlink() for path in published)
                    or (namespace == 'raw' and key in referenced)):
                result['skipped_files'] += 1
                continue
            lock_name = name[:-5] + '.lock'
            try:
                lock_fd = os.open(lock_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                  dir_fd=directory_fd)
            except OSError:
                result['skipped_files'] += 1
                continue
            try:
                if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                    result['skipped_files'] += 1
                    continue
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    result['skipped_files'] += 1
                    continue
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                        or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
                        != (observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns)):
                    result['skipped_files'] += 1
                    continue
                result['eligible_files'] += 1
                result['eligible_bytes'] += current.st_size
                if apply:
                    os.unlink(name, dir_fd=directory_fd)
                    os.fsync(directory_fd)
                    result['removed_files'] += 1
                    result['removed_bytes'] += current.st_size
            finally:
                os.close(lock_fd)
    result['status'] = 'pruned' if apply else 'planned'
    return result
