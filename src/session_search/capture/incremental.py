"""Verified-prefix parser checkpoints. Persistence and capture integration are separate."""

import hashlib
from dataclasses import dataclass, replace
from itertools import chain
from pathlib import Path

from session_search.capture.local import normalize_session
from session_search.capture.parsers.codex import CodexParser, _iter_json_records
from session_search.core.records import Event


@dataclass(frozen=True)
class ParseCheckpoint:
    filename: str
    size: int
    sha256: str
    lines: int
    metadata: tuple
    ownership: str
    turn_offset: int
    turn_line: int
    completed_events: tuple[Event, ...]


def scan_bytes(path, prefix_size):
    full = hashlib.sha256()
    prefix_digest = full.hexdigest() if prefix_size == 0 else None
    size = lines = 0
    last = b""
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            split = prefix_size - size
            if prefix_digest is None and 0 < split <= len(block):
                full.update(block[:split])
                prefix_digest = full.hexdigest()
                full.update(block[split:])
            else:
                full.update(block)
            size += len(block)
            lines += block.count(b"\n")
            last = block[-1:]
    if size and last != b"\n":
        raise ValueError("incremental parser requires a complete-record staging file")
    return size, lines, full.hexdigest(), prefix_digest


def line_offset(path, target, *, start_offset=0, start_line=1):
    """Locate a line boundary without allocating a potentially huge JSON record."""
    remaining = target - start_line
    offset = start_offset
    if remaining < 0:
        raise ValueError("target precedes the known line boundary")
    with path.open("rb") as stream:
        stream.seek(start_offset)
        while remaining:
            block = stream.read(1024 * 1024)
            if not block:
                raise ValueError("turn line is outside the staged file")
            count = block.count(b"\n")
            if count < remaining:
                remaining -= count
                offset += len(block)
                continue
            position = -1
            for _ in range(remaining):
                position = block.find(b"\n", position + 1)
            return offset + position + 1
    return offset


def parse_incremental(path: Path, checkpoint: ParseCheckpoint | None = None, *,
                      project_fallback: str | None = None):
    """Return normalized evidence, an in-memory checkpoint, and work diagnostics.

    Input must be an immutable complete-record staging file. The checkpoint is a
    trusted in-process object, not a serialized interchange format.
    """
    before = path.stat()
    size, lines, sha256, prefix = scan_bytes(path, checkpoint.size if checkpoint else 0)
    reusable = bool(checkpoint and checkpoint.filename == path.name
                    and size >= checkpoint.size and prefix == checkpoint.sha256)
    parser = CodexParser(session_index=path.parent / "no-index", strict=True)
    metadata = resume = None
    if reusable:
        metadata = chain(checkpoint.metadata, _iter_json_records(
            path, start_offset=checkpoint.size, start_line=checkpoint.lines, strict=True))
        resume = {"ownership": checkpoint.ownership, "offset": checkpoint.turn_offset,
                  "line": checkpoint.turn_line}
    parsed = parser.parse_session(path, _metadata_records=metadata, _resume=resume)
    if project_fallback is not None and parsed.project_dir == str(path.parent):
        parsed.project_dir = project_fallback
    normalized = normalize_session(parsed)
    if parser.resumed:
        normalized = replace(normalized, events=checkpoint.completed_events + normalized.events)
    next_checkpoint = None
    if parsed.turns and parser.scan_ownership is not None:
        last = parsed.turns[-1]
        last_events = normalize_session(replace(parsed, turns=[last])).events
        completed = normalized.events[:-len(last_events)] if last_events else normalized.events
        turn_line = last.provenance.start
        if parser.resumed and turn_line == checkpoint.turn_line:
            turn_offset = checkpoint.turn_offset
        elif parser.resumed and turn_line > checkpoint.lines:
            turn_offset = line_offset(path, turn_line, start_offset=checkpoint.size,
                                      start_line=checkpoint.lines + 1)
        else:
            turn_offset = line_offset(path, turn_line)
        next_checkpoint = ParseCheckpoint(
            path.name, size, sha256, lines, tuple(parser.scan_metadata), parser.scan_ownership,
            turn_offset, turn_line, completed)
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("staged source changed during incremental parsing")
    return normalized, next_checkpoint, {
        "mode": "incremental" if parser.resumed else "full",
        "reused_events": len(checkpoint.completed_events) if parser.resumed else 0,
        "metadata_scan_start": checkpoint.size if reusable else 0,
        "response_scan_start": checkpoint.turn_offset if parser.resumed else 0,
    }
