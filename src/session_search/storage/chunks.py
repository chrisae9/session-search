"""Exact raw-file recipes over immutable shared chunks."""

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from session_search.core.records import canonical_json
from session_search.storage.objects import sync_directory

CHUNK_SIZE = 4 * 1024 * 1024
MAX_SIZE = 32 * 1024 ** 3


def _key(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('invalid chunk or raw-file digest')
    return value


def _mkdir(path):
    if path.is_symlink():
        raise ValueError('chunk-store directories must not be symlinks')
    if not path.exists():
        _mkdir(path.parent)
        path.mkdir(mode=0o700, exist_ok=True)
        sync_directory(path.parent)


class ChunkStore:
    def __init__(self, root: Path):
        self.root = root.resolve() / 'objects' / 'chunked-raw'

    def path(self, kind: str, key: str) -> Path:
        if kind not in {'chunks', 'recipes'}:
            raise ValueError('invalid chunk-store namespace')
        key = _key(key)
        path = self.root / kind / key[:2] / key[2:]
        for parent in (self.root.parent, self.root, path.parent.parent, path.parent, path):
            if parent.is_symlink():
                raise ValueError('chunk-store paths must not be symlinks')
        return path

    def _publish(self, path: Path, content: bytes):
        _mkdir(path.parent)
        if path.exists():
            with path.open('rb') as stream:
                if stream.read(len(content) + 1) != content:
                    raise ValueError('existing chunk-store object is corrupt')
                os.fsync(stream.fileno())
            sync_directory(path.parent)
            return
        if shutil.disk_usage(path.parent).free < len(content) + 64 * 1024 * 1024:
            raise OSError('insufficient space for a new raw chunk; existing evidence retained')
        fd, name = tempfile.mkstemp(dir=path.parent, prefix='.chunk-')
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                with path.open('rb') as stream:
                    if stream.read(len(content) + 1) != content:
                        raise ValueError('existing chunk-store object is corrupt') from None
                    os.fsync(stream.fileno())
            sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def put(self, source: Path, *, expected_digest: str | None = None) -> dict:
        if expected_digest is not None:
            _key(expected_digest)
        chunks, hasher, size = [], hashlib.sha256(), 0
        with source.open('rb') as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > MAX_SIZE:
                raise ValueError('raw file exceeds chunk-store limit')
            while content := stream.read(CHUNK_SIZE):
                size += len(content)
                if size > MAX_SIZE:
                    raise ValueError('raw file exceeds chunk-store limit')
                hasher.update(content)
                key = hashlib.sha256(content).hexdigest()
                self._publish(self.path('chunks', key), content)
                chunks.append({'digest': key, 'size': len(content)})
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('raw source changed while chunking')
        key = hasher.hexdigest()
        if expected_digest is not None and key != expected_digest:
            raise ValueError('raw file checksum mismatch')
        recipe = {'version': 1, 'digest': key, 'size': size, 'chunk_size': CHUNK_SIZE, 'chunks': chunks}
        self._publish(self.path('recipes', key), canonical_json(recipe).encode())
        return {'digest': key, 'size': size}

    def recipe(self, key: str) -> dict:
        with self.path('recipes', key).open('rb') as stream:
            content = stream.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError('raw-file recipe exceeds limit')
        value = json.loads(content)
        if (not isinstance(value, dict) or set(value) != {'version', 'digest', 'size', 'chunk_size', 'chunks'}
                or value['version'] != 1 or value['digest'] != key or value['chunk_size'] != CHUNK_SIZE
                or type(value['size']) is not int or not 0 <= value['size'] <= MAX_SIZE
                or not isinstance(value['chunks'], list)):
            raise ValueError('invalid raw-file recipe')
        if len(value['chunks']) != (value['size'] + CHUNK_SIZE - 1) // CHUNK_SIZE:
            raise ValueError('raw-file recipe has incorrect chunk count')
        for i, chunk in enumerate(value['chunks']):
            if (not isinstance(chunk, dict) or set(chunk) != {'digest', 'size'}
                    or type(chunk['size']) is not int
                    or chunk['size'] != min(CHUNK_SIZE, value['size'] - i * CHUNK_SIZE)):
                raise ValueError('raw-file recipe has invalid chunk size')
            _key(chunk['digest'])
        return value

    def iter_bytes(self, key: str):
        recipe = self.recipe(key)
        hasher = hashlib.sha256()
        for chunk in recipe['chunks']:
            with self.path('chunks', chunk['digest']).open('rb') as stream:
                content = stream.read(CHUNK_SIZE + 1)
            if len(content) != chunk['size'] or hashlib.sha256(content).hexdigest() != chunk['digest']:
                raise ValueError('raw chunk is missing or corrupt')
            hasher.update(content)
            yield content
        if hasher.hexdigest() != key:
            raise ValueError('reconstructed raw-file checksum mismatch')

    def verify(self, key: str) -> bool:
        try:
            for _ in self.iter_bytes(key):
                pass
            return True
        except (OSError, ValueError):
            return False

    def restore(self, key: str, destination: Path):
        if destination.exists() or destination.is_symlink():
            raise FileExistsError('raw restore destination already exists')
        _mkdir(destination.parent)
        fd, name = tempfile.mkstemp(dir=destination.parent, prefix='.raw-restore-')
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'wb') as stream:
                for content in self.iter_bytes(key):
                    stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, destination)
            sync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)
