#!/usr/bin/env python3
"""Open a manifest-only PR with a diagnostic task for a stopped recovery loop."""

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
from pathlib import Path
from typing import Any


RECOVERY_MARKER = "AUTONOMOUS_CIRCUIT_BREAKER_FOLLOWUP_TASK"
RECOVERY_BRANCH_PREFIX = "automation-circuit-breaker-followup"
MAX_PENDING_FOLLOWUP_TASKS = 1
QUALITY_FIX_FINDING_ID = "quality_fix_circuit_breaker"
CONFLICT_RECOVERY_FINDING_ID = "conflict_recovery_circuit_breaker"
FOLLOWUP_KINDS = {
    QUALITY_FIX_FINDING_ID: {
        "task_prefix": "automation-quality-loop-pr",
        "title": "Diagnose stopped quality loop for autonomous PR",
        "failure": "quality-fix prompts did not converge",
        "evidence": "quality gate output, recovery-router ledger evidence, or CI artifacts",
        "loop": "quality-loop diagnostic",
    },
    CONFLICT_RECOVERY_FINDING_ID: {
        "task_prefix": "automation-conflict-loop-pr",
        "title": "Diagnose stopped conflict recovery for autonomous PR",
        "failure": "conflict-recovery prompts did not converge",
        "evidence": "PR comments, merge conflict state, recovery-router ledger evidence, or CI artifacts",
        "loop": "conflict-recovery diagnostic",
    },
}
RECOVERY_LABELS = {
    "automation-recovery": {
        "color": "5319e7",
        "description": "Control-plane autonomous recovery PR",
    },
    "self-improvement": {
        "color": "0e8a16",
        "description": "Automation self-improvement or recovery",
    },
}
FOLLOWUP_ALLOWED_PATHS = [
    ".github/scripts/jules-recovery-router.py",
    ".github/scripts/review-autonomous-pr-quality.py",
    ".github/scripts/update-quality-fix-comment.py",
    ".github/workflows/jules_automerge.yml",
    ".github/workflows/jules_recovery_router.yml",
    "scripts/select_agent_task.py",
    "agent_tasks.json",
]
URL_RE = re.compile(r"https?://[^\s)>\"]+")
SECRET_RE = re.compile(
    r"(?i)\b("
    r"sk-[A-Za-z0-9_-]{8,}|"
    r"ghp_[A-Za-z0-9_]{8,}|"
    r"github_pat_[A-Za-z0-9_]{8,}|"
    r"bearer\s+[A-Za-z0-9._-]{8,}|"
    r"(?:token|cookie|session|password|secret)\s*[:=]\s*[^\s,;]+"
    r")"
)


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def load_manifest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as manifest_file:
        data = json.load(manifest_file)
    if not isinstance(data, dict):
        raise ValueError("manifest root must be an object")
    return data


def write_manifest(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sanitize_reason(reason: str, *, limit: int = 500) -> str:
    cleaned = URL_RE.sub("[redacted-url]", reason)
    cleaned = SECRET_RE.sub("[redacted-secret]", cleaned)
    cleaned = "\n".join(line.rstrip() for line in cleaned.splitlines()).strip()
    return cleaned[:limit] or "circuit breaker exhausted repeated recovery attempts"


def followup_hash(
    *,
    pr_number: int,
    source_sha: str,
    source_task_id: str,
    source_finding_id: str = QUALITY_FIX_FINDING_ID,
) -> str:
    payload = {
        "pr_number": int(pr_number),
        "source_sha": source_sha,
        "source_task_id": source_task_id,
    }
    if source_finding_id != QUALITY_FIX_FINDING_ID:
        payload["source_finding_id"] = source_finding_id
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def followup_task_id(
    *,
    pr_number: int,
    source_sha: str,
    source_task_id: str,
    source_finding_id: str = QUALITY_FIX_FINDING_ID,
) -> str:
    kind = FOLLOWUP_KINDS[source_finding_id]
    digest = followup_hash(
        pr_number=pr_number,
        source_sha=source_sha,
        source_task_id=source_task_id,
        source_finding_id=source_finding_id,
    )
    return f"{kind['task_prefix']}-{int(pr_number)}-{digest[:8]}"


def existing_followup_task(manifest: dict[str, Any], *, task_id: str, digest: str) -> bool:
    for task in manifest.get("tasks", []):
        if not isinstance(task, dict):
            continue
        if task.get("id") == task_id or task.get("circuit_breaker_followup_hash") == digest:
            return True
    return False


def pending_followup_task_ids(manifest: dict[str, Any], *, source_finding_id: str) -> list[str]:
    task_ids: list[str] = []
    for task in manifest.get("tasks", []):
        if not isinstance(task, dict):
            continue
        if task.get("status") not in {"todo", "in_progress"}:
            continue
        if task.get("source_finding_id") != source_finding_id:
            continue
        task_id = str(task.get("id") or "")
        if task_id:
            task_ids.append(task_id)
    return task_ids


def make_followup_task(
    *,
    pr_number: int,
    source_sha: str,
    source_task_id: str,
    reason: str,
    source_finding_id: str = QUALITY_FIX_FINDING_ID,
) -> dict[str, Any]:
    kind = FOLLOWUP_KINDS[source_finding_id]
    digest = followup_hash(
        pr_number=pr_number,
        source_sha=source_sha,
        source_task_id=source_task_id,
        source_finding_id=source_finding_id,
    )
    task_id = followup_task_id(
        pr_number=pr_number,
        source_sha=source_sha,
        source_task_id=source_task_id,
        source_finding_id=source_finding_id,
    )
    source_scope = source_task_id or "unknown task"
    return {
        "id": task_id,
        "title": f"{kind['title']} #{int(pr_number)}",
        "area": "automation",
        "risk": "low",
        "status": "todo",
        "description": (
            f"Autonomous recovery circuit breaker stopped PR #{int(pr_number)} at SHA `{source_sha}` "
            f"for `{source_scope}`. Diagnose why repeated {kind['failure']}. "
            f"Sanitized reason: {sanitize_reason(reason)}"
        ),
        "allowed_paths": FOLLOWUP_ALLOWED_PATHS,
        "acceptance": [
            (
                f"Root cause for stopped autonomous PR #{int(pr_number)} is identified from PR comments, "
                f"{kind['evidence']}."
            ),
            (
                "The automation or manifest state is improved so the same deterministic recovery loop is "
                "not repeated, or this diagnostic task is blocked with a concrete missing-evidence reason."
            ),
            (
                "If code changes are made, targeted script/workflow tests are run; no secrets, raw "
                "transcripts, production URLs, or destructive actions are introduced."
            ),
        ],
        "source_finding_id": source_finding_id,
        "source_document": "jules-recovery-router",
        "source_reference": f"pull/{int(pr_number)}@{source_sha}",
        "source_task_id": source_task_id,
        "circuit_breaker_followup_hash": digest,
    }


def request(method: str, path: str, *, token: str, api_url: str, body: Any = None) -> Any:
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(f"{api_url}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            content = resp.read()
            if not content:
                return None
            return json.loads(content.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc


def ensure_label(*, name: str, color: str, description: str, token: str, repo: str, api_url: str) -> None:
    try:
        request(
            "POST",
            f"/repos/{repo}/labels",
            token=token,
            api_url=api_url,
            body={"name": name, "color": color, "description": description},
        )
    except RuntimeError as exc:
        if "HTTP 422" not in str(exc):
            raise


def open_followup_pr(
    *,
    manifest_path: Path,
    pr_number: int,
    source_sha: str,
    source_task_id: str,
    reason: str,
    source_finding_id: str,
    token: str,
    repo: str,
    api_url: str,
) -> int:
    digest = followup_hash(
        pr_number=pr_number,
        source_sha=source_sha,
        source_task_id=source_task_id,
        source_finding_id=source_finding_id,
    )
    kind = FOLLOWUP_KINDS[source_finding_id]
    task_id = followup_task_id(
        pr_number=pr_number,
        source_sha=source_sha,
        source_task_id=source_task_id,
        source_finding_id=source_finding_id,
    )
    branch = f"{RECOVERY_BRANCH_PREFIX}-{int(pr_number)}-{digest[:8]}"

    open_pulls = request("GET", f"/repos/{repo}/pulls?state=open&per_page=100", token=token, api_url=api_url) or []
    for pr in open_pulls:
        head_ref = (pr.get("head") or {}).get("ref", "")
        body = pr.get("body") or ""
        if head_ref == branch or (RECOVERY_MARKER in body and task_id in body):
            print(f"Open circuit-breaker follow-up PR already exists: #{pr['number']}.")
            return 0

    manifest = load_manifest(manifest_path)
    if existing_followup_task(manifest, task_id=task_id, digest=digest):
        print(f"Circuit-breaker follow-up task already exists: {task_id}.")
        return 0
    pending_followups = pending_followup_task_ids(manifest, source_finding_id=source_finding_id)
    if len(pending_followups) >= MAX_PENDING_FOLLOWUP_TASKS:
        loop_name = FOLLOWUP_KINDS[source_finding_id]["loop"]
        print(
            "Circuit-breaker follow-up task already pending: "
            + ", ".join(pending_followups[:MAX_PENDING_FOLLOWUP_TASKS])
            + f". Resolve or block it before adding another {loop_name}."
        )
        return 0

    tasks = manifest.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("manifest tasks must be an array")
    tasks.append(
        make_followup_task(
            pr_number=pr_number,
            source_sha=source_sha,
            source_task_id=source_task_id,
            reason=reason,
            source_finding_id=source_finding_id,
        )
    )
    write_manifest(manifest_path, manifest)

    run(["git", "config", "user.name", "github-actions[bot]"])
    run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])
    run(["git", "checkout", "-B", branch])
    run(["git", "add", str(manifest_path)])
    diff = run(["git", "diff", "--cached", "--quiet"], check=False)
    if diff.returncode == 0:
        print("No manifest changes to commit.")
        return 0

    run(["git", "commit", "-m", f"Add {kind['loop']} task for PR {int(pr_number)}"])
    remote_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    run(["git", "remote", "set-url", "origin", remote_url])
    run(["git", "push", "-u", "origin", branch, "--force-with-lease"])

    default_branch = os.environ.get("AUTONOMOUS_DEFAULT_BRANCH", "master")
    body = (
        f"{RECOVERY_MARKER}\n\n"
        "This manifest-only PR was generated after the Jules recovery circuit breaker stopped "
        f"a repeated {kind['loop']}.\n\n"
        "Scope:\n"
        "- adds one diagnostic automation task to `agent_tasks.json`\n"
        "- does not change runtime product code\n"
        "- does not reopen or mutate the stopped PR\n"
        "- does not expose secrets, raw transcripts, production URLs, or raw account data\n\n"
        f"Follow-up task: `{task_id}`\n"
    )
    pr = request(
        "POST",
        f"/repos/{repo}/pulls",
        token=token,
        api_url=api_url,
        body={
            "title": f"Add {kind['loop']} task for PR #{int(pr_number)}",
            "head": branch,
            "base": default_branch,
            "body": body,
        },
    )
    number = pr["number"]
    print(f"Opened circuit-breaker follow-up PR #{number}.")
    for label, meta in RECOVERY_LABELS.items():
        ensure_label(
            name=label,
            color=meta["color"],
            description=meta["description"],
            token=token,
            repo=repo,
            api_url=api_url,
        )
    request(
        "POST",
        f"/repos/{repo}/issues/{number}/labels",
        token=token,
        api_url=api_url,
        body={"labels": sorted(RECOVERY_LABELS)},
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="agent_tasks.json", type=Path)
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-task-id", default="")
    parser.add_argument(
        "--source-finding-id",
        choices=sorted(FOLLOWUP_KINDS),
        default=QUALITY_FIX_FINDING_ID,
    )
    parser.add_argument("--reason", default="quality-fix circuit breaker exhausted repeated recovery attempts")
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_API_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    if not token or not repo:
        print("GITHUB_API_TOKEN and GITHUB_REPOSITORY are required.", file=sys.stderr)
        return 2

    try:
        return open_followup_pr(
            manifest_path=args.manifest,
            pr_number=args.pr_number,
            source_sha=args.source_sha,
            source_task_id=args.source_task_id.strip(),
            reason=args.reason,
            source_finding_id=args.source_finding_id,
            token=token,
            repo=repo,
            api_url=api_url,
        )
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError):
            if exc.stdout:
                print(exc.stdout, file=sys.stderr)
            if exc.stderr:
                print(exc.stderr, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
