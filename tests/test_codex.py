import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from session_search.capture.parsers import codex
from session_search.capture.parsers.codex import CodexParser


class CodexParserTests(unittest.TestCase):
    def test_uuid7_timestamp_decoding_is_python_version_independent(self):
        self.assertEqual(
            codex._uuid7_time("019f60d8-2692-7053-afff-d4d23edbbdc4"),
            1784036206226,
        )

    def test_recent_session_is_deferred_without_thread_environment(self):
        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "2026" / "07" / "30"
            sessions.mkdir(parents=True)
            recent = sessions / "rollout-recent.jsonl"
            old = sessions / "rollout-old.jsonl"
            recent.write_text("{}\n")
            old.write_text("{}\n")
            now = time.time()
            os.utime(recent, (now, now))
            os.utime(old, (now - 3600, now - 3600))

            with (
                patch.object(codex, "SESSIONS_DIR", Path(root)),
                patch.object(
                    codex, "ARCHIVED_SESSIONS_DIR", Path(root) / "archived"
                ),
                patch.dict(
                    os.environ,
                    {"SESSION_SEARCH_ACTIVE_GRACE_SECONDS": "900"},
                    clear=False,
                ),
            ):
                os.environ.pop("CODEX_THREAD_ID", None)
                parser = CodexParser()
                changed, current, unchanged = parser.find_sessions(
                    {}, full=False, skip_active=True
                )

            self.assertEqual(changed, [old])
            self.assertEqual(current, {str(recent), str(old)})
            self.assertEqual(unchanged, {str(recent)})
            self.assertEqual(parser.deferred_active_keys, {str(recent)})

    def test_archived_sessions_are_discovered_and_active_copy_wins(self):
        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions" / "2026" / "07" / "30"
            archived = Path(root) / "archived"
            sessions.mkdir(parents=True)
            archived.mkdir()
            active = sessions / "same-session.jsonl"
            archived_duplicate = archived / "same-session.jsonl"
            archived_only = archived / "archived-only.jsonl"
            for path in (active, archived_duplicate, archived_only):
                path.write_text("{}\n")
            old = time.time() - 3600
            os.utime(active, (old, old))

            with (
                patch.object(codex, "SESSIONS_DIR", Path(root) / "sessions"),
                patch.object(codex, "ARCHIVED_SESSIONS_DIR", archived),
            ):
                parser = CodexParser()
                changed, current, unchanged = parser.find_sessions(
                    {}, full=True, skip_active=True
                )
                resolved = parser.resolve_session("archived-only")
                detected = parser.detect()

            self.assertTrue(detected)
            self.assertEqual(set(changed), {active, archived_only})
            self.assertEqual(current, {str(active), str(archived_only)})
            self.assertEqual(unchanged, set())
            self.assertEqual(resolved.native_session_id, "archived-only")
            self.assertEqual(
                parser.session_ids_for_keys(current),
                {"same-session", "archived-only"},
            )

    def test_spawned_thread_is_classified_as_subagent(self):
        with tempfile.TemporaryDirectory() as root:
            session_file = Path(root) / "child-session.jsonl"
            rows = [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "child-session",
                        "session_id": "parent-session",
                        "parent_thread_id": "parent-session",
                        "thread_source": "subagent",
                        "source": {"subagent": {"thread_spawn": {"depth": 1}}},
                        "cwd": "/workspace/project",
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-29T12:00:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Review the implementation."}],
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-29T12:00:01Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Review complete."}],
                    },
                },
            ]
            session_file.write_text("".join(json.dumps(row) + "\n" for row in rows))

            parsed = CodexParser().parse_session(session_file)

            self.assertTrue(parsed.is_subagent)
            self.assertEqual(parsed.parent_session_id, "parent-session")
            self.assertEqual(parsed.native_session_id, "child-session")
            self.assertEqual(parsed.turns[0].assistant_text, "Review complete.")
            self.assertEqual(parsed.turns[0].provenance.kind, "jsonl-lines")
            self.assertEqual(parsed.turns[0].provenance.source, str(session_file.resolve()))
            self.assertEqual(parsed.turns[0].provenance.start, 2)
            self.assertEqual(parsed.turns[0].provenance.end, 3)

    def test_spawned_thread_ignores_replayed_parent_transcript(self):
        with tempfile.TemporaryDirectory() as root:
            session_file = Path(root) / "child-session.jsonl"
            rows = [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "child-session",
                        "session_id": "parent-session",
                        "parent_thread_id": "parent-session",
                        "forked_from_id": "parent-session",
                        "thread_source": "subagent",
                        "source": {"subagent": {"thread_spawn": {"depth": 1}}},
                        "agent_nickname": "Reviewer",
                        "cwd": "/workspace/project",
                    },
                },
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "parent-session",
                        "session_id": "parent-session",
                        "thread_source": "user",
                        "source": "vscode",
                        "cwd": "/workspace/project",
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-29T11:00:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Parent question."}],
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-29T11:00:01Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Parent answer."}],
                    },
                },
                {
                    "type": "inter_agent_communication_metadata",
                    "payload": {"trigger_turn": True},
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-29T12:00:00Z",
                    "payload": {
                        "type": "agent_message",
                        "author": "/root",
                        "recipient": "/root/reviewer",
                        "content": json.dumps([
                            {
                                "type": "input_text",
                                "text": (
                                    "Message Type: NEW_TASK\nTask name: review"
                                ),
                            },
                            {
                                "type": "encrypted_content",
                                "encrypted_content": "ciphertext-must-not-index",
                            },
                        ]),
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-29T12:00:01Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Child review."}],
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-29T12:00:02Z",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "call_id": "call-1",
                        "input": {"cmd": "pytest"},
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-29T12:00:03Z",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "call-1",
                        "output": "1 passed",
                    },
                },
            ]
            session_file.write_text("".join(json.dumps(row) + "\n" for row in rows))

            parsed = CodexParser().parse_session(session_file)

            self.assertTrue(parsed.is_subagent)
            self.assertEqual(parsed.parent_session_id, "parent-session")
            self.assertEqual(parsed.native_session_id, "child-session")
            self.assertEqual(parsed.slug, "Reviewer")
            self.assertEqual(len(parsed.turns), 1)
            self.assertNotIn("Parent question", parsed.turns[0].user_text)
            self.assertEqual(
                parsed.turns[0].user_text,
                "Message Type: NEW_TASK\nTask name: review",
            )
            self.assertNotIn("ciphertext", parsed.turns[0].user_text)
            self.assertEqual(parsed.turns[0].assistant_text, "Child review.")
            self.assertEqual(parsed.turns[0].tool_calls[0].name, "exec")
            self.assertEqual(parsed.turns[0].blocks[-1]["type"], "tool_result")
            self.assertEqual(parsed.turns[0].blocks[-1]["text"], "1 passed")

    def test_replayed_child_without_trigger_uses_uuid_task_boundary(self):
        child_id = "019f4cad-95cf-7020-beca-f51e4b7415fe"
        parent_id = "019f45da-338c-7ce2-9bd5-15137e643dbc"
        with tempfile.TemporaryDirectory() as root:
            session_file = Path(root) / f"rollout-{child_id}.jsonl"
            rows = [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": child_id,
                        "session_id": parent_id,
                        "thread_source": "subagent",
                        "source": {
                            "subagent": {
                                "thread_spawn": {
                                    "depth": 1,
                                    "parent_thread_id": parent_id,
                                }
                            }
                        },
                        "cwd": "/workspace/child",
                    },
                },
                {
                    "type": "session_meta",
                    "payload": {
                        "id": parent_id,
                        "session_id": parent_id,
                        "source": "vscode",
                        "cwd": "/workspace/parent",
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-10T11:00:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Parent question"}],
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-10T11:00:01Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Parent answer"}],
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_started",
                        "turn_id": "019f4cad-9661-7d50-bf30-151392274434",
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-10T12:00:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Child task"}],
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-10T12:00:01Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Child answer"}],
                    },
                },
            ]
            session_file.write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )

            parsed = CodexParser().parse_session(session_file)

            self.assertTrue(parsed.is_subagent)
            self.assertEqual(parsed.parent_session_id, parent_id)
            self.assertEqual(parsed.project_dir, "/workspace/child")
            self.assertFalse(parsed.authoritative_empty)
            self.assertEqual(len(parsed.turns), 1)
            self.assertEqual(parsed.turns[0].user_text, "Child task")
            self.assertEqual(parsed.turns[0].assistant_text, "Child answer")

    def test_replayed_child_without_defensible_boundary_fails_closed(self):
        child_id = "019f4cad-95cf-7020-beca-f51e4b7415fe"
        parent_id = "019f45da-338c-7ce2-9bd5-15137e643dbc"
        with tempfile.TemporaryDirectory() as root:
            session_file = Path(root) / f"rollout-{child_id}.jsonl"
            rows = [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": child_id,
                        "session_id": parent_id,
                        "thread_source": "subagent",
                        "source": {
                            "subagent": {
                                "thread_spawn": {
                                    "parent_thread_id": parent_id,
                                }
                            }
                        },
                    },
                },
                {
                    "type": "session_meta",
                    "payload": {"id": parent_id, "source": "vscode"},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Parent only"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Inherited"}],
                    },
                },
            ]
            session_file.write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )

            parsed = CodexParser().parse_session(session_file)

            self.assertTrue(parsed.is_subagent)
            self.assertTrue(parsed.authoritative_empty)
            self.assertEqual(parsed.turns, [])

    def test_foreign_only_metadata_does_not_become_primary(self):
        child_id = "019f4cad-95cf-7020-beca-f51e4b7415fe"
        parent_id = "019f45da-338c-7ce2-9bd5-15137e643dbc"
        with tempfile.TemporaryDirectory() as root:
            session_file = Path(root) / f"rollout-{child_id}.jsonl"
            rows = [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": parent_id,
                        "session_id": parent_id,
                        "source": "vscode",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Parent"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Answer"}],
                    },
                },
            ]
            session_file.write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )

            parsed = CodexParser().parse_session(session_file)

            self.assertTrue(parsed.is_subagent)
            self.assertEqual(parsed.parent_session_id, parent_id)
            self.assertTrue(parsed.authoritative_empty)
            self.assertEqual(parsed.turns, [])

    def test_empty_rollout_is_a_stable_stub(self):
        parser = CodexParser()
        self.assertTrue(parser.empty_session_is_valid(None, None))

    def test_short_user_reply_starts_a_new_turn_and_keeps_timestamp(self):
        with tempfile.TemporaryDirectory() as root:
            session_file = Path(root) / "fixture.jsonl"
            messages = [
                ("user", "Should I deploy the change?"),
                ("assistant", "Awaiting your decision."),
                ("user", "no"),
                ("assistant", "Deployment cancelled as requested."),
            ]
            rows = [
                {
                    "type": "response_item",
                    "timestamp": f"2026-09-01T00:00:0{index}Z",
                    "payload": {
                        "type": "message",
                        "id": f"msg-{index}",
                        "role": role,
                        "content": text,
                    },
                }
                for index, (role, text) in enumerate(messages)
            ]
            session_file.write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )

            turns = CodexParser().parse_session(session_file).turns

            self.assertEqual(len(turns), 2)
            self.assertEqual(turns[1].user_text, "no")
            self.assertEqual(turns[1].timestamp, "2026-09-01T00:00:02Z")
            self.assertEqual(turns[1].native_turn_id, "msg-2")
            self.assertNotIn("cancelled", turns[0].assistant_text)

    def test_genuine_json_with_type_field_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            session_file = Path(root) / "fixture.jsonl"
            rows = [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message", "role": "user",
                        "content": '{"type":"deployment","approve":false}',
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message", "role": "assistant",
                        "content": "I will not deploy it.",
                    },
                },
            ]
            session_file.write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )

            turns = CodexParser().parse_session(session_file).turns

            self.assertEqual(len(turns), 1)
            self.assertIn('"type":"deployment"', turns[0].user_text)

    def test_machine_report_origin_is_retained(self):
        with tempfile.TemporaryDirectory() as root:
            session_file = Path(root) / "fixture.jsonl"
            rows = [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message", "role": "user",
                        "content": (
                            "<subagent_notification>"
                            '{"status":{"completed":"review report"}}'
                            "</subagent_notification>"
                        ),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message", "role": "assistant",
                        "content": "I reviewed the report.",
                    },
                },
            ]
            session_file.write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )

            turn = CodexParser().parse_session(session_file).turns[0]

            self.assertEqual(turn.user_origin, "notification")
            self.assertIn("review report", turn.user_text)
            self.assertEqual(turn.blocks[0]["user_origin"], "notification")

    def test_realtime_wrapper_indexes_current_input_separately(self):
        with tempfile.TemporaryDirectory() as root:
            session_file = Path(root) / "fixture.jsonl"
            wrapper = (
                "<realtime_delegation>"
                "<input>What's the status?</input>"
                "<transcript_delta>user: old quoted request\n"
                "assistant: old answer</transcript_delta>"
                "</realtime_delegation>"
            )
            rows = [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message", "role": "user",
                        "content": wrapper,
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message", "role": "assistant",
                        "content": "Current status follows.",
                    },
                },
            ]
            session_file.write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )

            turn = CodexParser().parse_session(session_file).turns[0]

            self.assertEqual(turn.user_text, "What's the status?")
            self.assertEqual(turn.user_origin, "user")
            context = next(
                block for block in turn.blocks
                if block["type"] == "user_context"
            )
            self.assertEqual(context["user_origin"], "summary")
            self.assertIn("old quoted request", context["text"])

    def test_exec_wrapper_extracts_static_nested_command_and_paths(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            session_file = root_path / "fixture.jsonl"
            marker = root_path / "must-not-exist"
            code = (
                "const quoted = 'tools.exec_command({cmd:\"touch "
                + str(marker)
                + "\"})';\n"
                "// tools.exec_command({cmd:\"false-decoy\"});\n"
                "const result = await tools.exec_command({"
                "cmd:\"cat /workspace/special-release.yaml\","
                "workdir:\"/workspace\"});"
            )
            rows = [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message", "role": "user",
                        "content": "Inspect the requested fixture.",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call", "name": "exec",
                        "input": code,
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message", "role": "assistant",
                        "content": "Inspection complete.",
                    },
                },
            ]
            session_file.write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )

            turn = CodexParser().parse_session(session_file).turns[0]

            self.assertFalse(marker.exists())
            self.assertIn("cat /workspace/special-release.yaml", turn.commands_run)
            self.assertIn("/workspace/special-release.yaml", turn.files_referenced)
            self.assertIn("/workspace", turn.files_referenced)
            self.assertIn("Bash", [call.name for call in turn.tool_calls])
            self.assertNotIn("false-decoy", turn.commands_run)

    def test_structured_nested_calls_are_preferred_when_available(self):
        calls = codex._structured_nested_tool_calls({}, {
            "nested_calls": [{
                "name": "exec_command",
                "arguments": {
                    "cmd": "rg stable-id /workspace/session.jsonl",
                    "workdir": "/workspace",
                },
            }],
        })

        self.assertEqual(calls, [(
            "exec_command",
            {
                "cmd": "rg stable-id /workspace/session.jsonl",
                "workdir": "/workspace",
            },
        )])


if __name__ == "__main__":
    unittest.main()
