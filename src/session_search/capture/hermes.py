"""Bounded, WAL-aware capture of explicitly selected Hermes SQLite transcripts."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import re
import sqlite3
import stat
import time

from session_search.capture.parsers.base import redact_sensitive_text, strip_injected_context_text
from session_search.core.records import Event, SessionRevision, canonical_json, digest

_SESSION_COLUMNS = {"id", "parent_session_id", "source", "hidden", "end_reason", "model_config", "title"}
_MESSAGE_COLUMNS = {
    "id", "session_id", "role", "content", "timestamp", "active", "compacted",
    "message_uid", "display_identity", "display_order", "display_kind",
    "display_metadata", "_compressed_summary", "absorbed_message_uids",
}
_INTERNAL = ("tool", "subagent", "cron", "worker", "batch", "kanban", "curator")
# Hermes hides the canonical Bot Chat from Sessions because it appears in Bots.
# Match the registry title for local chats, without admitting arbitrary hidden rows.
_ELIGIBLE = f"""(COALESCE(hidden, 0) = 0 OR (
      title = 'Bot Chat' AND source IN ('local', 'cli', 'tui', 'gui', 'desktop', 'oneshot', 'acp')
 )) AND COALESCE(source, '') NOT IN ({",".join("?" for _ in _INTERNAL)})
 AND (model_config IS NULL OR CASE WHEN json_valid(model_config)
      THEN json_type(model_config, '$._delegate_from') IS NULL
           OR json_type(model_config, '$._delegate_from') = 'null' ELSE 0 END)"""
_SUMMARY_END = "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"
_SUMMARY_SPLIT = "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]"
_PRIOR_HEADER = "[PRIOR CONTEXT — for reference only; not a new message]"
_STEER_END = "[/OUT-OF-BAND USER MESSAGE]"
_SYNTHETIC = (
    "[System:", "[CONTEXT", "[PRIOR CONTEXT", "[IMPORTANT: Background", "[Your active task list",
    "[Planning state preserved", "[ASYNC DELEGATION", "[OUT-OF-BAND", "Cronjob Response:",
)
_MEMORY_FENCE = re.compile(r"<\s*memory-context\s*>.*?(?:</\s*memory-context\s*>|\Z)", re.I | re.S)
_WITHHELD = "[Message withheld: a pre_llm_call policy hook blocked it.]"


class HermesReadError(ValueError):
    """A fixed diagnostic code, without paths or transcript text."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _source_identity(path: Path) -> tuple[int, int]:
    try:
        info = path.stat()
    except OSError:
        raise HermesReadError("source_unavailable") from None
    if not stat.S_ISREG(info.st_mode):
        raise HermesReadError("source_unavailable")
    return info.st_dev, info.st_ino


@contextmanager
def _snapshot(path: Path, identity: tuple[int, int]):
    if _source_identity(path) != identity:
        raise HermesReadError("changed_during_read")
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.25)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA trusted_schema = OFF")
            deadline = time.monotonic() + 2
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            conn.execute("BEGIN")
            yield conn
        finally:
            conn.close()
    except sqlite3.Error as exc:
        code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
        reason = "source_busy" if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) else (
            "source_read_budget" if code == sqlite3.SQLITE_INTERRUPT else "source_read_error"
        )
        raise HermesReadError(reason) from None
    if _source_identity(path) != identity:
        raise HermesReadError("changed_during_read")


def _check_schema(conn: sqlite3.Connection) -> None:
    for table, required in (("sessions", _SESSION_COLUMNS), ("messages", _MESSAGE_COLUMNS)):
        obj = conn.execute("SELECT type FROM sqlite_schema WHERE name = ?", (table,)).fetchone()
        if obj is None or obj[0] != "table":
            raise HermesReadError("unsupported_schema")
        if not required.issubset({row[1] for row in conn.execute(f"PRAGMA table_info({table})")}):
            raise HermesReadError("unsupported_schema")


def _identity(*parts: str) -> str:
    return digest(canonical_json(parts).encode())


def _session_key(producer: str, profile: str, session: str) -> str:
    return "hermes:" + _identity(producer, profile, session)


def _checked_id(value, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise HermesReadError("invalid_session_identity")
    return value


def _conversation_text(row: sqlite3.Row) -> str:
    if row["model_only"] or row["display_kind"] not in (None, "", "steer", "hidden"):
        return ""
    if row["withheld"]:
        return _WITHHELD if row["display_kind"] != "hidden" else ""
    text = row["content"]
    # The structured Hermes content encoding starts with a NUL sentinel. V1
    # skips envelopes rather than flattening unknown attachment or tool parts.
    if not isinstance(text, str) or not text.strip() or "\x00" in text:
        return ""
    kind = row["display_kind"]
    if row["_compressed_summary"] or text.lstrip().startswith(("[CONTEXT", _PRIOR_HEADER)):
        if row["role"] != "user":
            return ""
        if _SUMMARY_SPLIT in text:
            text = text.split(_SUMMARY_SPLIT, 1)[0].strip().removeprefix(_PRIOR_HEADER)
        elif _SUMMARY_END in text:
            text = text.split(_SUMMARY_END, 1)[1]
        else:
            return ""
    elif kind == "hidden":
        return ""
    if kind == "steer":
        opener, sep, body = text.strip().partition("\n")
        if not opener.startswith("[OUT-OF-BAND USER MESSAGE — ") or not sep or not body.endswith(_STEER_END):
            return ""
        text = body.removesuffix(_STEER_END)
    if row["role"] == "user" and text.lstrip().startswith(_SYNTHETIC):
        return ""
    return redact_sensitive_text(strip_injected_context_text(_MEMORY_FENCE.sub("", text))).strip()


def _rows(conn: sqlite3.Connection, native_id: str, max_messages: int, max_bytes: int):
    # Never retrieve withdrawn bodies or metadata bodies. BLOB length counts
    # bytes past a NUL sentinel, unlike SQLite's text length.
    rows = conn.execute("""
        WITH selected AS (
            SELECT id, role, timestamp, active, compacted, message_uid, display_identity,
                   display_order, display_kind, _compressed_summary,
                   CASE WHEN length(CAST(absorbed_message_uids AS BLOB)) > 65536 THEN ''
                        ELSE absorbed_message_uids END AS absorbed_message_uids,
                   CASE WHEN display_metadata IS NULL THEN 0
                        WHEN json_valid(display_metadata)
                        THEN COALESCE(json_extract(display_metadata, '$.model_only'), 0) != 0
                        ELSE 1 END AS model_only,
                   CASE WHEN json_valid(display_metadata)
                        THEN json_type(display_metadata, '$.withheld_prompt') = 'text'
                        END AS withheld,
                   CASE WHEN display_kind = 'failed_turn' AND json_valid(display_metadata)
                        THEN json_extract(display_metadata, '$.error_surface.code') = 'content_policy_blocked'
                        END AS refused_boundary,
                   content
            FROM messages
            WHERE session_id = ? AND role IN ('user', 'assistant')
            ORDER BY id LIMIT ?
        ), visible AS (
            SELECT id, role, timestamp, active, compacted, message_uid, display_identity,
                   display_order, display_kind, _compressed_summary, model_only, withheld, refused_boundary,
                   absorbed_message_uids,
                   CASE WHEN (active = 1 OR compacted = 1) AND NOT model_only
                         AND NOT COALESCE(withheld, 0)
                         AND (COALESCE(display_kind, '') IN ('', 'steer')
                              OR (display_kind = 'hidden' AND _compressed_summary = 1))
                        THEN content END AS payload
            FROM selected
        )
        SELECT id, role, timestamp, active, compacted, message_uid, display_identity,
               display_order, display_kind, _compressed_summary, model_only, withheld, refused_boundary,
               absorbed_message_uids,
               COALESCE(length(cast(payload AS blob)), 0) AS byte_size,
               CASE WHEN length(cast(payload AS blob)) <= ? THEN payload END AS content
        FROM visible ORDER BY id
        """, (native_id, max_messages + 1, max_bytes))
    admitted = 0
    for count, row in enumerate(rows, 1):
        admitted += row["byte_size"] + len((row["absorbed_message_uids"] or "").encode())
        if count > max_messages:
            raise HermesReadError("session_row_limit")
        if admitted > max_bytes:
            raise HermesReadError("session_byte_limit")
        yield row


def _aliases(row: sqlite3.Row) -> tuple:
    aliases = []
    if row["message_uid"]:
        # Copies retain the original timestamp. A bare UID is not sufficient
        # to identify an occurrence; import and branch can carry the UID.
        aliases.append(("uid", row["message_uid"], row["role"], row["timestamp"]))
    if row["display_identity"]:
        aliases.append(("display_identity", row["display_identity"]))
    return tuple(aliases)


def _events(rows: list[sqlite3.Row]) -> tuple[Event, ...]:
    # Rewinds use active=0, compacted=0, whereas preserved history remains
    # compacted=1. The newest copy's flags suppress its archived predecessors.
    last = {}
    first = {}
    pending_turn = []
    refused = set()
    withdrawn = {}
    row_uids = {}
    for row in rows:
        aliases = _aliases(row)
        visible = row["active"] or row["compacted"]
        raw_uids = row["absorbed_message_uids"]
        try:
            absorbed = json.loads(raw_uids) if raw_uids is not None else []
        except (ValueError, TypeError, RecursionError):
            raise HermesReadError("invalid_absorbed_message_uids") from None
        if not isinstance(absorbed, list) or any(
            not isinstance(uid, str) or len(uid) > 1024 for uid in absorbed
        ):
            raise HermesReadError("invalid_absorbed_message_uids")
        uids = {uid for uid in [row["message_uid"], *absorbed] if uid}
        row_uids[row["id"]] = uids
        withdrawn.update(dict.fromkeys(uids, not visible))
        if row["role"] == "user" and row["display_kind"] in (None, ""):
            pending_turn = []
        if visible and row["refused_boundary"]:
            for previous in pending_turn:
                refused.add(previous["id"])
                refused.update(_aliases(previous))
        if visible and row["display_kind"] == "content_refused":
            refused.update(aliases)
        pending_turn.append(row)
        for alias in aliases:
            last[alias] = row
            first.setdefault(alias, row["id"])
    candidates = []
    live = set()
    for row in rows:
        if not (row["active"] or row["compacted"]):
            continue
        if any(withdrawn[uid] for uid in row_uids[row["id"]]):
            continue
        aliases = _aliases(row)
        if row["id"] in refused or any(key in refused for key in aliases):
            continue
        if any(last[key]["withheld"] for key in aliases) and not row["withheld"]:
            continue
        if not row["active"] and any(
            not (last[key]["active"] or last[key]["compacted"]) or (
                last[key]["id"] != row["id"]
                and not last[key]["_compressed_summary"]
                and not last[key]["model_only"]
            )
            for key in aliases
        ):
            # A newer copy can retain the UID while changing text. Leave
            # separately active occurrences alone, but publish neither a
            # superseded archived version nor a withdrawn copy's predecessor.
            continue
        text = _conversation_text(row)
        if not text:
            continue
        shape = row["role"], row["timestamp"], text
        keys = {(alias, shape) for alias in aliases}
        if row["display_order"] is not None:
            keys.add((("display_order", row["display_order"]), shape))
        if row["active"]:
            live.update(keys)
        order = row["display_order"] if row["display_order"] is not None else min(
            (first[key] for key in aliases), default=row["id"]
        )
        candidates.append((order, row["id"], row, text, keys))
    candidates.sort(key=lambda candidate: candidate[:2])
    archived = set()
    occurrences = Counter()
    events = []
    for _, row_id, row, text, keys in candidates:
        if not row["active"]:
            if keys & live or keys & archived:
                continue
            archived.update(keys)
        base = ("uid:" + row["message_uid"]) if row["message_uid"] else (
            "row:" + str(min((first[key] for key in _aliases(row)), default=row_id))
        )
        occurrences[base] += 1
        try:
            ts = datetime.fromtimestamp(row["timestamp"], timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            raise HermesReadError("invalid_message_timestamp") from None
        role = row["role"]
        events.append(Event(_identity(base, str(occurrences[base])), role, text, ts, role, f"{role}_text"))
    return tuple(events)


def iter_hermes_revisions(
    db_path: Path, *, producer: str, profile: str,
    max_sessions: int = 1000, max_messages: int = 20000, max_session_bytes: int = 8 * 1024**2,
    on_deferred: Callable[[str], None] | None = None,
    is_known: Callable[[str], bool] | None = None,
) -> Iterator[SessionRevision]:
    """Yield complete revisions, releasing each read transaction before yield.

    A still-present session that becomes ineligible may publish an empty
    revision if the caller recognizes its previously captured ID. Missing
    sessions and source failures never withdraw existing evidence.
    """
    if any(type(n) is not int or n < 1 for n in (max_sessions, max_messages, max_session_bytes)):
        raise ValueError("Hermes reader limits must be positive integers")
    _checked_id(producer)
    _checked_id(profile)
    path = db_path.absolute()
    identity = _source_identity(path)
    with _snapshot(path, identity) as conn:
        _check_schema(conn)
        sessions = conn.execute(
            "SELECT id FROM sessions ORDER BY id LIMIT ?",
            (max_sessions + 1,),
        ).fetchall()
    if len(sessions) > max_sessions:
        raise HermesReadError("profile_session_limit")
    for candidate in sessions:
        try:
            native_id = _checked_id(candidate["id"])
            session_key = _session_key(producer, profile, native_id)
            known = is_known is not None and is_known(session_key)
            with _snapshot(path, identity) as conn:
                session = conn.execute(f"""
                    SELECT id, parent_session_id, end_reason, ({_ELIGIBLE}) AS eligible,
                           EXISTS(SELECT 1 FROM sessions parent
                                  WHERE parent.id = s.parent_session_id
                                    AND parent.end_reason = 'compression') AS compression_parent,
                           CASE WHEN json_valid(model_config) THEN
                               parent_session_id = json_extract(model_config, '$._branched_from')
                               OR parent_session_id = json_extract(model_config, '$._reset_from')
                           END AS fork_from_parent
                    FROM sessions s WHERE id = ?
                    """, (*_INTERNAL, native_id)).fetchone()
                if session is None:
                    continue
                # A rewind in a compression child can suppress the parent's
                # copies; v1 defers those lineages instead of stitching them.
                rotation = session["end_reason"] == "compression" or (
                    session["compression_parent"] and not session["fork_from_parent"]
                )
                parent = _checked_id(session["parent_session_id"], optional=True)
                # Parent-linked rows need an explicit user-visible branch/reset
                # marker in v1. An unmarked non-rotation child may be a worker
                # transcript on the parent's platform.
                eligible = session["eligible"] and (parent is None or session["fork_from_parent"])
                if eligible and not rotation:
                    rows = list(_rows(conn, native_id, max_messages, max_session_bytes))
                else:
                    parent, rows = None, []
            if rotation:
                if on_deferred is not None:
                    on_deferred("compression_lineage")
                elif not known:
                    raise HermesReadError("compression_lineage")
            if (rotation or not eligible) and not known:
                continue
            events = _events(rows)
            if _source_identity(path) != identity:
                raise HermesReadError("changed_during_read")
            yield SessionRevision(
                session_id=session_key, events=events, source="hermes", parser_version="hermes-sqlite-v1",
                parent_session_id=_session_key(producer, profile, parent) if parent else None,
            )
        except HermesReadError as exc:
            if exc.reason in ("changed_during_read", "source_unavailable") or on_deferred is None:
                raise
            on_deferred(exc.reason)
