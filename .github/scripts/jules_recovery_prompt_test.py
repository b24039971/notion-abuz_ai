#!/usr/bin/env python3
"""Unit tests for jules_recovery_prompt.py."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("jules_recovery_prompt.py")
SPEC = importlib.util.spec_from_file_location("jules_recovery_prompt", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def agent(text: str) -> dict:
    return {
        "originator": "AGENT",
        "createTime": "2026-07-06T10:00:00Z",
        "message": {"text": text},
    }


class JulesRecoveryPromptTest(unittest.TestCase):
    def assert_reason(self, text: str, reason: str, action: str) -> None:
        summary = module.summarize_activities([agent(text)])
        self.assertEqual(summary["wait_reason"], reason)
        self.assertEqual(summary["prompt_action"], action)

    def test_classifies_routine_question(self) -> None:
        self.assert_reason(
            "Should I run the local server and tests before opening the PR?",
            "routine_question",
            "choose_safe_next_step",
        )

    def test_classifies_transient_api_or_partial_context(self) -> None:
        self.assert_reason(
            "The API error interrupted the file search, so I only have a partial file list.",
            "transient_api_or_partial_context",
            "repeat_targeted_context_collection",
        )

    def test_classifies_finalize_request(self) -> None:
        self.assert_reason(
            "All plan steps completed. Anything else you'd like me to review?",
            "finalize",
            "finalize_pr",
        )

    def test_classifies_missing_secret_or_permission(self) -> None:
        self.assert_reason(
            "I need production credentials or an API key to continue.",
            "missing_secret_or_permission",
            "block_with_reason",
        )

    def test_classifies_high_risk_confirmation(self) -> None:
        self.assert_reason(
            "This is a high risk destructive action. Should I proceed?",
            "high_risk_confirmation",
            "block_or_limit_scope",
        )

    def test_high_risk_prompt_allows_only_guarded_lab_scope(self) -> None:
        payload = module.build_prompt_payload(
            summary={
                "wait_reason": "high_risk_confirmation",
                "prompt_action": "block_or_limit_scope",
                "latest_agent_excerpt": "This is high risk. Should I proceed?",
                "continue_token_count": 0,
            },
            task={
                "id": "legacy-high",
                "status": "todo",
                "risk": "high",
                "area": "automation",
                "title": "Enable legacy compatibility smoke",
                "allowed_paths": [".github/workflows/legacy_compat_smoke.yml", "agent_tasks.json"],
                "acceptance": ["Use self-hosted legacy smoke evidence and rollback plan."],
            },
            task_id="legacy-high",
            repo="Omnividente/notion-abuz_ai",
            session_id="123",
            session_state="AWAITING_USER_FEEDBACK",
        )

        prompt = payload["prompt"]
        self.assertIn("task_risk: high", prompt)
        self.assertIn("bounded high-risk legacy/offline/lab task", prompt)
        self.assertIn("CI/smoke/artifact/self-hosted evidence", prompt)
        self.assertIn("rollback plan", prompt)
        self.assertIn("unguarded high-risk", prompt)

    def test_classifies_unknown_continue(self) -> None:
        self.assert_reason(
            "I have paused and need your input before continuing.",
            "unknown_continue",
            "continue_safely",
        )

    def test_prompt_includes_task_context_and_redacts_secret_like_values(self) -> None:
        manifest = {
            "tasks": [
                {
                    "id": "task-one",
                    "status": "todo",
                    "area": "proxy",
                    "risk": "medium",
                    "title": "Fix bridge recovery",
                    "allowed_paths": ["internal/proxy/tools.go", "agent_tasks.json"],
                    "acceptance": ["Retry targeted context collection"],
                }
            ]
        }
        payload = module.build_from_activities(
            activities=[
                agent(
                    "selected task id: task-one\n"
                    "API error left partial context. ghp_abcdef1234567890"
                )
            ],
            manifest=manifest,
            repo="Omnividente/notion-abuz_ai",
            session_id="1234567890123456789",
            session_state="AWAITING_USER_FEEDBACK",
            max_continue_attempts=2,
        )

        prompt = payload["prompt"]
        self.assertEqual(payload["repo"], "Omnividente/notion-abuz_ai")
        self.assertEqual(payload["session_id"], "1234567890123456789")
        self.assertEqual(payload["session_state"], "AWAITING_USER_FEEDBACK")
        self.assertIn("repo: Omnividente/notion-abuz_ai", prompt)
        self.assertIn("session_id: 1234567890123456789", prompt)
        self.assertIn("session_state: AWAITING_USER_FEEDBACK", prompt)
        self.assertIn("task_id: task-one", prompt)
        self.assertIn("wait_reason: transient_api_or_partial_context", prompt)
        self.assertIn("prompt_action: repeat_targeted_context_collection", prompt)
        self.assertIn("allowed_paths: internal/proxy/tools.go; agent_tasks.json", prompt)
        self.assertIn("[REDACTED]", prompt)
        self.assertNotIn("ghp_abcdef1234567890", prompt)

    def test_prompt_includes_sanitized_pr_failed_check_context(self) -> None:
        payload = module.build_prompt_payload(
            summary={
                "wait_reason": "unknown_continue",
                "prompt_action": "continue_safely",
                "latest_agent_excerpt": "Waiting for input.",
                "continue_token_count": 1,
            },
            task={"id": "task-one", "status": "todo", "risk": "low", "area": "automation"},
            task_id="task-one",
            repo="Omnividente/notion-abuz_ai",
            session_id="sessions/1234567890123456789?token=ghp_abcdef1234567890",
            session_state="AWAITING_USER_FEEDBACK",
            pr_context={
                "repo": "Omnividente/notion-abuz_ai",
                "pr_number": "#401",
                "head_sha": "abc123",
                "changed_files": [
                    ".github/scripts/jules-recovery-router.py",
                    "pr_body.txt",
                ],
                "failed_checks": [
                    {
                        "name": "CI / validate",
                        "conclusion": "failure",
                        "run_id": "12345",
                        "details_url": "https://github.com/o/r/actions/runs/12345/job/9?token=ghp_abcdef1234567890",
                        "annotations": [
                            "pr_body.txt: PR changes protected scratch file with token=ghp_abcdef1234567890"
                        ],
                        "log_excerpt": (
                            "pr_body.txt\n"
                            "##[error]PR changes protected runtime, secret, binary, log, data, account, workflow, or temporary scratch files."
                        ),
                    }
                ],
            },
        )

        prompt = payload["prompt"]
        self.assertIn("repo: Omnividente/notion-abuz_ai", prompt)
        self.assertIn("session_id: sessions/1234567890123456789?token=[REDACTED]", prompt)
        self.assertIn("session_state: AWAITING_USER_FEEDBACK", prompt)
        self.assertIn("pr_context: available", prompt)
        self.assertIn("pr_number: #401", prompt)
        self.assertIn("changed_files:", prompt)
        self.assertIn("pr_body.txt", prompt)
        self.assertIn("CI / validate: failure", prompt)
        self.assertIn("annotation:", prompt)
        self.assertIn("log_excerpt:", prompt)
        self.assertIn("используй annotations/log_excerpt/changed_files", prompt)
        self.assertIn("[REDACTED]", prompt)
        self.assertNotIn("ghp_abcdef1234567890", prompt)

    def test_cli_accepts_pr_context_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            activities = tmp_path / "activities.json"
            pr_context = tmp_path / "pr-context.json"
            activities.write_text(
                json.dumps(
                    {
                        "activities": [
                            agent("selected task id: task-one\nI am waiting for input.")
                        ]
                    }
                ),
                encoding="utf-8",
            )
            pr_context.write_text(
                json.dumps(
                    {
                        "repo": "Omnividente/notion-abuz_ai",
                        "pr_number": "#401",
                        "failed_checks": [
                            {
                                "name": "CI / validate",
                                "conclusion": "failure",
                                "log_excerpt": "##[error]Process completed with exit code 1.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            output = subprocess.check_output(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--activities",
                    str(activities),
                    "--manifest",
                    "",
                    "--task-id",
                    "task-one",
                    "--repo",
                    "Omnividente/notion-abuz_ai",
                    "--session-id",
                    "1234567890123456789",
                    "--session-state",
                    "AWAITING_USER_FEEDBACK",
                    "--pr-context-file",
                    str(pr_context),
                ],
                text=True,
            )

        payload = json.loads(output)
        self.assertIn("pr_context: available", payload["prompt"])
        self.assertIn("CI / validate: failure", payload["prompt"])
        self.assertIn("log_excerpt:", payload["prompt"])

    def test_sanitizes_password_like_values(self) -> None:
        self.assertEqual(
            module.sanitize_text("password=super-secret"),
            "password=[REDACTED]",
        )
        self.assertEqual(
            module.sanitize_text("API key: sk-testsecret123"),
            "API key: [REDACTED]",
        )


if __name__ == "__main__":
    unittest.main()
