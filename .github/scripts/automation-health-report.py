#!/usr/bin/env python3
"""Generate a read-only health report for the autonomous Jules/Codex loop."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ACTIVE_STATES = {
    "QUEUED",
    "PLANNING",
    "IN_PROGRESS",
    "AWAITING_PLAN_APPROVAL",
    "AWAITING_USER_FEEDBACK",
}
BLOCKING_LABELS = {"needs-quality-fix", "critic-blocked"}
TRACKED_LABELS = {"needs-quality-fix", "critic-blocked", "human-review", "no-automerge"}
MICRO_KEYWORDS = (
    "add test",
    "add tests",
    "test coverage",
    "edge case",
    "handleframe",
    "missing metadata",
    "missing field",
    "malformed json",
    "trimcitationcontext",
)
EVIDENCE_TOKENS = (
    "live smoke",
    "local live smoke",
    "artifact",
    "transcript",
    "ci failure",
    "reproduced",
    "runtime failure",
    "offline reproduction",
)
SEVERITY_ORDER = {"healthy": 0, "degraded": 1, "critical": 2}


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    window: str = "current"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "evidence": self.evidence,
            "window": self.window,
        }


class ApiError(RuntimeError):
    """Raised for best-effort live API collection failures."""


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def labels_of(pr: dict[str, Any]) -> set[str]:
    labels = pr.get("labels") or []
    result: set[str] = set()
    for label in labels:
        if isinstance(label, str):
            result.add(label)
        elif isinstance(label, dict) and isinstance(label.get("name"), str):
            result.add(label["name"])
    return result


def normalize_path(path: str) -> str:
    return path.replace("\\", "/").lower()


def is_test_or_doc_or_manifest(path: str) -> bool:
    normalized = normalize_path(path)
    return (
        normalized == "agent_tasks.json"
        or normalized.startswith("docs/")
        or normalized.endswith(".md")
        or normalized.endswith("_test.go")
        or normalized.endswith(".test.ts")
        or normalized.endswith(".test.tsx")
    )


def task_ids_from_manifest(manifest: dict[str, Any]) -> list[str]:
    tasks = manifest.get("tasks") or []
    return [
        str(task.get("id"))
        for task in tasks
        if isinstance(task, dict) and isinstance(task.get("id"), str)
    ]


def task_statuses(manifest: dict[str, Any]) -> dict[str, str]:
    return {
        str(task.get("id")): str(task.get("status", ""))
        for task in manifest.get("tasks", [])
        if isinstance(task, dict) and isinstance(task.get("id"), str)
    }


def is_autonomous_pr(pr: dict[str, Any], task_ids: list[str], repo: str) -> bool:
    labels = labels_of(pr)
    body = str(pr.get("body") or "")
    title = str(pr.get("title") or "")
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    head_ref = str(head.get("ref") or "")
    head_repo_obj = head.get("repo") if isinstance(head.get("repo"), dict) else {}
    head_repo = str(head_repo_obj.get("full_name") or pr.get("head_repo") or "")

    if "jules" in labels:
        return True
    if "PR created automatically by Jules" in body or "jules.google.com/task" in body:
        return True
    if any(task_id and (task_id in title or task_id in body) for task_id in task_ids):
        return True
    if head_repo and repo and head_repo != repo:
        return False
    if head_ref.startswith(("jules-", "jules/")):
        return True
    return any(head_ref == task_id or head_ref.startswith(f"{task_id}-") for task_id in task_ids)


def pr_time(pr: dict[str, Any]) -> datetime | None:
    return (
        parse_time(str(pr.get("merged_at") or ""))
        or parse_time(str(pr.get("closed_at") or ""))
        or parse_time(str(pr.get("updated_at") or ""))
        or parse_time(str(pr.get("created_at") or ""))
    )


def is_merged_pr(pr: dict[str, Any]) -> bool:
    return bool(pr.get("merged_at") or pr.get("merged") is True)


def has_unresolved_blocking_label(pr: dict[str, Any]) -> bool:
    return bool(labels_of(pr).intersection(BLOCKING_LABELS)) and not is_merged_pr(pr)


def has_unresolved_quality_label(pr: dict[str, Any]) -> bool:
    return "needs-quality-fix" in labels_of(pr) and not is_merged_pr(pr)


def in_window(item_time: datetime | None, now: datetime, hours: int) -> bool:
    if item_time is None:
        return True
    return item_time >= now - timedelta(hours=hours)


def changed_files(pr: dict[str, Any]) -> list[str]:
    values = pr.get("changed_files")
    if values is None:
        values = pr.get("files")
    result: list[str] = []
    if isinstance(values, list):
        for item in values:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict) and isinstance(item.get("filename"), str):
                result.append(item["filename"])
    return result


def is_suspicious_micro_pr(pr: dict[str, Any]) -> bool:
    files = changed_files(pr)
    if not files or any(not is_test_or_doc_or_manifest(path) for path in files):
        return False
    text = f"{pr.get('title') or ''}\n{pr.get('body') or ''}".lower()
    if any(token in text for token in EVIDENCE_TOKENS):
        return False
    return any(keyword in text for keyword in MICRO_KEYWORDS)


def session_id(session: dict[str, Any]) -> str:
    value = str(session.get("id") or "")
    if value:
        return value
    name = str(session.get("name") or "")
    return name.rsplit("/", 1)[-1] if name else ""


def session_task_id(session: dict[str, Any], session_task_map: dict[str, Any]) -> str:
    direct = session.get("task_id") or session.get("taskId")
    if isinstance(direct, str) and direct:
        return direct
    sid = session_id(session)
    mapped = session_task_map.get(sid) if sid else None
    if isinstance(mapped, str):
        return mapped
    if isinstance(mapped, dict) and isinstance(mapped.get("task_id"), str):
        return mapped["task_id"]
    text = json.dumps(session, ensure_ascii=False)
    match = re.search(r"(?i)(?:selected\s+task\s+id|task_id)\s*[:=]\s*\"?([a-z0-9][a-z0-9_.-]{2,})", text)
    return match.group(1) if match else ""


def session_kind(session: dict[str, Any]) -> str:
    text = json.dumps(session, ensure_ascii=False).lower()
    if "autonomous_critic_review_token" in text or "critic" in str(session.get("title", "")).lower():
        return "critic"
    return str(session.get("session_kind") or session.get("sessionKind") or "product")


def status_from_findings(findings: list[Finding]) -> str:
    status = "healthy"
    for finding in findings:
        if SEVERITY_ORDER[finding.severity] > SEVERITY_ORDER[status]:
            status = finding.severity
    return status


def add_finding_once(findings: list[Finding], finding: Finding) -> None:
    key = (finding.code, finding.window, json.dumps(finding.evidence, sort_keys=True))
    existing = {
        (item.code, item.window, json.dumps(item.evidence, sort_keys=True))
        for item in findings
    }
    if key not in existing:
        findings.append(finding)


def selector_diagnostics(manifest: dict[str, Any]) -> dict[str, Any]:
    selector_path = Path(__file__).parents[2] / "scripts" / "select_agent_task.py"
    if not selector_path.exists():
        return {"available": False, "error": "scripts/select_agent_task.py not found"}

    try:
        spec = importlib.util.spec_from_file_location("automation_health_select_agent_task", selector_path)
        if spec is None or spec.loader is None:
            return {"available": False, "error": "could not load selector module"}
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        selection = module.select_task(manifest, risk_ceiling="medium", focus="proxy")
        data = selection.to_dict()
        data["available"] = True
        return data
    except Exception as exc:  # pragma: no cover - defensive live-report path
        return {"available": False, "error": str(exc)[:300]}


def analyze(data: dict[str, Any]) -> dict[str, Any]:
    now = parse_time(str(data.get("now") or "")) or datetime.now(timezone.utc)
    repo = str(data.get("repository") or os.environ.get("GITHUB_REPOSITORY") or "")
    manifest = data.get("manifest") if isinstance(data.get("manifest"), dict) else {}
    task_ids = task_ids_from_manifest(manifest)
    statuses = task_statuses(manifest)
    pulls = data.get("pulls") if isinstance(data.get("pulls"), list) else []
    workflow_runs = data.get("workflow_runs") if isinstance(data.get("workflow_runs"), list) else []
    sessions = data.get("jules_sessions") if isinstance(data.get("jules_sessions"), list) else []
    session_task_map = data.get("session_task_map") if isinstance(data.get("session_task_map"), dict) else {}
    findings: list[Finding] = []
    missing_sources = list(data.get("missing_sources") or [])

    api_errors = data.get("api_errors") if isinstance(data.get("api_errors"), dict) else {}
    if api_errors.get("github"):
        add_finding_once(
            findings,
            Finding(
                code="github_api_partial_failure",
                severity="degraded",
                message="GitHub API collection was partial.",
                evidence={"error": str(api_errors["github"])[:300]},
            ),
        )
    if api_errors.get("jules"):
        add_finding_once(
            findings,
            Finding(
                code="jules_api_unavailable",
                severity="degraded",
                message="Jules API was unavailable; report preserved available GitHub data.",
                evidence={"error": str(api_errors["jules"])[:300]},
            ),
        )
    if data.get("jules_api_missing"):
        missing_sources.append("jules_api")

    blocked_without_reason = [
        task.get("id")
        for task in manifest.get("tasks", [])
        if isinstance(task, dict)
        and task.get("status") == "blocked"
        and (not isinstance(task.get("blocked_reason"), str) or not task.get("blocked_reason", "").strip())
    ]
    if blocked_without_reason:
        add_finding_once(
            findings,
            Finding(
                code="blocked_task_without_reason",
                severity="degraded",
                message="Blocked tasks without blocked_reason are present.",
                evidence={"task_ids": blocked_without_reason},
            ),
        )

    todo_count = sum(
        1
        for task in manifest.get("tasks", [])
        if isinstance(task, dict) and task.get("status") == "todo"
    )
    minimum = ((manifest.get("replenishment_policy") or {}).get("minimum_todo_tasks"))
    if isinstance(minimum, int) and todo_count < minimum:
        add_finding_once(
            findings,
            Finding(
                code="todo_below_minimum",
                severity="degraded",
                message="Todo task count is below replenishment minimum.",
                evidence={"todo_count": todo_count, "minimum": minimum},
            ),
        )

    selector = selector_diagnostics(manifest)
    if selector.get("available") is False:
        add_finding_once(
            findings,
            Finding(
                code="selector_diagnostics_unavailable",
                severity="degraded",
                message="Task selector diagnostics could not be collected.",
                evidence={"error": str(selector.get("error") or "")[:300]},
            ),
        )
    elif (
        selector.get("selected") is False
        and selector.get("reason_code") == "no_eligible_autonomous_task"
        and int(selector.get("todo_count") or 0) > 0
    ):
        add_finding_once(
            findings,
            Finding(
                code="no_eligible_autonomous_task",
                severity="degraded",
                message="Todo tasks exist, but none are eligible under risk ceiling and micro-task policy.",
                evidence={
                    "todo_count": selector.get("todo_count"),
                    "eligible_count": selector.get("eligible_count"),
                    "rejected_count": selector.get("rejected_count"),
                    "reason": selector.get("reason"),
                    "rejected_task_ids": [
                        item.get("task_id")
                        for item in selector.get("rejected", [])
                        if isinstance(item, dict)
                    ],
                },
            ),
        )

    autonomous_pulls = [pr for pr in pulls if isinstance(pr, dict) and is_autonomous_pr(pr, task_ids, repo)]
    open_autonomous = [pr for pr in autonomous_pulls if pr.get("state") == "open"]
    autonomous_head_refs = {
        str((pr.get("head") if isinstance(pr.get("head"), dict) else {}).get("ref") or pr.get("head_ref") or "")
        for pr in autonomous_pulls
        if str((pr.get("head") if isinstance(pr.get("head"), dict) else {}).get("ref") or pr.get("head_ref") or "")
    }
    if len(open_autonomous) > 1:
        add_finding_once(
            findings,
            Finding(
                code="duplicate_open_autonomous_prs",
                severity="critical",
                message="More than one autonomous PR is open.",
                evidence={"pr_numbers": [pr.get("number") for pr in open_autonomous]},
            ),
        )

    label_counts = {label: 0 for label in TRACKED_LABELS}
    unresolved_label_counts = {label: 0 for label in TRACKED_LABELS}
    windows: dict[str, Any] = {}
    for label in TRACKED_LABELS:
        label_counts[label] = sum(1 for pr in autonomous_pulls if label in labels_of(pr))
        unresolved_label_counts[label] = sum(
            1
            for pr in autonomous_pulls
            if label in labels_of(pr) and not is_merged_pr(pr)
        )

    for hours in (24, 168):
        window_name = "24h" if hours == 24 else "7d"
        recent = [pr for pr in autonomous_pulls if in_window(pr_time(pr), now, hours)]
        needs_quality = [pr for pr in recent if has_unresolved_quality_label(pr)]
        blocked = [pr for pr in recent if has_unresolved_blocking_label(pr)]
        suspicious = [pr for pr in recent if is_suspicious_micro_pr(pr)]
        auto_continue_count = sum(int(pr.get("auto_continue_count") or 0) for pr in recent)

        if hours == 24 and len(needs_quality) >= 3:
            add_finding_once(
                findings,
                Finding(
                    code="quality_failures_spike",
                    severity="critical",
                    message="Three or more autonomous PRs were marked needs-quality-fix in 24h.",
                    evidence={"pr_numbers": [pr.get("number") for pr in needs_quality]},
                    window=window_name,
                ),
            )
        elif hours == 24 and needs_quality:
            add_finding_once(
                findings,
                Finding(
                    code="quality_failure",
                    severity="degraded",
                    message="At least one autonomous PR was marked needs-quality-fix.",
                    evidence={"pr_numbers": [pr.get("number") for pr in needs_quality]},
                    window=window_name,
                ),
            )

        recent_sorted = sorted(
            recent,
            key=lambda pr: pr_time(pr) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        first_two = recent_sorted[:2]
        if len(first_two) == 2 and all(has_unresolved_blocking_label(pr) for pr in first_two):
            add_finding_once(
                findings,
                Finding(
                    code="consecutive_blocked_autonomous_prs",
                    severity="critical",
                    message="Two latest autonomous PRs were quality/critic blocked.",
                    evidence={"pr_numbers": [pr.get("number") for pr in first_two]},
                    window=window_name,
                ),
            )

        if auto_continue_count >= 3:
            add_finding_once(
                findings,
                Finding(
                    code="frequent_auto_continue",
                    severity="degraded",
                    message="Autonomous monitor continued Jules three or more times.",
                    evidence={"count": auto_continue_count},
                    window=window_name,
                ),
            )

        if suspicious:
            add_finding_once(
                findings,
                Finding(
                    code="suspicious_micro_test_pr",
                    severity="degraded",
                    message="Autonomous PRs look like micro/test-only work without concrete evidence.",
                    evidence={"pr_numbers": [pr.get("number") for pr in suspicious]},
                    window=window_name,
                ),
            )

        windows[window_name] = {
            "autonomous_pr_count": len(recent),
            "merged_count": sum(1 for pr in recent if pr.get("merged_at") or pr.get("merged") is True),
            "open_count": sum(1 for pr in recent if pr.get("state") == "open"),
            "needs_quality_fix_count": len(needs_quality),
            "blocked_count": len(blocked),
            "suspicious_micro_pr_count": len(suspicious),
        }

    open_agent_task_conflicts = []
    for pr in open_autonomous:
        files = changed_files(pr)
        mergeable = pr.get("mergeable")
        mergeable_state = str(pr.get("mergeable_state") or "")
        if "agent_tasks.json" in files and (mergeable is False or mergeable_state in {"dirty", "unknown", "blocked"}):
            open_agent_task_conflicts.append(pr.get("number"))
    if open_agent_task_conflicts:
        add_finding_once(
            findings,
            Finding(
                code="agent_tasks_conflicting_open_pr",
                severity="degraded",
                message="Open autonomous PRs touch agent_tasks.json and appear non-mergeable.",
                evidence={"pr_numbers": open_agent_task_conflicts},
            ),
        )

    fenced_code_re = re.compile(r"```.*?```", re.DOTALL)
    inline_code_re = re.compile(r"`[^`\n]*`")
    followup_word_re = re.compile(r"(?i)(?<![A-Za-z0-9_])follow-?up(?![A-Za-z0-9_])")

    def repeated_followup_mentions(pr_body: str) -> bool:
        if "automation-health-repeated-followup" in pr_body.lower():
            return False
        without_fenced = fenced_code_re.sub(" ", pr_body)
        prose_body = inline_code_re.sub(" ", without_fenced)
        return len(followup_word_re.findall(prose_body)) >= 2

    followup_prs = [
        pr.get("number")
        for pr in autonomous_pulls
        if repeated_followup_mentions(str(pr.get("body") or ""))
    ]
    if followup_prs:
        add_finding_once(
            findings,
            Finding(
                code="repeated_followup_generation",
                severity="degraded",
                message="Autonomous PRs repeatedly mention follow-up task generation.",
                evidence={"pr_numbers": followup_prs},
            ),
        )

    latest_ci = latest_completed_workflow(workflow_runs, name="CI", branch="master")
    if latest_ci and latest_ci.get("conclusion") != "success":
        add_finding_once(
            findings,
            Finding(
                code="master_ci_failed",
                severity="critical",
                message="Latest completed CI workflow on master failed.",
                evidence={"run_id": latest_ci.get("id"), "conclusion": latest_ci.get("conclusion")},
            ),
        )
    elif latest_ci is None:
        missing_sources.append("master_ci")

    local_smoke_failures = [
        run
        for run in workflow_runs
        if str(run.get("name") or run.get("workflowName") or "") == "RDSH Local Live Smoke"
        and run.get("conclusion") == "failure"
        and (
            not autonomous_head_refs
            or str(run.get("head_branch") or run.get("branch") or "") in autonomous_head_refs
        )
    ]
    if local_smoke_failures:
        add_finding_once(
            findings,
            Finding(
                code="local_live_smoke_failure",
                severity="degraded",
                message="Local PR-code live smoke failures were observed.",
                evidence={"run_ids": [run.get("id") or run.get("databaseId") for run in local_smoke_failures]},
            ),
        )

    sessions_by_state: dict[str, int] = {}
    active_product_sessions = []
    failed_by_task: dict[str, list[str]] = {}
    failed_without_task = []
    for session in sessions:
        if not isinstance(session, dict):
            continue
        state = str(session.get("state") or "")
        sessions_by_state[state] = sessions_by_state.get(state, 0) + 1
        kind = session_kind(session)
        sid = session_id(session)
        task_id = session_task_id(session, session_task_map)
        if kind == "product" and state in ACTIVE_STATES:
            active_product_sessions.append(sid)
        if state == "FAILED":
            if task_id:
                failed_by_task.setdefault(task_id, []).append(sid)
            else:
                failed_without_task.append(sid)

    if len(active_product_sessions) > 1:
        add_finding_once(
            findings,
            Finding(
                code="duplicate_active_product_sessions",
                severity="critical",
                message="More than one active product Jules session exists.",
                evidence={"session_ids": active_product_sessions},
            ),
        )

    for task_id, ids in failed_by_task.items():
        if len(ids) >= 2 and statuses.get(task_id) != "blocked":
            add_finding_once(
                findings,
                Finding(
                    code="repeated_failed_sessions_same_task",
                    severity="critical",
                    message="A task has repeated FAILED Jules sessions and is not blocked.",
                    evidence={"task_id": task_id, "session_ids": ids},
                ),
            )
        elif ids and statuses.get(task_id) == "todo":
            add_finding_once(
                findings,
                Finding(
                    code="failed_session",
                    severity="degraded",
                    message="A Jules session failed for a todo task.",
                    evidence={"task_id": task_id, "session_ids": ids},
                ),
            )

    if failed_without_task:
        add_finding_once(
            findings,
            Finding(
                code="failed_session_without_task_id",
                severity="degraded",
                message="FAILED Jules sessions did not expose a task id.",
                evidence={"session_ids": failed_without_task},
            ),
        )

    metrics = {
        "autonomous_prs": {
            "total": len(autonomous_pulls),
            "open": len(open_autonomous),
            "merged": sum(1 for pr in autonomous_pulls if pr.get("merged_at") or pr.get("merged") is True),
            "labels": label_counts,
            "unresolved_labels": unresolved_label_counts,
        },
        "tasks": {
            "todo_count": todo_count,
            "minimum_todo_tasks": minimum,
            "eligible_count": selector.get("eligible_count"),
            "rejected_count": selector.get("rejected_count"),
            "selector_selected": selector.get("selected"),
            "selector_reason": selector.get("reason"),
            "selector_reason_code": selector.get("reason_code"),
            "blocked_without_reason_count": len(blocked_without_reason),
        },
        "jules_sessions": {
            "by_state": sessions_by_state,
            "active_product_count": len(active_product_sessions),
            "failed_by_task": {task_id: len(ids) for task_id, ids in failed_by_task.items()},
        },
        "windows": windows,
    }

    status = status_from_findings(findings)
    report = {
        "generated_at": iso_now(),
        "status": status,
        "pause_loop": False,
        "create_meta_task": status in {"degraded", "critical"},
        "read_only": True,
        "repository": repo,
        "windows": ["24h", "7d"],
        "metrics": metrics,
        "findings": [finding.to_dict() for finding in findings],
        "missing_sources": sorted(set(missing_sources)),
    }
    return report


def latest_completed_workflow(workflow_runs: list[Any], *, name: str, branch: str) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for run in workflow_runs:
        if not isinstance(run, dict):
            continue
        run_name = str(run.get("name") or run.get("workflowName") or "")
        run_branch = str(run.get("head_branch") or run.get("branch") or "")
        if run_name == name and run_branch == branch and run.get("status") == "completed":
            candidates.append(run)
    candidates.sort(
        key=lambda run: parse_time(str(run.get("updated_at") or run.get("created_at") or ""))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return candidates[0] if candidates else None


def write_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Automation Health Report",
        "",
        f"- Status: **{report['status']}**",
        f"- Repository: `{report.get('repository') or 'unknown'}`",
        f"- Generated at: `{report['generated_at']}`",
        "- Advisory pause_loop: `false`",
        f"- Advisory create_meta_task: `{str(report['create_meta_task']).lower()}`",
        "- Mode: read-only report, no repo state changed",
        "",
        "## Top Findings",
        "",
    ]
    findings = report.get("findings") or []
    if not findings:
        lines.append("- No degraded or critical findings.")
    else:
        for finding in findings[:20]:
            lines.append(
                f"- **{finding['severity']}** `{finding['code']}` ({finding['window']}): {finding['message']}"
            )

    metrics = report.get("metrics") or {}
    pr_metrics = metrics.get("autonomous_prs") or {}
    task_metrics = metrics.get("tasks") or {}
    session_metrics = metrics.get("jules_sessions") or {}
    unresolved_labels = pr_metrics.get("unresolved_labels") or {}
    lines.extend(
        [
            "",
            "## Metrics",
            "",
            (
                f"- Autonomous PRs: total `{pr_metrics.get('total', 0)}`, "
                f"open `{pr_metrics.get('open', 0)}`, merged `{pr_metrics.get('merged', 0)}`"
            ),
            f"- Unresolved needs-quality-fix PRs: `{unresolved_labels.get('needs-quality-fix', 0)}`",
            (
                f"- Todo tasks: `{task_metrics.get('todo_count', 0)}` / "
                f"minimum `{task_metrics.get('minimum_todo_tasks')}`"
            ),
            (
                f"- Eligible autonomous tasks: `{task_metrics.get('eligible_count')}`; "
                f"rejected by selector: `{task_metrics.get('rejected_count')}`"
            ),
            f"- Selector reason: `{task_metrics.get('selector_reason_code') or ''}`",
            f"- Blocked tasks without reason: `{task_metrics.get('blocked_without_reason_count', 0)}`",
            f"- Active product Jules sessions: `{session_metrics.get('active_product_count', 0)}`",
            "",
            "## Missing Sources",
            "",
        ]
    )
    missing = report.get("missing_sources") or []
    if missing:
        lines.extend(f"- `{source}`" for source in missing)
    else:
        lines.append("- None.")

    next_action = "No action required."
    if report["status"] == "critical":
        next_action = "Review critical findings and create or triage an automation meta-task; the loop remains non-stopping."
    elif report["status"] == "degraded":
        next_action = "Review degraded findings and consider a future automation meta-task."
    lines.extend(["", "## Recommended Next Action", "", next_action, ""])
    return "\n".join(lines)


def read_fixture(path: Path) -> dict[str, Any]:
    input_path = path / "input.json"
    with input_path.open("r", encoding="utf-8") as fixture_file:
        data = json.load(fixture_file)
    if not isinstance(data, dict):
        raise ValueError(f"{input_path} must contain a JSON object")
    return data


def request_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ApiError(str(exc)) from exc


def github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def collect_live(repo: str, api_url: str) -> dict[str, Any]:
    data: dict[str, Any] = {
        "now": iso_now(),
        "repository": repo,
        "missing_sources": [],
        "api_errors": {},
    }
    manifest_path = Path("agent_tasks.json")
    if manifest_path.exists():
        with manifest_path.open("r", encoding="utf-8") as manifest_file:
            data["manifest"] = json.load(manifest_file)
    else:
        data["manifest"] = {}
        data["missing_sources"].append("agent_tasks.json")

    github_token = os.environ.get("GITHUB_API_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not github_token:
        data["api_errors"]["github"] = "missing GitHub token"
    else:
        headers = github_headers(github_token)
        try:
            pulls = request_json(
                f"{api_url}/repos/{repo}/pulls?state=all&per_page=100&sort=updated&direction=desc",
                headers,
            )
            data["pulls"] = pulls if isinstance(pulls, list) else []
            open_numbers = [
                pr.get("number")
                for pr in data["pulls"]
                if isinstance(pr, dict) and pr.get("state") == "open"
            ]
            for pr in data["pulls"]:
                if not isinstance(pr, dict):
                    continue
                if pr.get("number") not in open_numbers:
                    continue
                details = request_json(f"{api_url}/repos/{repo}/pulls/{pr['number']}", headers)
                if isinstance(details, dict):
                    pr["mergeable"] = details.get("mergeable")
                    pr["mergeable_state"] = details.get("mergeable_state")
                files = request_json(f"{api_url}/repos/{repo}/pulls/{pr['number']}/files?per_page=100", headers)
                pr["changed_files"] = [
                    item.get("filename")
                    for item in files
                    if isinstance(item, dict) and isinstance(item.get("filename"), str)
                ]
        except ApiError as exc:
            data["api_errors"]["github"] = f"pull collection failed: {exc}"

        try:
            runs = request_json(f"{api_url}/repos/{repo}/actions/runs?per_page=100", headers)
            data["workflow_runs"] = runs.get("workflow_runs", []) if isinstance(runs, dict) else []
        except ApiError as exc:
            existing = data["api_errors"].get("github")
            data["api_errors"]["github"] = "; ".join(
                filter(None, [existing, f"workflow run collection failed: {exc}"])
            )

        try:
            variable = request_json(f"{api_url}/repos/{repo}/actions/variables/JULES_RECENT_SESSION_TASKS", headers)
            if isinstance(variable, dict) and isinstance(variable.get("value"), str):
                data["session_task_map"] = json.loads(variable["value"])
        except (ApiError, json.JSONDecodeError):
            pass

    jules_keys = [
        key
        for key in (
            os.environ.get("JULES_API_KEY"),
            os.environ.get("JULES_API_KEY_BACKUP"),
        )
        if key
    ]
    if not jules_keys:
        data["jules_api_missing"] = True
        return data

    source = f"sources/github/{repo}"
    errors: list[str] = []
    for key in jules_keys:
        try:
            jules_data = request_json(
                "https://jules.googleapis.com/v1alpha/sessions?pageSize=100",
                {"X-Goog-Api-Key": key},
            )
            sessions = jules_data.get("sessions", []) if isinstance(jules_data, dict) else []
            data["jules_sessions"] = [
                session
                for session in sessions
                if isinstance(session, dict)
                and ((session.get("sourceContext") or {}).get("source") == source)
            ]
            return data
        except ApiError as exc:
            errors.append(str(exc))
    data["api_errors"]["jules"] = "; ".join(errors) or "Jules API request failed"
    return data


def write_outputs(path: str, report: dict[str, Any]) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as output_file:
        output_file.write(f"status={report['status']}\n")
        output_file.write(f"pause_loop={str(report['pause_loop']).lower()}\n")
        output_file.write(f"create_meta_task={str(report['create_meta_task']).lower()}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture-dir", type=Path)
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    parser.add_argument("--output-json", default="automation-health.json", type=Path)
    parser.add_argument("--output-md", default="automation-health.md", type=Path)
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT", ""))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        data = read_fixture(args.fixture_dir) if args.fixture_dir else collect_live(args.repo, args.api_url)
        report = analyze(data)
        markdown = write_markdown(report)
        args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        args.output_md.write_text(markdown, encoding="utf-8")
        write_outputs(args.github_output, report)
        print(json.dumps({"status": report["status"], "findings": len(report["findings"])}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
