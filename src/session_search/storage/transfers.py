"""Resumable raw uploads with durable offsets and content-addressed completion."""

import fcntl
import hashlib
import os
import shutil
import stat
from contextlib import contextmanager
from pathlib import Path

from session_search.core.records import digest
from session_search.storage.objects import ObjectStore, sync_directory

MAX_CHUNK = 1024 * 1024


@contextmanager
def transfer_lock(partial: Path, lock: Path, *, create: bool = True, blocking: bool = True):
    """Retry retired inodes: a waiter must never write through an unlinked lock."""
    while True:
        try:
            fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
                         | (os.O_CREAT if create else 0), 0o600)
        except FileNotFoundError:
            if create:
                raise
            yield False
            return
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError('invalid transfer lock')
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            held = os.fstat(fd)
            try:
                current = lock.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
                continue
            try:
                yield True
            finally:
                # Keep locks for resumable partials. Retire completed, rejected,
                # and status-only locks while still holding the checked inode.
                if not partial.exists() and not partial.is_symlink():
                    lock.unlink()
                    sync_directory(lock.parent)
            return
        finally:
            os.close(fd)


class OffsetConflict(ValueError):
    pass


class RawTransfers:
    def __init__(self, root: Path, *, namespace: str = "raw", max_size: int = 32 * 1024 ** 3,
                 chunked: bool = False, max_staging_bytes: int | None = None, staging_scan_limit: int = 10000):
        if chunked and namespace != "raw":
            raise ValueError("only raw archival supports shared chunks")
        from session_search.storage.transfer_capacity import validate_budget
        validate_budget(max_staging_bytes, staging_scan_limit)
        self.max_staging_bytes = max_staging_bytes
        self.staging_scan_limit = staging_scan_limit
        self.chunked = chunked
        self.root = root
        self.objects = ObjectStore(root, namespace=namespace)
        self.staging = root / ("transfers" if namespace == "raw" else "revision-transfers")
        self.max_size = max_size

    def paths(self, producer: str, key: str, *, create: bool = True):
        self.objects.path(key)  # Reject path traversal before forming any path.
        if create:
            self.staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        stem = digest(producer.encode()) + "-" + key
        return self.staging / (stem + ".part"), self.staging / (stem + ".lock")

    def _completed_path(self, key: str):
        try:
            layout = self.objects.layout(key)
        except FileNotFoundError:
            return None
        if layout == "file":
            return self.objects.path(key)
        from session_search.storage.chunks import ChunkStore
        return ChunkStore(self.root).path("recipes", key)

    def _sync_completed(self, completed: Path):
        # A reader can encounter an object linked by a different producer just
        # before that process crashes. Make publication durable before acking it.
        with completed.open("rb") as stream:
            os.fsync(stream.fileno())
        sync_directory(completed.parent)
        sync_directory(completed.parent.parent)

    def _reclaim_completed_partial(self, producer: str, key: str):
        """Best-effort retirement of this producer's verified transfer duplicate."""
        from session_search.storage.fencing import writer_lease
        partial = self.staging / (digest(producer.encode()) + '-' + key + '.part')
        if not partial.exists():
            return
        try:
            with writer_lease(self.root):
                _, lock = self.paths(producer, key)
                with transfer_lock(partial, lock, blocking=False):
                    completed = self._completed_path(key)
                    if completed is None or not self.objects.verify(key):
                        raise ValueError('completed object changed before staging cleanup')
                    self._sync_completed(completed)
                    partial.unlink(missing_ok=True)
                    sync_directory(self.staging)
        except (BlockingIOError, PermissionError):
            # Fenced stores remain readable; a busy writer owns its cleanup.
            return

    def status(self, producer: str, key: str) -> dict:
        completed = self._completed_path(key)
        if completed is not None:
            if not self.objects.verify(key):
                raise ValueError("completed raw object is corrupt")
            self._sync_completed(completed)
            self._reclaim_completed_partial(producer, key)
            return {"version": 1, "status": "complete", "offset": self.objects.size(key)}
        partial, lock = self.paths(producer, key, create=False)
        with transfer_lock(partial, lock, create=False) as acquired:
            # An absent lock means no writer had started at this observation.
            # Returning zero is safe: a racing append rejects stale offsets.
            return {"version": 1, "status": "pending",
                    "offset": partial.stat().st_size if acquired and partial.exists() else 0}

    def append(self, producer: str, key: str, offset: int, total: int, chunk: bytes) -> dict:
        from session_search.storage.fencing import writer_lease
        with writer_lease(self.root):
            return self._append(producer, key, offset, total, chunk)

    def _append(self, producer: str, key: str, offset: int, total: int, chunk: bytes) -> dict:
        if not 0 <= offset <= total <= self.max_size or len(chunk) > MAX_CHUNK:
            raise ValueError("invalid transfer size or offset")
        if offset + len(chunk) > total or (not chunk and offset != total):
            raise ValueError("chunk exceeds declared file size")
        partial, lock = self.paths(producer, key)
        with transfer_lock(partial, lock):
            completed = self._completed_path(key)
            if completed is not None:
                if not self.objects.verify(key) or self.objects.size(key) != total:
                    raise ValueError("completed object does not match upload")
                self._sync_completed(completed)
                # A prior process may have published the object but crashed
                # before dropping its staging link. The verified object wins.
                partial.unlink(missing_ok=True)
                sync_directory(self.staging)
                return {"version": 1, "status": "complete", "offset": total}
            observed = partial.stat().st_size if partial.exists() else 0
            if offset != observed:
                raise OffsetConflict("transfer offset conflict; query durable progress before retrying")
            from session_search.storage.transfer_capacity import staging_admission
            with staging_admission(self.root, len(chunk), self.max_staging_bytes,
                                   self.staging_scan_limit):
                if shutil.disk_usage(self.root).free < len(chunk) + 64 * 1024 * 1024:
                    raise OSError("insufficient space; pending upload retained")
                with os.fdopen(os.open(partial, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "ab") as output:
                    output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                sync_directory(self.staging)
            current = observed + len(chunk)
            if current != total:
                return {"version": 1, "status": "pending", "offset": current}
            with partial.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != key:
                    raise ValueError("completed upload checksum mismatch")
            if self.chunked:
                from session_search.storage.chunks import ChunkStore
                result = ChunkStore(self.root).put(partial, expected_digest=key)
                if result["size"] != total:
                    raise ValueError("raw file changed during chunk publication")
                self._sync_completed(self._completed_path(key))
                partial.unlink()
                sync_directory(self.staging)
                return {"version": 1, "status": "complete", "offset": current}
            completed = self.objects.path(key)
            # Both paths are managed under the same data root. Publish the
            # fsynced, checksum-verified inode without allocating a second full
            # file. Exclusive link creation also handles another producer that
            # completed the identical object concurrently.
            completed.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.link(partial, completed)
            except FileExistsError:
                if not self.objects.verify(key) or completed.stat().st_size != total:
                    raise ValueError("existing completed upload is corrupt") from None
            self._sync_completed(completed)
            partial.unlink()
            sync_directory(self.staging)
            return {"version": 1, "status": "complete", "offset": current}
