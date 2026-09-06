"""Parser for Codex session history.

Codex stores active sessions under ~/.codex/sessions/YYYY/MM/DD/ and moves
closed sessions to ~/.codex/archived_sessions/. Each JSONL line is an event
envelope with a payload. Conversation content lives in response_item payloads
as messages, function calls, and function outputs.
"""

import json
import os
import re
import shlex
import time
import uuid
from pathlib import Path

from .base import (
    BaseParser, ParsedSession, SourceProvenance, ToolCall, Turn,
    classify_user_content, is_known_control_envelope,
)

SESSIONS_DIR = Path.home() / ".codex" / "sessions"
ARCHIVED_SESSIONS_DIR = Path.home() / ".codex" / "archived_sessions"
SESSION_INDEX = Path.home() / ".codex" / "session_index.jsonl"
MAX_TOOL_RESULT_CHARS = 12000
DEFAULT_ACTIVE_GRACE_SECONDS = 900
MAX_WRAPPER_SCAN_CHARS = 65536
MAX_NESTED_TOOL_CALLS = 8

TOOL_MAP = {
    "exec_command": "Bash",
    "shell_command": "Bash",
    "shell": "Bash",
    "write_stdin": "BashInput",
    "apply_patch": "Edit",
    "update_plan": "TodoWrite",
    "request_permissions": "PermissionRequest",
}


def _extract_text(content):
    """Extract text from Codex message content blocks."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("input_text", "output_text", "text"):
                text = block.get("text", "").strip()
                if text:
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _extract_agent_task_text(content):
    """Extract only plaintext task metadata from an inter-agent message."""
    if isinstance(content, str) and content.lstrip().startswith("["):
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError:
            decoded = content
        content = decoded
    return _extract_text(content)[:2000]


def _uuid7_time(value):
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None
    # UUIDv7 stores Unix epoch milliseconds in its first 48 bits. Python 3.14
    # exposes that through UUID.time, but 3.10-3.13 interpret .time using the
    # UUIDv1 layout even when version == 7. Decode the RFC 9562 field directly.
    return parsed.int >> 80 if parsed.version == 7 else None


def _nested_parent_id(source):
    if not isinstance(source, dict):
        return None
    subagent = source.get("subagent")
    if not isinstance(subagent, dict):
        return None
    spawn = subagent.get("thread_spawn")
    if not isinstance(spawn, dict):
        return None
    return spawn.get("parent_thread_id")


def _parse_arguments(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"input": raw}
    return {}


class _StaticJSLiteralParser:
    """Small parser for JSON-like JavaScript literals; never evaluates code."""

    def __init__(self, source, start=0):
        self.source = source
        self.index = start

    def _skip_space(self):
        while self.index < len(self.source):
            if self.source[self.index].isspace():
                self.index += 1
            elif self.source.startswith("//", self.index):
                end = self.source.find("\n", self.index + 2)
                self.index = len(self.source) if end < 0 else end + 1
            elif self.source.startswith("/*", self.index):
                end = self.source.find("*/", self.index + 2)
                self.index = len(self.source) if end < 0 else end + 2
            else:
                break

    def _string(self):
        quote = self.source[self.index]
        self.index += 1
        chars = []
        while self.index < len(self.source):
            char = self.source[self.index]
            self.index += 1
            if char == quote:
                return "".join(chars)
            if char == "\\" and self.index < len(self.source):
                escaped = self.source[self.index]
                self.index += 1
                chars.append({
                    "n": "\n", "r": "\r", "t": "\t", "b": "\b",
                    "f": "\f", "v": "\v", "0": "\0",
                }.get(escaped, escaped))
            else:
                if quote == "`" and char == "$" and self.source.startswith(
                    "{", self.index
                ):
                    raise ValueError("dynamic template literal")
                chars.append(char)
        raise ValueError("unterminated string")

    def _identifier(self):
        match = re.match(r"[A-Za-z_$][A-Za-z0-9_$]*", self.source[self.index:])
        if not match:
            raise ValueError("expected identifier")
        self.index += len(match.group(0))
        return match.group(0)

    def parse(self, depth=0):
        if depth > 8:
            raise ValueError("literal nesting too deep")
        self._skip_space()
        if self.index >= len(self.source):
            raise ValueError("missing literal")
        char = self.source[self.index]
        if char in "'\"`":
            return self._string()
        if char == "{":
            self.index += 1
            output = {}
            while True:
                self._skip_space()
                if self.index < len(self.source) and self.source[self.index] == "}":
                    self.index += 1
                    return output
                if self.index >= len(self.source):
                    raise ValueError("unterminated object")
                key = (
                    self._string()
                    if self.source[self.index] in "'\"`"
                    else self._identifier()
                )
                self._skip_space()
                if self.index >= len(self.source) or self.source[self.index] != ":":
                    raise ValueError("expected colon")
                self.index += 1
                output[key] = self.parse(depth + 1)
                self._skip_space()
                if self.index < len(self.source) and self.source[self.index] == ",":
                    self.index += 1
                    continue
                if self.index < len(self.source) and self.source[self.index] == "}":
                    self.index += 1
                    return output
                raise ValueError("expected object delimiter")
        if char == "[":
            self.index += 1
            output = []
            while True:
                self._skip_space()
                if self.index < len(self.source) and self.source[self.index] == "]":
                    self.index += 1
                    return output
                output.append(self.parse(depth + 1))
                self._skip_space()
                if self.index < len(self.source) and self.source[self.index] == ",":
                    self.index += 1
                    continue
                if self.index < len(self.source) and self.source[self.index] == "]":
                    self.index += 1
                    return output
                raise ValueError("expected array delimiter")
        match = re.match(
            r"(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?)\b",
            self.source[self.index:],
        )
        if not match:
            raise ValueError("dynamic expression")
        token = match.group(0)
        self.index += len(token)
        if token == "true":
            return True
        if token == "false":
            return False
        if token == "null":
            return None
        return float(token) if "." in token else int(token)


def _static_nested_tool_calls(value):
    """Extract supported ``tools.NAME({...})`` calls from wrapper source."""
    source = str(value or "")[:MAX_WRAPPER_SCAN_CHARS]
    output = []
    index = 0
    marker = re.compile(
        r"\btools\s*\.\s*(exec_command|apply_patch|write_stdin)\s*\("
    )
    while index < len(source) and len(output) < MAX_NESTED_TOOL_CALLS:
        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            index = len(source) if end < 0 else end + 1
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            index = len(source) if end < 0 else end + 2
            continue
        if source[index] in "'\"`":
            parser = _StaticJSLiteralParser(source, index)
            try:
                parser._string()
            except ValueError:
                return output
            index = parser.index
            continue
        match = marker.match(source, index)
        if not match:
            index += 1
            continue
        parser = _StaticJSLiteralParser(source, match.end())
        try:
            value = parser.parse()
        except ValueError:
            index = match.end()
            continue
        if isinstance(value, dict):
            output.append((match.group(1), value))
        elif match.group(1) == "apply_patch" and isinstance(value, str):
            output.append((match.group(1), {"input": value}))
        index = max(parser.index, match.end())
    return output


def _structured_nested_tool_calls(payload, inp):
    """Read explicit nested-call arrays produced by newer wrapper formats."""
    containers = []
    for owner in (payload, inp):
        if not isinstance(owner, dict):
            continue
        for key in ("nested_calls", "tool_calls", "calls"):
            value = owner.get(key)
            if isinstance(value, list):
                containers.extend(value)
    output = []
    for item in containers[:MAX_NESTED_TOOL_CALLS]:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("tool_name")
        if name not in TOOL_MAP:
            continue
        arguments = item.get("arguments", item.get("input", {}))
        output.append((name, _parse_arguments(arguments)))
    return output


def _tool_input_summary(name, inp):
    """Brief summary of a Codex tool call's input."""
    if not isinstance(inp, dict):
        return str(inp)[:200]
    try:
        if name in ("Bash", "BashInput"):
            return str(inp.get("cmd") or inp.get("command") or inp.get("chars") or "")[:300]
        if name == "Edit":
            patch = str(inp.get("input") or inp.get("patch") or "")
            for line in patch.splitlines():
                if line.startswith(("*** Add File: ", "*** Update File: ", "*** Delete File: ")):
                    return line.replace("*** ", "", 1)[:300]
            return patch[:300]
        if name == "TodoWrite":
            plan = inp.get("plan")
            if isinstance(plan, list):
                return f"{len(plan)} plan items"
        if name == "WebSearch":
            action = inp.get("action", {})
            if isinstance(action, dict):
                return str(action.get("query") or action.get("url") or action.get("pattern") or "")[:300]
        for k, v in list(inp.items())[:2]:
            return f"{k}={str(v)[:100]}"
    except Exception:
        return str(inp)[:200]
    return ""


def _append_tool_call(turn, raw_name, inp, line_number):
    """Append one normalized tool call and its bounded evidence."""
    name = TOOL_MAP.get(raw_name, raw_name)
    summary = _tool_input_summary(name, inp)
    turn.tool_calls.append(ToolCall(name=name, input_summary=summary))
    turn.blocks.append({
        "type": "tool_use",
        "name": name,
        "input": inp,
        "summary": summary,
    })
    if turn.provenance:
        turn.provenance.end = line_number

    if name == "Bash":
        command = inp.get("cmd") or inp.get("command")
        if command:
            command = str(command)
            turn.commands_run.append(command[:300])
            turn.files_referenced.extend(
                path[:500]
                for path in _extract_command_paths(command[:MAX_WRAPPER_SCAN_CHARS])[:12]
            )
        workdir = inp.get("workdir")
        if workdir:
            turn.files_referenced.append(str(workdir)[:500])
    elif name == "Edit":
        turn.files_referenced.extend(
            path[:500] for path in _extract_patch_paths(inp)[:12]
        )


def _extract_output_text(payload):
    output = payload.get("output", "")
    if isinstance(output, str):
        return output[:MAX_TOOL_RESULT_CHARS]
    if isinstance(output, list):
        parts = []
        for block in output:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
            else:
                parts.append(str(block))
        return "\n".join(p for p in parts if p)[:MAX_TOOL_RESULT_CHARS]
    return str(output)[:500]


def _project_slug(path_str):
    """Convert a filesystem path to a readable project slug."""
    if not path_str or path_str == "/":
        return "root"
    parts = Path(path_str).parts
    skip = {"home", "Users", "", "/"}
    meaningful = [p for p in parts if p not in skip and len(p) > 1]
    if len(meaningful) >= 2:
        return "-".join(meaningful[-2:])
    if meaningful:
        return meaningful[-1]
    return "root"


def _session_id_from_path(jsonl_path):
    stem = jsonl_path.stem
    if stem.startswith("rollout-"):
        parts = stem.split("-")
        if len(parts) >= 6:
            return "-".join(parts[-5:])
    return stem


def _is_archived_session(jsonl_path):
    try:
        Path(jsonl_path).relative_to(ARCHIVED_SESSIONS_DIR)
        return True
    except ValueError:
        return False


def _session_files():
    """Return one file per native session, preferring the active location."""
    by_session_id = {}
    if ARCHIVED_SESSIONS_DIR.exists():
        for path in ARCHIVED_SESSIONS_DIR.glob("**/*.jsonl"):
            by_session_id[_session_id_from_path(path)] = path
    if SESSIONS_DIR.exists():
        for path in SESSIONS_DIR.glob("*/*/*/*.jsonl"):
            by_session_id[_session_id_from_path(path)] = path
    return sorted(by_session_id.values(), key=str)


def _load_thread_names(index_path=None):
    names = {}
    index_path = SESSION_INDEX if index_path is None else index_path
    if not index_path.exists():
        return names
    with open(index_path) as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = row.get("id")
            name = row.get("thread_name")
            if sid and name:
                names[sid] = name
    return names


class CodexParser(BaseParser):
    """Parser for Codex JSONL session files."""

    name = "codex"

    def __init__(self, *, session_index=None):
        self.session_index = session_index

    def detect(self) -> bool:
        return bool(_session_files())

    def find_sessions(self, state, full=False, skip_active=True):
        jsonl_files = _session_files()
        self.deferred_active_keys = set()
        if not jsonl_files:
            return [], set(), set()

        archived_count = sum(_is_archived_session(path) for path in jsonl_files)
        print(
            f"Codex: {len(jsonl_files)} session files "
            f"({archived_count} archived)"
        )

        current_keys = {str(f) for f in jsonl_files}
        files_to_process = []
        unchanged_keys = set()
        active_thread_id = os.environ.get("CODEX_THREAD_ID", "") if skip_active else ""
        try:
            active_grace = max(0, int(os.environ.get(
                "SESSION_SEARCH_ACTIVE_GRACE_SECONDS",
                str(DEFAULT_ACTIVE_GRACE_SECONDS),
            )))
        except ValueError:
            active_grace = DEFAULT_ACTIVE_GRACE_SECONDS
        scan_time = time.time()

        for jsonl_path in jsonl_files:
            key = str(jsonl_path)
            stat = jsonl_path.stat()
            if skip_active and not _is_archived_session(jsonl_path) and (
                (
                    active_thread_id
                    and _session_id_from_path(jsonl_path) == active_thread_id
                )
                or scan_time - stat.st_mtime < active_grace
            ):
                unchanged_keys.add(key)
                self.deferred_active_keys.add(key)
                continue
            file_info = {"mtime": stat.st_mtime, "size": stat.st_size}
            if not full and key in state and state[key] == file_info:
                unchanged_keys.add(key)
            else:
                files_to_process.append(jsonl_path)
                state[key] = file_info

        return files_to_process, current_keys, unchanged_keys

    def parse_session(self, jsonl_path) -> ParsedSession:
        native_session_id = _session_id_from_path(jsonl_path)
        session_metadata = []
        trigger_line = None
        task_starts = []
        boundary_line = 0
        source_path = str(jsonl_path.expanduser().resolve())

        # Ownership metadata can appear after replayed parent history. Scan it
        # first without retaining response payloads (including embedded images).
        metadata_keys = ("id", "session_id", "cwd", "source", "thread_source",
                         "parent_thread_id", "forked_from_id", "agent_path", "agent_nickname")
        for line_number, obj in _iter_json_records(jsonl_path):
            if obj.get("type") == "session_meta":
                payload = obj.get("payload", {})
                session_metadata.append((line_number, {key: payload[key] for key in metadata_keys if key in payload}))
            elif (obj.get("type") == "inter_agent_communication_metadata"
                    and obj.get("payload", {}).get("trigger_turn") and trigger_line is None):
                trigger_line = line_number
            elif (obj.get("type") == "event_msg"
                    and obj.get("payload", {}).get("type") == "task_started"):
                task_starts.append((line_number, obj.get("payload", {}).get("turn_id")))
        obj = None  # Release the final decoded response before the second pass.

        matching_metadata = next((
            payload for _line_number, payload in session_metadata
            if payload.get("id") == native_session_id
        ), None)
        if matching_metadata is None and any(
            payload.get("id") for _line_number, payload in session_metadata
        ):
            # A modern rollout whose path identity has no matching metadata is
            # not safe to reinterpret as the foreign parent session. Keep a
            # stable authoritative-empty child so inherited content is removed.
            foreign = session_metadata[0][1]
            foreign_id = foreign.get("id") or foreign.get("session_id")
            return ParsedSession(
                slug=native_session_id,
                turns=[],
                project_dir=foreign.get("cwd") or str(jsonl_path.parent),
                source="codex",
                is_subagent=True,
                parent_session_id=foreign_id,
                native_session_id=native_session_id,
                authoritative_empty=True,
            )
        matching_metadata = matching_metadata or (
            session_metadata[0][1] if session_metadata else {}
        )
        thread_id = (
            matching_metadata.get("id")
            or matching_metadata.get("session_id")
            or native_session_id
        )
        cwd = matching_metadata.get("cwd")
        source = matching_metadata.get("source")
        is_subagent = (
            matching_metadata.get("thread_source") == "subagent"
            or (isinstance(source, dict) and "subagent" in source)
        )
        parent_session_id = None
        authoritative_empty = False
        if is_subagent:
            metadata_session_id = matching_metadata.get("session_id")
            parent_session_id = (
                matching_metadata.get("parent_thread_id")
                or matching_metadata.get("forked_from_id")
                or _nested_parent_id(source)
                or (
                    metadata_session_id
                    if metadata_session_id != native_session_id else None
                )
            )
            foreign_metadata = [
                (line_number, payload)
                for line_number, payload in session_metadata
                if payload.get("id")
                and payload.get("id") != native_session_id
            ]
            # Replayed rollouts contain complete parent history after the
            # child-owned metadata. A UUIDv7 task-start at or after the child
            # session creation is the stable child boundary for both ordinary
            # user tasks and inter-agent NEW_TASK messages.
            if foreign_metadata:
                child_time = _uuid7_time(native_session_id)
                boundary_line = next((
                    line_number
                    for line_number, turn_id in task_starts
                    if child_time is not None
                    and (_uuid7_time(turn_id) or -1) >= child_time
                ), None)
                if boundary_line is None and trigger_line is not None:
                    last_foreign_line = max(
                        line_number for line_number, _payload in foreign_metadata
                    )
                    if trigger_line > last_foreign_line:
                        boundary_line = trigger_line
                authoritative_empty = boundary_line is None

        turns = []
        current_turn = None

        for line_number, obj in _iter_json_records(jsonl_path, after_line=boundary_line):
            if obj.get("type") != "response_item":
                continue
            timestamp = obj.get("timestamp", "")
            payload = obj.get("payload", {})
            ptype = payload.get("type")

            if (
                ptype == "agent_message"
                and is_subagent
                and current_turn is None
            ):
                user_text = _extract_agent_task_text(
                    payload.get("content", "")
                )
                if not user_text:
                    label = (
                        matching_metadata.get("agent_path")
                        or matching_metadata.get("agent_nickname")
                        or native_session_id
                    )
                    user_text = f"Subagent task: {label}"
                if current_turn:
                    turns.append(current_turn)
                current_turn = Turn(
                    timestamp=timestamp,
                    user_text=user_text,
                    assistant_text="",
                    blocks=[{
                        "type": "user_text", "text": user_text, "timestamp": timestamp,
                        "user_origin": "user",
                    }],
                    provenance=SourceProvenance(
                        kind="jsonl-lines",
                        source=source_path,
                        start=line_number,
                        end=line_number,
                    ),
                )

            elif ptype == "message":
                role = payload.get("role")
                if role == "user":
                    user_text = _extract_text(payload.get("content", ""))
                    if not user_text or is_known_control_envelope(user_text):
                        continue
                    user_text, user_origin, context_blocks = (
                        classify_user_content(user_text)
                    )
                    if not user_text:
                        continue

                    if current_turn:
                        turns.append(current_turn)

                    user_block = {
                        "type": "user_text",
                        "timestamp": timestamp,
                        "text": user_text,
                        "user_origin": user_origin,
                    }
                    current_turn = Turn(
                        timestamp=timestamp,
                        user_text=user_text,
                        assistant_text="",
                        blocks=[user_block, *context_blocks],
                        provenance=SourceProvenance(
                            kind="jsonl-lines",
                            source=source_path,
                            start=line_number,
                            end=line_number,
                        ),
                        user_origin=user_origin,
                        native_turn_id=(
                            str(payload.get("id"))
                            if payload.get("id") else None
                        ),
                    )

                elif role == "assistant" and current_turn is not None:
                    text = _extract_text(payload.get("content", ""))
                    if text:
                        current_turn.assistant_text += text + "\n"
                        current_turn.blocks.append({"type": "assistant_text", "text": text, "timestamp": timestamp})
                        current_turn.provenance.end = line_number

            elif ptype == "function_call" and current_turn is not None:
                raw_name = payload.get("name", "unknown")
                inp = _parse_arguments(payload.get("arguments", {}))
                _append_tool_call(current_turn, raw_name, inp, line_number)

            elif ptype == "custom_tool_call" and current_turn is not None:
                raw_name = payload.get("name", "unknown")
                inp = _parse_arguments(payload.get("input", ""))
                _append_tool_call(current_turn, raw_name, inp, line_number)
                if raw_name == "exec":
                    nested = _structured_nested_tool_calls(payload, inp)
                    if not nested:
                        nested = _static_nested_tool_calls(
                            payload.get("input", "")
                        )
                    for nested_name, nested_input in nested:
                        _append_tool_call(
                            current_turn, nested_name, nested_input,
                            line_number,
                        )

            elif ptype == "web_search_call" and current_turn is not None:
                inp = {"action": payload.get("action", {})}
                summary = _tool_input_summary("WebSearch", inp)
                current_turn.tool_calls.append(ToolCall(name="WebSearch", input_summary=summary))
                current_turn.blocks.append({
                    "type": "tool_use",
                    "name": "WebSearch",
                    "input": inp,
                    "summary": summary,
                })
                current_turn.provenance.end = line_number

            elif ptype in ("function_call_output", "custom_tool_call_output") and current_turn is not None:
                text = _extract_output_text(payload)
                current_turn.blocks.append({
                    "type": "tool_result",
                    "tool_use_id": payload.get("call_id"),
                    "text": text,
                })
                current_turn.provenance.end = line_number

        if current_turn:
            turns.append(current_turn)

        for turn in turns:
            turn.assistant_text = turn.assistant_text.strip()
        for turn in turns:
            turn.files_referenced = _dedupe(turn.files_referenced)
            turn.commands_run = _dedupe(turn.commands_run)

        thread_name = _load_thread_names(self.session_index).get(thread_id)
        session_label = (
            thread_name
            or matching_metadata.get("agent_nickname")
            or matching_metadata.get("agent_path")
            or thread_id
        )

        return ParsedSession(
            slug=session_label,
            turns=turns,
            project_dir=cwd or str(jsonl_path.parent),
            source="codex",
            is_subagent=is_subagent,
            parent_session_id=parent_session_id,
            native_session_id=native_session_id,
            authoritative_empty=authoritative_empty,
        )

    def session_key(self, jsonl_path) -> str:
        return str(jsonl_path)

    def session_id(self, jsonl_path) -> str:
        return _session_id_from_path(jsonl_path)

    def session_ids_for_keys(self, session_keys):
        return {
            _session_id_from_path(Path(session_key))
            for session_key in session_keys
            if session_key
        }

    def project_slug(self, jsonl_path) -> str:
        cwd = _read_session_cwd(jsonl_path)
        return _project_slug(cwd or str(jsonl_path.parent))

    def empty_session_is_valid(self, session_info, parsed):
        # Codex leaves durable stubs for aborted or metadata-only rollouts. The
        # file fingerprint will change if they later receive indexable turns.
        return True

    def resolve_session(self, session_id_prefix):
        for match in _session_files():
            if (
                _session_id_from_path(match).startswith(session_id_prefix)
                or match.stem.startswith(session_id_prefix)
            ):
                return self.parse_session(match)
        return None


def _extract_patch_paths(inp):
    patch = ""
    if isinstance(inp, dict):
        patch = str(inp.get("input") or inp.get("patch") or "")
    paths = []
    prefixes = ("*** Add File: ", "*** Update File: ", "*** Delete File: ")
    for line in patch.splitlines():
        for prefix in prefixes:
            if line.startswith(prefix):
                paths.append(line[len(prefix):])
                break
    return paths


def _extract_command_paths(command):
    paths = []
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    for token in tokens:
        token = token.strip("\"'`:,;()[]{}")
        if not token or token.startswith("-"):
            continue
        if _looks_like_path(token):
            paths.append(token)

    return _dedupe(paths)


def _looks_like_path(token):
    if any(ch in token for ch in "|*?[]{}()"):
        return False
    if token.startswith(("/", "./", "../", "~/")):
        return True
    if "/" in token:
        return True
    return bool(re.search(r"\.[A-Za-z0-9][A-Za-z0-9_-]{0,8}(:\d+)?$", token))


def _dedupe(values):
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _iter_json_records(path, *, after_line=0):
    """Yield one decoded record at a time, preserving original line citations."""
    if after_line is None:
        return
    with open(path) as stream:
        for line_number, line in enumerate(stream, start=1):
            if line_number <= after_line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield line_number, value


def _read_session_cwd(jsonl_path):
    try:
        with open(jsonl_path) as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "session_meta":
                    payload = obj.get("payload", {})
                    return payload.get("cwd")
    except OSError:
        return None
    return None
