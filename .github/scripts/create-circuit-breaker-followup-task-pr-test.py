#!/usr/bin/env python3
"""Unit tests for create-circuit-breaker-followup-task-pr.py."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).with_name("create-circuit-breaker-followup-task-pr.py")
SPEC = importlib.util.spec_from_file_location("create_circuit_breaker_followup_task_pr", SCRIPT_PATH)
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


class CircuitBreakerFollowupTaskTest(unittest.TestCase):
    def test_recovery_pr_uses_control_plane_marker_and_branch(self) -> None:
        self.assertEqual(module.RECOVERY_MARKER, "AUTONOMOUS_CIRCUIT_BREAKER_FOLLOWUP_TASK")
        self.assertEqual(module.RECOVERY_BRANCH_PREFIX, "automation-circuit-breaker-followup")
        self.assertFalse(module.RECOVERY_BRANCH_PREFIX.startswith(("jules-", "jules/")))
        self.assertNotIn("jules", module.RECOVERY_LABELS)
        self.assertIn("automation-recovery", module.RECOVERY_LABELS)
        self.assertIn("self-improvement", module.RECOVERY_LABELS)

    def test_followup_task_is_deterministic_actionable_and_sanitized(self) -> None:
        task = module.make_followup_task(
            pr_number=362,
            source_sha="abc123",
            source_task_id="proxy-runtime-fix",
            reason=(
                "quality failed against https://rdsh.example.invalid:3120/v1 "
                "with token=secret-value and sk-live-secret"
            ),
        )

        self.assertTrue(task["id"].startswith("automation-quality-loop-pr-362-"))
        self.assertEqual(task["status"], "todo")
        self.assertEqual(task["area"], "automation")
        self.assertEqual(task["risk"], "low")
        self.assertIn(".github/scripts/jules-recovery-router.py", task["allowed_paths"])
        self.assertIn(".github/scripts/review-autonomous-pr-quality.py", task["allowed_paths"])
        self.assertIn("agent_tasks.json", task["allowed_paths"])
        self.assertIn("quality-fix prompts did not converge", task["description"])
        self.assertIn("[redacted-url]", task["description"])
        self.assertIn("[redacted-secret]", task["description"])
        self.assertNotIn("https://rdsh.example.invalid", task["description"])
        self.assertNotIn("secret-value", task["description"])
        self.assertEqual(task["source_finding_id"], "quality_fix_circuit_breaker")
        self.assertEqual(task["source_task_id"], "proxy-runtime-fix")
        self.assertEqual(
            task["circuit_breaker_followup_hash"],
            module.followup_hash(
                pr_number=362,
                source_sha="abc123",
                source_task_id="proxy-runtime-fix",
            ),
        )

    def test_conflict_recovery_followup_task_uses_separate_failure_class(self) -> None:
        task = module.make_followup_task(
            pr_number=400,
            source_sha="abc123",
            source_task_id="proxy-runtime-fix",
            source_finding_id=module.CONFLICT_RECOVERY_FINDING_ID,
            reason="pull_request checks do not run while the PR is dirty",
        )

        self.assertTrue(task["id"].startswith("automation-conflict-loop-pr-400-"))
        self.assertEqual(task["source_finding_id"], module.CONFLICT_RECOVERY_FINDING_ID)
        self.assertIn("stopped conflict recovery", task["title"])
        self.assertIn("conflict-recovery prompts did not converge", task["description"])
        self.assertIn("merge conflict state", task["acceptance"][0])

    def test_existing_followup_task_is_deduped_by_id_or_hash(self) -> None:
        digest = module.followup_hash(
            pr_number=362,
            source_sha="abc123",
            source_task_id="proxy-runtime-fix",
        )
        task_id = module.followup_task_id(
            pr_number=362,
            source_sha="abc123",
            source_task_id="proxy-runtime-fix",
        )

        self.assertTrue(
            module.existing_followup_task(
                {"tasks": [{"id": task_id}]},
                task_id=task_id,
                digest=digest,
            )
        )
        self.assertTrue(
            module.existing_followup_task(
                {"tasks": [{"id": "other", "circuit_breaker_followup_hash": digest}]},
                task_id=task_id,
                digest=digest,
            )
        )
        self.assertFalse(module.existing_followup_task({"tasks": []}, task_id=task_id, digest=digest))

    def test_pending_followup_task_ids_find_unresolved_quality_loop_tasks(self) -> None:
        manifest = {
            "tasks": [
                {
                    "id": "automation-quality-loop-pr-10-a",
                    "status": "done",
                    "source_finding_id": "quality_fix_circuit_breaker",
                },
                {
                    "id": "automation-quality-loop-pr-11-b",
                    "status": "todo",
                    "source_finding_id": "quality_fix_circuit_breaker",
                },
                {
                    "id": "automation-quality-loop-pr-12-c",
                    "status": "in_progress",
                    "source_finding_id": "quality_fix_circuit_breaker",
                },
                {
                    "id": "automation-health-repeated-followup",
                    "status": "todo",
                    "health_finding_code": "repeated_followup_generation",
                },
            ]
        }

        self.assertEqual(
            module.pending_followup_task_ids(
                manifest,
                source_finding_id=module.QUALITY_FIX_FINDING_ID,
            ),
            ["automation-quality-loop-pr-11-b", "automation-quality-loop-pr-12-c"],
        )


if __name__ == "__main__":
    unittest.main()
