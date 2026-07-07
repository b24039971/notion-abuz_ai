#!/usr/bin/env python3
"""Review autonomous Jules PRs for result quality before automerge."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


OPERATIONAL_KEYWORDS = (
    "runtime",
    "bridge decision",
    "observability",
    "logging",
    "log ",
    "logs ",
    "logged",
    "live smoke",
    "diagnostic",
    "diagnostics",
    "artifact",
    "artifacts",
    "compatibility",
    "notion persona",
    "tool-call",
    "workspace reframing",
    "transcript",
    "session",
    "final-answer",
    "json tool-call",
)

OBSERVABILITY_KEYWORDS = (
    "bridge decision",
    "observability",
    "logging",
    "logged",
    "diagnostic",
    "diagnostics",
    "workspace reframing",
    "notion persona",
    "tool-call refusal",
)

DIRECT_OBSERVABILITY_DIFF_MARKERS = (
    "[bridge] decision",
    "decision:",
    "bridge decision",
    "logger",
    "logf",
    "slog",
    "zap.",
    "bytes.buffer",
    "capture",
    "captured",
    "stderr",
    "stdout",
)

COMPROMISE_PHRASES = (
    "сложно мок",
    "вместо этого",
    "не удалось",
    "не получилось",
    "отдельная задача",
    "последующая задача",
    "отдельной задачей",
    "оставлено как",
    "requires complex mocking",
    "complex mocking",
    "instead",
    "unable to",
    "could not",
    "not possible",
    "left as follow-up",
    "follow-up task",
    "separate task",
)

EVIDENCE_BLOCK_RE = re.compile(
    r"<!--\s*AUTONOMOUS_TASK_EVIDENCE\s*(?P<body>.*?)\s*-->",
    re.IGNORECASE | re.DOTALL,
)
FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
FOLLOWUP_WORD_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])follow-?up(?![A-Za-z0-9_])")
FOLLOWUP_IDENTIFIER_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:[a-z0-9]+[-_])+[a-z0-9]*follow-?up[a-z0-9]*(?:[-_][a-z0-9]+)*(?![A-Za-z0-9_])"
)

EVIDENCE_DIRECT_FIELDS = {
    "task_id",
    "status",
    "blocked_reason",
    "micro_pr_justification",
}
EVIDENCE_LIST_SECTIONS = {
    "acceptance",
    "evidence",
    "evidence_files",
    "checks",
}
EVIDENCE_SECTION_ALIASES = {
    "evidence_files": "evidence",
    "files": "evidence",
    "validation": "checks",
    "validations": "checks",
    "tests": "checks",
    "acceptance_criteria": "acceptance",
    "criteria": "acceptance",
}
EVIDENCE_FILE_RE = re.compile(
    r"(?P<path>(?:agent_tasks\.json|README\.md|AGENTS\.md|docs/|internal/|cmd/|scripts/|\.github/|web/)"
    r"[A-Za-z0-9_./@+:-]*)"
)
SCRATCH_FILE_RE = re.compile(
    r"^(?:plan[a-z0-9_-]*|scratch[a-z0-9_-]*|tmp[a-z0-9_-]*|jules-notes[a-z0-9_-]*|"
    r"pr[-_]?body[a-z0-9_-]*|pull[-_]?request[-_]?body[a-z0-9_-]*)\.(?:md|txt)$",
    re.IGNORECASE,
)


@dataclass
class ChangedTask:
    task_id: str
    before_status: str
    after_status: str
    task: dict[str, Any]


@dataclass
class EvidenceBlock:
    present: bool = False
    raw_count: int = 0
    source: str = "missing"
    autofilled: bool = False
    task_id: str = ""
    status: str = ""
    blocked_reason: str = ""
    micro_pr_justification: str = ""
    acceptance: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    evidence_files: list[str] = field(default_factory=list)


@dataclass
class QualityDecision:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    blocked_task_ids: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    new_task_ids: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    autofill_evidence_block: str = ""
    recommendation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "task_ids": self.task_ids,
            "blocked_task_ids": self.blocked_task_ids,
            "changed_files": self.changed_files,
            "new_task_ids": self.new_task_ids,
            "evidence": self.evidence,
            "autofill_evidence_block": self.autofill_evidence_block,
            "recommendation": self.recommendation,
        }


class QualityInputError(RuntimeError):
    """Raised when the quality gate cannot inspect the PR."""


def run_git(args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip()
        command = "git " + " ".join(args)
        raise QualityInputError(f"{command} failed: {stderr}") from exc
    return result.stdout


def load_manifest_from_ref(ref: str, path: str) -> dict[str, Any]:
    raw = run_git(["show", f"{ref}:{path}"])
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise QualityInputError(f"{path} at {ref} is not a JSON object")
    return data


def changed_files_between(base: str, head: str) -> list[str]:
    raw = run_git(["diff", "--name-only", base, head])
    return [line.strip() for line in raw.splitlines() if line.strip()]


def diff_text_between(base: str, head: str) -> str:
    return run_git(["diff", "--unified=0", base, head])


def diff_numstat_between(base: str, head: str) -> dict[str, tuple[int, int]]:
    raw = run_git(["diff", "--numstat", base, head])
    result: dict[str, tuple[int, int]] = {}
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added_raw, deleted_raw, path = parts[0], parts[1], parts[2]
        if added_raw == "-" or deleted_raw == "-":
            result[path] = (0, 0)
            continue
        result[path] = (int(added_raw), int(deleted_raw))
    return result


def normalize_path(path: str) -> str:
    return path.replace("\\", "/").lower()


def is_manifest_path(path: str) -> bool:
    return normalize_path(path) == "agent_tasks.json"


def is_test_path(path: str) -> bool:
    normalized = normalize_path(path)
    return normalized.endswith("_test.go") or normalized.endswith(".test.ts") or normalized.endswith(".test.tsx")


def is_doc_path(path: str) -> bool:
    normalized = normalize_path(path)
    return normalized.startswith("docs/") or normalized in {"readme.md", "agents.md"} or normalized.endswith(".md")


def is_scratch_file(path: str) -> bool:
    return bool(SCRATCH_FILE_RE.match(normalize_path(path)))


def is_runtime_or_script_path(path: str) -> bool:
    normalized = normalize_path(path)
    if is_manifest_path(normalized) or is_test_path(normalized) or is_doc_path(normalized):
        return False
    return (
        normalized.endswith(".go")
        or normalized.startswith(".github/scripts/")
        or normalized.startswith("scripts/")
        or normalized.startswith("web/")
        or normalized.endswith(".sh")
        or normalized.endswith(".py")
    )


def only_tests_docs_manifest(changed_files: list[str]) -> bool:
    return bool(changed_files) and all(
        is_manifest_path(path) or is_test_path(path) or is_doc_path(path)
        for path in changed_files
    )


def only_tests_manifest(changed_files: list[str]) -> bool:
    return bool(changed_files) and all(is_manifest_path(path) or is_test_path(path) for path in changed_files)


def non_manifest_files(changed_files: list[str]) -> list[str]:
    return [path for path in changed_files if not is_manifest_path(path)]


def task_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tasks = manifest.get("tasks", [])
    if not isinstance(tasks, list):
        raise QualityInputError("agent_tasks.json tasks must be an array")
    result: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if isinstance(task, dict) and isinstance(task.get("id"), str):
            result[task["id"]] = task
    return result


def task_goal_text(task: dict[str, Any]) -> str:
    parts: list[str] = []
    for field_name in ("title", "description"):
        value = task.get(field_name)
        if isinstance(value, str):
            parts.append(value)
    acceptance = task.get("acceptance")
    if isinstance(acceptance, list):
        parts.extend(str(item) for item in acceptance)
    return "\n".join(parts).lower()


def status_changes(
    before_manifest: dict[str, Any],
    after_manifest: dict[str, Any],
    target_status: str,
) -> list[ChangedTask]:
    before = task_map(before_manifest)
    after = task_map(after_manifest)
    changes: list[ChangedTask] = []
    for task_id, after_task in after.items():
        before_status = str((before.get(task_id) or {}).get("status", "missing"))
        after_status = str(after_task.get("status", ""))
        if before_status != target_status and after_status == target_status:
            changes.append(
                ChangedTask(
                    task_id=task_id,
                    before_status=before_status,
                    after_status=after_status,
                    task=after_task,
                )
            )
    return changes


def new_task_ids(before_manifest: dict[str, Any], after_manifest: dict[str, Any]) -> list[str]:
    before_ids = set(task_map(before_manifest))
    after_ids = set(task_map(after_manifest))
    return sorted(after_ids - before_ids)


def is_operational_task(task: dict[str, Any]) -> bool:
    text = task_goal_text(task)
    return any(keyword in text for keyword in OPERATIONAL_KEYWORDS)


def requires_observability_proof(task: dict[str, Any]) -> bool:
    text = task_goal_text(task)
    return any(keyword in text for keyword in OBSERVABILITY_KEYWORDS)


def diff_has_direct_observability_assertion(diff_text: str) -> bool:
    lower = diff_text.lower()
    return any(marker in lower for marker in DIRECT_OBSERVABILITY_DIFF_MARKERS)


def body_has_compromise(pr_title: str, pr_body: str) -> bool:
    text = f"{pr_title}\n{pr_body}".lower()
    return any(phrase in text for phrase in COMPROMISE_PHRASES)


def strip_markdown_code(text: str) -> str:
    without_fenced = FENCED_CODE_RE.sub(" ", text or "")
    return INLINE_CODE_RE.sub(" ", without_fenced)


def repeated_followup_mentions(pr_body: str) -> bool:
    prose_body = strip_markdown_code(pr_body)
    prose_body = FOLLOWUP_IDENTIFIER_RE.sub(" ", prose_body)
    return len(FOLLOWUP_WORD_RE.findall(prose_body)) >= 2


def normalize_evidence_key(key: str) -> str:
    normalized = key.strip().lower().replace("-", "_").replace(" ", "_")
    return EVIDENCE_SECTION_ALIASES.get(normalized, normalized)


def clean_evidence_value(value: str) -> str:
    return value.strip().strip("`").strip()


def safe_evidence_line(value: str) -> str:
    return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())


def extract_evidence_files(entries: list[str]) -> list[str]:
    files: list[str] = []
    for entry in entries:
        for match in EVIDENCE_FILE_RE.finditer(entry):
            path = match.group("path").rstrip(".,;:)`")
            if path and path not in files:
                files.append(path)
    return files


def parse_evidence_block(pr_body: str) -> EvidenceBlock:
    matches = list(EVIDENCE_BLOCK_RE.finditer(pr_body or ""))
    block = EvidenceBlock(present=bool(matches), raw_count=len(matches), source="pr_body" if matches else "missing")
    if not matches:
        return block

    section = ""
    for raw_line in matches[0].group("body").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("-") and section:
            value = clean_evidence_value(line[1:])
            if not value:
                continue
            if section == "acceptance":
                block.acceptance.append(value)
            elif section == "checks":
                block.checks.append(value)
            elif section == "evidence":
                block.evidence.append(value)
            continue

        if ":" not in line:
            continue

        key, value = line.split(":", 1)
        key = normalize_evidence_key(key)
        value = clean_evidence_value(value)

        if key in EVIDENCE_DIRECT_FIELDS:
            setattr(block, key, value)
            section = ""
        elif key in EVIDENCE_LIST_SECTIONS:
            section = EVIDENCE_SECTION_ALIASES.get(key, key)
            if section == "evidence_files":
                section = "evidence"
            if value:
                if section == "acceptance":
                    block.acceptance.append(value)
                elif section == "checks":
                    block.checks.append(value)
                elif section == "evidence":
                    block.evidence.append(value)
        else:
            section = ""

    block.evidence_files = extract_evidence_files(block.evidence)
    return block


def render_evidence_block(block: EvidenceBlock) -> str:
    lines = [
        "<!-- AUTONOMOUS_TASK_EVIDENCE",
        f"task_id: {safe_evidence_line(block.task_id)}",
        f"status: {safe_evidence_line(block.status)}",
    ]
    if block.blocked_reason:
        lines.append(f"blocked_reason: {safe_evidence_line(block.blocked_reason)}")
    lines.append("acceptance:")
    lines.extend(f"- {safe_evidence_line(item)}" for item in block.acceptance)
    lines.append("evidence_files:")
    lines.extend(f"- {safe_evidence_line(item)}" for item in block.evidence_files)
    lines.append("checks:")
    lines.extend(f"- {safe_evidence_line(item)}" for item in block.checks)
    lines.append(f"micro_pr_justification: {safe_evidence_line(block.micro_pr_justification)}")
    lines.append("-->")
    return "\n".join(lines)


def synthesize_evidence_block(
    *,
    done_changes: list[ChangedTask],
    blocked_changes: list[ChangedTask],
    changed_files: list[str],
    pr_body: str,
) -> EvidenceBlock | None:
    completed = done_changes or blocked_changes
    if len(completed) != 1:
        return None
    if not any(is_manifest_path(path) for path in changed_files):
        return None

    change = completed[0]
    status = "done" if done_changes else "blocked"
    if status == "done":
        evidence_files = [path for path in changed_files if not is_scratch_file(path)]
    else:
        evidence_files = ["agent_tasks.json"]
    if not evidence_files:
        return None

    task = change.task
    acceptance_items = task.get("acceptance") if isinstance(task.get("acceptance"), list) else []
    evidence_target = ", ".join(evidence_files)
    acceptance = [
        f"{str(item).strip()} -> {evidence_target}"
        for item in acceptance_items
        if str(item).strip()
    ]
    if status == "blocked" and not acceptance:
        acceptance = ["Task blocked with concrete manifest reason -> agent_tasks.json"]

    checks = [
        "python3 scripts/validate_agent_tasks.py agent_tasks.json",
        "GitHub Actions CI/live-smoke checks for this PR",
    ]
    body_lower = pr_body.lower()
    if "go test ./..." in body_lower:
        checks.append("go test ./...")
    if "go vet ./..." in body_lower:
        checks.append("go vet ./...")

    block = EvidenceBlock(
        present=True,
        raw_count=1,
        source="autofill",
        autofilled=True,
        task_id=change.task_id,
        status=status,
        blocked_reason=str(task.get("blocked_reason") or "").strip() if status == "blocked" else "",
        acceptance=acceptance,
        evidence=evidence_files,
        checks=checks,
        evidence_files=evidence_files,
        micro_pr_justification=(
            "Synthesized by trusted quality gate from one changed task status, "
            "changed evidence files, and PR checks because Jules may not have "
            "permission to edit PR metadata."
        ),
    )
    return block


def evidence_to_dict(block: EvidenceBlock) -> dict[str, Any]:
    return {
        "present": block.present,
        "raw_count": block.raw_count,
        "source": block.source,
        "autofilled": block.autofilled,
        "task_id": block.task_id,
        "status": block.status,
        "acceptance_count": len(block.acceptance),
        "evidence_count": len(block.evidence),
        "evidence_files": block.evidence_files,
        "checks_count": len(block.checks),
        "has_micro_pr_justification": bool(block.micro_pr_justification),
        "has_blocked_reason": bool(block.blocked_reason),
    }


def task_acceptance_count(task: dict[str, Any]) -> int:
    acceptance = task.get("acceptance")
    if not isinstance(acceptance, list):
        return 0
    return len([item for item in acceptance if str(item).strip()])


def changed_line_count(numstat: dict[str, tuple[int, int]], changed_files: list[str]) -> int:
    total = 0
    for path in changed_files:
        if is_manifest_path(path):
            continue
        added, deleted = numstat.get(path, (0, 0))
        total += added + deleted
    return total


def evaluate_quality(
    *,
    before_manifest: dict[str, Any],
    after_manifest: dict[str, Any],
    changed_files: list[str],
    diff_text: str,
    numstat: dict[str, tuple[int, int]],
    pr_title: str,
    pr_body: str,
    allow_evidence_autofill: bool = False,
) -> QualityDecision:
    done_changes = status_changes(before_manifest, after_manifest, "done")
    blocked_changes = status_changes(before_manifest, after_manifest, "blocked")
    added_tasks = new_task_ids(before_manifest, after_manifest)

    reasons: list[str] = []
    warnings: list[str] = []
    done_ids = [change.task_id for change in done_changes]
    blocked_ids = [change.task_id for change in blocked_changes]
    non_manifest = non_manifest_files(changed_files)
    has_runtime_or_script = any(is_runtime_or_script_path(path) for path in non_manifest)
    has_direct_observability_assertion = diff_has_direct_observability_assertion(diff_text)
    compromise = body_has_compromise(pr_title, pr_body)
    changed_lines = changed_line_count(numstat, changed_files)
    evidence = parse_evidence_block(pr_body)
    scratch_files = [path for path in changed_files if is_scratch_file(path)]
    autofill_evidence_block = ""

    if scratch_files:
        reasons.append(
            "Autonomous PR contains temporary scratch/planning files: "
            + ", ".join(sorted(scratch_files))
        )

    lower_body = pr_body.lower()
    if "automation-health-repeated-followup" not in lower_body:
        if repeated_followup_mentions(pr_body):
            reasons.append(
                "PR body repeatedly mentions follow-up tasks. Complete the current task "
                "fully without leaving follow-up work, or avoid using the word 'follow-up' if it is a false positive."
            )

    if len(done_changes) > 1:
        reasons.append(
            "More than one task was marked done; autonomous PRs must complete one task id per PR."
        )

    if not done_changes and not blocked_changes:
        reasons.append(
            "No task moved to done or blocked in agent_tasks.json; autonomous PR has no durable task state update."
        )

    if done_changes or blocked_changes:
        if not evidence.present:
            if allow_evidence_autofill:
                autofill = synthesize_evidence_block(
                    done_changes=done_changes,
                    blocked_changes=blocked_changes,
                    changed_files=changed_files,
                    pr_body=pr_body,
                )
                if autofill is not None:
                    evidence = autofill
                    autofill_evidence_block = render_evidence_block(autofill)
                    warnings.append(
                        "AUTONOMOUS_TASK_EVIDENCE was missing from PR body; "
                        "trusted quality gate synthesized it from manifest diff and changed files."
                    )
                else:
                    reasons.append("PR body is missing the AUTONOMOUS_TASK_EVIDENCE block required for autonomous PRs.")
            else:
                reasons.append("PR body is missing the AUTONOMOUS_TASK_EVIDENCE block required for autonomous PRs.")
        elif evidence.raw_count > 1:
            reasons.append("PR body contains more than one AUTONOMOUS_TASK_EVIDENCE block.")
        else:
            completed_ids = done_ids or blocked_ids
            expected_status = "done" if done_ids else "blocked"
            expected_task_id = completed_ids[0] if len(completed_ids) == 1 else ""
            if not evidence.task_id:
                reasons.append("AUTONOMOUS_TASK_EVIDENCE is missing task_id.")
            elif expected_task_id and evidence.task_id != expected_task_id:
                reasons.append(
                    f"AUTONOMOUS_TASK_EVIDENCE task_id {evidence.task_id} "
                    f"does not match changed task {expected_task_id}."
                )
            if evidence.status not in {"done", "blocked"}:
                reasons.append("AUTONOMOUS_TASK_EVIDENCE status must be done or blocked.")
            elif evidence.status != expected_status:
                if expected_status == "blocked" and evidence.status == "done":
                    warnings.append(
                        f"AUTONOMOUS_TASK_EVIDENCE status is {evidence.status} "
                        f"but manifest status is {expected_status}. Tolerating."
                    )
                else:
                    reasons.append(
                        f"AUTONOMOUS_TASK_EVIDENCE status {evidence.status} "
                        f"does not match manifest status {expected_status}."
                    )
            if not evidence.checks:
                reasons.append("AUTONOMOUS_TASK_EVIDENCE must list validation checks that were run.")
            if not evidence.micro_pr_justification:
                reasons.append("AUTONOMOUS_TASK_EVIDENCE is missing micro_pr_justification.")

            changed_file_set = {normalize_path(path) for path in changed_files}
            missing_evidence_files = [
                path
                for path in evidence.evidence_files
                if normalize_path(path) not in changed_file_set
            ]
            if missing_evidence_files:
                reasons.append(
                    "AUTONOMOUS_TASK_EVIDENCE references files not changed by this PR: "
                    + ", ".join(sorted(missing_evidence_files))
                )
            if not evidence.evidence_files:
                reasons.append(
                    "AUTONOMOUS_TASK_EVIDENCE must list changed evidence files using repo-relative paths."
                )

    for change in blocked_changes:
        blocked_reason = str(change.task.get("blocked_reason", "")).strip()
        if not blocked_reason:
            reasons.append(f"Task {change.task_id} moved to blocked without blocked_reason.")
        if evidence.present and evidence.status == "blocked" and not evidence.blocked_reason:
            reasons.append("Blocked autonomous PR evidence must include blocked_reason.")

    for change in done_changes:
        task = change.task
        operational = is_operational_task(task)
        observability = requires_observability_proof(task)
        expected_acceptance = task_acceptance_count(task)

        if evidence.present and evidence.status == "done" and len(evidence.acceptance) < expected_acceptance:
            reasons.append(
                f"AUTONOMOUS_TASK_EVIDENCE maps {len(evidence.acceptance)} acceptance items, "
                f"but task {change.task_id} has {expected_acceptance} acceptance criteria."
            )
        if evidence.present and evidence.status == "done":
            unmapped_acceptance = [item for item in evidence.acceptance if "->" not in item]
            if unmapped_acceptance:
                reasons.append("AUTONOMOUS_TASK_EVIDENCE acceptance items must map criteria to evidence with '->'.")

        if operational and only_tests_manifest(changed_files):
            reasons.append(
                f"Task {change.task_id} is operational/diagnostic but the PR changed only tests and agent_tasks.json."
            )

        if observability and not has_runtime_or_script and not has_direct_observability_assertion:
            reasons.append(
                f"Task {change.task_id} requires observability/logging proof, "
                "but the diff has no runtime/script change and no direct log-capture assertion."
            )

        if operational and compromise and only_tests_docs_manifest(changed_files) and not has_runtime_or_script:
            reasons.append(
                f"Task {change.task_id} was marked done while the PR text describes "
                "a compromise or moved core work into a follow-up."
            )

        if operational and added_tasks and only_tests_docs_manifest(changed_files) and not has_runtime_or_script:
            reasons.append(
                f"Task {change.task_id} was marked done while adding follow-up tasks, "
                "but the PR only changed tests/docs/manifest."
            )

        if operational and changed_lines <= 6 and not has_runtime_or_script:
            warnings.append(
                f"Task {change.task_id} has a very small non-manifest diff "
                f"({changed_lines} changed lines); verify this is not a formal-only completion."
            )

    passed = not reasons
    if passed:
        if blocked_changes and not done_changes:
            recommendation = "Manifest-only block update is acceptable; no merge-quality blocker found."
        else:
            recommendation = "Autonomous PR quality gate passed."
    else:
        recommendation = (
            "Do not merge automatically. Ask Jules/Codex to update the same PR with direct evidence "
            "for the selected task, or block the task with a concrete reason instead of marking it done."
        )

    return QualityDecision(
        passed=passed,
        reasons=reasons,
        warnings=warnings,
        task_ids=done_ids,
        blocked_task_ids=blocked_ids,
        changed_files=changed_files,
        new_task_ids=added_tasks,
        evidence=evidence_to_dict(evidence),
        autofill_evidence_block=autofill_evidence_block,
        recommendation=recommendation,
    )


def write_github_outputs(path: Path, decision: QualityDecision) -> None:
    summary = "; ".join(decision.reasons or decision.warnings or [decision.recommendation])
    def write_output(output: Any, key: str, value: str) -> None:
        if "\n" in value:
            output.write(f"{key}<<EOF\n{value}\nEOF\n")
        else:
            output.write(f"{key}={value}\n")

    with path.open("a", encoding="utf-8") as output:
        write_output(output, "passed", "true" if decision.passed else "false")
        write_output(output, "summary", summary)
        write_output(output, "evidence_autofill", "true" if decision.autofill_evidence_block else "false")
        write_output(output, "evidence_source", str(decision.evidence.get("source") or ""))


def write_report(path: Path, decision: QualityDecision) -> None:
    lines = [
        "# Autonomous PR quality gate",
        "",
        f"Status: {'passed' if decision.passed else 'failed'}",
        "",
    ]
    if decision.task_ids:
        lines.append("Done task ids:")
        lines.extend(f"- `{task_id}`" for task_id in decision.task_ids)
        lines.append("")
    if decision.blocked_task_ids:
        lines.append("Blocked task ids:")
        lines.extend(f"- `{task_id}`" for task_id in decision.blocked_task_ids)
        lines.append("")
    if decision.reasons:
        lines.append("Blocking reasons:")
        lines.extend(f"- {reason}" for reason in decision.reasons)
        lines.append("")
    if decision.warnings:
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in decision.warnings)
        lines.append("")
    if decision.new_task_ids:
        lines.append("New task ids:")
        lines.extend(f"- `{task_id}`" for task_id in decision.new_task_ids)
        lines.append("")
    evidence = decision.evidence
    if evidence:
        lines.append("Evidence block:")
        lines.append(f"- present: `{str(evidence.get('present')).lower()}`")
        lines.append(f"- source: `{evidence.get('source') or ''}`")
        lines.append(f"- autofilled: `{str(evidence.get('autofilled')).lower()}`")
        lines.append(f"- task_id: `{evidence.get('task_id') or ''}`")
        lines.append(f"- status: `{evidence.get('status') or ''}`")
        lines.append(f"- evidence_files: `{', '.join(evidence.get('evidence_files') or [])}`")
        lines.append("")
    lines.append("Recommendation:")
    lines.append(decision.recommendation)
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def read_pr_body(args: argparse.Namespace) -> str:
    if args.pr_body_file:
        return Path(args.pr_body_file).read_text(encoding="utf-8")
    return args.pr_body


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Base git ref/sha for PR diff")
    parser.add_argument("--head", required=True, help="Head git ref/sha for PR diff")
    parser.add_argument("--manifest", default="agent_tasks.json")
    parser.add_argument("--pr-title", default="")
    parser.add_argument("--pr-body", default="")
    parser.add_argument("--pr-body-file", default="")
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--allow-evidence-autofill",
        action="store_true",
        help="allow trusted automation to synthesize missing PR-body evidence from the manifest diff",
    )
    parser.add_argument("--autofill-evidence-file", type=Path)
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT", ""))
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        before_manifest = load_manifest_from_ref(args.base, args.manifest)
        after_manifest = load_manifest_from_ref(args.head, args.manifest)
        changed_files = changed_files_between(args.base, args.head)
        diff_text = diff_text_between(args.base, args.head)
        numstat = diff_numstat_between(args.base, args.head)
        decision = evaluate_quality(
            before_manifest=before_manifest,
            after_manifest=after_manifest,
            changed_files=changed_files,
            diff_text=diff_text,
            numstat=numstat,
            pr_title=args.pr_title,
            pr_body=read_pr_body(args),
            allow_evidence_autofill=args.allow_evidence_autofill,
        )
    except (QualityInputError, json.JSONDecodeError, OSError) as exc:
        decision = QualityDecision(
            passed=False,
            reasons=[f"Quality gate could not inspect this PR: {exc}"],
            recommendation="Do not merge automatically until the quality gate can inspect the PR.",
        )

    if args.report:
        write_report(args.report, decision)

    if args.autofill_evidence_file and decision.autofill_evidence_block:
        args.autofill_evidence_file.write_text(decision.autofill_evidence_block + "\n", encoding="utf-8")

    if args.github_output:
        write_github_outputs(Path(args.github_output), decision)

    if args.json:
        print(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2))
    elif decision.passed:
        print(decision.recommendation)
    else:
        for reason in decision.reasons:
            print(f"ERROR: {reason}", file=sys.stderr)
        print(decision.recommendation, file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
