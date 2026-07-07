#!/usr/bin/env python3
"""Unit tests for create-automation-meta-task-pr.py."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).with_name("create-automation-meta-task-pr.py")
SPEC = importlib.util.spec_from_file_location("create_automation_meta_task_pr", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
meta = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = meta
SPEC.loader.exec_module(meta)


def manifest(tasks: list[dict] | None = None, *, max_todo_tasks: int = 40) -> dict:
    return {
        "replenishment_policy": {
            "max_todo_tasks": max_todo_tasks,
        },
        "tasks": tasks or [],
    }


def health_report(*findings: dict, create_meta_task: bool = True) -> dict:
    return {
        "generated_at": "2026-06-29T00:00:00Z",
        "status": "degraded" if findings else "healthy",
        "create_meta_task": create_meta_task,
        "findings": list(findings),
    }


def finding(code: str = "quality_failure", *, severity: str = "degraded") -> dict:
    return {
        "code": code,
        "severity": severity,
        "message": "Quality gate failed.",
        "window": "24h",
        "evidence": {"pr_numbers": [121]},
    }


class AutomationMetaTaskTest(unittest.TestCase):
    def test_healthy_report_plans_no_task(self) -> None:
        plan = meta.plan_meta_tasks(health_report(create_meta_task=False), manifest(), max_tasks=3)

        self.assertEqual(plan.tasks, [])
        self.assertIn("did not request", plan.reason)

    def test_degraded_finding_creates_deterministic_task(self) -> None:
        item = finding("quality_failure")
        plan = meta.plan_meta_tasks(health_report(item), manifest(), max_tasks=3)

        self.assertEqual(len(plan.tasks), 1)
        task = plan.tasks[0]
        self.assertTrue(task["id"].startswith("automation-health-quality-failure-"))
        self.assertEqual(task["status"], "todo")
        self.assertEqual(task["area"], "automation")
        self.assertEqual(task["risk"], "low")
        self.assertEqual(task["health_finding_hash"], meta.stable_finding_hash(item))
        self.assertIn(".github/scripts/review-autonomous-pr-quality.py", task["allowed_paths"])

    def test_critical_finding_creates_medium_risk_task(self) -> None:
        plan = meta.plan_meta_tasks(
            health_report(finding("duplicate_active_product_sessions", severity="critical")),
            manifest(),
            max_tasks=3,
        )

        self.assertEqual(plan.tasks[0]["risk"], "medium")
        self.assertIn(".github/scripts/jules-unattended-monitor.sh", plan.tasks[0]["allowed_paths"])

    def test_no_eligible_autonomous_task_uses_selector_scope(self) -> None:
        plan = meta.plan_meta_tasks(
            health_report(finding("no_eligible_autonomous_task")),
            manifest(),
            max_tasks=3,
        )

        self.assertEqual(len(plan.tasks), 1)
        self.assertTrue(plan.tasks[0]["id"].startswith("automation-health-no-eligible-autonomous-task-"))
        self.assertIn("scripts/select_agent_task.py", plan.tasks[0]["allowed_paths"])
        self.assertIn(".github/scripts/automation-health-report.py", plan.tasks[0]["allowed_paths"])
        self.assertIn("agent_tasks.json", plan.tasks[0]["allowed_paths"])

    def test_todo_below_minimum_creates_actionable_manifest_task(self) -> None:
        plan = meta.plan_meta_tasks(
            health_report(finding("todo_below_minimum")),
            manifest(),
            max_tasks=3,
        )

        self.assertEqual(len(plan.tasks), 1)
        self.assertTrue(plan.tasks[0]["id"].startswith("automation-health-todo-below-minimum-"))
        self.assertIn("scripts/validate_agent_tasks.py", plan.tasks[0]["allowed_paths"])
        self.assertIn("scripts/select_agent_task.py", plan.tasks[0]["allowed_paths"])
        self.assertIn("agent_tasks.json", plan.tasks[0]["allowed_paths"])

    def test_existing_hash_is_deduped(self) -> None:
        item = finding("quality_failure")
        existing = {
            "id": "automation-health-quality-failure-existing",
            "status": "todo",
            "health_finding_hash": meta.stable_finding_hash(item),
        }

        plan = meta.plan_meta_tasks(health_report(item), manifest([existing]), max_tasks=3)

        self.assertEqual(plan.tasks, [])
        self.assertEqual(plan.skipped_hashes, [meta.stable_finding_hash(item)])

    def test_max_todo_tasks_is_respected(self) -> None:
        existing_todo = [{"id": "existing", "status": "todo"}]
        plan = meta.plan_meta_tasks(
            health_report(finding("quality_failure")),
            manifest(existing_todo, max_todo_tasks=1),
            max_tasks=3,
        )

        self.assertEqual(plan.tasks, [])
        self.assertIn("max_todo_tasks", plan.reason)

    def test_max_tasks_limits_batch_size(self) -> None:
        plan = meta.plan_meta_tasks(
            health_report(
                finding("quality_failure"),
                finding("failed_session"),
                finding("suspicious_micro_test_pr"),
            ),
            manifest(),
            max_tasks=2,
        )

        self.assertEqual(len(plan.tasks), 2)


if __name__ == "__main__":
    unittest.main()
