"""Portable, content-addressed evidence. No machine paths participate in citations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalize_time(value: str) -> str:
    if not value:
        return ""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class Event:
    event_id: str
    role: str
    text: str
    timestamp: str = ""
    origin: str = "user"
    kind: str = "message"

    def __post_init__(self):
        if not isinstance(self.event_id, str) or not self.event_id or self.role not in {"user", "assistant", "tool", "context"}:
            raise ValueError("invalid event identity or role")
        if not all(isinstance(value, str) for value in (self.text, self.timestamp, self.origin, self.kind)):
            raise ValueError("event fields must be strings")
        object.__setattr__(self, "timestamp", normalize_time(self.timestamp))


@dataclass(frozen=True)
class SessionRevision:
    session_id: str
    events: tuple[Event, ...]
    source: str = "codex"
    project: str = ""
    title: str = ""
    parent_session_id: str | None = None
    is_subagent: bool = False
    parser_version: str = "codex-v1"

    def __post_init__(self):
        if not isinstance(self.session_id, str) or not self.session_id or not self.source:
            raise ValueError("session identity is required")
        if not all(isinstance(value, str) for value in (self.source, self.project, self.title, self.parser_version)):
            raise ValueError("session metadata must be strings")
        if self.parent_session_id is not None and not isinstance(self.parent_session_id, str):
            raise ValueError("parent session identity must be a string")
        if not isinstance(self.is_subagent, bool) or not all(isinstance(event, Event) for event in self.events):
            raise ValueError("invalid session events or subagent flag")
        ids = [event.event_id for event in self.events]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate event identity")

    @property
    def revision(self) -> str:
        return digest(canonical_json(asdict(self)).encode())


@dataclass(frozen=True)
class Citation:
    session_id: str
    revision: str
    event_id: str
    offset: int = 0

    def __post_init__(self):
        if not all(isinstance(value, str) and value for value in (self.session_id, self.revision, self.event_id)):
            raise ValueError("citation identity must contain nonempty strings")
        if not isinstance(self.offset, int) or self.offset < 0:
            raise ValueError("citation offset must be a nonnegative integer")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SearchQuery:
    text: str
    literal: bool = False
    role: str | None = None
    after: str | None = None
    before: str | None = None
    project: str | None = None
    session_id: str | None = None
    producer: str | None = None
    exclude_sessions: tuple[str, ...] = field(default_factory=tuple)
    include_subagents: bool = False
    limit: int = 10

    def __post_init__(self):
        if not isinstance(self.text, str) or not self.text.strip() or len(self.text) > 8192:
            raise ValueError("query must contain 1–8192 characters")
        if self.role not in {None, "user", "assistant"}:
            raise ValueError("role must be user or assistant")
        if not 1 <= self.limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if not isinstance(self.exclude_sessions, (tuple, list)) or len(self.exclude_sessions) > 100:
            raise ValueError("exclude_sessions must contain at most 100 session identities")
        if not all(isinstance(value, str) for value in self.exclude_sessions):
            raise ValueError("excluded session identities must be strings")
        for bound in ("after", "before"):
            if getattr(self, bound):
                object.__setattr__(self, bound, normalize_time(getattr(self, bound)))
        if self.after and self.before and self.after >= self.before:
            raise ValueError("after must precede before")
