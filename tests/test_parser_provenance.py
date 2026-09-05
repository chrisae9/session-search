import unittest

from session_search.capture.parsers.base import (
    SourceProvenance, Turn, classify_user_content,
    is_known_control_envelope,
)


class ParserProvenanceContractTests(unittest.TestCase):
    def test_existing_turn_constructor_remains_backward_compatible(self):
        turn = Turn("2026-01-01T00:00:00Z", "question", "answer")

        self.assertIsNone(turn.provenance)

    def test_turn_accepts_a_durable_non_secret_locator(self):
        provenance = SourceProvenance(
            kind="jsonl-lines",
            source="/tmp/session.jsonl",
            start=2,
            end=5,
        )
        turn = Turn("timestamp", "question", "answer", provenance=provenance)

        self.assertIs(turn.provenance, provenance)
        self.assertEqual(turn.provenance.start, 2)
        self.assertEqual(turn.provenance.end, 5)

    def test_turn_provenance_fields_have_backward_compatible_defaults(self):
        turn = Turn("timestamp", "question", "answer")

        self.assertEqual(turn.user_origin, "user")
        self.assertIsNone(turn.native_turn_id)
        self.assertIsNone(turn.turn_index)

    def test_only_known_structured_json_control_envelopes_are_rejected(self):
        self.assertTrue(is_known_control_envelope(
            '{"type":"queue-operation","operation":"dequeue"}'
        ))
        self.assertFalse(is_known_control_envelope(
            '{"type":"deployment","question":"ship it?"}'
        ))
        self.assertFalse(is_known_control_envelope(
            '{"type":"queue-operation","question":"what does this mean?"}'
        ))

    def test_realtime_wrapper_separates_current_input_from_quoted_history(self):
        text, origin, contexts = classify_user_content(
            "<realtime_delegation>"
            "<input>no</input>"
            "<transcript_delta>assistant: deploy?\nuser: maybe</transcript_delta>"
            "</realtime_delegation>"
        )

        self.assertEqual(text, "no")
        self.assertEqual(origin, "user")
        self.assertEqual(contexts[0]["user_origin"], "summary")
        self.assertIn("assistant: deploy?", contexts[0]["text"])

    def test_machine_reports_and_continuation_summaries_are_classified(self):
        _text, notification_origin, _contexts = classify_user_content(
            '<subagent_notification>{"status":"completed"}'
            '</subagent_notification>'
        )
        _text, summary_origin, _contexts = classify_user_content(
            "This session is being continued from a previous conversation. "
            "The prior work is summarized below."
        )

        self.assertEqual(notification_origin, "notification")
        self.assertEqual(summary_origin, "summary")
