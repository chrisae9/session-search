"""Hermes capture contracts, using synthetic SQLite profiles only."""

from contextlib import closing
import json
import sqlite3

import pytest

from session_search.capture.hermes import HermesReadError, iter_hermes_revisions

_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, parent_session_id TEXT, source TEXT DEFAULT 'local',
    hidden INTEGER DEFAULT 0, end_reason TEXT, model_config TEXT, title TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
    timestamp REAL DEFAULT 1700000000, active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0,
    message_uid TEXT, display_identity BLOB, display_order INTEGER, display_kind TEXT,
    display_metadata TEXT, _compressed_summary INTEGER DEFAULT 0,
    reasoning TEXT, api_content TEXT, tool_calls TEXT, absorbed_message_uids TEXT
);
CREATE INDEX session_messages ON messages(session_id, id);
"""


def _db(path):
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    return conn


def _session(conn, id="one", **fields):
    conn.execute(
        f"INSERT INTO sessions ({', '.join(('id', *fields))}) "
        f"VALUES ({', '.join('?' for _ in range(1 + len(fields)))})",
        (id, *fields.values()),
    )


def _message(conn, content, *, session_id="one", role="user", **fields):
    fields = {"session_id": session_id, "role": role, "content": content, **fields}
    conn.execute(
        f"INSERT INTO messages ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
        tuple(fields.values()),
    )


def _read(path, **options):
    return list(iter_hermes_revisions(path, producer="fixture", profile="default", **options))


def _text(revision):
    return [event.text for event in revision.events]


def test_canonical_bot_chat_is_searchable_without_admitting_other_hidden_sessions(tmp_path):
    path = tmp_path / "state.db"
    with closing(_db(path)) as writer, writer:
        for name, source, title in (
            ("bot", "tui", "Bot Chat"),
            ("other", "local", "Bot Chat #2"),
            ("worker", "tool", "Bot Chat"),
            ("routed", "discord", "Bot Chat"),
        ):
            _session(writer, name, hidden=1, source=source, title=title)
            _message(writer, f"{name} message", session_id=name)
    revision, = _read(path)
    assert _text(revision) == ["bot message"]


def test_wal_snapshot_excludes_uncommitted_rows_and_reconciles_edit(tmp_path):
    path = tmp_path / "state.db"
    with closing(_db(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        _session(writer)
        _message(writer, "first text", message_uid="u1")
        writer.commit()
        assert path.with_name("state.db-wal").stat().st_size > 0
        writer.execute("UPDATE messages SET content='changed text' WHERE id=1")
        before, = _read(path)
        assert _text(before) == ["first text"]
        writer.commit()
        after, = _read(path)
        assert _text(after) == ["changed text"]
        assert before.session_id == after.session_id
        assert before.events[0].event_id == after.events[0].event_id
        assert before.revision != after.revision
        assert _read(path) == [after]


def test_text_only_projection_withholds_private_metadata_and_hidden_rows(tmp_path):
    path = tmp_path / "state.db"
    with closing(_db(path)) as conn, conn:
        _session(conn)
        _session(conn, "hidden", hidden=1)
        _session(conn, "worker", source="tool")
        _session(conn, "child", model_config=json.dumps({"_delegate_from": "one"}))
        _message(conn, "prompt password=private-value", reasoning="private chain",
                 api_content="private API context", tool_calls="private call")
        _message(conn, "response", role="assistant")
        _message(conn, "private tool result", role="tool")
        _message(conn, "private instructions", role="system")
        _message(conn, "private scaffold", display_kind="internal_notification")
        _message(conn, "private hidden", display_kind="hidden")
        _message(conn, "private model-only", display_metadata='{"model_only": true}')
        _message(conn, "invalid metadata is not a visibility signal", display_metadata="{")
        _message(conn, "private pre-override payload",
                 display_metadata='{"withheld_prompt":"private blocked prompt"}')
        _message(conn, "\x00json:" + json.dumps([{"type": "image", "data": "private image"}]))
        _message(conn, "actual ask\n<memory-context>private recalled data</memory-context>")
        _message(conn, "private hidden session", session_id="hidden")
        _message(conn, "private tool invocation", session_id="worker")
        _message(conn, "private delegated transcript", session_id="child")
    revision, = _read(path)
    assert _text(revision) == [
        "prompt password=[REDACTED]", "response",
        "[Message withheld: a pre_llm_call policy hook blocked it.]", "actual ask",
    ]
    assert revision.source == "hermes"
    assert [event.role for event in revision.events] == ["user", "assistant", "user", "user"]


def test_compaction_preserves_text_without_duplicate_or_rewound_copies(tmp_path):
    path = tmp_path / "state.db"
    with closing(_db(path)) as conn, conn:
        _session(conn)
        _message(conn, "older prompt", active=0, compacted=1, message_uid="old", display_order=1)
        _message(conn, "tail", active=0, compacted=1, message_uid="tail", display_order=2)
        _message(conn, "tail", message_uid="tail", display_order=2)
        _message(conn, "discarded", active=0, compacted=1, message_uid="undo", display_order=4)
        _message(conn, "discarded", active=0, compacted=0, message_uid="undo", display_order=4)
        _message(conn, "before rewrite", active=0, compacted=1, message_uid="edit", display_order=6)
        _message(conn, "after rewrite", message_uid="edit", display_order=6)
        _message(conn, "separate active occurrence", message_uid="edit", display_order=8)
        _message(conn, "another archived occurrence", active=0, compacted=1,
                 message_uid="edit", timestamp=1700000001, display_order=9)
        _message(conn, "generated summary", _compressed_summary=1, display_kind="hidden")
        _message(
            conn,
            "[PRIOR CONTEXT — for reference only; not a new message]\nreal wrapped ask\n"
            "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]\nprivate summary",
            _compressed_summary=1, display_kind="hidden",
        )
    revision, = _read(path)
    assert _text(revision) == [
        "older prompt", "tail", "after rewrite", "separate active occurrence",
        "another archived occurrence", "real wrapped ask",
    ]
    assert len({event.event_id for event in revision.events}) == 6


def test_physical_rewind_to_empty_and_source_disappearance_differ(tmp_path):
    path = tmp_path / "state.db"
    with closing(_db(path)) as conn:
        _session(conn)
        _message(conn, "to discard")
        conn.commit()
        previous, = _read(path)
        conn.execute("UPDATE messages SET active=0")
        conn.commit()
        empty, = _read(path)
        assert empty.session_id == previous.session_id
        assert empty.events == ()
        conn.execute("DELETE FROM sessions")
        conn.commit()
        assert _read(path, is_known=lambda _: True) == []


def test_rewind_of_merged_row_hides_absorbed_compaction_history(tmp_path):
    path = tmp_path / "state.db"
    with closing(_db(path)) as writer, writer:
        _session(writer)
        _message(writer, "withdrawn compacted text", active=0, compacted=1, message_uid="old")
        _message(writer, "merged text", active=0, message_uid="merge",
                 absorbed_message_uids=json.dumps(["old"]))
        _message(writer, "still visible", message_uid="keep")
    revision, = _read(path)
    assert _text(revision) == ["still visible"]


@pytest.mark.parametrize("change", ["hidden=1", "source='tool'", "end_reason='compression'"])
def test_known_present_ineligible_session_withdraws_only_current_head(tmp_path, change):
    path = tmp_path / "state.db"
    with closing(_db(path)) as conn:
        _session(conn)
        _message(conn, "previously eligible")
        conn.commit()
        previous, = _read(path)
        conn.execute(f"UPDATE sessions SET {change}")
        conn.commit()
        assert _read(path, on_deferred=lambda _: None) == []
        empty, = _read(
            path, is_known=lambda k: k == previous.session_id, on_deferred=lambda _: None
        )
        assert empty.session_id == previous.session_id
        assert empty.events == ()


def test_compression_lineage_deferred_but_explicit_branch_remains_independent(tmp_path):
    path = tmp_path / "state.db"
    with closing(_db(path)) as conn, conn:
        _session(conn, end_reason="compression")
        _session(conn, "rotation", parent_session_id="one")
        _session(conn, "branch", parent_session_id="one",
                 model_config=json.dumps({"_branched_from": "one"}))
        _session(conn, "reset", parent_session_id="one",
                 model_config=json.dumps({"_reset_from": "one"}))
        for session in ("one", "rotation", "branch", "reset"):
            _message(conn, session, session_id=session)
    reasons = []
    branch, reset = _read(path, on_deferred=reasons.append)
    assert _text(branch) == ["branch"] and _text(reset) == ["reset"]
    assert branch.session_id != reset.session_id
    assert branch.parent_session_id == reset.parent_session_id
    assert reasons == ["compression_lineage", "compression_lineage"]
    other, *_ = iter_hermes_revisions(
        path, producer="fixture", profile="different", on_deferred=lambda _: None
    )
    assert branch.session_id != other.session_id
    assert branch.parent_session_id != other.parent_session_id


@pytest.mark.parametrize("budget,reason", [
    ({"max_messages": 1}, "session_row_limit"),
    ({"max_session_bytes": 10}, "session_byte_limit"),
])
def test_over_budget_session_defers_without_partial_publication_or_starvation(tmp_path, budget, reason):
    path = tmp_path / "state.db"
    with closing(_db(path)) as conn, conn:
        _session(conn)
        _session(conn, "two")
        _message(conn, "unpublishable" * 1024)
        _message(conn, "second row")
        _message(conn, "small", session_id="two")
    deferred = []
    small, = _read(path, on_deferred=deferred.append, **budget)
    assert _text(small) == ["small"]
    assert deferred == [reason]


def test_replaced_database_interrupts_scan_without_withdrawal(tmp_path):
    path = tmp_path / "state.db"
    with closing(_db(path)) as conn, conn:
        _session(conn)
        _session(conn, "two")
        _message(conn, "first")
        _message(conn, "second", session_id="two")
    deferred = []
    scan = iter_hermes_revisions(
        path, producer="fixture", profile="default", is_known=lambda _: True,
        on_deferred=deferred.append,
    )
    assert _text(next(scan)) == ["first"]
    replacement = tmp_path / "restored.db"
    with closing(_db(replacement)) as conn, conn:
        _session(conn, "two", hidden=1)
    replacement.replace(path)
    with pytest.raises(HermesReadError, match="changed_during_read"):
        next(scan)
    assert not deferred


def test_missing_and_unrecognized_database_fail_closed(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(HermesReadError, match="source_unavailable"):
        _read(missing)
    assert not missing.exists()
    unknown = tmp_path / "other.db"
    with closing(sqlite3.connect(unknown)) as conn, conn:
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY)")
    with pytest.raises(HermesReadError, match="unsupported_schema"):
        _read(unknown)


def test_legacy_policy_refusal_and_its_copies_are_not_searchable(tmp_path):
    path = tmp_path / "state.db"
    with closing(_db(path)) as conn, conn:
        _session(conn)
        _message(conn, "earlier question", message_uid="earlier")
        _message(conn, "earlier answer", role="assistant")
        _message(conn, "private refused request", message_uid="refused")
        _message(conn, "private partial result", role="assistant")
        _message(conn, "private failure metadata", role="assistant", display_kind="failed_turn",
                 display_metadata='{"error_surface":{"code":"content_policy_blocked"}}')
        _message(conn, "private refused request", message_uid="refused")
        _message(conn, "public later question")
        _message(conn, "public later answer", role="assistant")
    revision, = _read(path)
    assert _text(revision) == ["earlier question", "earlier answer",
                               "public later question", "public later answer"]


def test_unmarked_child_on_visible_platform_is_not_an_interactive_transcript(tmp_path):
    path = tmp_path / "state.db"
    with closing(_db(path)) as conn, conn:
        _session(conn)
        _session(conn, "ephemeral", source="discord", parent_session_id="one")
        _message(conn, "visible text")
        _message(conn, "internal payload", session_id="ephemeral")
    revision, = _read(path)
    assert _text(revision) == ["visible text"]
