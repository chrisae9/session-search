"""Base class for session parsers.

Each parser handles one AI coding tool's session storage format.
To add support for a new tool, create a new .py file in this directory
implementing a subclass of BaseParser. It will be auto-discovered.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import html
import json
import re


PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?P<kind>[A-Z0-9 ]*PRIVATE KEY)-----.*?"
    r"-----END (?P=kind)-----",
    flags=re.DOTALL,
)
AUTHORIZATION_RE = re.compile(
    r"(?i)(\bAuthorization\s*:\s*(?:Bearer|Basic)\s+)[^\s,;]+"
)
BEARER_RE = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{12,}")
URL_PASSWORD_RE = re.compile(
    r"(?i)(?P<prefix>https?://[^/\s:@]+:)"
    r"(?P<secret>[^@\s/]+)(?P<suffix>@)"
)
CURL_PASSWORD_RE = re.compile(
    r"(?i)(?P<prefix>(?<!\S)(?:-u|--user)\s+"
    r"(?P<quote>['\"]?)[^:\s'\"]+:)"
    r"(?P<secret>[^@\s'\"]+)(?P<suffix>@[^\s'\"]+)?(?P=quote)"
)
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<prefix>(?<![\w.-])(?:pass|[A-Za-z0-9_.-]*"
    r"(?:api[_-]?key|access[_-]?key|auth[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|private[_-]?key|password|passwd|secret|token))"
    r"\s*(?:=|:)\s*)"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}\]]+)"
)
KNOWN_TOKEN_RE = re.compile(
    r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"glpat-[A-Za-z0-9_-]{15,}|hf_[A-Za-z0-9]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b"
)
REDACTION_MARKER = "[REDACTED]"
_SAFE_SECRET_VALUES = {
    "", "none", "null", "true", "false", "unknown", "example", "dummy",
    "changeme", "redacted", "[redacted]", "<redacted>",
}

LEADING_INJECTED_BLOCK_RE = re.compile(
    r"^\s*<(?P<tag>skill|system-reminder|environment_context|"
    r"recommended_plugins)\b[^>]*>.*?</(?P=tag)>\s*",
    flags=re.IGNORECASE | re.DOTALL,
)
LEADING_AGENTS_BLOCK_RE = re.compile(
    r"^\s*#\s*AGENTS\.md instructions for [^\r\n]+\r?\n"
    r"\s*<INSTRUCTIONS\b[^>]*>.*?</INSTRUCTIONS>\s*",
    flags=re.IGNORECASE | re.DOTALL,
)
MALFORMED_INJECTED_PREFIX_RE = re.compile(
    r"^\s*(?:<skill(?:\s|>)|<system-reminder(?:\s|>)|"
    r"<environment_context(?:\s|>)|<recommended_plugins(?:\s|>)|"
    r"#\s*AGENTS\.md instructions for |Base directory for this skill:)",
    flags=re.IGNORECASE,
)

REALTIME_DELEGATION_RE = re.compile(
    r"^\s*<realtime_delegation\b[^>]*>\s*"
    r"(?P<body>.*?)\s*</realtime_delegation>\s*$",
    flags=re.IGNORECASE | re.DOTALL,
)
REALTIME_INPUT_RE = re.compile(
    r"<input\b[^>]*>(?P<text>.*?)</input>",
    flags=re.IGNORECASE | re.DOTALL,
)
REALTIME_TRANSCRIPT_RE = re.compile(
    r"<transcript_delta\b[^>]*>(?P<text>.*?)</transcript_delta>",
    flags=re.IGNORECASE | re.DOTALL,
)
NOTIFICATION_ENVELOPE_RE = re.compile(
    r"^\s*<(?:subagent_notification|subagent-notification|"
    r"task-notification|task_notification|"
    r"codex_delegation|turn_aborted)\b",
    flags=re.IGNORECASE,
)
SUMMARY_ENVELOPE_RE = re.compile(
    r"^\s*<(?:summary|codex_internal_context)\b",
    flags=re.IGNORECASE,
)
CONTINUATION_SUMMARY_RE = re.compile(
    r"^\s*(?:This session is being continued from a previous conversation|"
    r"This conversation is being continued from a previous conversation|"
    r"The conversation is summarized below:)",
    flags=re.IGNORECASE,
)

# Text-bearing control payloads emitted by known harnesses. A user may
# legitimately paste arbitrary JSON with a ``type`` field, so only skip a
# decoded object when both its type and its expected structural marker match.
KNOWN_CONTROL_ENVELOPES = {
    "file-history-snapshot": {"snapshot", "files", "fileHistory"},
    "progress": {"data", "progress", "toolUseID", "tool_use_id"},
    "queue-operation": {"operation", "action"},
}


def _redact_sensitive_once(text: str) -> str:
    """Apply one redaction pass.

    Some structured text contains nested assignments. Redacting an outer value
    can expose another credential-shaped value, so callers iterate this helper
    to a fixed point rather than assuming one regex pass is sufficient.
    """
    text = PRIVATE_KEY_RE.sub(REDACTION_MARKER + " PRIVATE KEY", text)
    text = AUTHORIZATION_RE.sub(r"\1" + REDACTION_MARKER, text)
    text = BEARER_RE.sub(r"\1" + REDACTION_MARKER, text)

    def replace_delimited_secret(match):
        secret = match.group("secret")
        if REDACTION_MARKER.casefold() in secret.casefold():
            return match.group(0)
        suffix = match.groupdict().get("suffix") or ""
        quote = match.groupdict().get("quote") or ""
        return match.group("prefix") + REDACTION_MARKER + suffix + quote

    text = URL_PASSWORD_RE.sub(replace_delimited_secret, text)
    text = CURL_PASSWORD_RE.sub(replace_delimited_secret, text)

    def replace_assignment(match):
        raw = match.group("value")
        quote = raw[0] if raw[:1] in {"\"", "'"} and raw[-1:] == raw[:1] else ""
        plain = raw[1:-1] if quote else raw
        normalized = plain.strip().casefold()
        if (
            normalized in _SAFE_SECRET_VALUES
            or REDACTION_MARKER.casefold() in normalized
            or plain.startswith(("$", "${", "<", "["))
        ):
            return match.group(0)
        replacement = f"{quote}{REDACTION_MARKER}{quote}"
        return match.group("prefix") + replacement

    text = SENSITIVE_ASSIGNMENT_RE.sub(replace_assignment, text)
    return KNOWN_TOKEN_RE.sub(REDACTION_MARKER + " TOKEN", text)


def redact_sensitive_text(value) -> str:
    """Redact high-confidence credential values while preserving useful keys."""
    text = str(value or "")
    for _ in range(4):
        redacted = _redact_sensitive_once(text)
        if redacted == text:
            break
        text = redacted
    return text


def redact_sensitive_preview(value, max_chars: int) -> str:
    """Return a bounded preview without re-exposing a truncated credential."""
    redacted = redact_sensitive_text(value)
    if len(redacted) <= max_chars:
        return redacted
    # A cut can turn a previously short token fragment into a pattern that now
    # meets a detector threshold. Redact the bounded text again before the
    # final length cap; at this point any replaced source value is gone.
    bounded = redact_sensitive_text(redacted[:max_chars])
    if len(bounded) <= max_chars:
        return bounded
    # If the cap bisects a replacement marker, stop before the marker. Leaving
    # an incomplete marker after an Authorization/assignment prefix would make
    # each later redaction pass expand it again.
    marker_start = bounded.rfind(
        REDACTION_MARKER, 0, max_chars + len(REDACTION_MARKER)
    )
    if marker_start >= 0 and marker_start + len(REDACTION_MARKER) > max_chars:
        return bounded[:marker_start]
    return bounded[:max_chars]


def strip_injected_context_text(value) -> str:
    """Remove anchored harness context while preserving trailing user intent.

    Harnesses can prepend balanced skill, system-reminder, environment,
    plugin, or AGENTS.md blocks to a real user message. Remove only recognized
    blocks at the beginning and keep any text that follows. A recognized but
    malformed prefix fails closed because no safe user boundary is available.
    """
    text = str(value or "")
    original = text
    while True:
        match = (
            LEADING_INJECTED_BLOCK_RE.match(text)
            or LEADING_AGENTS_BLOCK_RE.match(text)
        )
        if not match:
            break
        text = text[match.end():]
    if text != original:
        return text.strip()
    if MALFORMED_INJECTED_PREFIX_RE.match(text):
        return ""
    return text


def is_known_control_envelope(value) -> bool:
    """Return whether text is a recognized machine-only JSON envelope."""
    text = str(value or "").strip()
    if not text.startswith("{"):
        return False
    try:
        decoded = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(decoded, dict):
        return False
    expected = KNOWN_CONTROL_ENVELOPES.get(decoded.get("type"))
    return bool(expected and expected.intersection(decoded))


def classify_user_content(value):
    """Separate original user intent from known generated wrapper material.

    Returns ``(user_text, user_origin, context_blocks)``. Realtime wrappers
    contain a current utterance plus a generated rolling transcript; keeping
    those as separate blocks makes the utterance eligible for user-intent
    ranking without discarding useful quoted context.
    """
    text = str(value or "").strip()
    match = REALTIME_DELEGATION_RE.match(text)
    if match:
        body = match.group("body")
        input_match = REALTIME_INPUT_RE.search(body)
        transcript_match = REALTIME_TRANSCRIPT_RE.search(body)
        if input_match:
            user_text = html.unescape(input_match.group("text")).strip()
            contexts = []
            if transcript_match:
                transcript = html.unescape(
                    transcript_match.group("text")
                ).strip()
                if transcript:
                    contexts.append({
                        "type": "user_context",
                        "text": transcript,
                        "user_origin": "summary",
                    })
            return user_text, "user", contexts
        return text, "summary", []
    if NOTIFICATION_ENVELOPE_RE.match(text):
        return text, "notification", []
    if (
        SUMMARY_ENVELOPE_RE.match(text)
        or CONTINUATION_SUMMARY_RE.match(text)
    ):
        return text, "summary", []
    return text, "user", []


def _bounded_head_tail(value, max_chars: int) -> str:
    redacted = redact_sensitive_text(value).strip()
    if max_chars <= 0:
        return ""
    if len(redacted) <= max_chars:
        return redacted
    marker = "\n...[truncated]...\n"
    if max_chars <= len(marker):
        return redact_sensitive_preview(redacted, max_chars)
    available = max(0, max_chars - len(marker))
    head = available // 2
    tail = available - head
    bounded = redacted[:head] + marker + (redacted[-tail:] if tail else "")
    return redact_sensitive_preview(bounded, max_chars)


def tool_result_excerpts(turn: "Turn", *, max_chars: int = 1200,
                         max_items: int = 4) -> list[str]:
    """Return a small redacted head/tail sample of tool results.

    The canonical archive must not become a raw command-output mirror. This
    keeps only a bounded diagnostic breadcrumb while retaining exact errors
    that otherwise disappear during cross-machine drill-down.
    """
    raw_results = [
        block.get("text", "")
        for block in turn.blocks
        if block.get("type") == "tool_result" and block.get("text")
    ][:max_items]
    if not raw_results or max_chars <= 0:
        return []
    per_item = max(1, max_chars // len(raw_results))
    excerpts = [
        _bounded_head_tail(text, per_item)
        for text in raw_results
    ]
    return [excerpt for excerpt in excerpts if excerpt]


@dataclass
class ToolCall:
    name: str
    input_summary: str


@dataclass
class SourceProvenance:
    """Durable, non-secret locator for the records backing a parsed turn."""
    kind: str
    source: str
    start: int | str | None = None
    end: int | str | None = None


@dataclass
class Turn:
    """A single user-assistant exchange within a session."""
    timestamp: str
    user_text: str
    assistant_text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    files_referenced: list[str] = field(default_factory=list)
    commands_run: list[str] = field(default_factory=list)
    blocks: list[dict] = field(default_factory=list)
    provenance: SourceProvenance | None = None
    user_origin: str = "user"
    native_turn_id: str | None = None
    turn_index: int | None = None


@dataclass
class ParsedSession:
    """Common format for a parsed session, regardless of source tool."""
    slug: str | None
    turns: list[Turn]
    project_dir: str | None = None
    source: str = ""
    is_subagent: bool = False
    parent_session_id: str | None = None
    native_session_id: str | None = None
    authoritative_empty: bool = False


def turn_breadcrumb_events(turn: Turn, *, max_chars: int = 1200,
                           include_tool_results: bool = True):
    """Return bounded typed events from normalized turn metadata.

    Tool results are represented only by small redacted head/tail excerpts;
    full arbitrary command output remains local to the source computer.
    """
    events = []

    tool_summaries = []
    for call in turn.tool_calls[:8]:
        name = str(call.name or "unknown").strip() or "unknown"
        summary = redact_sensitive_text(call.input_summary).strip()
        tool_summaries.append(f"{name}: {summary}" if summary else name)
    if tool_summaries:
        events.append(("tool_call", "tool", "Tools: " + "; ".join(tool_summaries)))

    def unique(values, limit):
        seen = set()
        output = []
        for value in values:
            value = str(value or "").strip()
            if value and value not in seen:
                seen.add(value)
                output.append(redact_sensitive_text(value))
            if len(output) == limit:
                break
        return output

    files = unique(turn.files_referenced, 12)
    if files:
        events.append(("file", "file", "Files: " + " ".join(files)))

    commands = unique(turn.commands_run, 8)
    if commands:
        events.append(("command", "command", "Commands: " + " ; ".join(commands)))

    if include_tool_results:
        prefix = "Result: "
        excerpt_budget = max(0, max_chars - len(prefix))
        for excerpt in tool_result_excerpts(
            turn, max_chars=excerpt_budget
        ):
            events.append(("tool_result", "tool_result", prefix + excerpt))

    return [
        (event_type, role, text[:max_chars])
        for event_type, role, text in events
        if text
    ]


class BaseParser(ABC):
    """Abstract base for tool-specific session parsers."""

    # Display name for this tool (e.g. "claude-code", "opencode")
    name: str = "unknown"

    @property
    def source_id(self) -> str:
        """Stable backend identity used for deletion and failure isolation."""
        return self.name

    replaces_source_ids: set[str] = set()

    @abstractmethod
    def detect(self) -> bool:
        """Return True if this tool's session data exists on the system."""

    @abstractmethod
    def find_sessions(self, state: dict, full: bool = False, skip_active: bool = True) -> tuple[list, set, set]:
        """Scan for sessions to index.

        Args:
            state: Persistent indexing state dict (mutable, parser owns its keys).
            full: If True, ignore state and reprocess everything.

        Returns:
            (to_process, current_keys, unchanged_keys)
            - to_process: list of session-specific info objects to pass to parse_session()
            - current_keys: set of all session keys that currently exist
            - unchanged_keys: set of session keys that haven't changed since last index
        """

    @abstractmethod
    def parse_session(self, session_info) -> ParsedSession:
        """Parse a single session into the common Turn format.

        Args:
            session_info: An item from the to_process list returned by find_sessions().

        Returns:
            ParsedSession with turns in chronological order.
        """

    @abstractmethod
    def session_key(self, session_info) -> str:
        """Unique key for tracking this session in the index state."""

    @abstractmethod
    def session_id(self, session_info) -> str:
        """Short identifier used in search results and show commands."""

    def session_ids_for_keys(self, session_keys) -> set[str]:
        """Resolve discovery keys to stable session IDs when possible.

        Override this for sources whose storage key can move while the logical
        session remains the same. The indexer uses it to preserve an unchanged
        session across moves such as Codex active-to-archived relocation.
        """
        return set()

    @abstractmethod
    def project_slug(self, session_info) -> str:
        """Human-readable project name for display."""

    def resolve_session(self, session_id_prefix: str) -> "ParsedSession | None":
        """Resolve a session ID prefix to a full parsed session for the show command.

        Override this if your parser can look up sessions by prefix.
        Default returns None (parser doesn't support show lookups).
        """
        return None

    def mark_index_failed(self, state: dict, previous_state: dict, session_info):
        """Roll back optimistic discovery state after a parse failure.

        File-backed parsers store their fingerprint directly under session_key,
        so restoring that entry makes the next index retry the session while the
        caller keeps the last known-good indexed document.
        """
        key = self.session_key(session_info)
        if key in previous_state:
            state[key] = previous_state[key]
        else:
            state.pop(key, None)

    def empty_session_is_valid(self, session_info, parsed: ParsedSession) -> bool:
        """Return whether zero indexable turns is a successful source result.

        Override when the source has a reliable change fingerprint and commonly
        exposes empty/stub sessions. The default is conservative so a malformed
        local parser cannot erase the last known-good entry.
        """
        return False
