#!/usr/bin/env python3
"""Unit tests for collect_failed_pr_context.py."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).with_name("collect_failed_pr_context.py")
SPEC = importlib.util.spec_from_file_location("collect_failed_pr_context", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


class FakeClient:
    api_url = "https://api.github.test"
    repo = "Omnividente/notion-abuz_ai"

    def request_json(self, path: str) -> Any:
        if path == "/repos/Omnividente/notion-abuz_ai/pulls?state=open&per_page=100":
            return [
                {
                    "number": 401,
                    "title": "Fix proxy-runtime-final-answer-mode-stability",
                    "body": "task_id: proxy-runtime-final-answer-mode-stability",
                    "head": {"sha": "abc123", "ref": "jules-task"},
                }
            ]
        if path == "/repos/Omnividente/notion-abuz_ai/commits/abc123/check-runs?per_page=100":
            return {
                "check_runs": [
                    {
                        "name": "validate",
                        "workflowName": "CI",
                        "status": "completed",
                        "conclusion": "failure",
                        "details_url": (
                            "https://github.com/Omnividente/notion-abuz_ai/actions/runs/123/job/987"
                            "?token=ghp_abcdef1234567890"
                        ),
                        "output": {
                            "annotations_url": (
                                "https://api.github.test/repos/Omnividente/notion-abuz_ai/check-runs/987/annotations"
                            )
                        },
                    }
                ]
            }
        if path == "/repos/Omnividente/notion-abuz_ai/pulls/401/files?per_page=100":
            return [{"filename": ".github/scripts/jules-unattended-monitor.sh"}]
        if path == "/repos/Omnividente/notion-abuz_ai/check-runs/987/annotations?per_page=5":
            return [
                {
                    "path": ".github/scripts/jules-unattended-monitor.sh",
                    "start_line": 10,
                    "message": "failed with token=ghp_abcdef1234567890",
                }
            ]
        raise AssertionError(f"unexpected JSON path {path}")

    def request_text(self, path: str) -> str:
        if path == "/repos/Omnividente/notion-abuz_ai/actions/jobs/987/logs":
            return "\n".join(
                [
                    "2026-07-07T06:08:03Z setup",
                    "2026-07-07T06:08:04Z ##[error]Process completed with exit code 1.",
                ]
            )
        raise AssertionError(f"unexpected text path {path}")


class CollectFailedPrContextTest(unittest.TestCase):
    def test_collects_sanitized_failed_check_context_for_matching_task(self) -> None:
        context = module.collect_context(
            FakeClient(),
            task_id="proxy-runtime-final-answer-mode-stability",
            session_id="",
        )

        self.assertEqual(context["repo"], "Omnividente/notion-abuz_ai")
        self.assertEqual(context["pr_number"], "#401")
        self.assertEqual(context["head_sha"], "abc123")
        self.assertIn(".github/scripts/jules-unattended-monitor.sh", context["changed_files"])
        failed = context["failed_checks"][0]
        self.assertEqual(failed["name"], "CI / validate")
        self.assertEqual(failed["conclusion"], "failure")
        self.assertIn("[REDACTED]", failed["details_url"])
        self.assertIn("[REDACTED]", failed["annotations"][0])
        self.assertIn("Process completed with exit code 1", failed["log_excerpt"])
        self.assertNotIn("ghp_abcdef1234567890", str(context))

    def test_returns_empty_context_without_matching_pr(self) -> None:
        context = module.collect_context(FakeClient(), task_id="other-task", session_id="")
        self.assertEqual(context, {})


if __name__ == "__main__":
    unittest.main()
