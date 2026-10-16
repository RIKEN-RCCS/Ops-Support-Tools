"""Offline regression tests for contradictory structured LLM results."""
import unittest
from unittest.mock import patch

import generator
import followup_responder
import llm_client


def result(followup=False, **changes):
    data = dict(safe_to_reply_to_user=True, answer_confidence="high",
                requires_environment_knowledge=False, requires_runbook=False,
                draft_reply="検証用の返信案です。" * 5)
    data.update(dict(answerable=True, needs_agent_review=False) if followup
                else dict(requires_operator_check=False))
    data.update(changes)
    return data


class AnswerGateTests(unittest.TestCase):
    def test_contradictory_triage_is_held_before_note_rendering(self):
        raw = result(answer_confidence="low", requires_environment_knowledge=True,
                     requires_runbook=True, requires_operator_check=True)
        with patch.object(llm_client, "chat_json", return_value=raw):
            actual = llm_client.triage("synthetic", "synthetic", model="test")
        self.assertIs(actual["safe_to_reply_to_user"], False)
        self.assertIs(actual["requires_runbook"], True)
        self.assertEqual(actual["answer_confidence"], "low")
        note = generator._build_note_body(actual, "test")
        self.assertIn("safe_to_reply_to_user: no", note)
        self.assertIn("公開返信への利用は保留", note)

    def test_each_blocker_wins_over_model_safe_flag(self):
        for followup in (False, True):
            blockers = dict(requires_environment_knowledge=True, requires_runbook=True,
                            answer_confidence="low", safe_to_reply_to_user=False)
            blockers.update(dict(needs_agent_review=True, answerable=False) if followup
                            else dict(requires_operator_check=True))
            for key, value in blockers.items():
                with self.subTest(followup=followup, blocker=key):
                    actual = llm_client.enforce_answer_gate(result(followup, **{key: value}), followup=followup)
                    self.assertIs(actual["safe_to_reply_to_user"], False)
                    if followup:
                        self.assertIs(actual["answerable"], False)
                        self.assertIs(actual["needs_agent_review"], True)

    def test_followup_api_boundary_and_note_are_consistent(self):
        raw = result(True, requires_runbook=True)
        with patch.object(llm_client, "chat_json", return_value=raw):
            actual = llm_client.followup_reply("synthetic", "synthetic", "synthetic", model="test")
        self.assertFalse(actual["safe_to_reply_to_user"])
        self.assertFalse(actual["answerable"])
        self.assertTrue(actual["needs_agent_review"])
        note = followup_responder._build_internal_note(actual, model="test", target_comment_id=1)
        self.assertIn("safe_to_reply_to_user: no", note)

    def test_valid_answers_are_unchanged_and_hold_is_idempotent(self):
        for followup in (False, True):
            for confidence in ("medium", "high"):
                data = result(followup, answer_confidence=confidence)
                self.assertEqual(llm_client.enforce_answer_gate(dict(data), followup=followup), data)
            held = result(followup, requires_runbook=True)
            llm_client.enforce_answer_gate(held, followup=followup)
            self.assertEqual(llm_client.enforce_answer_gate(dict(held), followup=followup), held)

    def test_missing_or_invalid_gate_fields_fail_closed(self):
        for key in ("requires_environment_knowledge", "requires_runbook",
                    "requires_operator_check", "answer_confidence", "safe_to_reply_to_user"):
            for invalid in (None, "true", 1):
                with self.subTest(key=key, invalid=invalid):
                    data = result(**{key: invalid})
                    self.assertFalse(llm_client.enforce_answer_gate(data)["safe_to_reply_to_user"])


if __name__ == "__main__":
    unittest.main()
