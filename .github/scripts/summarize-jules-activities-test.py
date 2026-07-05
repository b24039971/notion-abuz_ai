#!/usr/bin/env python3
"""Unit tests for summarize-jules-activities.py."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("summarize-jules-activities.py")


def activity(originator: str, create_time: str, text: str) -> dict:
    return {
        "originator": originator,
        "createTime": create_time,
        "message": {"text": text},
    }


class SummarizeJulesActivitiesTest(unittest.TestCase):
    def summarize(self, activities: list[dict]) -> tuple[int, int, int, str, int]:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
            json.dump({"activities": activities}, handle)
            path = Path(handle.name)

        try:
            output = subprocess.check_output(
                [sys.executable, str(SCRIPT), str(path)],
                text=True,
                encoding="utf-8",
            ).strip()
        finally:
            path.unlink(missing_ok=True)

        agent_epoch, user_epoch, token_epoch, wait_kind, continue_token_count = output.split("\t")
        return int(agent_epoch), int(user_epoch), int(token_epoch), wait_kind, int(continue_token_count)

    def test_user_continue_token_after_agent_wait_is_detected(self) -> None:
        result = self.summarize(
            [
                activity("AGENT", "2026-06-28T10:00:00Z", "Need input?"),
                activity("USER", "2026-06-28T10:01:00Z", "AUTONOMOUS_CONTINUE_TOKEN\nContinue."),
            ]
        )

        self.assertGreaterEqual(result[2], result[0])
        self.assertEqual(result[3], "continue")
        self.assertEqual(result[4], 1)

    def test_plain_user_reply_does_not_set_token_epoch(self) -> None:
        result = self.summarize(
            [
                activity("AGENT", "2026-06-28T10:00:00Z", "Need input?"),
                activity("USER", "2026-06-28T10:01:00Z", "Continue without waiting."),
            ]
        )

        self.assertGreater(result[1], result[0])
        self.assertEqual(result[2], 0)
        self.assertEqual(result[4], 0)

    def test_finalize_marker_is_classified(self) -> None:
        result = self.summarize(
            [
                activity("AGENT", "2026-06-28T10:00:00Z", "All plan steps completed. Ready for submission."),
            ]
        )

        self.assertEqual(result[3], "finalize")

    def test_counts_multiple_autonomous_continue_tokens(self) -> None:
        result = self.summarize(
            [
                activity("AGENT", "2026-06-28T10:00:00Z", "Need input?"),
                activity("USER", "2026-06-28T10:01:00Z", "AUTONOMOUS_CONTINUE_TOKEN\nContinue."),
                activity("AGENT", "2026-06-28T10:02:00Z", "Still need input?"),
                activity("USER", "2026-06-28T10:03:00Z", "AUTONOMOUS_CONTINUE_TOKEN\nContinue again."),
            ]
        )

        self.assertEqual(result[4], 2)


if __name__ == "__main__":
    unittest.main()
