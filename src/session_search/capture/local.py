"""Capture finite complete-record revisions without changing source sessions."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from session_search.capture.parsers.base import redact_sensitive_text
from session_search.capture.parsers.codex import CodexParser, _session_id_from_path
from session_search.core.records import Event, SessionRevision, canonical_json, digest
from session_search.storage.catalog import Catalog


def fingerprint(path: Path) -> str:
    stat = path.stat()
    return canonical_json([stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns])


def normalize_session(parsed) -> SessionRevision:
    events = []
    for turn_index, turn in enumerate(parsed.turns):
        turn_key = turn.native_turn_id or str(
            turn.provenance.start if turn.provenance else turn_index
        )
        blocks = turn.blocks or [
            {"type": "user_text", "text": turn.user_text},
            {"type": "assistant_text", "text": turn.assistant_text},
        ]
        for block_index, block in enumerate(blocks):
            kind = block.get("type", "")
            role = {"user_text": "user", "assistant_text": "assistant",
                    "tool_result": "tool", "tool_use": "tool"}.get(kind, "context")
            text = block.get("text") or block.get("summary", "")
            if not text:
                continue
            text = redact_sensitive_text(str(text))
            if role == "tool":
                text = text[:12000]
            events.append(Event(
                event_id=f"{turn_key}:{block_index}", role=role, text=text,
                timestamp=block.get("timestamp", turn.timestamp),
                origin=block.get("user_origin", turn.user_origin if role == "user" else role),
                kind=kind,
            ))
    return SessionRevision(
        session_id=parsed.native_session_id, events=tuple(events), source="codex",
        project=redact_sensitive_text(parsed.project_dir or ""),
        title=redact_sensitive_text(parsed.slug or ""),
        parent_session_id=parsed.parent_session_id, is_subagent=parsed.is_subagent,
    )


def capture_file(catalog: Catalog, path: Path, producer: str, *, archive_raw: bool = False) -> dict:
    path = path.resolve()
    before = fingerprint(path)
    if catalog.fingerprint(str(path)) == before:
        if not archive_raw or catalog.has_raw(str(path), before):
            return {"status": "unchanged"}
    # Preserve the rollout basename: the Codex parser uses it to distinguish
    # fork-owned evidence from inherited parent history.
    complete_bytes = 0
    partial_tail = False
    with tempfile.TemporaryDirectory(prefix="session-search-capture-") as staging:
        snapshot = Path(staging) / path.name
        with path.open("rb") as source, snapshot.open("wb") as dest:
            # Bound the snapshot to the size observed before reading an active writer.
            remaining = os.fstat(source.fileno()).st_size
            while remaining:
                line = source.readline(remaining)
                remaining -= len(line)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    partial_tail = True
                    break
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise ValueError("invalid Codex event envelope")
                dest.write(line)
                complete_bytes += len(line)
        if fingerprint(path) != before:
            return {"status": "changed_during_read", "retryable": True}
        parsed = CodexParser(session_index=Path(staging) / "no-index").parse_session(snapshot)
        if parsed.project_dir == staging:
            parsed.project_dir = str(path.parent)
        session = normalize_session(parsed)
        raw = None
        if archive_raw:
            from session_search.storage.objects import ObjectStore
            raw = {**ObjectStore(catalog.root).put(path), "path": str(path), "fingerprint": before}
            if fingerprint(path) != before:
                return {"status": "changed_during_read", "retryable": True}
        receipt = catalog.ingest(
            session, producer=producer, request_id=digest(
                f"{producer}:{path}:{before}:raw={archive_raw}".encode()
            ), checkpoint=(str(path), before), **({"raw": raw} if raw else {}),
        )
    return {"status": "captured", **receipt, "events": len(session.events),
            "complete_bytes": complete_bytes, "partial_tail": partial_tail}


def capture_home(catalog: Catalog, home: Path, producer: str, *, archive_raw: bool = False) -> dict:
    home = home.resolve()
    files = {}
    accessible_roots = 0
    errors = []
    for name in ("archived_sessions", "sessions"):
        directory = home / name
        if directory.exists():
            accessible_roots += 1
            for path in directory.rglob("*.jsonl"):
                files[_session_id_from_path(path)] = path
    counts: dict[str, int] = {}
    for path in sorted(files.values()):
        try:
            result = capture_file(catalog, path, producer, archive_raw=archive_raw)
            label = result["status"]
            counts[label] = counts.get(label, 0) + 1
        except (OSError, ValueError) as exc:
            # Do not echo paths or transcript-containing parse errors into logs.
            errors.append({"source_id": digest(str(path).encode()),
                           "error": type(exc).__name__, "retryable": True})
    state = "partial" if errors else "complete" if files else (
        "empty" if accessible_roots else "unavailable"
    )
    return {"version": 1, "status": state, "counts": counts, "errors": errors,
            "discovered": len(files), "raw_archival_enabled": archive_raw}
