#!/usr/bin/env python3
"""Create manifest-only automation meta-task PRs from Automation Health findings."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


META_MARKER = "AUTOMATION_HEALTH_META_TASK"
DEFAULT_ALLOWED_PATHS = [
    ".github/scripts/automation-health-report.py",
    ".github/workflows/automation_health.yml",
    ".github/scripts/create-automation-meta-task-pr.py",
    "agent_tasks.json",
]
CODE_ALLOWED_PATHS = {
    "quality_failure": [
        ".github/scripts/review-autonomous-pr-quality.py",
        ".github/workflows/jules_automerge.yml",
        "agent_tasks.json",
    ],
    "quality_failures_spike": [
        ".github/scripts/review-autonomous-pr-quality.py",
        ".github/workflows/jules_automerge.yml",
        "agent_tasks.json",
    ],
    "consecutive_blocked_autonomous_prs": [
        ".github/scripts/review-autonomous-pr-quality.py",
        ".github/workflows/jules_automerge.yml",
        "agent_tasks.json",
    ],
    "duplicate_active_product_sessions": [
        ".github/scripts/jules-unattended-monitor.sh",
        ".github/workflows/jules_next_task.yml",
        ".github/workflows/jules_unattended_monitor.yml",
        "agent_tasks.json",
    ],
    "duplicate_open_autonomous_prs": [
        ".github/scripts/count-autonomous-prs.py",
        ".github/workflows/jules_next_task.yml",
        ".github/workflows/jules_unattended_monitor.yml",
        "agent_tasks.json",
    ],
    "repeated_failed_sessions_same_task": [
        ".github/scripts/summarize-jules-failures.py",
        ".github/scripts/block-failed-agent-task.py",
        ".github/workflows/jules_unattended_monitor.yml",
        "agent_tasks.json",
    ],
    "failed_session": [
        ".github/scripts/summarize-jules-failures.py",
        ".github/workflows/jules_unattended_monitor.yml",
        "agent_tasks.json",
    ],
    "failed_session_without_task_id": [
        ".github/scripts/summarize-jules-failures.py",
        ".github/scripts/summarize-jules-activities.py",
        ".github/workflows/jules_unattended_monitor.yml",
        "agent_tasks.json",
    ],
    "blocked_task_without_reason": [
        "scripts/validate_agent_tasks.py",
        "agent_tasks.json",
    ],
    "todo_below_minimum": [
        "scripts/validate_agent_tasks.py",
        "scripts/select_agent_task.py",
        "agent_tasks.json",
    ],
    "suspicious_micro_test_pr": [
        "scripts/select_agent_task.py",
        ".github/scripts/review-autonomous-pr-quality.py",
        "agent_tasks.json",
    ],
    "jules_api_unavailable": [
        ".github/scripts/automation-health-report.py",
        ".github/workflows/automation_health.yml",
        "agent_tasks.json",
    ],
    "github_api_partial_failure": [
        ".github/scripts/automation-health-report.py",
        ".github/workflows/automation_health.yml",
        "agent_tasks.json",
    ],
    "local_live_smoke_failure": [
        ".github/scripts/rdsh-local-live-smoke.sh",
        ".github/workflows/rdsh_local_live_smoke.yml",
        "agent_tasks.json",
    ],
    "agent_tasks_conflicting_open_pr": [
        ".github/scripts/open-ready-jules-prs.py",
        ".github/workflows/jules_automerge.yml",
        "agent_tasks.json",
    ],
    "repeated_followup_generation": [
        "scripts/select_agent_task.py",
        ".github/scripts/review-autonomous-pr-quality.py",
        "agent_tasks.json",
    ],
    "master_ci_failed": [
        ".github/workflows/ci.yml",
        ".github/scripts/automation-health-report.py",
        "agent_tasks.json",
    ],
}


@dataclass
class MetaTaskPlan:
    tasks: list[dict[str, Any]] = field(default_factory=list)
    skipped_hashes: list[str] = field(default_factory=list)
    reason: str = ""


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as json_file:
        data = json.load(json_file)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def dump_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned or "finding"


def stable_finding_hash(finding: dict[str, Any]) -> str:
    payload = {
        "code": finding.get("code"),
        "severity": finding.get("severity"),
        "window": finding.get("window"),
        "evidence": finding.get("evidence") or {},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def existing_finding_hashes(manifest: dict[str, Any]) -> set[str]:
    hashes: set[str] = set()
    for task in manifest.get("tasks", []):
        if not isinstance(task, dict):
            continue
        value = task.get("health_finding_hash")
        if isinstance(value, str) and value:
            hashes.add(value)
    return hashes


def summarize_evidence(evidence: dict[str, Any]) -> str:
    if not evidence:
        return "No structured evidence was attached to the finding."
    compact = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
    return compact[:600] + ("..." if len(compact) > 600 else "")


def allowed_paths_for_code(code: str) -> list[str]:
    return CODE_ALLOWED_PATHS.get(code, DEFAULT_ALLOWED_PATHS)


def make_task(finding: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    code = str(finding.get("code") or "unknown_finding")
    severity = str(finding.get("severity") or "degraded")
    window = str(finding.get("window") or "current")
    message = str(finding.get("message") or "Automation health finding requires triage.")
    finding_hash = stable_finding_hash(finding)
    task_id = f"automation-health-{slug(code)}-{finding_hash[:8]}"
    risk = "medium" if severity == "critical" else "low"

    return {
        "id": task_id,
        "status": "todo",
        "area": "automation",
        "risk": risk,
        "title": f"Fix automation health finding: {code}",
        "description": (
            f"Automation Health reported {severity} finding `{code}` in window `{window}`. "
            f"Message: {message} Evidence: {summarize_evidence(finding.get('evidence') or {})}"
        ),
        "allowed_paths": allowed_paths_for_code(code),
        "acceptance": [
            (
                f"Root cause for Automation Health finding `{code}` is identified using "
                "automation-health artifacts, CI checks, Jules session data, or manifest evidence."
            ),
            (
                "The implementation fixes the automation issue or blocks this task with a concrete "
                "blocked_reason naming missing evidence, permissions, or external API instability."
            ),
            (
                f"A follow-up Automation Health run no longer reports `{code}`, or the PR body "
                "explains why the finding is intentionally tolerated."
            ),
        ],
        "health_finding_hash": finding_hash,
        "health_finding_code": code,
        "health_finding_severity": severity,
        "health_report_generated_at": str(report.get("generated_at") or ""),
    }


def plan_meta_tasks(
    report: dict[str, Any],
    manifest: dict[str, Any],
    *,
    max_tasks: int,
) -> MetaTaskPlan:
    if not report.get("create_meta_task"):
        return MetaTaskPlan(reason="health report did not request meta-task creation")

    findings = [finding for finding in report.get("findings", []) if isinstance(finding, dict)]
    if not findings:
        return MetaTaskPlan(reason="health report has no findings")

    todo_count = sum(
        1
        for task in manifest.get("tasks", [])
        if isinstance(task, dict) and task.get("status") == "todo"
    )
    max_todo = (manifest.get("replenishment_policy") or {}).get("max_todo_tasks")
    remaining_slots = max_tasks
    if isinstance(max_todo, int):
        remaining_slots = min(remaining_slots, max(0, max_todo - todo_count))
    if remaining_slots <= 0:
        return MetaTaskPlan(reason="todo queue is already at max_todo_tasks")

    seen = existing_finding_hashes(manifest)
    planned: list[dict[str, Any]] = []
    skipped: list[str] = []
    severity_rank = {"critical": 0, "degraded": 1, "healthy": 2}
    findings.sort(
        key=lambda item: (
            severity_rank.get(str(item.get("severity") or ""), 9),
            str(item.get("code") or ""),
            stable_finding_hash(item),
        )
    )
    for finding in findings:
        finding_hash = stable_finding_hash(finding)
        if finding_hash in seen:
            skipped.append(finding_hash)
            continue
        planned.append(make_task(finding, report))
        seen.add(finding_hash)
        if len(planned) >= remaining_slots:
            break

    reason = "planned meta tasks" if planned else "all findings already have meta tasks"
    return MetaTaskPlan(tasks=planned, skipped_hashes=skipped, reason=reason)


def append_tasks(manifest: dict[str, Any], tasks: list[dict[str, Any]]) -> None:
    current = manifest.get("tasks")
    if not isinstance(current, list):
        raise ValueError("manifest tasks must be an array")
    current.extend(tasks)


def run_command(args: list[str]) -> str:
    result = subprocess.run(
        args,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def request_json(url: str, headers: dict[str, str], *, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, headers=headers, method=method, data=payload)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def open_meta_pr_exists(repo: str, api_url: str, headers: dict[str, str], branch: str) -> str:
    owner = repo.split("/", 1)[0]
    url = f"{api_url}/repos/{repo}/pulls?state=open&head={owner}:{branch}"
    pulls = request_json(url, headers)
    if isinstance(pulls, list) and pulls:
        return str(pulls[0].get("html_url") or "")
    return ""


def create_branch_commit_and_pr(
    *,
    repo: str,
    api_url: str,
    token: str,
    base: str,
    manifest_path: Path,
    tasks: list[dict[str, Any]],
) -> str:
    first_hash = str(tasks[0]["health_finding_hash"])
    branch = f"automation-health-meta-{first_hash[:8]}"
    headers = github_headers(token)
    existing = open_meta_pr_exists(repo, api_url, headers, branch)
    if existing:
        return existing

    run_command(["git", "config", "user.name", "github-actions[bot]"])
    run_command(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])
    run_command(["git", "checkout", "-B", branch])
    run_command(["git", "add", str(manifest_path)])
    run_command(["git", "commit", "-m", "Add automation health meta task"])
    run_command(["git", "push", "--force-with-lease", "origin", f"HEAD:{branch}"])

    codes = sorted({str(task.get("health_finding_code") or "") for task in tasks})
    body = {
        "title": "Add automation health meta-task",
        "head": branch,
        "base": base,
        "body": (
            f"{META_MARKER}\n\n"
            "This manifest-only PR was generated by Automation Health in enforce mode.\n\n"
            "Scope:\n"
            "- adds one or more automation meta-tasks to `agent_tasks.json`\n"
            "- does not change `JULES_LOOP_ENABLED`\n"
            "- does not modify runtime product code\n"
            "- does not modify repository variables, secrets, labels, or comments\n\n"
            f"Finding codes: {', '.join(codes)}\n"
        ),
    }
    pr = request_json(f"{api_url}/repos/{repo}/pulls", headers, method="POST", body=body)
    return str(pr.get("html_url") or "")


def write_outputs(path: str, values: dict[str, Any]) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as output_file:
        for key, value in values.items():
            output_file.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--health-report", type=Path, default=Path("automation-health.json"))
    parser.add_argument("--manifest", type=Path, default=Path("agent_tasks.json"))
    parser.add_argument("--mode", choices=["shadow", "enforce"], default="shadow")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    parser.add_argument("--base", default="master")
    parser.add_argument("--max-tasks", type=int, default=3)
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT", ""))
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = load_json(args.health_report)
        manifest = load_json(args.manifest)
        plan = plan_meta_tasks(report, manifest, max_tasks=args.max_tasks)
        pr_url = ""

        if args.mode == "enforce" and plan.tasks:
            token = os.environ.get("GITHUB_API_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
            if not token:
                raise RuntimeError("enforce mode requires GITHUB_API_TOKEN or GITHUB_TOKEN")
            append_tasks(manifest, plan.tasks)
            dump_json(args.manifest, manifest)
            run_command([sys.executable, "scripts/validate_agent_tasks.py", str(args.manifest)])
            pr_url = create_branch_commit_and_pr(
                repo=args.repo,
                api_url=args.api_url,
                token=token,
                base=args.base,
                manifest_path=args.manifest,
                tasks=plan.tasks,
            )

        result = {
            "mode": args.mode,
            "created": bool(pr_url),
            "would_create": bool(plan.tasks),
            "task_count": len(plan.tasks),
            "skipped_hashes": plan.skipped_hashes,
            "reason": plan.reason,
            "pr_url": pr_url,
        }
        write_outputs(
            args.github_output,
            {
                "created": result["created"],
                "would_create": result["would_create"],
                "task_count": result["task_count"],
                "pr_url": result["pr_url"],
                "reason": result["reason"],
            },
        )
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result["reason"])
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, urllib.error.URLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
