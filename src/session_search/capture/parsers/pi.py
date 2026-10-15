"""Pi v3 evidence from the latest recorded branch, without model thinking blocks."""

import json
from pathlib import Path

from session_search.capture.parsers.base import redact_sensitive_text
from session_search.core.records import Event, SessionRevision


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block["text"] for block in content
                         if isinstance(block, dict) and block.get("type") == "text"
                         and isinstance(block.get("text"), str))
    return ""


def parse_pi(path: Path) -> SessionRevision:
    header = None
    entries = {}
    with path.open() as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError) as exc:
                raise ValueError("invalid Pi record") from exc
            if not isinstance(record, dict):
                raise ValueError("invalid Pi record")
            if record.get("type") == "session":
                if header is not None or record.get("version") != 3 or not record.get("id"):
                    raise ValueError("unsupported Pi session header")
                header = record
            else:
                key = record.get("id")
                if not isinstance(key, str) or not key or key in entries:
                    raise ValueError("invalid Pi entry identity")
                entries[key] = record
    if header is None:
        raise ValueError("missing Pi session header")
    # Pi records form a parent-linked tree. The final entry is the last persisted
    # leaf; never concatenate sibling branches into a conversation.
    branch = []
    key = next(reversed(entries), None)
    seen = set()
    while key is not None:
        if key in seen or key not in entries:
            raise ValueError("invalid Pi branch ancestry")
        seen.add(key)
        entry = entries[key]
        branch.append(entry)
        key = entry.get("parentId")
    branch.reverse()
    edits = {entry.get("targetId"): entry.get("replacement")
             for entry in branch if entry.get("type") == "context_edit"}
    events = []

    def emit(entry, suffix, role, text, kind="message"):
        text = redact_sensitive_text(text)
        if text.strip():
            events.append(Event(f'{entry["id"]}:{suffix}', role, text[:12000] if role == "tool" else text,
                                entry.get("timestamp", ""),
                                "user" if role == "user" else role, kind))

    for entry in branch:
        kind = entry.get("type")
        if kind in {"compaction", "branch_summary"}:
            emit(entry, "summary", "context", entry.get("summary", ""), "summary")
            continue
        if kind != "message":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            raise ValueError("invalid Pi message")
        content = message.get("content")
        if entry["id"] in edits:
            replacement = edits[entry["id"]]
            if replacement is None:
                continue
            if not isinstance(replacement, dict) or "content" not in replacement:
                raise ValueError("invalid Pi context edit")
            content = replacement["content"]
        native_role = message.get("role")
        role = {"user": "user", "assistant": "assistant", "toolResult": "tool", "system": "context"}.get(native_role)
        if role is None:
            continue
        emit(entry, "text", role, _text(content), "tool_result" if role == "tool" else "message")
        if native_role == "assistant" and isinstance(content, list):
            for i, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    arguments = block.get("arguments", block.get("input", {}))
                    emit(entry, f"tool:{i}", "tool", str(block.get("name", "tool")) + " " +
                         json.dumps(arguments, ensure_ascii=False), "tool_use")
    return SessionRevision(
        session_id="pi:" + str(header["id"]), events=tuple(events), source="pi",
        project=redact_sensitive_text(str(header.get("cwd", ""))), parser_version="pi-v3-1",
    )
