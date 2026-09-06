"""Immutable content-addressed objects with separate archival and transfer namespaces."""

import hashlib
import errno
import shutil
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
    def __init__(self, root: Path, *, namespace: str = "raw"):
        if namespace not in {"raw", "revision-upload"}:
            raise ValueError("unsupported object namespace")
        self.data_root = root
        self.namespace = namespace
        self.root = root / "objects" / namespace

    def path(self, digest: str) -> Path:
        if not re.fullmatch("[0-9a-f]{64}", digest):
            raise ValueError("invalid raw object digest")
        return self.root / digest[:2] / digest[2:]

    def verify(self, digest: str) -> bool:
        path = self.path(digest)
        if path.is_symlink():
            return False
        if not path.exists() and self.namespace == "raw":
            from session_search.storage.chunks import ChunkStore
            return ChunkStore(self.data_root).verify(digest)
        try:
            with path.open("rb") as f:
                return hashlib.file_digest(f, "sha256").hexdigest() == digest
        except OSError:
            return False

    def size(self, digest: str) -> int:
        if self.layout(digest) == "file":
            return self.path(digest).stat().st_size
        from session_search.storage.chunks import ChunkStore
        return ChunkStore(self.data_root).recipe(digest)["size"]

    def layout(self, digest: str) -> str:
        path = self.path(digest)
        if path.is_symlink():
            raise ValueError("raw objects must not be symlinks")
        if path.exists():
            return "file"
        if self.namespace == "raw":
            from session_search.storage.chunks import ChunkStore
            if ChunkStore(self.data_root).path("recipes", digest).is_file():
                return "chunks-v1"
        raise FileNotFoundError("raw object is unavailable")

    def copy_to(self, target, digest: str, *, allow_links: bool = True):
        """Copy verified storage members, retaining shared chunks and exact recipes."""
        if self.layout(digest) == "file":
            members = [(self.path(digest), target.path(digest))]
        else:
            from session_search.storage.chunks import ChunkStore
            source_chunks, target_chunks = ChunkStore(self.data_root), ChunkStore(target.data_root)
            recipe = source_chunks.recipe(digest)
            keys = dict.fromkeys(c["digest"] for c in recipe["chunks"])
            members = [(source_chunks.path("chunks", key), target_chunks.path("chunks", key))
                       for key in keys]
            members.append((source_chunks.path("recipes", digest), target_chunks.path("recipes", digest)))
        for source, destination in members:
            _copy_member(source, destination, allow_links=allow_links)
        if not target.verify(digest):
            raise ValueError("copied raw evidence is missing or corrupt")

    def put(self, source: Path, *, chunked: bool = False) -> dict:
        if chunked:
            if self.namespace != "raw":
                raise ValueError("only raw archival supports shared chunks")
            from session_search.storage.chunks import ChunkStore
            return ChunkStore(self.data_root).put(source)
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


def _copy_member(source: Path, destination: Path, *, allow_links: bool):
    from session_search.storage.chunks import _mkdir
    _mkdir(destination.parent)
    if source.is_symlink() or destination.is_symlink():
        raise ValueError("object members must not be symlinks")
    compare_existing = destination.exists()
    if not compare_existing:
        try:
            if not allow_links:
                raise OSError(errno.EXDEV, "independent copy requested")
            os.link(source, destination)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                fd, name = tempfile.mkstemp(dir=destination.parent, prefix=".member-")
                os.close(fd)
                temporary = Path(name)
                try:
                    shutil.copyfile(source, temporary)
                    with temporary.open("rb") as stream:
                        os.fsync(stream.fileno())
                    try:
                        os.link(temporary, destination)
                    except FileExistsError:
                        compare_existing = True
                finally:
                    temporary.unlink(missing_ok=True)
            elif exc.errno == errno.EEXIST:
                compare_existing = True
            else:
                raise
    if compare_existing:
        with source.open("rb") as original, destination.open("rb") as copied:
            if hashlib.file_digest(original, "sha256").digest() != hashlib.file_digest(copied, "sha256").digest():
                raise ValueError("object member differs after copying")
    with destination.open("rb") as copied:
        os.fsync(copied.fileno())
    sync_directory(destination.parent)
