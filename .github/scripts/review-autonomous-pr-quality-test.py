#!/usr/bin/env python3
"""Unit tests for review-autonomous-pr-quality.py."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).with_name("review-autonomous-pr-quality.py")
SPEC = importlib.util.spec_from_file_location("review_autonomous_pr_quality", SCRIPT_PATH)
assert SPEC is not None
quality = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = quality
SPEC.loader.exec_module(quality)


def task(
    task_id: str,
    *,
    status: str,
    title: str = "Ensure workspace reframing events are logged as bridge decisions",
    description: str = "Verify and test that workspace reframing is explicitly emitted as a bridge decision log.",
    allowed_paths: list[str] | None = None,
    acceptance: list[str] | None = None,
    blocked_reason: str | None = None,
) -> dict:
    result = {
        "id": task_id,
        "status": status,
        "area": "proxy",
        "risk": "low",
        "title": title,
        "description": description,
        "allowed_paths": allowed_paths
        or ["internal/proxy/anthropic_bridge_test.go", "internal/proxy/anthropic.go", "agent_tasks.json"],
        "acceptance": acceptance
        or [
            "Bridge decision logging includes workspace reframing explicitly.",
            "Tests cover the new or existing observability path for this signal.",
        ],
    }
    if blocked_reason is not None:
        result["blocked_reason"] = blocked_reason
    return result


def manifest(tasks: list[dict]) -> dict:
    return {"tasks": tasks}


def evidence_body(
    task_id: str,
    *,
    status: str = "done",
    acceptance: list[str] | None = None,
    evidence_files: list[str] | None = None,
    checks: list[str] | None = None,
    blocked_reason: str = "",
    micro_pr_justification: str = "Runtime and test changes are grouped under the same task.",
) -> str:
    acceptance = acceptance or [
        "Bridge decision logging includes workspace reframing explicitly -> internal/proxy/anthropic.go",
        "Tests cover the observability path -> internal/proxy/anthropic_bridge_test.go",
    ]
    evidence_files = evidence_files or [
        "internal/proxy/anthropic.go",
        "internal/proxy/anthropic_bridge_test.go",
        "agent_tasks.json",
    ]
    checks = checks or [
        "python3 scripts/validate_agent_tasks.py agent_tasks.json",
        "go test ./...",
    ]
    lines = [
        "<!-- AUTONOMOUS_TASK_EVIDENCE",
        f"task_id: {task_id}",
        f"status: {status}",
    ]
    if blocked_reason:
        lines.append(f"blocked_reason: {blocked_reason}")
    lines.append("acceptance:")
    lines.extend(f"- {item}" for item in acceptance)
    lines.append("evidence_files:")
    lines.extend(f"- {item}" for item in evidence_files)
    lines.append("checks:")
    lines.extend(f"- {item}" for item in checks)
    if micro_pr_justification:
        lines.append(f"micro_pr_justification: {micro_pr_justification}")
    lines.append("-->")
    return "\n".join(lines)


class ReviewAutonomousPRQualityTest(unittest.TestCase):
    def evaluate(
        self,
        before: dict,
        after: dict,
        *,
        changed_files: list[str],
        diff_text: str = "",
        pr_title: str = "",
        pr_body: str = "",
        numstat: dict[str, tuple[int, int]] | None = None,
        allow_evidence_autofill: bool = False,
    ):
        return quality.evaluate_quality(
            before_manifest=before,
            after_manifest=after,
            changed_files=changed_files,
            diff_text=diff_text,
            numstat=numstat or {path: (10, 0) for path in changed_files},
            pr_title=pr_title,
            pr_body=pr_body,
            allow_evidence_autofill=allow_evidence_autofill,
        )

    def test_blocks_116_style_test_only_observability_completion(self) -> None:
        before = manifest([task("proxy-observability-workspace-reframing-4964e353", status="todo")])
        after = manifest(
            [
                task("proxy-observability-workspace-reframing-4964e353", status="done"),
                task(
                    "proxy-observability-notion-persona-leakage-4964e354",
                    status="todo",
                    title="Ensure Notion persona leakage events are logged as bridge decisions",
                ),
            ]
        )

        decision = self.evaluate(
            before,
            after,
            changed_files=["internal/proxy/anthropic_bridge_test.go", "agent_tasks.json"],
            diff_text="+ {name: \"JSON block\", output: \"```json ... workspace ...```\"}",
            pr_body="Тесты добавлены. Runtime logging требует сложного мокинга, вместо этого добавлен follow-up.",
        )

        self.assertFalse(decision.passed)
        self.assertIn("proxy-observability-workspace-reframing-4964e353", decision.task_ids)
        self.assertTrue(any("changed only tests" in reason for reason in decision.reasons))
        self.assertTrue(any("follow-up" in reason.lower() for reason in decision.reasons))

    def test_runtime_change_for_operational_task_passes(self) -> None:
        before = manifest([task("runtime-fix", status="todo")])
        after = manifest([task("runtime-fix", status="done")])

        decision = self.evaluate(
            before,
            after,
            changed_files=[
                "internal/proxy/anthropic.go",
                "internal/proxy/anthropic_bridge_test.go",
                "agent_tasks.json",
            ],
            diff_text='+ logger.Printf("[bridge] decision: workspace reframing")',
            pr_body=evidence_body("runtime-fix"),
        )

        self.assertTrue(decision.passed)
        self.assertEqual(decision.recommendation, "Autonomous PR quality gate passed.")
        self.assertTrue(decision.evidence["present"])

    def test_followup_code_identifiers_do_not_trigger_repeated_followup_failure(self) -> None:
        before = manifest([task("runtime-fix", status="todo")])
        after = manifest([task("runtime-fix", status="done")])

        body = "\n".join(
            [
                "Implemented context preservation in `buildSessionChainFollowUp`.",
                "Added `TestBuildSessionChainFollowUp_DiffContextPreservation`.",
                evidence_body("runtime-fix"),
            ]
        )

        decision = self.evaluate(
            before,
            after,
            changed_files=[
                "internal/proxy/anthropic.go",
                "internal/proxy/anthropic_bridge_test.go",
                "agent_tasks.json",
            ],
            diff_text='+ logger.Printf("[bridge] decision: workspace reframing")',
            pr_body=body,
        )

        self.assertTrue(decision.passed)
        self.assertFalse(any("follow-up tasks" in reason for reason in decision.reasons))

    def test_followup_task_ids_do_not_trigger_repeated_followup_failure(self) -> None:
        task_id = "proxy-observability-json-tool-call-mode-loss-diagnostics-followup"
        before = manifest([task(task_id, status="todo")])
        after = manifest([task(task_id, status="done")])

        body = "\n".join(
            [
                evidence_body(task_id),
                f"### Task {task_id}",
                "Completed the requested runtime diagnostic logging and tests.",
            ]
        )

        decision = self.evaluate(
            before,
            after,
            changed_files=[
                "internal/proxy/anthropic.go",
                "internal/proxy/anthropic_bridge_test.go",
                "agent_tasks.json",
            ],
            diff_text='+ logger.Printf("[bridge] decision: workspace reframing")',
            pr_body=body,
        )

        self.assertTrue(decision.passed)
        self.assertFalse(any("follow-up tasks" in reason for reason in decision.reasons))

    def test_repeated_followup_prose_still_fails(self) -> None:
        before = manifest([task("runtime-fix", status="todo")])
        after = manifest([task("runtime-fix", status="done")])

        body = "\n".join(
            [
                "The runtime change is included, but one follow-up remains for logs.",
                "A second followup will handle remaining diagnostics.",
                evidence_body("runtime-fix"),
            ]
        )

        decision = self.evaluate(
            before,
            after,
            changed_files=[
                "internal/proxy/anthropic.go",
                "internal/proxy/anthropic_bridge_test.go",
                "agent_tasks.json",
            ],
            diff_text='+ logger.Printf("[bridge] decision: workspace reframing")',
            pr_body=body,
        )

        self.assertFalse(decision.passed)
        self.assertTrue(any("follow-up tasks" in reason for reason in decision.reasons))

    def test_test_only_task_without_operational_claim_passes(self) -> None:
        before = manifest(
            [
                task(
                    "parser-test",
                    status="todo",
                    title="Add parser edge-case tests",
                    description="Add focused offline tests for parser behavior.",
                    allowed_paths=["internal/proxy/tools_test.go", "agent_tasks.json"],
                    acceptance=["Tests cover the parser edge case."],
                )
            ]
        )
        after = manifest(
            [
                task(
                    "parser-test",
                    status="done",
                    title="Add parser edge-case tests",
                    description="Add focused offline tests for parser behavior.",
                    allowed_paths=["internal/proxy/tools_test.go", "agent_tasks.json"],
                    acceptance=["Tests cover the parser edge case."],
                )
            ]
        )

        decision = self.evaluate(
            before,
            after,
            changed_files=["internal/proxy/tools_test.go", "agent_tasks.json"],
            diff_text="+ t.Run(\"parser edge case\", func(t *testing.T) {})",
            pr_body=evidence_body(
                "parser-test",
                acceptance=["Tests cover the parser edge case -> internal/proxy/tools_test.go"],
                evidence_files=["internal/proxy/tools_test.go", "agent_tasks.json"],
                micro_pr_justification="This task is explicitly scoped to focused offline parser tests.",
            ),
        )

        self.assertTrue(decision.passed)

    def test_temporary_scratch_markdown_file_fails(self) -> None:
        before = manifest([task("runtime-fix", status="todo")])
        after = manifest([task("runtime-fix", status="done")])

        decision = self.evaluate(
            before,
            after,
            changed_files=[
                "internal/proxy/anthropic.go",
                "internal/proxy/anthropic_bridge_test.go",
                "agent_tasks.json",
                "plan.md",
            ],
            diff_text='+ logger.Printf("[bridge] decision: workspace reframing")',
            pr_body=evidence_body(
                "runtime-fix",
                evidence_files=[
                    "internal/proxy/anthropic.go",
                    "internal/proxy/anthropic_bridge_test.go",
                    "agent_tasks.json",
                    "plan.md",
                ],
            ),
        )

        self.assertFalse(decision.passed)
        self.assertTrue(any("scratch/planning files" in reason for reason in decision.reasons))

    def test_pr_body_scratch_artifact_fails(self) -> None:
        before = manifest([task("runtime-fix", status="todo")])
        after = manifest([task("runtime-fix", status="done")])

        decision = self.evaluate(
            before,
            after,
            changed_files=[
                "internal/proxy/anthropic.go",
                "internal/proxy/anthropic_bridge_test.go",
                "agent_tasks.json",
                "pr_body.txt",
            ],
            diff_text='+ logger.Printf("[bridge] decision: workspace reframing")',
            pr_body=evidence_body(
                "runtime-fix",
                evidence_files=[
                    "internal/proxy/anthropic.go",
                    "internal/proxy/anthropic_bridge_test.go",
                    "agent_tasks.json",
                    "pr_body.txt",
                ],
            ),
        )

        self.assertFalse(decision.passed)
        self.assertTrue(any("pr_body.txt" in reason for reason in decision.reasons))

    def test_manifest_only_block_with_reason_passes(self) -> None:
        before = manifest([task("blocked-task", status="todo")])
        after = manifest(
            [
                task(
                    "blocked-task",
                    status="blocked",
                    blocked_reason="Paused after repeated Jules FAILED sessions.",
                )
            ]
        )

        decision = self.evaluate(
            before,
            after,
            changed_files=["agent_tasks.json"],
            diff_text='+ "status": "blocked"',
            pr_body=evidence_body(
                "blocked-task",
                status="blocked",
                acceptance=[],
                evidence_files=["agent_tasks.json"],
                checks=["python3 scripts/validate_agent_tasks.py agent_tasks.json"],
                blocked_reason="Paused after repeated Jules FAILED sessions.",
                micro_pr_justification="Manifest-only blocked update documents missing evidence.",
            ),
        )

        self.assertTrue(decision.passed)
        self.assertEqual(decision.blocked_task_ids, ["blocked-task"])

    def test_manifest_only_block_without_reason_fails(self) -> None:
        before = manifest([task("blocked-task", status="todo")])
        after = manifest([task("blocked-task", status="blocked")])

        decision = self.evaluate(
            before,
            after,
            changed_files=["agent_tasks.json"],
            diff_text='+ "status": "blocked"',
        )

        self.assertFalse(decision.passed)
        self.assertTrue(any("without blocked_reason" in reason for reason in decision.reasons))

    def test_no_task_state_update_fails(self) -> None:
        before = manifest([task("unchanged-task", status="todo")])
        after = manifest([task("unchanged-task", status="todo")])

        decision = self.evaluate(
            before,
            after,
            changed_files=["internal/proxy/anthropic.go"],
            diff_text="+ runtime change",
        )

        self.assertFalse(decision.passed)
        self.assertTrue(any("no durable task state update" in reason.lower() for reason in decision.reasons))

    def test_missing_evidence_block_fails_done_task(self) -> None:
        before = manifest([task("runtime-fix", status="todo")])
        after = manifest([task("runtime-fix", status="done")])

        decision = self.evaluate(
            before,
            after,
            changed_files=["internal/proxy/anthropic.go", "agent_tasks.json"],
            diff_text='+ logger.Printf("[bridge] decision: workspace reframing")',
            pr_body="Runtime bridge decision logging was updated.",
        )

        self.assertFalse(decision.passed)
        self.assertTrue(any("AUTONOMOUS_TASK_EVIDENCE" in reason for reason in decision.reasons))

    def test_trusted_autofill_missing_evidence_block_for_done_task(self) -> None:
        before = manifest([task("runtime-fix", status="todo")])
        after = manifest([task("runtime-fix", status="done")])

        decision = self.evaluate(
            before,
            after,
            changed_files=["internal/proxy/anthropic.go", "agent_tasks.json"],
            diff_text='+ logger.Printf("[bridge] decision: workspace reframing")',
            pr_body="Runtime bridge decision logging was updated. Checks: go test ./...",
            allow_evidence_autofill=True,
        )

        self.assertTrue(decision.passed)
        self.assertEqual(decision.evidence["source"], "autofill")
        self.assertTrue(decision.evidence["autofilled"])
        self.assertIn("runtime-fix", decision.autofill_evidence_block)
        self.assertIn("internal/proxy/anthropic.go", decision.autofill_evidence_block)
        self.assertIn("go test ./...", decision.autofill_evidence_block)

    def test_trusted_autofill_requires_single_changed_task(self) -> None:
        before = manifest(
            [
                task("runtime-fix-one", status="todo"),
                task("runtime-fix-two", status="todo"),
            ]
        )
        after = manifest(
            [
                task("runtime-fix-one", status="done"),
                task("runtime-fix-two", status="done"),
            ]
        )

        decision = self.evaluate(
            before,
            after,
            changed_files=["internal/proxy/anthropic.go", "agent_tasks.json"],
            diff_text='+ logger.Printf("[bridge] decision: workspace reframing")',
            allow_evidence_autofill=True,
        )

        self.assertFalse(decision.passed)
        self.assertEqual(decision.evidence["source"], "missing")
        self.assertFalse(decision.autofill_evidence_block)

    def test_mismatched_evidence_task_id_fails(self) -> None:
        before = manifest([task("runtime-fix", status="todo")])
        after = manifest([task("runtime-fix", status="done")])

        decision = self.evaluate(
            before,
            after,
            changed_files=[
                "internal/proxy/anthropic.go",
                "internal/proxy/anthropic_bridge_test.go",
                "agent_tasks.json",
            ],
            diff_text='+ logger.Printf("[bridge] decision: workspace reframing")',
            pr_body=evidence_body("other-task"),
        )

        self.assertFalse(decision.passed)
        self.assertTrue(any("does not match changed task" in reason for reason in decision.reasons))

    def test_evidence_file_must_be_changed_by_pr(self) -> None:
        before = manifest([task("runtime-fix", status="todo")])
        after = manifest([task("runtime-fix", status="done")])

        decision = self.evaluate(
            before,
            after,
            changed_files=["internal/proxy/anthropic.go", "agent_tasks.json"],
            diff_text='+ logger.Printf("[bridge] decision: workspace reframing")',
            pr_body=evidence_body(
                "runtime-fix",
                evidence_files=["internal/proxy/anthropic.go", "docs/missing.md", "agent_tasks.json"],
            ),
        )

        self.assertFalse(decision.passed)
        self.assertTrue(any("files not changed" in reason for reason in decision.reasons))

    def test_done_evidence_must_cover_acceptance_count(self) -> None:
        before = manifest([task("runtime-fix", status="todo")])
        after = manifest([task("runtime-fix", status="done")])

        decision = self.evaluate(
            before,
            after,
            changed_files=[
                "internal/proxy/anthropic.go",
                "internal/proxy/anthropic_bridge_test.go",
                "agent_tasks.json",
            ],
            diff_text='+ logger.Printf("[bridge] decision: workspace reframing")',
            pr_body=evidence_body(
                "runtime-fix",
                acceptance=["Only one criterion -> internal/proxy/anthropic.go"],
            ),
        )

        self.assertFalse(decision.passed)
        self.assertTrue(any("acceptance criteria" in reason for reason in decision.reasons))

    def test_evidence_requires_micro_pr_justification(self) -> None:
        before = manifest([task("runtime-fix", status="todo")])
        after = manifest([task("runtime-fix", status="done")])

        decision = self.evaluate(
            before,
            after,
            changed_files=[
                "internal/proxy/anthropic.go",
                "internal/proxy/anthropic_bridge_test.go",
                "agent_tasks.json",
            ],
            diff_text='+ logger.Printf("[bridge] decision: workspace reframing")',
            pr_body=evidence_body("runtime-fix", micro_pr_justification=""),
        )

        self.assertFalse(decision.passed)
        self.assertTrue(any("micro_pr_justification" in reason for reason in decision.reasons))


if __name__ == "__main__":
    unittest.main()
