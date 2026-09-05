"""Immutable content-addressed raw objects. Only explicit archival calls write here."""

import hashlib
import os
import re
import tempfile
from pathlib import Path


def sync_directory(path: Path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ObjectStore:
    def __init__(self, root: Path):
        self.root = root / "objects" / "raw"

    def path(self, digest: str) -> Path:
        if not re.fullmatch("[0-9a-f]{64}", digest):
            raise ValueError("invalid raw object digest")
        return self.root / digest[:2] / digest[2:]

    def verify(self, digest: str) -> bool:
        path = self.path(digest)
        if path.is_symlink():
            return False
        try:
            with path.open("rb") as f:
                return hashlib.file_digest(f, "sha256").hexdigest() == digest
        except OSError:
            return False

    def put(self, source: Path) -> dict:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(dir=self.root, prefix=".upload-")
        temporary = Path(name)
        hasher = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as output, source.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    output.write(chunk)
                    hasher.update(chunk)
                    size += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            digest = hasher.hexdigest()
            destination = self.path(digest)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if not self.verify(digest):
                    raise ValueError("existing raw object is corrupt")
            sync_directory(destination.parent)
            sync_directory(self.root)
            return {"digest": digest, "size": size}
        finally:
            temporary.unlink(missing_ok=True)
