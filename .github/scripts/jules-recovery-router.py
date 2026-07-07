#!/usr/bin/env python3
"""Route stuck autonomous-loop states to one deterministic recovery action."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import jules_recovery_prompt


LEDGER_VARIABLE = "JULES_RECOVERY_ROUTER_LEDGER"
ROUTER_MARKER = "AUTONOMOUS_RECOVERY_ROUTER"
QUALITY_FIX_MARKER = "AUTONOMOUS_QUALITY_FIX_REQUEST"
ACTIVE_NEXT_TASK_COOLDOWN_MINUTES = 10
HEALTH_ENFORCE_COOLDOWN_MINUTES = 20
RERUN_AUTOMERGE_COOLDOWN_MINUTES = 120
FAILED_CHECK_RECOVERY_COOLDOWN_MINUTES = 30
MONITOR_COOLDOWN_MINUTES = 7
QUALITY_FIX_RECOVERY_COOLDOWN_MINUTES = 30
QUALITY_FIX_CIRCUIT_BREAKER_ATTEMPTS = 3
QUALITY_FIX_CIRCUIT_BREAKER_TTL_MINUTES = 7 * 24 * 60
QUALITY_FIX_FOLLOWUP_TTL_MINUTES = 7 * 24 * 60
QUALITY_FIX_CIRCUIT_BREAKER_LABELS = ("human-review", "no-automerge", "stop-loop")
AUTONOMOUS_PR_STOP_LABELS = set(QUALITY_FIX_CIRCUIT_BREAKER_LABELS)
DEFERRED_TASK_MARKER_RE = re.compile(r"\bfollow-?up\b", re.IGNORECASE)
RECOVERY_LABEL_DEFINITIONS = {
    "human-review": {
        "color": "d4c5f9",
        "description": "Needs human review before autonomous processing continues",
    },
    "no-automerge": {
        "color": "b60205",
        "description": "Do not merge automatically",
    },
    "stop-loop": {
        "color": "5319e7",
        "description": "Autonomous recovery circuit breaker stopped this loop",
    },
}
CONFLICT_RECOVERY_COOLDOWN_MINUTES = 30
CONFLICT_RECOVERY_CIRCUIT_BREAKER_ATTEMPTS = 3
CONFLICT_RECOVERY_CIRCUIT_BREAKER_TTL_MINUTES = 7 * 24 * 60
MAX_QUALITY_FIX_DETAILS_CHARS = 5000
MAX_FAILED_CHECK_ANNOTATIONS = 5
MAX_FAILED_CHECK_LOG_LINES = 24
MAX_FAILED_CHECK_LOG_EXCERPT_CHARS = 2400
MAX_FAILED_CHECK_CHANGED_FILES = 30
STALE_AWAITING_FEEDBACK_MINUTES = 10
STALE_AWAITING_FEEDBACK_COOLDOWN_MINUTES = 10
MAX_STALE_AWAITING_FEEDBACK_ESCALATIONS = 2
GITHUB_API_TRANSIENT_STATUS_CODES = {502, 503, 504}
GITHUB_API_MAX_READ_ATTEMPTS = 3
MAX_HTTP_ERROR_DETAIL_CHARS = 1200
PULL_DETAIL_MERGEABILITY_ATTEMPTS = 2
PULL_DETAIL_MERGEABILITY_DELAY_SECONDS = 1
GIT_CONFLICT_FALLBACK_DEPTH = "2000"
SESSION_ID_RE = re.compile(r"(?<!\d)(\d{12,})(?!\d)")
TASK_ID_RE = re.compile(
    r"""(?ix)
    (?:
        selected \s+ task \s+ id
        | task_id
        | "task_id"
    )
    \s* [:=] \s*
    "?
    ([a-z0-9][a-z0-9_.-]{2,})
    "?
    """
)
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
LOG_TIMESTAMP_RE = re.compile(r"^\ufeff?\d{4}-\d{2}-\d{2}T[0-9:.]+Z\s+")
FAILED_LOG_MARKERS = (
    "##[error]",
    "::error::",
    "process completed with exit code",
    "exit code",
    "failed",
    "failure",
    "protected",
    "permission denied",
)
AUTONOMOUS_CONTINUE_TOKEN = jules_recovery_prompt.AUTONOMOUS_CONTINUE_TOKEN
ACTIVE_JULES_STATES = {
    "QUEUED",
    "PLANNING",
    "IN_PROGRESS",
    "AWAITING_PLAN_APPROVAL",
    "AWAITING_USER_FEEDBACK",
}


def selector_requires_health_recovery(selector: dict[str, Any]) -> bool:
    reason_code = str(selector.get("reason_code") or "")
    reason = str(selector.get("reason") or selector.get("error") or "").lower()
    return reason_code in {"no_todo_tasks", "no_eligible_autonomous_task"} or "no eligible" in reason


def selector_below_replenishment_minimum(state: dict[str, Any]) -> tuple[bool, int, int]:
    selector = state.get("selector") or {}
    metrics = state.get("task_metrics") or {}
    minimum = metrics.get("minimum_todo_tasks")
    if not isinstance(minimum, int) or minimum <= 0:
        return False, 0, 0
    raw_todo = selector.get("todo_count", metrics.get("todo_count"))
    try:
        todo_count = int(raw_todo)
    except (TypeError, ValueError):
        todo_count = int(metrics.get("todo_count") or 0)
    return todo_count < minimum, todo_count, minimum


def maybe_health_recovery_action(
    state: dict[str, Any],
    ledger: dict[str, Any],
    *,
    now: datetime,
    health_mode: str,
    reason: str,
    dedupe_suffix: str,
) -> RecoveryAction | None:
    if health_mode == "disabled" or workflow_recently_created(
        state,
        "automation_health.yml",
        now=now,
        minutes=HEALTH_ENFORCE_COOLDOWN_MINUTES,
    ):
        return None

    workflow_mode = "enforce" if health_mode == "enforce" else "shadow"
    dedupe_key = f"automation-health-{workflow_mode}:{slug(dedupe_suffix)[:48]}"
    if action_recently_done(
        ledger,
        dedupe_key,
        now=now,
        ttl_minutes=HEALTH_ENFORCE_COOLDOWN_MINUTES,
    ):
        return None
    return RecoveryAction(
        type="dispatch_workflow",
        dedupe_key=dedupe_key,
        reason=reason,
        ttl_minutes=HEALTH_ENFORCE_COOLDOWN_MINUTES,
        payload={
            "workflow": "automation_health.yml",
            "ref": "master",
            "inputs": {"mode": workflow_mode},
        },
    )




@dataclass(frozen=True)
class RecoveryAction:
    type: str
    dedupe_key: str
    reason: str
    ttl_minutes: int
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "dedupe_key": self.dedupe_key,
            "reason": self.reason,
            "ttl_minutes": self.ttl_minutes,
            "payload": self.payload,
        }


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class GitHubClient:
    def __init__(self, *, api_url: str, repo: str, token: str):
        self.api_url = api_url.rstrip("/")
        self.repo = repo
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        ok: tuple[int, ...] = (200, 201, 204),
    ) -> Any:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(
            f"{self.api_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        max_attempts = GITHUB_API_MAX_READ_ATTEMPTS if method.upper() in {"GET", "HEAD"} else 1
        for attempt in range(1, max_attempts + 1):
            try:
                with urllib.request.urlopen(req) as resp:
                    if resp.status not in ok:
                        raise RuntimeError(f"{method} {path} returned HTTP {resp.status}")
                    raw = resp.read()
                    if not raw:
                        return None
                    return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = truncate_http_error_detail(exc.read().decode("utf-8", errors="replace"))
                if (
                    exc.code in GITHUB_API_TRANSIENT_STATUS_CODES
                    and attempt < max_attempts
                ):
                    sleep_seconds = 2 ** (attempt - 1)
                    print(
                        f"GitHub API transient HTTP {exc.code} for {method} {path}; "
                        f"retrying in {sleep_seconds}s ({attempt}/{max_attempts}).",
                        file=sys.stderr,
                    )
                    time.sleep(sleep_seconds)
                    continue
                raise RuntimeError(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc

        raise RuntimeError(f"{method} {path} failed after {max_attempts} attempts")

    def request_text(
        self,
        method: str,
        path: str,
        ok: tuple[int, ...] = (200,),
    ) -> str:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        req = urllib.request.Request(
            f"{self.api_url}{path}",
            headers=headers,
            method=method,
        )
        opener = urllib.request.build_opener(NoRedirectHandler)
        max_attempts = GITHUB_API_MAX_READ_ATTEMPTS if method.upper() in {"GET", "HEAD"} else 1
        for attempt in range(1, max_attempts + 1):
            try:
                with opener.open(req) as resp:
                    if resp.status not in ok:
                        raise RuntimeError(f"{method} {path} returned HTTP {resp.status}")
                    return resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                if exc.code in {301, 302, 303, 307, 308}:
                    location = exc.headers.get("Location")
                    if not location:
                        raise RuntimeError(f"{method} {path} redirected without Location") from exc
                    redirect_req = urllib.request.Request(location, method="GET")
                    with urllib.request.urlopen(redirect_req) as resp:
                        if resp.status not in ok:
                            raise RuntimeError(
                                f"{method} {path} redirected log returned HTTP {resp.status}"
                            )
                        return resp.read().decode("utf-8", errors="replace")
                detail = truncate_http_error_detail(exc.read().decode("utf-8", errors="replace"))
                if (
                    exc.code in GITHUB_API_TRANSIENT_STATUS_CODES
                    and attempt < max_attempts
                ):
                    sleep_seconds = 2 ** (attempt - 1)
                    print(
                        f"GitHub API transient HTTP {exc.code} for {method} {path}; "
                        f"retrying in {sleep_seconds}s ({attempt}/{max_attempts}).",
                        file=sys.stderr,
                    )
                    time.sleep(sleep_seconds)
                    continue
                raise RuntimeError(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc

        raise RuntimeError(f"{method} {path} failed after {max_attempts} attempts")


def truncate_http_error_detail(detail: str) -> str:
    detail = detail.strip()
    if len(detail) <= MAX_HTTP_ERROR_DETAIL_CHARS:
        return detail
    return detail[:MAX_HTTP_ERROR_DETAIL_CHARS].rstrip() + "\n...[truncated]"


class JulesClient:
    def __init__(self, *, api_base: str, key: str, label: str):
        self.api_base = api_base.rstrip("/")
        self.key = key
        self.label = label

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        ok: tuple[int, ...] = (200, 201, 204),
    ) -> Any:
        headers = {"X-Goog-Api-Key": self.key}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"{self.api_base}/{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req) as resp:
                if resp.status not in ok:
                    raise RuntimeError(f"{method} {path} returned HTTP {resp.status}")
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_epoch(value: str | None) -> int:
    parsed = parse_time(value)
    return int(parsed.timestamp()) if parsed else 0


def labels_of(pr: dict[str, Any]) -> set[str]:
    return {
        str(label.get("name", ""))
        for label in pr.get("labels", [])
        if isinstance(label, dict) and label.get("name")
    }


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    with manifest_path.open("r", encoding="utf-8") as manifest_file:
        data = json.load(manifest_file)
    if not isinstance(data, dict):
        raise ValueError("manifest root must be a JSON object")
    return data


def load_task_ids(manifest_path: Path) -> list[str]:
    manifest = load_manifest(manifest_path)
    return task_ids_from_manifest(manifest)


def task_ids_from_manifest(manifest: dict[str, Any]) -> list[str]:
    return [
        str(task.get("id", ""))
        for task in manifest.get("tasks", [])
        if isinstance(task, dict) and task.get("id")
    ]


def task_statuses_from_manifest(manifest: dict[str, Any]) -> dict[str, str]:
    return {
        str(task.get("id", "")): str(task.get("status", ""))
        for task in manifest.get("tasks", [])
        if isinstance(task, dict) and task.get("id")
    }


def task_metrics_from_manifest(manifest: dict[str, Any]) -> dict[str, int]:
    tasks = [task for task in manifest.get("tasks", []) if isinstance(task, dict)]
    todo_count = sum(1 for task in tasks if task.get("status") == "todo")
    policy = manifest.get("replenishment_policy") or {}
    minimum = policy.get("minimum_todo_tasks")
    max_todo = policy.get("max_todo_tasks")
    return {
        "todo_count": todo_count,
        "minimum_todo_tasks": minimum if isinstance(minimum, int) else 0,
        "max_todo_tasks": max_todo if isinstance(max_todo, int) else 0,
    }


def task_details_from_manifest(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return jules_recovery_prompt.task_details_from_manifest(manifest)


def summarize_activities(activities: list[dict[str, Any]]) -> dict[str, Any]:
    summary = jules_recovery_prompt.summarize_activities(activities)
    if summary.get("wait_reason") == "routine_question":
        summary["failure_kind"] = "routine_question"
    else:
        summary["failure_kind"] = ""
    return summary


def is_autonomous_pr(pr: dict[str, Any], *, repo: str, task_ids: list[str]) -> bool:
    head = pr.get("head") or {}
    head_ref = str(head.get("ref") or "")
    head_repo = str((head.get("repo") or {}).get("full_name") or "")
    user = str((pr.get("user") or {}).get("login") or "")
    body = str(pr.get("body") or "")

    user_lower = user.lower()
    if user == "google-jules[bot]" or ("jules" in user_lower and user_lower.endswith("[bot]")):
        return True
    if "PR created automatically by Jules" in body or "jules.google.com/task" in body:
        return True
    if head_repo != repo:
        return False
    if head_ref.startswith(("jules-", "jules/")):
        return True
    return any(
        head_ref == task_id
        or head_ref.startswith(f"{task_id}-")
        for task_id in task_ids
    )


def extract_session_id_from_pr(pr: dict[str, Any]) -> str:
    fields = [
        str((pr.get("head") or {}).get("ref") or ""),
        str(pr.get("title") or ""),
        str(pr.get("body") or ""),
    ]
    for field in fields:
        match = SESSION_ID_RE.search(field)
        if match:
            return match.group(1)
    return ""


def comments_contain(pr: dict[str, Any], marker: str) -> bool:
    for comment in pr.get("comments", []):
        if marker in str(comment.get("body") or ""):
            return True
    return False


def has_autonomous_stop_label(pr: dict[str, Any]) -> bool:
    return bool(labels_of(pr) & AUTONOMOUS_PR_STOP_LABELS)


def task_id_from_pr(pr: dict[str, Any], task_ids: list[str]) -> str:
    fields = [
        str(pr.get("body") or ""),
        str(pr.get("title") or ""),
        str((pr.get("head") or {}).get("ref") or ""),
    ]
    for field in fields:
        match = TASK_ID_RE.search(field)
        if match:
            return match.group(1)
    for task_id in task_ids:
        if any(task_id and task_id in field for field in fields):
            return task_id
    return ""


def stopped_task_ids_from_prs(open_pulls: list[dict[str, Any]], task_ids: list[str]) -> list[str]:
    excluded: list[str] = []
    seen: set[str] = set()
    for pr in open_pulls:
        if not has_autonomous_stop_label(pr):
            continue
        task_id = task_id_from_pr(pr, task_ids)
        if not task_id or task_id in seen:
            continue
        seen.add(task_id)
        excluded.append(task_id)
    return excluded


def has_computed_mergeability(pr: dict[str, Any]) -> bool:
    mergeable_state = str(pr.get("mergeable_state") or "").lower()
    return pr.get("mergeable") is not None and mergeable_state not in {"", "unknown"}


def enrich_open_pull_details(client: GitHubClient, open_pulls: list[dict[str, Any]]) -> None:
    detail_fields = (
        "title",
        "body",
        "labels",
        "user",
        "head",
        "base",
        "draft",
        "mergeable",
        "mergeable_state",
        "updated_at",
    )
    for pr in open_pulls:
        number = pr.get("number")
        if not number:
            continue
        details: dict[str, Any] = {}
        for attempt in range(1, PULL_DETAIL_MERGEABILITY_ATTEMPTS + 1):
            details = client.request("GET", f"/repos/{client.repo}/pulls/{number}") or {}
            if has_computed_mergeability(details) or attempt == PULL_DETAIL_MERGEABILITY_ATTEMPTS:
                break
            time.sleep(PULL_DETAIL_MERGEABILITY_DELAY_SECONDS)
        for field in detail_fields:
            if field in details:
                pr[field] = details[field]


def enrich_open_pull_git_conflicts(open_pulls: list[dict[str, Any]], *, repo: str) -> None:
    for pr in open_pulls:
        if has_computed_mergeability(pr):
            continue
        head = pr.get("head") or {}
        head_ref = str(head.get("ref") or "")
        head_repo = str((head.get("repo") or {}).get("full_name") or "")
        if not head_ref or head_repo != repo:
            continue

        remote_ref = f"refs/remotes/origin/{head_ref}"
        fetch = subprocess.run(
            [
                "git",
                "fetch",
                "--no-tags",
                f"--depth={GIT_CONFLICT_FALLBACK_DEPTH}",
                "origin",
                "master:refs/remotes/origin/master",
                f"{head_ref}:{remote_ref}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if fetch.returncode != 0:
            print(
                f"Could not fetch PR branch {head_ref} for conflict fallback: "
                f"{(fetch.stderr or fetch.stdout).strip()}",
                file=sys.stderr,
            )
            continue

        merge = subprocess.run(
            ["git", "merge-tree", "--write-tree", "origin/master", remote_ref],
            check=False,
            capture_output=True,
            text=True,
        )
        merge_output = f"{merge.stdout}\n{merge.stderr}"
        if merge.returncode != 0 and "unrelated histories" in merge_output:
            subprocess.run(
                ["git", "fetch", "--no-tags", "--unshallow", "origin"],
                check=False,
                capture_output=True,
                text=True,
            )
            fetch_full = subprocess.run(
                [
                    "git",
                    "fetch",
                    "--no-tags",
                    "origin",
                    "master:refs/remotes/origin/master",
                    f"{head_ref}:{remote_ref}",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if fetch_full.returncode == 0:
                merge = subprocess.run(
                    ["git", "merge-tree", "--write-tree", "origin/master", remote_ref],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                merge_output = f"{merge.stdout}\n{merge.stderr}"
        if merge.returncode != 0 and "CONFLICT" in merge_output:
            pr["mergeable"] = False
            pr["mergeable_state"] = "dirty"
            pr["mergeability_source"] = "git-merge-tree"
        elif merge.returncode != 0:
            print(
                f"Could not compute git conflict fallback for PR branch {head_ref}: "
                f"{merge_output.strip()}",
                file=sys.stderr,
            )


def action_recently_done(
    ledger: dict[str, Any],
    dedupe_key: str,
    *,
    now: datetime,
    ttl_minutes: int,
) -> bool:
    entries = ledger.get("actions") or {}
    entry = entries.get(dedupe_key) if isinstance(entries, dict) else None
    if not isinstance(entry, dict):
        return False
    timestamp = parse_time(str(entry.get("time") or ""))
    if timestamp is None:
        return False
    return now - timestamp < timedelta(minutes=ttl_minutes)


def action_time(ledger: dict[str, Any], dedupe_key: str) -> datetime | None:
    entries = ledger.get("actions") or {}
    entry = entries.get(dedupe_key) if isinstance(entries, dict) else None
    if not isinstance(entry, dict):
        return None
    return parse_time(str(entry.get("time") or ""))


def action_times_with_prefix(
    ledger: dict[str, Any],
    prefix: str,
    *,
    now: datetime,
    ttl_minutes: int,
) -> list[datetime]:
    entries = ledger.get("actions") or {}
    if not isinstance(entries, dict):
        return []

    cutoff = now - timedelta(minutes=ttl_minutes)
    times: list[datetime] = []
    for key, entry in entries.items():
        if not str(key).startswith(prefix) or not isinstance(entry, dict):
            continue
        timestamp = parse_time(str(entry.get("time") or ""))
        if timestamp and timestamp >= cutoff:
            times.append(timestamp)
    return sorted(times)


def workflow_recently_created(
    state: dict[str, Any],
    workflow: str,
    *,
    now: datetime,
    minutes: int,
    event: str | None = None,
) -> bool:
    for run in (state.get("workflow_runs") or {}).get(workflow, []):
        if event and run.get("event") != event:
            continue
        created = parse_time(str(run.get("created_at") or ""))
        if created and now - created < timedelta(minutes=minutes):
            return True
    return False


def workflow_in_progress(state: dict[str, Any], workflow: str) -> bool:
    return any(
        run.get("status") in {"queued", "pending", "in_progress", "waiting"}
        for run in (state.get("workflow_runs") or {}).get(workflow, [])
    )


def check_run_run_id(check_run: dict[str, Any]) -> str:
    if check_run.get("run_id"):
        return str(check_run["run_id"])
    details_url = str(check_run.get("details_url") or check_run.get("html_url") or "")
    match = re.search(r"/actions/runs/(\d+)", details_url)
    return match.group(1) if match else ""


def check_run_job_id(check_run: dict[str, Any]) -> str:
    details_url = str(check_run.get("details_url") or check_run.get("html_url") or "")
    match = re.search(r"/actions/runs/\d+/job/(\d+)", details_url)
    if match:
        return match.group(1)
    if check_run.get("id"):
        return str(check_run["id"])
    return ""


def github_api_path(client: GitHubClient, url_or_path: str) -> str:
    value = str(url_or_path or "")
    if not value:
        return ""
    if value.startswith("/"):
        return value
    if value.startswith(client.api_url):
        return value.removeprefix(client.api_url)
    parsed = urllib.parse.urlparse(value)
    if parsed.netloc != urllib.parse.urlparse(client.api_url).netloc:
        return ""
    path = parsed.path
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return path


def add_query_param(path: str, key: str, value: str) -> str:
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{urllib.parse.quote(key)}={urllib.parse.quote(value)}"


def sanitize_changed_files(files: list[Any]) -> list[str]:
    sanitized: list[str] = []
    seen: set[str] = set()
    for item in files:
        if isinstance(item, dict):
            name = str(item.get("filename") or "")
        else:
            name = str(item or "")
        clean = jules_recovery_prompt.sanitize_text(name, limit=220)
        if not clean or clean in seen:
            continue
        seen.add(clean)
        sanitized.append(clean)
        if len(sanitized) >= MAX_FAILED_CHECK_CHANGED_FILES:
            break
    return sanitized


def annotation_excerpt(annotation: dict[str, Any]) -> str:
    path = str(annotation.get("path") or "")
    line = str(annotation.get("start_line") or "")
    message = str(annotation.get("message") or annotation.get("title") or "")
    location = f"{path}:{line}" if path and line else path
    raw = f"{location}: {message}" if location else message
    return jules_recovery_prompt.sanitize_text(raw, limit=360)


def clean_log_line(line: str) -> str:
    line = ANSI_ESCAPE_RE.sub("", line)
    line = LOG_TIMESTAMP_RE.sub("", line)
    return line.strip()


def failure_log_excerpt(log_text: str) -> str:
    raw_lines = [clean_log_line(line) for line in str(log_text or "").splitlines()]
    raw_lines = [line for line in raw_lines if line]
    if not raw_lines:
        return ""

    hit_indexes: list[int] = []
    for index, line in enumerate(raw_lines):
        lower = line.lower()
        if any(marker in lower for marker in FAILED_LOG_MARKERS):
            hit_indexes.append(index)

    selected_indexes: list[int] = []
    if hit_indexes:
        for index in hit_indexes[:4]:
            start = max(0, index - 10)
            end = min(len(raw_lines), index + 3)
            selected_indexes.extend(range(start, end))
    else:
        selected_indexes = list(range(max(0, len(raw_lines) - MAX_FAILED_CHECK_LOG_LINES), len(raw_lines)))

    selected_lines: list[str] = []
    seen: set[str] = set()
    for index in selected_indexes:
        line = raw_lines[index]
        if line in seen:
            continue
        seen.add(line)
        selected_lines.append(jules_recovery_prompt.sanitize_text(line, limit=320))
        if len(selected_lines) >= MAX_FAILED_CHECK_LOG_LINES:
            break

    excerpt = "\n".join(line for line in selected_lines if line)
    if len(excerpt) > MAX_FAILED_CHECK_LOG_EXCERPT_CHARS:
        excerpt = excerpt[:MAX_FAILED_CHECK_LOG_EXCERPT_CHARS].rstrip() + "\n...[truncated]"
    return excerpt


def enrich_failed_check_evidence(client: GitHubClient, pr: dict[str, Any]) -> None:
    number = pr.get("number")
    if number and "changed_files" not in pr:
        try:
            files = client.request(
                "GET",
                f"/repos/{client.repo}/pulls/{number}/files?per_page=100",
            ) or []
            pr["changed_files"] = sanitize_changed_files(files if isinstance(files, list) else [])
        except RuntimeError as exc:
            pr["changed_files_error"] = jules_recovery_prompt.sanitize_text(str(exc), limit=260)

    for check_run in failed_check_runs(pr)[:8]:
        output = check_run.get("output") if isinstance(check_run.get("output"), dict) else {}
        annotations_url = str((output or {}).get("annotations_url") or "")
        if annotations_url and "annotations" not in check_run:
            path = github_api_path(client, annotations_url)
            if path:
                path = add_query_param(path, "per_page", str(MAX_FAILED_CHECK_ANNOTATIONS))
                try:
                    annotations = client.request("GET", path) or []
                    if isinstance(annotations, list):
                        check_run["annotations"] = [
                            excerpt
                            for annotation in annotations[:MAX_FAILED_CHECK_ANNOTATIONS]
                            if isinstance(annotation, dict)
                            for excerpt in [annotation_excerpt(annotation)]
                            if excerpt
                        ]
                except RuntimeError as exc:
                    check_run["annotations_error"] = jules_recovery_prompt.sanitize_text(str(exc), limit=260)

        job_id = check_run_job_id(check_run)
        if job_id and "log_excerpt" not in check_run:
            try:
                log_text = client.request_text(
                    "GET",
                    f"/repos/{client.repo}/actions/jobs/{job_id}/logs",
                )
                excerpt = failure_log_excerpt(log_text)
                if excerpt:
                    check_run["log_excerpt"] = excerpt
            except RuntimeError as exc:
                check_run["log_excerpt_error"] = jules_recovery_prompt.sanitize_text(str(exc), limit=260)


def latest_failed_automerge_run(pr: dict[str, Any]) -> str:
    for check_run in pr.get("check_runs", []):
        name = str(check_run.get("name") or "")
        conclusion = str(check_run.get("conclusion") or "")
        if name in {"test-and-merge", "validate-and-merge"} and conclusion == "failure":
            return check_run_run_id(check_run)
    return ""


def failed_check_runs(pr: dict[str, Any]) -> list[dict[str, Any]]:
    failed_conclusions = {"failure", "timed_out", "action_required", "startup_failure"}
    return [
        check_run
        for check_run in pr.get("check_runs", [])
        if str(check_run.get("status") or "") == "completed"
        and str(check_run.get("conclusion") or "") in failed_conclusions
    ]


def check_run_display_name(check_run: dict[str, Any]) -> str:
    workflow = str(check_run.get("workflowName") or check_run.get("workflow_name") or "")
    name = str(check_run.get("name") or "unknown")
    return f"{workflow} / {name}" if workflow else name


def failed_check_fingerprint(pr: dict[str, Any]) -> str:
    parts: list[str] = []
    for check_run in failed_check_runs(pr):
        name = check_run_display_name(check_run)
        run_id = check_run_run_id(check_run)
        conclusion = str(check_run.get("conclusion") or "")
        parts.append(f"{name}:{run_id}:{conclusion}")
    return hashlib.sha256("|".join(sorted(parts)).encode("utf-8")).hexdigest()[:16]


def has_pending_checks(pr: dict[str, Any]) -> bool:
    return any(
        str(check_run.get("status") or "") != "completed"
        for check_run in pr.get("check_runs", [])
    )


def is_conflicting_pr(pr: dict[str, Any]) -> bool:
    mergeable_state = str(pr.get("mergeable_state") or "").lower()
    return pr.get("mergeable") is False or mergeable_state == "dirty"


def latest_quality_fix_details(pr: dict[str, Any]) -> str:
    for comment in reversed(pr.get("comments", [])):
        body = str(comment.get("body") or "")
        if not body.lstrip().startswith(f"<!-- {QUALITY_FIX_MARKER}"):
            continue

        starts = [
            body.find("История последних failed SHA/reasons:"),
            body.find("# Autonomous PR quality gate"),
        ]
        starts = [index for index in starts if index >= 0]
        details = body[min(starts):] if starts else body
        details = details.strip()
        if len(details) > MAX_QUALITY_FIX_DETAILS_CHARS:
            details = details[:MAX_QUALITY_FIX_DETAILS_CHARS].rstrip() + "\n...[truncated]"
        return sanitize_quality_fix_text(details)
    return ""


def sanitize_quality_fix_text(value: str) -> str:
    return DEFERRED_TASK_MARKER_RE.sub("[deferred-task marker]", value)


def quality_fix_recovery_attempt_shas(pr: dict[str, Any], ledger: dict[str, Any]) -> list[str]:
    number = str(pr.get("number") or "")
    shas: list[str] = []
    seen: set[str] = set()

    def add_sha(value: str) -> None:
        sha = value.strip()
        if not sha or sha in seen:
            return
        seen.add(sha)
        shas.append(sha)

    marker_pattern = re.compile(
        rf"{re.escape(ROUTER_MARKER)}\s+action=quality-fix\s+sha=([a-zA-Z0-9._/-]+)"
    )
    for comment in pr.get("comments", []):
        body = str(comment.get("body") or "")
        for match in marker_pattern.finditer(body):
            add_sha(match.group(1))

    prefix = f"quality-fix:{number}:"
    for key, entry in (ledger.get("actions") or {}).items():
        if not isinstance(entry, dict) or entry.get("type") != "quality_fix_recovery":
            continue
        key_text = str(key)
        if key_text.startswith(prefix):
            add_sha(key_text.removeprefix(prefix))

    return shas


def conflict_recovery_attempt_shas(pr: dict[str, Any], ledger: dict[str, Any]) -> list[str]:
    number = str(pr.get("number") or "")
    shas: list[str] = []
    seen: set[str] = set()

    def add_sha(value: str) -> None:
        sha = value.strip()
        if not sha or sha in seen:
            return
        seen.add(sha)
        shas.append(sha)

    marker_pattern = re.compile(
        rf"{re.escape(ROUTER_MARKER)}\s+action=conflict-recovery\s+sha=([a-zA-Z0-9._/-]+)"
    )
    for comment in pr.get("comments", []):
        body = str(comment.get("body") or "")
        for match in marker_pattern.finditer(body):
            add_sha(match.group(1))

    prefix = f"conflict-recovery:{number}:"
    for key, entry in (ledger.get("actions") or {}).items():
        if not isinstance(entry, dict) or entry.get("type") != "conflict_recovery":
            continue
        key_text = str(key)
        if key_text.startswith(prefix):
            add_sha(key_text.removeprefix(prefix))

    return shas


def quality_fix_circuit_breaker_marker(pr: dict[str, Any]) -> str:
    sha = (pr.get("head") or {}).get("sha") or ""
    return f"{ROUTER_MARKER} action=quality-fix-circuit-breaker sha={sha}"


def has_quality_fix_circuit_breaker_marker(pr: dict[str, Any]) -> bool:
    return comments_contain(pr, f"{ROUTER_MARKER} action=quality-fix-circuit-breaker")


def conflict_recovery_circuit_breaker_marker(pr: dict[str, Any]) -> str:
    sha = (pr.get("head") or {}).get("sha") or ""
    return f"{ROUTER_MARKER} action=conflict-recovery-circuit-breaker sha={sha}"


def has_conflict_recovery_circuit_breaker_marker(pr: dict[str, Any]) -> bool:
    return comments_contain(pr, f"{ROUTER_MARKER} action=conflict-recovery-circuit-breaker")


def conflict_recovery_circuit_breaker_comment(
    pr: dict[str, Any],
    *,
    attempt_shas: list[str],
) -> str:
    number = pr.get("number")
    sha = (pr.get("head") or {}).get("sha") or ""
    marker = f"<!-- {conflict_recovery_circuit_breaker_marker(pr)} -->"
    attempt_list = ", ".join(attempt_shas[-5:]) if attempt_shas else "unknown"
    labels = ", ".join(f"`{label}`" for label in QUALITY_FIX_CIRCUIT_BREAKER_LABELS)
    return (
        f"{marker}\n\n"
        f"Autonomous recovery circuit breaker остановил conflict recovery для PR #{number} "
        f"на SHA `{sha}`.\n\n"
        "Причина: PR остаётся конфликтным после нескольких Jules recovery попыток. "
        "Обычные `pull_request` checks не стартуют для конфликтного PR, поэтому повторять "
        "тот же conflict prompt дальше бесполезно.\n\n"
        f"Router добавляет labels {labels} и больше не будет отправлять Jules повторный "
        "conflict-recovery prompt по этому PR. Петля может продолжить следующую автономную "
        "задачу; этот PR требует ручного закрытия, rebase/resolve вне петли или отдельной "
        "manifest-only diagnostic задачи.\n\n"
        f"Учтённые conflict-recovery SHA: {attempt_list}."
    )


def quality_fix_followup_hash(pr: dict[str, Any], task_ids: list[str]) -> str:
    payload = {
        "pr_number": int(pr.get("number") or 0),
        "source_sha": str((pr.get("head") or {}).get("sha") or ""),
        "source_task_id": task_id_from_pr(pr, task_ids),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def quality_fix_followup_task_id(pr: dict[str, Any], task_ids: list[str]) -> str:
    number = int(pr.get("number") or 0)
    return f"automation-quality-loop-pr-{number}-{quality_fix_followup_hash(pr, task_ids)[:8]}"


def conflict_recovery_followup_hash(pr: dict[str, Any], task_ids: list[str]) -> str:
    payload = {
        "pr_number": int(pr.get("number") or 0),
        "source_sha": str((pr.get("head") or {}).get("sha") or ""),
        "source_task_id": task_id_from_pr(pr, task_ids),
        "source_finding_id": "conflict_recovery_circuit_breaker",
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def conflict_recovery_followup_task_id(pr: dict[str, Any], task_ids: list[str]) -> str:
    number = int(pr.get("number") or 0)
    return f"automation-conflict-loop-pr-{number}-{conflict_recovery_followup_hash(pr, task_ids)[:8]}"


def quality_fix_followup_exists(state: dict[str, Any], task_id: str) -> bool:
    return task_id in (state.get("task_details") or {})


def quality_fix_circuit_breaker_comment(
    pr: dict[str, Any],
    *,
    attempt_shas: list[str],
) -> str:
    number = pr.get("number")
    sha = (pr.get("head") or {}).get("sha") or ""
    marker = f"<!-- {quality_fix_circuit_breaker_marker(pr)} -->"
    details = latest_quality_fix_details(pr)
    attempt_list = ", ".join(attempt_shas[-5:]) if attempt_shas else "unknown"
    details_block = f"\n\nПоследний quality gate details:\n\n{details}" if details else ""
    labels = ", ".join(f"`{label}`" for label in QUALITY_FIX_CIRCUIT_BREAKER_LABELS)
    return (
        f"{marker}\n\n"
        f"Autonomous recovery circuit breaker остановил PR #{number} на SHA `{sha}`.\n\n"
        f"Причина: PR уже получил {len(attempt_shas)} quality-fix recovery попытки "
        "и продолжает возвращаться в тот же failure loop.\n\n"
        f"Router добавляет labels {labels} и больше не будет отправлять Jules "
        "повторный quality-fix prompt по этому PR. Следующая автономная задача может продолжаться, "
        "а этот PR требует отдельного human review или закрытия/ручной правки.\n\n"
        f"Учтённые recovery SHA: {attempt_list}."
        f"{details_block}"
    )


def quality_fix_prompt(pr: dict[str, Any]) -> str:
    number = pr.get("number")
    sha = (pr.get("head") or {}).get("sha") or ""
    marker = f"<!-- {ROUTER_MARKER} action=quality-fix sha={sha} -->"
    details = latest_quality_fix_details(pr)
    details_block = ""
    if details:
        details_block = (
            "\n\nДетали текущего quality gate failure ниже. Используй их как source of truth "
            "для исправления этого же PR:\n\n"
            f"{details}"
        )
    return (
        f"{marker}\n\n"
        f"Jules, исправь этот же PR #{number}; не открывай новый PR и не создавай отдельную задачу на потом.\n\n"
        "Что нужно сделать:\n"
        "- исправь deterministic autonomous quality gate failure;\n"
        "- синхронизируй AUTONOMOUS_TASK_EVIDENCE с фактическим статусом задачи в agent_tasks.json;\n"
        "- если задача в manifest имеет status blocked, evidence тоже должен иметь status: blocked и concrete blocked_reason;\n"
        "- если задача реально выполнена, manifest должен быть status done и evidence status done с changed evidence files;\n"
        "- убери временные scratch-файлы из PR, если они не являются частью acceptance/evidence;\n"
        "- push исправление в эту же PR ветку и дождись повторных checks.\n\n"
        "Не жди ответа пользователя, если действие безопасное и находится внутри scope текущей задачи."
        f"{details_block}"
    )


def conflict_recovery_prompt(pr: dict[str, Any]) -> str:
    number = pr.get("number")
    sha = (pr.get("head") or {}).get("sha") or ""
    marker = f"<!-- {ROUTER_MARKER} action=conflict-recovery sha={sha} -->"
    details = latest_quality_fix_details(pr)
    details_block = ""
    if details:
        details_block = (
            "\n\nУ PR также есть unresolved quality gate details. После синхронизации ветки "
            "исправь их в этом же PR:\n\n"
            f"{details}"
        )
    return (
        f"{marker}\n\n"
        f"Jules, PR #{number} конфликтует с текущим `master`; исправь эту же PR ветку и не открывай новый PR.\n\n"
        "Что нужно сделать:\n"
        "- синхронизируй PR branch с последним `master` (выполни `git fetch origin master` и `git merge origin/master`);\n"
        "- resolve merge conflicts внутри scope текущей задачи и allowed_paths;\n"
        "- при разрешении конфликтов в agent_tasks.json сохрани актуальную очередь из master и примени только изменение статуса текущей задачи (plus concrete newly discovered tasks, если есть);\n"
        "- сохрани один task status/evidence в agent_tasks.json и AUTONOMOUS_TASK_EVIDENCE;\n"
        "- если конфликт нельзя безопасно решить внутри scope, отметь текущую задачу `blocked` с concrete blocked_reason;\n"
        "- запусти релевантные проверки и push исправление в эту же PR ветку.\n\n"
        "Не жди ответа пользователя, если действие безопасное и не требует секретов, production-доступа или destructive changes."
        f"{details_block}"
    )


def changed_files_block(pr: dict[str, Any]) -> str:
    changed_files = sanitize_changed_files(list(pr.get("changed_files") or []))
    if not changed_files:
        return ""
    lines = "\n".join(f"- `{name}`" for name in changed_files)
    return f"\n\nChanged files from this PR:\n{lines}"


def failed_check_detail_lines(check_run: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    annotations = check_run.get("annotations") or []
    if isinstance(annotations, list):
        for annotation in annotations[:MAX_FAILED_CHECK_ANNOTATIONS]:
            text = jules_recovery_prompt.sanitize_text(str(annotation), limit=360)
            if text:
                lines.append(f"  annotation: {text}")

    log_excerpt = str(check_run.get("log_excerpt") or "")
    if log_excerpt:
        lines.append("  log_excerpt:")
        for line in log_excerpt.splitlines()[:MAX_FAILED_CHECK_LOG_LINES]:
            text = jules_recovery_prompt.sanitize_text(line, limit=320)
            if text:
                lines.append(f"    {text}")
    return lines


def failed_check_recovery_prompt(pr: dict[str, Any]) -> str:
    number = pr.get("number")
    sha = (pr.get("head") or {}).get("sha") or ""
    marker = f"<!-- {ROUTER_MARKER} action=failed-check sha={sha} -->"
    check_lines: list[str] = []
    for check_run in failed_check_runs(pr)[:8]:
        name = check_run_display_name(check_run)
        conclusion = str(check_run.get("conclusion") or "failure")
        url = str(check_run.get("details_url") or check_run.get("html_url") or "")
        run_id = check_run_run_id(check_run)
        detail = f"details: {url}" if url else f"run_id: {run_id}" if run_id else "details: unavailable"
        check_lines.append(f"- `{name}`: {conclusion}; {detail}")
        check_lines.extend(failed_check_detail_lines(check_run))
    checks_block = "\n".join(check_lines) if check_lines else "- failed check details unavailable"

    return (
        f"{marker}\n\n"
        f"Jules, PR #{number} has failed GitHub Actions checks on SHA `{sha}`. "
        "Исправь этот же PR; не открывай новый PR и не создавай отдельную задачу на потом.\n\n"
        "Failed checks:\n"
        f"{checks_block}\n\n"
        f"{changed_files_block(pr)}\n\n"
        "Что нужно сделать:\n"
        "- используй failed check annotations/log_excerpt/changed files выше как первичный recovery packet;\n"
        "- если этих excerpts недостаточно, открой/read linked job logs и артефакты failed checks;\n"
        "- если лог не содержит конкретных файлов, воспроизведи failing command локально в PR branch;\n"
        "- для Go formatting failures выполни `files=\"$(gofmt -l .)\"; if [ -n \"$files\" ]; then printf 'gofmt required for:\\n%s\\n' \"$files\"; fi`;\n"
        "- исправь причину failure внутри scope текущей задачи и allowed_paths;\n"
        "- обнови AUTONOMOUS_TASK_EVIDENCE/agent_tasks.json только если статус задачи реально изменился;\n"
        "- push исправление в эту же PR ветку и дождись повторных checks;\n"
        "- если failure требует секрета, production-доступа или действия вне scope, отметь задачу `blocked` с concrete blocked_reason.\n\n"
        "Не жди ответа пользователя, если исправление безопасное и находится внутри scope."
    )


def failed_check_prompt_context(pr: dict[str, Any], *, repo: str) -> dict[str, Any]:
    failed_checks: list[dict[str, Any]] = []
    for check_run in failed_check_runs(pr)[:8]:
        item: dict[str, Any] = {
            "name": jules_recovery_prompt.sanitize_text(check_run_display_name(check_run), limit=180),
            "conclusion": jules_recovery_prompt.sanitize_text(
                str(check_run.get("conclusion") or "failure"),
                limit=80,
            ),
            "run_id": jules_recovery_prompt.sanitize_text(check_run_run_id(check_run), limit=80),
            "details_url": jules_recovery_prompt.sanitize_text(
                str(check_run.get("details_url") or check_run.get("html_url") or ""),
                limit=240,
            ),
        }
        annotations = check_run.get("annotations") or []
        if isinstance(annotations, list) and annotations:
            item["annotations"] = [
                jules_recovery_prompt.sanitize_text(str(annotation), limit=360)
                for annotation in annotations[:MAX_FAILED_CHECK_ANNOTATIONS]
                if str(annotation).strip()
            ]
        log_excerpt = str(check_run.get("log_excerpt") or "")
        if log_excerpt:
            item["log_excerpt"] = failure_log_excerpt(log_excerpt) or jules_recovery_prompt.sanitize_text(
                log_excerpt,
                limit=MAX_FAILED_CHECK_LOG_EXCERPT_CHARS,
            )
        if check_run.get("annotations_error"):
            item["annotations_error"] = jules_recovery_prompt.sanitize_text(
                str(check_run.get("annotations_error")),
                limit=260,
            )
        if check_run.get("log_excerpt_error"):
            item["log_excerpt_error"] = jules_recovery_prompt.sanitize_text(
                str(check_run.get("log_excerpt_error")),
                limit=260,
            )
        failed_checks.append(item)
    if not failed_checks:
        return {}
    return {
        "repo": jules_recovery_prompt.sanitize_text(repo, limit=180),
        "pr_number": jules_recovery_prompt.sanitize_text(f"#{pr.get('number')}", limit=80),
        "head_sha": jules_recovery_prompt.sanitize_text(str((pr.get("head") or {}).get("sha") or ""), limit=80),
        "changed_files": sanitize_changed_files(list(pr.get("changed_files") or [])),
        "failed_checks": failed_checks,
    }


def associated_pr_for_session(
    session: dict[str, Any],
    open_prs: list[dict[str, Any]],
    task_ids: list[str],
) -> dict[str, Any] | None:
    session_id = str(session.get("session_id") or "")
    summary = session.get("activity_summary") or {}
    task_id = str(session.get("task_id") or summary.get("task_id") or "")

    if session_id:
        for pr in open_prs:
            if extract_session_id_from_pr(pr) == session_id:
                return pr
    if task_id:
        for pr in open_prs:
            if task_id_from_pr(pr, task_ids) == task_id:
                return pr
    return None


def session_pr_context(
    session: dict[str, Any],
    open_prs: list[dict[str, Any]],
    *,
    repo: str,
    task_ids: list[str],
) -> dict[str, Any]:
    pr = associated_pr_for_session(session, open_prs, task_ids)
    if not pr:
        return {}
    return failed_check_prompt_context(pr, repo=repo)


def jules_sessions(state: dict[str, Any]) -> list[dict[str, Any]]:
    jules = state.get("jules") or {}
    sessions = jules.get("sessions") or []
    return [session for session in sessions if isinstance(session, dict)]


def active_jules_sessions(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        session for session in jules_sessions(state)
        if str(session.get("state") or "") in ACTIVE_JULES_STATES
    ]


def should_continue_session(session: dict[str, Any]) -> bool:
    summary = session.get("activity_summary") or {}
    latest_agent_epoch = int(summary.get("latest_agent_epoch") or 0)
    latest_user_epoch = int(summary.get("latest_user_epoch") or 0)
    latest_token_epoch = int(summary.get("latest_token_epoch") or 0)
    if latest_agent_epoch <= 0:
        return False
    if latest_token_epoch >= latest_agent_epoch:
        return False
    if latest_user_epoch >= latest_agent_epoch:
        return False
    return True


def stale_after_autonomous_continue(session: dict[str, Any], *, now: datetime) -> bool:
    summary = session.get("activity_summary") or {}
    latest_agent_epoch = int(summary.get("latest_agent_epoch") or 0)
    latest_user_epoch = int(summary.get("latest_user_epoch") or 0)
    latest_token_epoch = int(summary.get("latest_token_epoch") or 0)
    if latest_agent_epoch <= 0 or latest_token_epoch < latest_agent_epoch:
        return False
    if latest_user_epoch > latest_token_epoch:
        return False

    token_time = datetime.fromtimestamp(latest_token_epoch, timezone.utc)
    return now - token_time >= timedelta(minutes=STALE_AWAITING_FEEDBACK_MINUTES)


def stale_after_recorded_continue(
    ledger: dict[str, Any],
    dedupe_key: str,
    *,
    now: datetime,
) -> bool:
    timestamp = action_time(ledger, dedupe_key)
    if timestamp is None:
        return False
    return now - timestamp >= timedelta(minutes=STALE_AWAITING_FEEDBACK_MINUTES)


def recovery_prompt_payload_for_session(
    session: dict[str, Any],
    state: dict[str, Any],
    *,
    mode: str,
    stale_reason: str = "",
    repo: str = "",
    task_ids: list[str] | None = None,
    open_prs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    summary = dict(session.get("activity_summary") or {})
    task_id = str(session.get("task_id") or summary.get("task_id") or "")
    session_name = str(session.get("name") or "")
    session_id = str(session.get("session_id") or session_name.rsplit("/", 1)[-1])
    session_state = str(session.get("state") or "")
    task = (state.get("task_details") or {}).get(task_id)
    pr_context = {}
    if repo and task_ids is not None:
        pr_context = session_pr_context(
            session,
            open_prs if open_prs is not None else list(state.get("open_pulls") or []),
            repo=repo,
            task_ids=task_ids,
        )
    return jules_recovery_prompt.build_prompt_payload(
        summary=summary,
        task=task,
        task_id=task_id,
        repo=repo,
        session_id=session_id,
        session_state=session_state,
        mode=mode,
        stale_reason=stale_reason,
        max_continue_attempts=MAX_STALE_AWAITING_FEEDBACK_ESCALATIONS,
        pr_context=pr_context,
    )


def plan_stale_feedback_action(
    session: dict[str, Any],
    state: dict[str, Any],
    ledger: dict[str, Any],
    *,
    now: datetime,
    reason: str,
    repo: str = "",
    task_ids: list[str] | None = None,
    open_prs: list[dict[str, Any]] | None = None,
) -> RecoveryAction | None:
    session_id = str(session.get("session_id") or "")
    if not session_id:
        return None
    session_name = str(session.get("name") or f"sessions/{session_id}")
    summary = session.get("activity_summary") or {}
    latest_agent = str(summary.get("latest_agent_epoch") or session.get("updateTime") or "")
    prefix = f"stale-continue:{session_id}:{latest_agent}:"
    previous = action_times_with_prefix(
        ledger,
        prefix,
        now=now,
        ttl_minutes=24 * 60,
    )
    if previous and now - previous[-1] < timedelta(minutes=STALE_AWAITING_FEEDBACK_COOLDOWN_MINUTES):
        return None
    if len(previous) >= MAX_STALE_AWAITING_FEEDBACK_ESCALATIONS:
        return RecoveryAction(
            type="jules_delete_session",
            dedupe_key=f"delete-stale-session:{session_id}:{latest_agent}",
            reason=f"Delete stale Jules session {session_id} after repeated unanswered autonomous continue prompts",
            ttl_minutes=24 * 60,
            payload={"session": session_name},
        )

    attempt = len(previous) + 1
    prompt_payload = recovery_prompt_payload_for_session(
        session,
        state,
        mode="stale",
        stale_reason=reason,
        repo=repo,
        task_ids=task_ids,
        open_prs=open_prs,
    )
    return RecoveryAction(
        type="jules_send_message",
        dedupe_key=f"{prefix}attempt-{attempt}",
        reason=f"Jules session {session_id} is still awaiting user feedback after autonomous continue: {reason}",
        ttl_minutes=24 * 60,
        payload={
            "session": session_name,
            "prompt": prompt_payload["prompt"],
            "wait_reason": prompt_payload["wait_reason"],
            "prompt_action": prompt_payload["prompt_action"],
            "task_id": prompt_payload["task_id"],
            "repo": prompt_payload["repo"],
            "session_id": prompt_payload["session_id"],
            "session_state": prompt_payload["session_state"],
            "pr_context": prompt_payload["pr_context"],
        },
    )


def failed_sessions_by_task(state: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for session in jules_sessions(state):
        if str(session.get("state") or "") != "FAILED":
            continue
        task_id = str(session.get("task_id") or "")
        if not task_id:
            continue
        grouped.setdefault(task_id, []).append(session)
    return grouped


def active_task_ids(state: dict[str, Any]) -> set[str]:
    return {
        str(session.get("task_id") or "")
        for session in active_jules_sessions(state)
        if session.get("task_id")
    }


def plan_recovery_actions(
    state: dict[str, Any],
    ledger: dict[str, Any],
    *,
    repo: str,
    task_ids: list[str],
    now: datetime,
    health_mode: str = "enforce",
) -> list[RecoveryAction]:
    actions: list[RecoveryAction] = []
    open_prs = [
        pr for pr in state.get("open_pulls", [])
        if is_autonomous_pr(pr, repo=repo, task_ids=task_ids)
        and not has_autonomous_stop_label(pr)
    ]
    open_prs.sort(key=lambda item: int(item.get("number") or 0))

    for pr in open_prs:
        labels = labels_of(pr)
        number = pr.get("number")
        sha = str((pr.get("head") or {}).get("sha") or "")

        if is_conflicting_pr(pr):
            attempt_shas = conflict_recovery_attempt_shas(pr, ledger)
            if len(attempt_shas) >= CONFLICT_RECOVERY_CIRCUIT_BREAKER_ATTEMPTS:
                dedupe_key = f"conflict-recovery-circuit-breaker:{number}:{sha}"
                marker = conflict_recovery_circuit_breaker_marker(pr)
                has_marker = comments_contain(pr, marker)
                if not action_recently_done(
                    ledger,
                    dedupe_key,
                    now=now,
                    ttl_minutes=CONFLICT_RECOVERY_CIRCUIT_BREAKER_TTL_MINUTES,
                ) or not has_marker:
                    actions.append(
                        RecoveryAction(
                            type="conflict_recovery_circuit_breaker",
                            dedupe_key=dedupe_key,
                            reason=(
                                f"PR #{number} exceeded "
                                f"{CONFLICT_RECOVERY_CIRCUIT_BREAKER_ATTEMPTS} conflict-recovery attempts"
                            ),
                            ttl_minutes=CONFLICT_RECOVERY_CIRCUIT_BREAKER_TTL_MINUTES,
                            payload={
                                "pr_number": number,
                                "labels": list(QUALITY_FIX_CIRCUIT_BREAKER_LABELS),
                                "body": conflict_recovery_circuit_breaker_comment(
                                    pr,
                                    attempt_shas=attempt_shas,
                                ),
                                "comment_needed": not has_marker,
                            },
                        )
                    )
                return actions
            dedupe_key = f"conflict-recovery:{number}:{sha}"
            marker = f"{ROUTER_MARKER} action=conflict-recovery sha={sha}"
            has_marker = comments_contain(pr, marker)
            if not action_recently_done(
                ledger,
                dedupe_key,
                now=now,
                ttl_minutes=CONFLICT_RECOVERY_COOLDOWN_MINUTES,
            ) and (
                not has_marker or extract_session_id_from_pr(pr)
            ):
                prompt = conflict_recovery_prompt(pr)
                actions.append(
                    RecoveryAction(
                        type="conflict_recovery",
                        dedupe_key=dedupe_key,
                        reason=f"PR #{number} is dirty/conflicting with master",
                        ttl_minutes=CONFLICT_RECOVERY_COOLDOWN_MINUTES,
                        payload={
                            "pr_number": number,
                            "body": prompt,
                            "comment_needed": not has_marker,
                            "session_id": extract_session_id_from_pr(pr),
                        },
                    )
                )
            return actions

        if "needs-quality-fix" in labels:
            if has_pending_checks(pr):
                return actions
            attempt_shas = quality_fix_recovery_attempt_shas(pr, ledger)
            if len(attempt_shas) >= QUALITY_FIX_CIRCUIT_BREAKER_ATTEMPTS:
                dedupe_key = f"quality-fix-circuit-breaker:{number}:{sha}"
                marker = quality_fix_circuit_breaker_marker(pr)
                has_marker = comments_contain(pr, marker)
                if not action_recently_done(
                    ledger,
                    dedupe_key,
                    now=now,
                    ttl_minutes=QUALITY_FIX_CIRCUIT_BREAKER_TTL_MINUTES,
                ) or not has_marker:
                    actions.append(
                        RecoveryAction(
                            type="quality_fix_circuit_breaker",
                            dedupe_key=dedupe_key,
                            reason=(
                                f"PR #{number} exceeded "
                                f"{QUALITY_FIX_CIRCUIT_BREAKER_ATTEMPTS} quality-fix recovery attempts"
                            ),
                            ttl_minutes=QUALITY_FIX_CIRCUIT_BREAKER_TTL_MINUTES,
                            payload={
                                "pr_number": number,
                                "labels": list(QUALITY_FIX_CIRCUIT_BREAKER_LABELS),
                                "body": quality_fix_circuit_breaker_comment(
                                    pr,
                                    attempt_shas=attempt_shas,
                                ),
                                "comment_needed": not has_marker,
                            },
                        )
                    )
                return actions
            dedupe_key = f"quality-fix:{number}:{sha}"
            marker = f"{ROUTER_MARKER} action=quality-fix sha={sha}"
            has_marker = comments_contain(pr, marker)
            if not action_recently_done(
                ledger,
                dedupe_key,
                now=now,
                ttl_minutes=QUALITY_FIX_RECOVERY_COOLDOWN_MINUTES,
            ) and (
                not has_marker or extract_session_id_from_pr(pr)
            ):
                prompt = quality_fix_prompt(pr)
                actions.append(
                    RecoveryAction(
                        type="quality_fix_recovery",
                        dedupe_key=dedupe_key,
                        reason=f"PR #{number} has unresolved needs-quality-fix",
                        ttl_minutes=QUALITY_FIX_RECOVERY_COOLDOWN_MINUTES,
                        payload={
                            "pr_number": number,
                            "body": prompt,
                            "comment_needed": not has_marker,
                            "session_id": extract_session_id_from_pr(pr),
                        },
                    )
                )
            return actions

        if "jules" not in labels:
            dedupe_key = f"ensure-jules-label:{number}:{sha}"
            if not action_recently_done(ledger, dedupe_key, now=now, ttl_minutes=7 * 24 * 60):
                actions.append(
                    RecoveryAction(
                        type="add_label",
                        dedupe_key=dedupe_key,
                        reason=f"Autonomous PR #{number} is missing jules label",
                        ttl_minutes=7 * 24 * 60,
                        payload={"pr_number": number, "labels": ["jules"]},
                    )
                )
                return actions

        failed_run_id = latest_failed_automerge_run(pr)
        if failed_run_id:
            dedupe_key = f"rerun-automerge:{number}:{sha}:{failed_run_id}"
            if not action_recently_done(
                ledger,
                dedupe_key,
                now=now,
                ttl_minutes=RERUN_AUTOMERGE_COOLDOWN_MINUTES,
            ):
                actions.append(
                    RecoveryAction(
                        type="rerun_workflow",
                        dedupe_key=dedupe_key,
                        reason=f"Automerge workflow failed for PR #{number} without a blocking label",
                        ttl_minutes=RERUN_AUTOMERGE_COOLDOWN_MINUTES,
                        payload={"run_id": failed_run_id},
                    )
                )
                return actions

        failed_checks = failed_check_runs(pr)
        if failed_checks:
            if has_pending_checks(pr):
                return actions
            fingerprint = failed_check_fingerprint(pr)
            dedupe_key = f"failed-check-recovery:{number}:{sha}:{fingerprint}"
            marker = f"{ROUTER_MARKER} action=failed-check sha={sha}"
            has_marker = comments_contain(pr, marker)
            if not action_recently_done(
                ledger,
                dedupe_key,
                now=now,
                ttl_minutes=FAILED_CHECK_RECOVERY_COOLDOWN_MINUTES,
            ) and (
                not has_marker or extract_session_id_from_pr(pr)
            ):
                prompt = failed_check_recovery_prompt(pr)
                actions.append(
                    RecoveryAction(
                        type="failed_check_recovery",
                        dedupe_key=dedupe_key,
                        reason=f"PR #{number} has failed checks without an active quality-fix label",
                        ttl_minutes=FAILED_CHECK_RECOVERY_COOLDOWN_MINUTES,
                        payload={
                            "pr_number": number,
                            "body": prompt,
                            "comment_needed": not has_marker,
                            "session_id": extract_session_id_from_pr(pr),
                        },
                    )
                )
                return actions

    for session in active_jules_sessions(state):
        session_id = str(session.get("session_id") or "")
        session_name = str(session.get("name") or f"sessions/{session_id}")
        session_state = str(session.get("state") or "")
        update_marker = str(session.get("updateTime") or session.get("createTime") or "")
        if session_state == "AWAITING_PLAN_APPROVAL":
            dedupe_key = f"approve-plan:{session_id}:{update_marker}"
            if not action_recently_done(ledger, dedupe_key, now=now, ttl_minutes=24 * 60):
                actions.append(
                    RecoveryAction(
                        type="jules_approve_plan",
                        dedupe_key=dedupe_key,
                        reason=f"Jules session {session_id} is awaiting plan approval",
                        ttl_minutes=24 * 60,
                        payload={"session": session_name},
                    )
                )
                return actions
            continue
        if session_state == "AWAITING_USER_FEEDBACK" and should_continue_session(session):
            summary = session.get("activity_summary") or {}
            prompt_payload = recovery_prompt_payload_for_session(
                session,
                state,
                mode="continue",
                repo=repo,
                task_ids=task_ids,
                open_prs=open_prs,
            )
            prompt = prompt_payload["prompt"]
            dedupe_key = f"continue:{session_id}:{summary.get('latest_agent_epoch') or update_marker}"
            if not action_recently_done(ledger, dedupe_key, now=now, ttl_minutes=24 * 60):
                actions.append(
                    RecoveryAction(
                        type="jules_send_message",
                        dedupe_key=dedupe_key,
                        reason=f"Jules session {session_id} is awaiting user feedback",
                        ttl_minutes=24 * 60,
                        payload={
                            "session": session_name,
                            "prompt": prompt,
                            "wait_reason": prompt_payload["wait_reason"],
                            "prompt_action": prompt_payload["prompt_action"],
                            "task_id": prompt_payload["task_id"],
                            "repo": prompt_payload["repo"],
                            "session_id": prompt_payload["session_id"],
                            "session_state": prompt_payload["session_state"],
                            "pr_context": prompt_payload["pr_context"],
                        },
                    )
                )
                return actions
            elif stale_after_recorded_continue(ledger, dedupe_key, now=now):
                action = plan_stale_feedback_action(
                    session,
                    state,
                    ledger,
                    now=now,
                    reason="recorded continue did not produce new agent activity",
                    repo=repo,
                    task_ids=task_ids,
                    open_prs=open_prs,
                )
                if action:
                    actions.append(action)
                    return actions
            continue
        if session_state == "AWAITING_USER_FEEDBACK" and stale_after_autonomous_continue(session, now=now):
            action = plan_stale_feedback_action(
                session,
                state,
                ledger,
                now=now,
                reason="latest autonomous continue token is stale",
                repo=repo,
                task_ids=task_ids,
                open_prs=open_prs,
            )
            if action:
                actions.append(action)
                return actions
            continue

    if open_prs:
        return actions

    task_statuses = state.get("task_statuses") or {}
    active_ids = active_task_ids(state)
    for task_id, sessions_for_task in sorted(failed_sessions_by_task(state).items()):
        if task_id not in task_statuses:
            continue
        status = str(task_statuses.get(task_id) or "")
        if status != "todo":
            continue
        if task_id in active_ids:
            continue
        session_ids = [
            str(session.get("session_id") or "")
            for session in sessions_for_task
            if session.get("session_id")
        ]
        if len(sessions_for_task) <= 1:
            session_id = session_ids[0] if session_ids else "unknown"
            dedupe_key = f"failed-retry:{task_id}:{session_id}"
            if not action_recently_done(ledger, dedupe_key, now=now, ttl_minutes=24 * 60):
                actions.append(
                    RecoveryAction(
                        type="dispatch_workflow",
                        dedupe_key=dedupe_key,
                        reason=f"Retry failed Jules task {task_id}",
                        ttl_minutes=24 * 60,
                        payload={
                            "workflow": "jules_next_task.yml",
                            "ref": "master",
                            "inputs": {
                                "task_id": task_id,
                                "focus": "proxy",
                                "risk_ceiling": "medium",
                                "allow_parallel": "false",
                                "recovery_session_id": session_id,
                                "recovery_reason": "failed Jules session recovered by router",
                            },
                        },
                    )
                )
            return actions

        dedupe_key = f"failed-block:{task_id}:{','.join(session_ids)}"
        if not action_recently_done(ledger, dedupe_key, now=now, ttl_minutes=7 * 24 * 60):
            actions.append(
                RecoveryAction(
                    type="block_failed_task",
                    dedupe_key=dedupe_key,
                    reason=f"Block task {task_id} after repeated failed Jules sessions",
                    ttl_minutes=7 * 24 * 60,
                    payload={"task_id": task_id, "failed_sessions": ",".join(session_ids)},
                )
            )
        return actions

    stopped_prs = [
        pr for pr in state.get("open_pulls", [])
        if is_autonomous_pr(pr, repo=repo, task_ids=task_ids)
        and has_autonomous_stop_label(pr)
        and has_quality_fix_circuit_breaker_marker(pr)
    ]
    stopped_prs.sort(key=lambda item: int(item.get("number") or 0))
    for pr in stopped_prs:
        number = int(pr.get("number") or 0)
        sha = str((pr.get("head") or {}).get("sha") or "")
        source_task_id = task_id_from_pr(pr, task_ids)
        followup_task_id = quality_fix_followup_task_id(pr, task_ids)
        if quality_fix_followup_exists(state, followup_task_id):
            continue
        dedupe_key = f"quality-fix-followup-task:{number}:{sha}:{followup_task_id}"
        if not action_recently_done(
            ledger,
            dedupe_key,
            now=now,
            ttl_minutes=QUALITY_FIX_FOLLOWUP_TTL_MINUTES,
        ):
            actions.append(
                RecoveryAction(
                    type="quality_fix_followup_task",
                    dedupe_key=dedupe_key,
                    reason=f"Create diagnostic task for stopped quality-fix loop on PR #{number}",
                    ttl_minutes=QUALITY_FIX_FOLLOWUP_TTL_MINUTES,
                    payload={
                        "pr_number": number,
                        "source_sha": sha,
                        "source_task_id": source_task_id,
                        "task_id": followup_task_id,
                        "reason": latest_quality_fix_details(pr),
                    },
                )
            )
        return actions

    stopped_conflict_prs = [
        pr for pr in state.get("open_pulls", [])
        if is_autonomous_pr(pr, repo=repo, task_ids=task_ids)
        and has_autonomous_stop_label(pr)
        and has_conflict_recovery_circuit_breaker_marker(pr)
    ]
    stopped_conflict_prs.sort(key=lambda item: int(item.get("number") or 0))
    for pr in stopped_conflict_prs:
        number = int(pr.get("number") or 0)
        sha = str((pr.get("head") or {}).get("sha") or "")
        source_task_id = task_id_from_pr(pr, task_ids)
        followup_task_id = conflict_recovery_followup_task_id(pr, task_ids)
        if quality_fix_followup_exists(state, followup_task_id):
            continue
        dedupe_key = f"conflict-recovery-followup-task:{number}:{sha}:{followup_task_id}"
        if not action_recently_done(
            ledger,
            dedupe_key,
            now=now,
            ttl_minutes=QUALITY_FIX_FOLLOWUP_TTL_MINUTES,
        ):
            actions.append(
                RecoveryAction(
                    type="conflict_recovery_followup_task",
                    dedupe_key=dedupe_key,
                    reason=f"Create diagnostic task for stopped conflict-recovery loop on PR #{number}",
                    ttl_minutes=QUALITY_FIX_FOLLOWUP_TTL_MINUTES,
                    payload={
                        "pr_number": number,
                        "source_sha": sha,
                        "source_task_id": source_task_id,
                        "task_id": followup_task_id,
                        "reason": "conflict-recovery circuit breaker stopped repeated dirty PR recovery",
                    },
                )
            )
        return actions

    if active_jules_sessions(state):
        return actions

    if (
        workflow_in_progress(state, "jules_burst_monitor.yml")
        or workflow_in_progress(state, "jules_unattended_monitor.yml")
        or workflow_in_progress(state, "jules_next_task.yml")
    ):
        return actions

    selector = state.get("selector") or {}
    selector_selected = bool(selector.get("selected"))
    selector_task_id = str(selector.get("task_id") or "")

    if selector_selected:
        below_minimum, todo_count, minimum = selector_below_replenishment_minimum(state)
        if below_minimum:
            action = maybe_health_recovery_action(
                state,
                ledger,
                now=now,
                health_mode=health_mode,
                reason=(
                    "Todo queue below replenishment minimum while selector still has an eligible task: "
                    f"{todo_count}/{minimum}"
                ),
                dedupe_suffix=f"todo-below-minimum:{todo_count}:{minimum}",
            )
            if action:
                actions.append(action)
                return actions

        if workflow_recently_created(
            state,
            "jules_next_task.yml",
            now=now,
            minutes=ACTIVE_NEXT_TASK_COOLDOWN_MINUTES,
        ):
            return actions
        dedupe_key = f"dispatch-next-task:{selector_task_id}"
        if not action_recently_done(
            ledger,
            dedupe_key,
            now=now,
            ttl_minutes=ACTIVE_NEXT_TASK_COOLDOWN_MINUTES,
        ):
            actions.append(
                RecoveryAction(
                    type="dispatch_workflow",
                    dedupe_key=dedupe_key,
                    reason=f"No autonomous PR is open and selector picked {selector_task_id}",
                    ttl_minutes=ACTIVE_NEXT_TASK_COOLDOWN_MINUTES,
                    payload={
                        "workflow": "jules_next_task.yml",
                        "ref": "master",
                        "inputs": {
                            "focus": "proxy",
                            "risk_ceiling": "medium",
                            "allow_parallel": "false",
                        },
                    },
                )
            )
        return actions

    if selector_requires_health_recovery(selector):
        if health_mode == "disabled":
            return actions

        reason = str(selector.get("reason") or selector.get("error") or "selector found no eligible task")
        action = maybe_health_recovery_action(
            state,
            ledger,
            now=now,
            health_mode=health_mode,
            reason=f"No eligible autonomous task: {reason}",
            dedupe_suffix=reason,
        )
        if action:
            actions.append(action)
        return actions

    if not workflow_in_progress(state, "jules_burst_monitor.yml") and not workflow_recently_created(
        state,
        "jules_unattended_monitor.yml",
        now=now,
        minutes=MONITOR_COOLDOWN_MINUTES,
    ):
        dedupe_key = "dispatch-unattended-monitor"
        if not action_recently_done(ledger, dedupe_key, now=now, ttl_minutes=MONITOR_COOLDOWN_MINUTES):
            actions.append(
                RecoveryAction(
                    type="dispatch_workflow",
                    dedupe_key=dedupe_key,
                    reason="No autonomous PR is open and unattended monitor is stale",
                    ttl_minutes=MONITOR_COOLDOWN_MINUTES,
                    payload={
                        "workflow": "jules_unattended_monitor.yml",
                        "ref": "master",
                        "inputs": {
                            "lookback_hours": "24",
                            "dispatch_if_idle": "true",
                            "idle_dispatch_minutes": "10",
                        },
                    },
                )
            )
            return actions

    selector = state.get("selector") or {}
    selector_selected = bool(selector.get("selected"))
    selector_task_id = str(selector.get("task_id") or "")

    if selector_selected:
        if workflow_recently_created(
            state,
            "jules_next_task.yml",
            now=now,
            minutes=ACTIVE_NEXT_TASK_COOLDOWN_MINUTES,
        ):
            return actions
        dedupe_key = f"dispatch-next-task:{selector_task_id}"
        if not action_recently_done(
            ledger,
            dedupe_key,
            now=now,
            ttl_minutes=ACTIVE_NEXT_TASK_COOLDOWN_MINUTES,
        ):
            actions.append(
                RecoveryAction(
                    type="dispatch_workflow",
                    dedupe_key=dedupe_key,
                    reason=f"No autonomous PR is open and selector picked {selector_task_id}",
                    ttl_minutes=ACTIVE_NEXT_TASK_COOLDOWN_MINUTES,
                    payload={
                        "workflow": "jules_next_task.yml",
                        "ref": "master",
                        "inputs": {
                            "focus": "proxy",
                            "risk_ceiling": "medium",
                            "allow_parallel": "false",
                        },
                    },
                )
            )
        return actions

    if health_mode != "disabled" and not workflow_recently_created(
        state,
        "automation_health.yml",
        now=now,
        minutes=HEALTH_ENFORCE_COOLDOWN_MINUTES,
    ):
        reason = str(selector.get("reason") or selector.get("error") or "selector found no eligible task")
        workflow_mode = "enforce" if health_mode == "enforce" else "shadow"
        dedupe_key = f"automation-health-{workflow_mode}:{slug(reason)[:48]}"
        if not action_recently_done(
            ledger,
            dedupe_key,
            now=now,
            ttl_minutes=HEALTH_ENFORCE_COOLDOWN_MINUTES,
        ):
            actions.append(
                RecoveryAction(
                    type="dispatch_workflow",
                    dedupe_key=dedupe_key,
                    reason=f"No eligible autonomous task: {reason}",
                    ttl_minutes=HEALTH_ENFORCE_COOLDOWN_MINUTES,
                    payload={
                        "workflow": "automation_health.yml",
                        "ref": "master",
                        "inputs": {"mode": workflow_mode},
                    },
                )
            )

    return actions


def slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned or "value"


def load_ledger(client: GitHubClient) -> dict[str, Any]:
    try:
        data = client.request(
            "GET",
            f"/repos/{client.repo}/actions/variables/{LEDGER_VARIABLE}",
            ok=(200,),
        )
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            return {"version": 1, "actions": {}}
        raise
    raw = str((data or {}).get("value") or "{}")
    try:
        ledger = json.loads(raw)
    except json.JSONDecodeError:
        return {"version": 1, "actions": {}}
    if not isinstance(ledger, dict):
        return {"version": 1, "actions": {}}
    ledger.setdefault("version", 1)
    ledger.setdefault("actions", {})
    return ledger


def prune_ledger(ledger: dict[str, Any], *, now: datetime, keep_days: int = 14) -> dict[str, Any]:
    entries = ledger.get("actions") or {}
    if not isinstance(entries, dict):
        entries = {}
    cutoff = now - timedelta(days=keep_days)
    kept: dict[str, Any] = {}
    for key, value in entries.items():
        if not isinstance(value, dict):
            continue
        timestamp = parse_time(str(value.get("time") or ""))
        if timestamp and timestamp >= cutoff:
            kept[str(key)] = value
    return {"version": 1, "actions": kept}


def record_action(ledger: dict[str, Any], action: RecoveryAction, *, now: datetime) -> dict[str, Any]:
    ledger = prune_ledger(ledger, now=now)
    entries = ledger.setdefault("actions", {})
    entries[action.dedupe_key] = {
        "time": now.isoformat().replace("+00:00", "Z"),
        "type": action.type,
        "reason": action.reason[:300],
    }
    return ledger


def save_ledger(client: GitHubClient, ledger: dict[str, Any]) -> None:
    value = json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    body = {"name": LEDGER_VARIABLE, "value": value}
    try:
        client.request(
            "PATCH",
            f"/repos/{client.repo}/actions/variables/{LEDGER_VARIABLE}",
            body,
            ok=(204,),
        )
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            raise
        client.request("POST", f"/repos/{client.repo}/actions/variables", body, ok=(201, 204))


def jules_clients_from_env(api_base: str) -> list[JulesClient]:
    clients: list[JulesClient] = []
    primary = os.environ.get("JULES_API_KEY", "")
    backup = os.environ.get("JULES_API_KEY_BACKUP", "")
    if primary:
        clients.append(JulesClient(api_base=api_base, key=primary, label="primary"))
    if backup:
        clients.append(JulesClient(api_base=api_base, key=backup, label="backup"))
    return clients


def jules_request_any(
    clients: list[JulesClient],
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> Any:
    errors: list[str] = []
    for client in clients:
        try:
            return client.request(method, path, body)
        except RuntimeError as exc:
            errors.append(f"{client.label}: {exc}")
    raise RuntimeError(f"All configured Jules API keys failed for {method} {path}: {'; '.join(errors)}")


def normalize_session_name(value: str) -> str:
    return value if value.startswith("sessions/") else f"sessions/{value}"


def collect_jules_sessions(
    clients: list[JulesClient],
    *,
    source: str,
    lookback_hours: int,
    recent_session_tasks: dict[str, Any],
) -> dict[str, Any]:
    if not clients:
        return {"api_available": False, "sessions": [], "reason": "no Jules API keys configured"}

    cutoff = now_utc() - timedelta(hours=lookback_hours)
    sessions_by_name: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    for client in clients:
        try:
            data = client.request("GET", "sessions?pageSize=100") or {}
        except RuntimeError as exc:
            errors.append(f"{client.label}: {exc}")
            continue
        for session in data.get("sessions", []):
            if not isinstance(session, dict):
                continue
            if str((session.get("sourceContext") or {}).get("source") or "") != source:
                continue
            timestamp = parse_time(str(session.get("updateTime") or session.get("createTime") or ""))
            if timestamp and timestamp < cutoff:
                continue
            name = str(session.get("name") or "")
            if not name or name in sessions_by_name:
                continue

            session_id = name.split("/")[-1]
            state = str(session.get("state") or "")
            enriched = dict(session)
            enriched["session_id"] = session_id
            enriched["key_label"] = client.label

            if state in ACTIVE_JULES_STATES or state == "FAILED":
                try:
                    activities = client.request("GET", f"{name}/activities?pageSize=50") or {}
                    activity_list = activities.get("activities", [])
                    if isinstance(activity_list, list):
                        enriched["activities"] = activity_list
                        enriched["activity_summary"] = summarize_activities(activity_list)
                        enriched["task_id"] = enriched["activity_summary"].get("task_id") or ""
                except RuntimeError as exc:
                    enriched["activity_error"] = str(exc)

            if not enriched.get("task_id"):
                mapped = recent_session_tasks.get(session_id)
                if isinstance(mapped, dict):
                    enriched["task_id"] = str(mapped.get("task_id") or "")
            sessions_by_name[name] = enriched

    return {
        "api_available": bool(sessions_by_name) or len(errors) < len(clients),
        "sessions": list(sessions_by_name.values()),
        "errors": errors,
    }


def load_recent_session_tasks(client: GitHubClient) -> dict[str, Any]:
    try:
        data = client.request(
            "GET",
            f"/repos/{client.repo}/actions/variables/JULES_RECENT_SESSION_TASKS",
            ok=(200,),
        )
    except RuntimeError:
        return {}
    raw = str((data or {}).get("value") or "{}")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def ensure_repository_labels(client: GitHubClient, labels: list[str]) -> None:
    for label in labels:
        definition = RECOVERY_LABEL_DEFINITIONS.get(label)
        if not definition:
            continue
        encoded = urllib.parse.quote(label, safe="")
        try:
            client.request(
                "GET",
                f"/repos/{client.repo}/labels/{encoded}",
                ok=(200,),
            )
            continue
        except RuntimeError as exc:
            if "HTTP 404" not in str(exc):
                raise

        client.request(
            "POST",
            f"/repos/{client.repo}/labels",
            {
                "name": label,
                "color": definition["color"],
                "description": definition["description"],
            },
            ok=(201,),
        )


def execute_action(
    client: GitHubClient,
    action: RecoveryAction,
    *,
    jules_clients: list[JulesClient] | None = None,
) -> None:
    payload = action.payload
    jules_clients = jules_clients or []
    if action.type in {"quality_fix_recovery", "conflict_recovery", "failed_check_recovery"}:
        if payload.get("comment_needed", True):
            client.request(
                "POST",
                f"/repos/{client.repo}/issues/{payload['pr_number']}/comments",
                {"body": payload["body"]},
                ok=(201,),
            )
        session_id = str(payload.get("session_id") or "")
        if session_id:
            if not jules_clients:
                raise RuntimeError(f"Jules API keys are required for {action.type} sendMessage")
            jules_request_any(
                jules_clients,
                "POST",
                f"{normalize_session_name(session_id)}:sendMessage",
                {"prompt": payload["body"]},
            )
        return
    if action.type == "comment_pr":
        client.request(
            "POST",
            f"/repos/{client.repo}/issues/{payload['pr_number']}/comments",
            {"body": payload["body"]},
            ok=(201,),
        )
        return
    if action.type == "add_label":
        client.request(
            "POST",
            f"/repos/{client.repo}/issues/{payload['pr_number']}/labels",
            {"labels": payload["labels"]},
            ok=(200, 201),
        )
        return
    if action.type in {"quality_fix_circuit_breaker", "conflict_recovery_circuit_breaker"}:
        labels = [str(label) for label in payload.get("labels", []) if str(label)]
        if payload.get("comment_needed", True):
            client.request(
                "POST",
                f"/repos/{client.repo}/issues/{payload['pr_number']}/comments",
                {"body": payload["body"]},
                ok=(201,),
            )
        ensure_repository_labels(client, labels)
        client.request(
            "POST",
            f"/repos/{client.repo}/issues/{payload['pr_number']}/labels",
            {"labels": labels},
            ok=(200, 201),
        )
        return
    if action.type == "jules_send_message":
        if not jules_clients:
            raise RuntimeError("Jules API keys are required for jules_send_message")
        jules_request_any(
            jules_clients,
            "POST",
            f"{normalize_session_name(str(payload['session']))}:sendMessage",
            {"prompt": payload["prompt"]},
        )
        return
    if action.type == "jules_approve_plan":
        if not jules_clients:
            raise RuntimeError("Jules API keys are required for jules_approve_plan")
        jules_request_any(
            jules_clients,
            "POST",
            f"{normalize_session_name(str(payload['session']))}:approvePlan",
            {},
        )
        return
    if action.type == "jules_delete_session":
        if not jules_clients:
            raise RuntimeError("Jules API keys are required for jules_delete_session")
        jules_request_any(
            jules_clients,
            "DELETE",
            normalize_session_name(str(payload["session"])),
        )
        return
    if action.type == "dispatch_workflow":
        body = {"ref": payload.get("ref", "master"), "inputs": payload.get("inputs", {})}
        client.request(
            "POST",
            f"/repos/{client.repo}/actions/workflows/{payload['workflow']}/dispatches",
            body,
            ok=(204,),
        )
        return
    if action.type == "rerun_workflow":
        client.request(
            "POST",
            f"/repos/{client.repo}/actions/runs/{payload['run_id']}/rerun-failed-jobs",
            ok=(201,),
        )
        return
    if action.type == "block_failed_task":
        subprocess.run(
            [
                sys.executable,
                ".github/scripts/block-failed-agent-task.py",
                "--manifest",
                "agent_tasks.json",
                "--task-id",
                str(payload["task_id"]),
                "--failed-sessions",
                str(payload["failed_sessions"]),
            ],
            check=True,
        )
        return
    if action.type in {"quality_fix_followup_task", "conflict_recovery_followup_task"}:
        source_finding_id = (
            "conflict_recovery_circuit_breaker"
            if action.type == "conflict_recovery_followup_task"
            else "quality_fix_circuit_breaker"
        )
        subprocess.run(
            [
                sys.executable,
                ".github/scripts/create-circuit-breaker-followup-task-pr.py",
                "--manifest",
                "agent_tasks.json",
                "--pr-number",
                str(payload["pr_number"]),
                "--source-sha",
                str(payload["source_sha"]),
                "--source-task-id",
                str(payload.get("source_task_id") or ""),
                "--source-finding-id",
                source_finding_id,
                "--reason",
                str(payload.get("reason") or ""),
            ],
            check=True,
        )
        return
    raise ValueError(f"unsupported action type: {action.type}")


def run_selector(
    manifest: Path,
    *,
    focus: str,
    risk_ceiling: str,
    exclude_task_ids: list[str] | None = None,
) -> dict[str, Any]:
    cmd = [
        sys.executable,
        "scripts/select_agent_task.py",
        "--manifest",
        str(manifest),
        "--risk-ceiling",
        risk_ceiling,
        "--focus",
        focus,
        "--json",
    ]
    for task_id in exclude_task_ids or []:
        cmd.extend(["--exclude-task-id", task_id])
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    except OSError as exc:
        return {"selected": False, "error": str(exc)}

    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        data = {"selected": False, "error": "selector did not return JSON"}
    if result.returncode != 0:
        data.setdefault("selected", False)
        data["error"] = data.get("error") or result.stderr.strip() or f"selector exited {result.returncode}"
    return data


def collect_live_state(
    client: GitHubClient,
    *,
    manifest: Path,
    focus: str,
    risk_ceiling: str,
    jules_clients: list[JulesClient],
    jules_source: str,
    jules_lookback_hours: int,
) -> dict[str, Any]:
    manifest_data = load_manifest(manifest)
    task_ids = task_ids_from_manifest(manifest_data)
    open_pulls = client.request("GET", f"/repos/{client.repo}/pulls?state=open&per_page=100") or []
    enrich_open_pull_details(client, open_pulls)
    enrich_open_pull_git_conflicts(open_pulls, repo=client.repo)
    for pr in open_pulls:
        number = pr.get("number")
        sha = (pr.get("head") or {}).get("sha")
        if number:
            pr["comments"] = client.request(
                "GET",
                f"/repos/{client.repo}/issues/{number}/comments?per_page=100",
            ) or []
        if sha:
            checks = client.request(
                "GET",
                f"/repos/{client.repo}/commits/{sha}/check-runs?per_page=100",
            ) or {}
            pr["check_runs"] = checks.get("check_runs", [])
        if number and failed_check_runs(pr):
            enrich_failed_check_evidence(client, pr)

    workflow_runs: dict[str, Any] = {}
    for workflow in (
        "jules_next_task.yml",
        "jules_unattended_monitor.yml",
        "jules_burst_monitor.yml",
        "automation_health.yml",
        "jules_automerge.yml",
    ):
        data = client.request(
            "GET",
            f"/repos/{client.repo}/actions/workflows/{workflow}/runs?per_page=10",
        ) or {}
        workflow_runs[workflow] = data.get("workflow_runs", [])

    return {
        "open_pulls": open_pulls,
        "workflow_runs": workflow_runs,
        "selector": run_selector(
            manifest,
            focus=focus,
            risk_ceiling=risk_ceiling,
            exclude_task_ids=stopped_task_ids_from_prs(open_pulls, task_ids),
        ),
        "task_statuses": task_statuses_from_manifest(manifest_data),
        "task_metrics": task_metrics_from_manifest(manifest_data),
        "task_details": task_details_from_manifest(manifest_data),
        "jules": collect_jules_sessions(
            jules_clients,
            source=jules_source,
            lookback_hours=jules_lookback_hours,
            recent_session_tasks=load_recent_session_tasks(client),
        ),
    }


def load_fixture_state(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fixture_file:
        data = json.load(fixture_file)
    if not isinstance(data, dict):
        raise ValueError("fixture must contain a JSON object")
    return data


def write_outputs(path: str, *, actions: list[RecoveryAction], executed: int) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as output:
        output.write(f"actions_planned={len(actions)}\n")
        output.write(f"actions_executed={executed}\n")
        first = actions[0].type if actions else "none"
        output.write(f"first_action={first}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    parser.add_argument("--manifest", type=Path, default=Path("agent_tasks.json"))
    parser.add_argument("--fixture", type=Path, default=None)
    parser.add_argument("--ledger-file", type=Path, default=None)
    parser.add_argument("--mode", choices=("plan", "act"), default="plan")
    parser.add_argument("--focus", default="proxy")
    parser.add_argument("--risk-ceiling", default="medium")
    parser.add_argument(
        "--health-mode",
        choices=("disabled", "shadow", "enforce"),
        default=os.environ.get("RECOVERY_ROUTER_HEALTH_MODE", "enforce"),
    )
    parser.add_argument("--jules-api-base", default=os.environ.get("JULES_API_BASE", "https://jules.googleapis.com/v1alpha"))
    parser.add_argument("--jules-source", default="")
    parser.add_argument("--jules-lookback-hours", type=int, default=24)
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT", ""))
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = args.repo
    if not repo:
        print("ERROR: --repo or GITHUB_REPOSITORY is required", file=sys.stderr)
        return 2

    current_time = now_utc()
    task_ids = load_task_ids(args.manifest)
    jules_source = args.jules_source or f"sources/github/{repo}"
    jules_clients = jules_clients_from_env(args.jules_api_base)

    client: GitHubClient | None = None
    if args.fixture:
        state = load_fixture_state(args.fixture)
        if args.ledger_file and args.ledger_file.exists():
            ledger = json.loads(args.ledger_file.read_text(encoding="utf-8"))
        else:
            ledger = {"version": 1, "actions": {}}
    else:
        token = os.environ.get("GITHUB_API_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            print("ERROR: GITHUB_API_TOKEN or GH_TOKEN is required for live mode", file=sys.stderr)
            return 2
        client = GitHubClient(api_url=args.api_url, repo=repo, token=token)
        state = collect_live_state(
            client,
            manifest=args.manifest,
            focus=args.focus,
            risk_ceiling=args.risk_ceiling,
            jules_clients=jules_clients,
            jules_source=jules_source,
            jules_lookback_hours=args.jules_lookback_hours,
        )
        ledger = load_ledger(client)

    actions = plan_recovery_actions(
        state,
        ledger,
        repo=repo,
        task_ids=task_ids,
        now=current_time,
        health_mode=args.health_mode,
    )

    actions = actions[:1]
    executed = 0
    if args.mode == "act":
        if client is None:
            print("ERROR: --mode act is only supported in live mode", file=sys.stderr)
            return 2
        for action in actions:
            if action_recently_done(
                ledger,
                action.dedupe_key,
                now=current_time,
                ttl_minutes=action.ttl_minutes,
            ):
                continue
            execute_action(client, action, jules_clients=jules_clients)
            ledger = record_action(ledger, action, now=current_time)
            executed += 1
        save_ledger(client, prune_ledger(ledger, now=current_time))

    write_outputs(args.github_output, actions=actions, executed=executed)
    result = {
        "mode": args.mode,
        "actions_planned": len(actions),
        "actions_executed": executed,
        "actions": [action.to_dict() for action in actions],
        "selector": state.get("selector") or {},
    }
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.json else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
