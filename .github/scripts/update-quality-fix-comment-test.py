#!/usr/bin/env python3
"""Unit tests for update-quality-fix-comment.py."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).with_name("update-quality-fix-comment.py")
SPEC = importlib.util.spec_from_file_location("update_quality_fix_comment", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
helper = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = helper
SPEC.loader.exec_module(helper)


class UpdateQualityFixCommentTest(unittest.TestCase):
    def test_builds_new_pr_level_comment(self) -> None:
        body = helper.build_body(
            pr_number=42,
            head_sha="abc123456789",
            summary="AUTONOMOUS_TASK_EVIDENCE missing",
            report="# Autonomous PR quality gate\n\nStatus: failed",
        )

        self.assertIn(helper.COMMENT_MARKER, body)
        self.assertIn("PR #42", body)
        self.assertNotIn("follow-up", body.lower())
        self.assertIn("`abc123456789`", body)
        self.assertEqual(body.count(helper.COMMENT_MARKER), 1)

    def test_merges_history_for_existing_comment(self) -> None:
        existing = "\n".join(
            [
                helper.COMMENT_MARKER,
                "",
                "История последних failed SHA/reasons:",
                "- `abcdef1`: old reason",
            ]
        )

        body = helper.build_body(
            pr_number=42,
            head_sha="abcdef2",
            summary="new reason",
            report="# Autonomous PR quality gate\n\nStatus: failed",
            existing_body=existing,
        )

        self.assertLess(body.index("`abcdef2`"), body.index("`abcdef1`"))
        self.assertEqual(body.count(helper.COMMENT_MARKER), 1)
        self.assertIn("new reason", body)
        self.assertIn("old reason", body)

    def test_sanitizes_deferred_task_marker_from_history_and_report(self) -> None:
        existing = "\n".join(
            [
                helper.COMMENT_MARKER,
                "",
                "История последних failed SHA/reasons:",
                "- `abcdef1`: PR body repeatedly mentions follow-up tasks.",
            ]
        )

        body = helper.build_body(
            pr_number=42,
            head_sha="abcdef2",
            summary="avoid using the word 'follow-up' in PR body",
            report=(
                "# Autonomous PR quality gate\n\n"
                "Blocking reasons:\n"
                "- PR body repeatedly mentions follow-up tasks.\n"
            ),
            existing_body=existing,
        )

        self.assertNotIn("follow-up", body.lower())
        self.assertIn("[deferred-task marker]", body)
        self.assertIn("Blocking reasons:", body)

    def test_main_outputs_existing_comment_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            comments = root / "comments.json"
            report = root / "report.md"
            body = root / "body.md"
            outputs = root / "github-output.txt"
            comments.write_text(
                json.dumps(
                    [
                        {
                            "id": 123,
                            "body": helper.COMMENT_MARKER
                            + "\n\nИстория последних failed SHA/reasons:\n- `abcdef1`: old reason\n",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            report.write_text("# Autonomous PR quality gate\n\nStatus: failed\n", encoding="utf-8")

            exit_code = helper.main(
                [
                    "--comments",
                    str(comments),
                    "--report",
                    str(report),
                    "--pr-number",
                    "42",
                    "--head-sha",
                    "abcdef2",
                    "--summary",
                    "new reason",
                    "--output-body",
                    str(body),
                    "--github-output",
                    str(outputs),
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertIn("comment_id=123", outputs.read_text(encoding="utf-8"))
            self.assertIn("`abcdef2`", body.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
