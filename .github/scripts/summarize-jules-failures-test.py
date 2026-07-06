#!/usr/bin/env python3
"""Unit tests for summarize-jules-failures.py."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest


SCRIPT = pathlib.Path(__file__).with_name("summarize-jules-failures.py")
SPEC = importlib.util.spec_from_file_location("summarize_jules_failures", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules["summarize_jules_failures"] = module
SPEC.loader.exec_module(module)


def manifest(status: str = "todo") -> dict:
    return {
        "tasks": [
            {
                "id": "task-one",
                "status": status,
                "area": "proxy",
                "risk": "medium",
                "title": "Task one",
                "description": "Task one",
                "allowed_paths": ["agent_tasks.json"],
                "acceptance": ["ok"],
            }
        ]
    }


class JulesFailureRecoveryTest(unittest.TestCase):
    def test_extracts_selected_task_id_from_activities(self) -> None:
        data = {
            "activities": [
                {
                    "originator": "AGENT",
                    "message": "Selected task id: task-one\nTASK_ID was selected.",
                }
            ]
        }

        self.assertEqual(module.extract_task_id_from_activities(data), "task-one")

    def test_classifies_routine_question_failed_activity(self) -> None:
        data = {
            "activities": [
                {
                    "originator": "AGENT",
                    "createTime": "2026-06-28T17:39:00Z",
                    "message": "Пожалуйста, подскажите, нужно ли мне запустить локальный сервер и воспроизвести утечку?",
                }
            ]
        }

        self.assertEqual(module.classify_failed_activities(data), "routine_question")

    def test_one_failed_session_retries(self) -> None:
        decision = module.decide_recovery(
            manifest(),
            [module.FailedSession(session_id="s1", task_id="task-one")],
        )

        self.assertEqual(decision.action, "retry")
        self.assertEqual(decision.count_for_task, 1)

    def test_routine_question_failed_session_retries_with_auto_answer_reason(self) -> None:
        decision = module.decide_recovery(
            manifest(),
            [
                module.FailedSession(
                    session_id="s1",
                    task_id="task-one",
                    failure_kind="routine_question",
                )
            ],
        )

        self.assertEqual(decision.action, "retry")
        self.assertIn("auto-answer", decision.reason)

    def test_repeated_stale_feedback_blocks_task(self) -> None:
        decision = module.decide_recovery(
            manifest(),
            [
                module.FailedSession(
                    session_id="s1",
                    task_id="task-one",
                    failure_kind="repeated_stale_feedback",
                )
            ],
        )

        self.assertEqual(decision.action, "block")
        self.assertIn("stale-feedback", decision.reason)

    def test_repeated_stale_feedback_blocks_already_blocked_task(self) -> None:
        decision = module.decide_recovery(
            manifest("blocked"),
            [
                module.FailedSession(
                    session_id="s1",
                    task_id="task-one",
                    failure_kind="repeated_stale_feedback",
                )
            ],
        )

        self.assertEqual(decision.action, "block")
        self.assertEqual(decision.task_id, "task-one")
        self.assertEqual(decision.session_id, "s1")

    def test_two_failed_sessions_block(self) -> None:
        decision = module.decide_recovery(
            manifest(),
            [
                module.FailedSession(session_id="s2", task_id="task-one"),
                module.FailedSession(session_id="s1", task_id="task-one"),
            ],
        )

        self.assertEqual(decision.action, "block")
        self.assertEqual(decision.count_for_task, 2)

    def test_done_task_is_not_retried(self) -> None:
        decision = module.decide_recovery(
            manifest("done"),
            [module.FailedSession(session_id="s1", task_id="task-one")],
        )

        self.assertEqual(decision.action, "none")
        self.assertIn("not todo", decision.reason)

    def test_missing_task_id_is_not_retried(self) -> None:
        decision = module.decide_recovery(
            manifest(),
            [module.FailedSession(session_id="s1", task_id="")],
        )

        self.assertEqual(decision.action, "none")
        self.assertIn("did not expose", decision.reason)

    def test_active_same_task_is_not_retried(self) -> None:
        decision = module.decide_recovery(
            manifest(),
            [module.FailedSession(session_id="s1", task_id="task-one")],
            {"task-one"},
        )

        self.assertEqual(decision.action, "none")
        self.assertIn("active Jules session", decision.reason)


if __name__ == "__main__":
    unittest.main()
