"""Resumable raw uploads with durable offsets and content-addressed completion."""

import fcntl
import hashlib
import os
import shutil
from pathlib import Path

from session_search.core.records import digest
from session_search.storage.objects import ObjectStore, sync_directory

MAX_CHUNK = 1024 * 1024


class OffsetConflict(ValueError):
    pass


class RawTransfers:
    def __init__(self, root: Path, *, namespace: str = "raw", max_size: int = 32 * 1024 ** 3):
        self.root = root
        self.objects = ObjectStore(root, namespace=namespace)
        self.staging = root / ("transfers" if namespace == "raw" else "revision-transfers")
        self.max_size = max_size

    def paths(self, producer: str, key: str):
        self.objects.path(key)  # Reject path traversal before forming any path.
        self.staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        stem = digest(producer.encode()) + "-" + key
        return self.staging / (stem + ".part"), self.staging / (stem + ".lock")

    def status(self, producer: str, key: str) -> dict:
        completed = self.objects.path(key)
        if completed.exists():
            if not self.objects.verify(key):
                raise ValueError("completed raw object is corrupt")
            return {"version": 1, "status": "complete", "offset": completed.stat().st_size}
        partial, lock = self.paths(producer, key)
        with lock.open("a") as guard:
            fcntl.flock(guard, fcntl.LOCK_SH)
            return {"version": 1, "status": "pending",
                    "offset": partial.stat().st_size if partial.exists() else 0}

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
        with lock.open("a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            completed = self.objects.path(key)
            if completed.exists():
                if not self.objects.verify(key) or completed.stat().st_size != total:
                    raise ValueError("completed object does not match upload")
                return {"version": 1, "status": "complete", "offset": total}
            observed = partial.stat().st_size if partial.exists() else 0
            if offset != observed:
                raise OffsetConflict("transfer offset conflict; query durable progress before retrying")
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
            result = self.objects.put(partial)
            if result["digest"] != key:
                raise ValueError("raw object changed during completion")
            partial.unlink()
            sync_directory(self.staging)
            return {"version": 1, "status": "complete", "offset": current}
