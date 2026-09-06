import json
from dataclasses import replace

import pytest

from session_search.capture.incremental import line_offset, parse_incremental
from session_search.capture.local import normalize_session
from session_search.capture.parsers.codex import CodexParser


def message(role, text, **extra):
    return {"type": "response_item", "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"type": "message", "role": role,
                        "content": [{"type": "input_text", "text": text}], **extra}}


def write(path, rows, mode="w"):
    with path.open(mode) as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def full(path):
    return normalize_session(CodexParser(session_index=path.parent / "no-index", strict=True).parse_session(path))


def test_append_reuses_completed_turns_and_preserves_exact_revision(tmp_path):
    path = tmp_path / "example.jsonl"
    write(path, [{"type": "session_meta", "payload": {"id": "example", "cwd": "/project"}},
                 message("user", "First café", id="native-first"), message("assistant", "answer"),
                 message("user", "Second"), message("assistant", "open")])
    original, checkpoint, work = parse_incremental(path)
    assert original == full(path) and work["mode"] == "full"
    for rows in [
        [message("assistant", "continued")],
        [{"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
           "arguments": '{"cmd":"echo synthetic"}'}},
         {"type": "response_item", "payload": {"type": "function_call_output",
           "call_id": "call", "output": "synthetic result"}}],
        [message("user", "<environment_context>injected</environment_context>\nThird"),
         message("assistant", "new answer")],
        [message("user", "Fourth"), message("assistant", "finished")],
    ]:
        write(path, rows, "a")
        actual, checkpoint, work = parse_incremental(path, checkpoint)
        assert actual == full(path)
        assert actual.revision == full(path).revision
        assert work["mode"] == "incremental" and work["reused_events"] >= 2
        assert work["metadata_scan_start"] > 0 and work["response_scan_start"] > 0


def test_prefix_rewrite_truncation_and_partial_tail_never_reuse(tmp_path):
    path = tmp_path / "example.jsonl"
    write(path, [message("user", "first"), message("assistant", "answer"), message("user", "last")])
    _, checkpoint, _ = parse_incremental(path)
    data = path.read_bytes()
    path.write_bytes(data.replace(b"first", b"other") + b'{"type":"ignored"}\n')
    actual, changed, work = parse_incremental(path, checkpoint)
    assert actual == full(path) and work["mode"] == "full"
    path.write_bytes(data[:data.index(b"\n") + 1])
    actual, _, work = parse_incremental(path, changed)
    assert actual == full(path) and work["mode"] == "full"
    with path.open("ab") as stream:
        stream.write(b'{"partial":')
    with pytest.raises(ValueError, match="complete-record"):
        parse_incremental(path, checkpoint)


def test_late_ownership_changes_force_full_reparse(tmp_path):
    path = tmp_path / "child.jsonl"
    write(path, [message("user", "ambiguous old history"), message("assistant", "old reply"),
                 message("user", "last old turn")])
    _, checkpoint, _ = parse_incremental(path)
    write(path, [{"type": "session_meta", "payload": {"id": "parent", "cwd": "/parent"}},
                 {"type": "session_meta", "payload": {"id": "child", "cwd": "/child",
                   "thread_source": "subagent", "parent_thread_id": "parent"}},
                 {"type": "inter_agent_communication_metadata", "payload": {"trigger_turn": True}},
                 message("user", "child owned task"), message("assistant", "child reply")], "a")
    actual, _, work = parse_incremental(path, checkpoint)
    assert actual == full(path)
    assert work["mode"] == "full"
    assert all("old" not in event.text for event in actual.events)


def test_blank_malformed_and_wrong_filename_preserve_validation(tmp_path):
    path = tmp_path / "example.jsonl"
    write(path, [message("user", "first"), message("user", "last")])
    _, checkpoint, _ = parse_incremental(path)
    actual, _, work = parse_incremental(path, replace(checkpoint, filename="different.jsonl"))
    assert actual == full(path) and work["mode"] == "full"
    with path.open("a") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="invalid Codex"):
        parse_incremental(path, checkpoint)


def test_byte_offsets_across_large_unicode_record(tmp_path):
    path = tmp_path / "large.jsonl"
    first = ("é" * (1024 * 1024) + "\n").encode()
    path.write_bytes(first + b"second\nthird\n")
    assert line_offset(path, 1) == 0
    assert line_offset(path, 2) == len(first)
    assert line_offset(path, 3) == len(first) + 7
