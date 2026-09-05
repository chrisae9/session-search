"""POSIX reader pins and bounded retention for immutable replica generations."""

import fcntl
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from session_search.storage.objects import sync_directory


@contextmanager
def publication_lock(root: Path, *, exclusive: bool):
    with (root / '.publication.lock').open('a+b' if exclusive else 'rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield


def pointer(root: Path, name: str) -> str | None:
    path = root / name
    if not path.exists():
        return None
    value = path.read_text().strip()
    if path.is_symlink() or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('invalid replica generation pointer')
    return value


def pin_current(root: Path):
    with publication_lock(root, exclusive=False):
        key = pointer(root, 'CURRENT')
        if key is None:
            raise ValueError('replica has no active generation')
        generation = root / 'generations' / key
        if generation.is_symlink():
            raise ValueError('replica generation must not be a symlink')
        lock = (generation / '.readers.lock').open('rb')
        try:
            fcntl.flock(lock, fcntl.LOCK_SH)
        except BaseException:
            lock.close()
            raise
        return generation, lock


def write_pointer(root: Path, name: str, key: str):
    fd, temporary = tempfile.mkstemp(dir=root, prefix='.pointer-')
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / name)
        sync_directory(root)
    finally:
        Path(temporary).unlink(missing_ok=True)


def publish_locked(root: Path, key: str):
    previous = pointer(root, 'CURRENT')
    if previous and previous != key:
        write_pointer(root, 'PREVIOUS', previous)
    write_pointer(root, 'CURRENT', key)


def prune_replica(root: Path) -> dict:
    """Remove only unpinned obsolete generations, never arbitrary staging files."""
    root = root.resolve()
    removed = pinned = 0
    with publication_lock(root, exclusive=True):
        current = pointer(root, 'CURRENT')
        if current is None:
            raise ValueError('replica has no active generation')
        protected = {current, pointer(root, 'PREVIOUS')}
        for generation in (root / 'generations').iterdir():
            if (generation.name in protected or generation.is_symlink()
                    or not generation.is_dir() or not re.fullmatch('[0-9a-f]{64}', generation.name)):
                continue
            # Only generations managed by this protocol are eligible.
            if not (generation / '.readers.lock').is_file():
                continue
            with (generation / '.readers.lock').open('rb') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pinned += 1
                    continue
                shutil.rmtree(generation)
                removed += 1
        sync_directory(root / 'generations')
    return {'version': 1, 'status': 'ok', 'removed': removed, 'pinned': pinned}
