"""Disposable, bounded, atomic parser checkpoints; never authoritative evidence."""

import fcntl
import hashlib
import json
import os
import re
import tempfile
import zlib
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

from session_search.capture.incremental import ParseCheckpoint
from session_search.core.records import Event, canonical_json, digest
from session_search.storage.objects import sync_directory

MAX_DECODED = 128 * 1024 ** 2
DEFAULT_BUDGET = 512 * 1024 ** 2


@lru_cache(maxsize=1)
def parser_identity():
    from session_search.capture import incremental, local
    from session_search.capture.parsers import base, codex
    from session_search.core import records
    value = hashlib.sha256()
    for module in (incremental, local, base, codex, records):
        value.update(Path(module.__file__).read_bytes())
    return value.hexdigest()


def decode(data, key):
    if len(data) < 32:
        raise ValueError("incomplete checkpoint")
    decoder = zlib.decompressobj()
    raw = decoder.decompress(data[32:], MAX_DECODED + 1)
    if len(raw) > MAX_DECODED or not decoder.eof or decoder.unused_data:
        raise ValueError("invalid checkpoint compression")
    if hashlib.sha256(raw).digest() != data[:32]:
        raise ValueError("checkpoint checksum mismatch")
    value = json.loads(raw)
    if value["version"] != 1 or value["parser"] != parser_identity() or value["key"] != key:
        raise ValueError("incompatible checkpoint")
    fields = value["checkpoint"]
    for name in ("size", "lines", "turn_offset", "turn_line"):
        if type(fields[name]) is not int or fields[name] < 0:
            raise ValueError("invalid checkpoint position")
    if not (0 < fields["turn_line"] <= fields["lines"]
            and fields["turn_offset"] < fields["size"]):
        raise ValueError("invalid open turn boundary")
    if not isinstance(fields["sha256"], str) or not re.fullmatch("[0-9a-f]{64}", fields["sha256"]):
        raise ValueError("invalid prefix identity")
    if not isinstance(fields["filename"], str) or Path(fields["filename"]).name != fields["filename"]:
        raise ValueError("invalid filename")
    if not isinstance(fields["ownership"], str) or not isinstance(json.loads(fields["ownership"]), list):
        raise ValueError("invalid ownership")
    metadata = fields["metadata"]
    if not isinstance(metadata, list):
        raise ValueError("invalid metadata")
    previous = 0
    for line, record in metadata:
        if type(line) is not int or not previous < line <= fields["lines"] or not isinstance(record, dict):
            raise ValueError("invalid metadata boundary")
        if record.get("type") not in {"session_meta", "event_msg", "inter_agent_communication_metadata"}:
            raise ValueError("invalid metadata record")
        if not isinstance(record.get("payload"), dict):
            raise ValueError("invalid metadata payload")
        previous = line
    events = tuple(Event(**event) for event in fields["completed_events"])
    if len({e.event_id for e in events}) != len(events):
        raise ValueError("duplicate cached event")
    return ParseCheckpoint(**{**fields, "metadata": tuple(metadata), "completed_events": events})


class CheckpointCache:
    def __init__(self, root: Path, *, budget_bytes=DEFAULT_BUDGET):
        if type(budget_bytes) is not int or budget_bytes <= 0:
            raise ValueError("checkpoint budget must be positive")
        self.root = root
        self.budget = budget_bytes

    def _entries(self):
        if self.root.is_symlink():
            raise ValueError("checkpoint root must not be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        entries = []
        for path in self.root.iterdir():
            if path.is_symlink() or not path.is_file():
                raise ValueError("unexpected checkpoint entry")
            if path.name == ".lock":
                continue
            if re.fullmatch("[a-f0-9]{64}\\.cache", path.name):
                entries.append(path)
            elif not path.name.startswith(".checkpoint-"):
                raise ValueError("unexpected checkpoint entry")
        return entries

    def load(self, source: Path):
        try:
            self._entries()
            key = digest(str(source.resolve()).encode())
            path = self.root / (key + ".cache")
            with path.open("rb") as stream:
                data = stream.read(min(self.budget, MAX_DECODED) + 1)
            if len(data) > min(self.budget, MAX_DECODED):
                return None
            result = decode(data, key)
            os.utime(path, None)
            return result
        except (OSError, ValueError, KeyError, TypeError, zlib.error, OverflowError):
            return None

    def save(self, source: Path, checkpoint: ParseCheckpoint | None):
        if checkpoint is None:
            return False
        temporary = None
        try:
            self._entries()
            key = digest(str(source.resolve()).encode())
            raw = canonical_json({"version": 1, "parser": parser_identity(), "key": key,
                                  "checkpoint": asdict(checkpoint)}).encode()
            if len(raw) > MAX_DECODED:
                return False
            data = hashlib.sha256(raw).digest() + zlib.compress(raw, level=3)
            if len(data) > min(self.budget, MAX_DECODED):
                return False
            target = self.root / (key + ".cache")
            with (self.root / ".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                entries = self._entries()
                # Only this cache writer creates these files, under this lock.
                for stale in self.root.glob(".checkpoint-*"):
                    stale.unlink()
                total = sum(p.stat().st_size for p in entries)
                for old in sorted(entries, key=lambda p: p.stat().st_mtime_ns):
                    if total + len(data) <= self.budget:
                        break
                    if old == target:
                        continue
                    total -= old.stat().st_size
                    old.unlink()
                if total + len(data) > self.budget:
                    return False  # Preserve an old entry if replacement cannot fit.
                fd, name = tempfile.mkstemp(prefix=".checkpoint-", dir=self.root)
                temporary = Path(name)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
                sync_directory(self.root)
            return True
        except (OSError, ValueError, KeyError, TypeError, zlib.error):
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass  # A later writer can reclaim abandoned cache staging.
