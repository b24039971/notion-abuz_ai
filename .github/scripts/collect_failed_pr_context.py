#!/usr/bin/env python3
"""Collect sanitized failed-check PR context for Jules recovery prompts."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import jules_recovery_prompt


MAX_CHANGED_FILES = 30
MAX_FAILED_CHECKS = 8
MAX_ANNOTATIONS = 5
MAX_LOG_LINES = 24
MAX_LOG_CHARS = 2400
FAILED_CONCLUSIONS = {"failure", "timed_out", "action_required", "startup_failure"}
FAILED_LOG_MARKERS = (
    "##[error]",
    "::error::",
    "process completed with exit code",
    "exit code",
    "failed",
    "failure",
    "permission denied",
)
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
LOG_TIMESTAMP_RE = re.compile(r"^\ufeff?\d{4}-\d{2}-\d{2}T[0-9:.]+Z\s+")
SESSION_ID_RE = re.compile(r"(?<!\d)(\d{12,})(?!\d)")


class GitHubApiError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, *, api_url: str, repo: str, token: str):
        self.api_url = api_url.rstrip("/")
        self.repo = repo
        self.token = token

    def request_json(self, path: str) -> Any:
        text = self.request_text(path)
        if not text:
            return None
        return json.loads(text)

    def request_text(self, path: str) -> str:
        url = f"{self.api_url}{path}" if path.startswith("/") else path
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise GitHubApiError(f"GitHub API HTTP {exc.code} for {path}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GitHubApiError(f"GitHub API request failed for {path}: {exc}") from exc


def github_api_path(client: GitHubClient, url_or_path: str) -> str:
    value = str(url_or_path or "")
    if not value:
        return ""
    if value.startswith("/"):
        return value
    parsed_api = urllib.parse.urlparse(client.api_url)
    parsed = urllib.parse.urlparse(value)
    if parsed.netloc != parsed_api.netloc:
        return ""
    path = parsed.path
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return path


def add_query_param(path: str, key: str, value: str) -> str:
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{urllib.parse.quote(key)}={urllib.parse.quote(value)}"


def pr_fields(pr: dict[str, Any]) -> str:
    return "\n".join(
        [
            str(pr.get("body") or ""),
            str(pr.get("title") or ""),
            str((pr.get("head") or {}).get("ref") or ""),
        ]
    )


def pr_matches(pr: dict[str, Any], *, task_id: str, session_id: str) -> bool:
    fields = pr_fields(pr)
    if session_id and re.search(rf"(?<!\d){re.escape(session_id)}(?!\d)", fields):
        return True
    return bool(task_id and task_id in fields)


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


def check_run_display_name(check_run: dict[str, Any]) -> str:
    workflow = str(check_run.get("workflowName") or check_run.get("workflow_name") or "")
    name = str(check_run.get("name") or "unknown")
    return f"{workflow} / {name}" if workflow else name


def failed_check_runs(check_runs: list[Any]) -> list[dict[str, Any]]:
    return [
        check_run
        for check_run in check_runs
        if isinstance(check_run, dict)
        and str(check_run.get("status") or "") == "completed"
        and str(check_run.get("conclusion") or "") in FAILED_CONCLUSIONS
    ]


def sanitize_changed_files(files: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in files:
        name = str(item.get("filename") or "") if isinstance(item, dict) else str(item or "")
        clean = jules_recovery_prompt.sanitize_text(name, limit=220)
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
        if len(result) >= MAX_CHANGED_FILES:
            break
    return result


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

    hit_indexes = [
        index
        for index, line in enumerate(raw_lines)
        if any(marker in line.lower() for marker in FAILED_LOG_MARKERS)
    ]
    selected_indexes: list[int] = []
    if hit_indexes:
        for index in hit_indexes[:4]:
            selected_indexes.extend(range(max(0, index - 10), min(len(raw_lines), index + 3)))
    else:
        selected_indexes = list(range(max(0, len(raw_lines) - MAX_LOG_LINES), len(raw_lines)))

    lines: list[str] = []
    seen: set[str] = set()
    for index in selected_indexes:
        line = raw_lines[index]
        if line in seen:
            continue
        seen.add(line)
        lines.append(jules_recovery_prompt.sanitize_text(line, limit=320))
        if len(lines) >= MAX_LOG_LINES:
            break

    excerpt = "\n".join(line for line in lines if line)
    if len(excerpt) > MAX_LOG_CHARS:
        excerpt = excerpt[:MAX_LOG_CHARS].rstrip() + "\n...[truncated]"
    return excerpt


def enrich_check(client: GitHubClient, check_run: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": jules_recovery_prompt.sanitize_text(check_run_display_name(check_run), limit=180),
        "conclusion": jules_recovery_prompt.sanitize_text(str(check_run.get("conclusion") or "failure"), limit=80),
        "run_id": jules_recovery_prompt.sanitize_text(check_run_run_id(check_run), limit=80),
        "details_url": jules_recovery_prompt.sanitize_text(
            str(check_run.get("details_url") or check_run.get("html_url") or ""),
            limit=240,
        ),
    }

    output = check_run.get("output") if isinstance(check_run.get("output"), dict) else {}
    annotations_url = str((output or {}).get("annotations_url") or "")
    annotations_path = github_api_path(client, annotations_url)
    if annotations_path:
        try:
            annotations = client.request_json(add_query_param(annotations_path, "per_page", str(MAX_ANNOTATIONS)))
            if isinstance(annotations, list):
                item["annotations"] = [
                    excerpt
                    for annotation in annotations[:MAX_ANNOTATIONS]
                    if isinstance(annotation, dict)
                    for excerpt in [annotation_excerpt(annotation)]
                    if excerpt
                ]
        except (GitHubApiError, json.JSONDecodeError) as exc:
            item["annotations_error"] = jules_recovery_prompt.sanitize_text(str(exc), limit=260)

    job_id = check_run_job_id(check_run)
    if job_id:
        try:
            item["log_excerpt"] = failure_log_excerpt(
                client.request_text(f"/repos/{client.repo}/actions/jobs/{job_id}/logs")
            )
        except GitHubApiError as exc:
            item["log_excerpt_error"] = jules_recovery_prompt.sanitize_text(str(exc), limit=260)

    return {key: value for key, value in item.items() if value}


def collect_context(client: GitHubClient, *, task_id: str, session_id: str) -> dict[str, Any]:
    pulls = client.request_json(f"/repos/{client.repo}/pulls?state=open&per_page=100") or []
    if not isinstance(pulls, list):
        return {}

    for pr in pulls:
        if not isinstance(pr, dict) or not pr_matches(pr, task_id=task_id, session_id=session_id):
            continue
        sha = str((pr.get("head") or {}).get("sha") or "")
        if not sha:
            continue
        checks = client.request_json(f"/repos/{client.repo}/commits/{sha}/check-runs?per_page=100") or {}
        check_runs = checks.get("check_runs", []) if isinstance(checks, dict) else []
        failed = failed_check_runs(check_runs)
        if not failed:
            continue
        number = pr.get("number")
        changed_files: list[str] = []
        if number:
            try:
                files = client.request_json(f"/repos/{client.repo}/pulls/{number}/files?per_page=100") or []
                changed_files = sanitize_changed_files(files if isinstance(files, list) else [])
            except (GitHubApiError, json.JSONDecodeError):
                changed_files = []
        return {
            "repo": jules_recovery_prompt.sanitize_text(client.repo, limit=180),
            "pr_number": jules_recovery_prompt.sanitize_text(f"#{number}", limit=80),
            "head_sha": jules_recovery_prompt.sanitize_text(sha, limit=80),
            "changed_files": changed_files,
            "failed_checks": [enrich_check(client, check_run) for check_run in failed[:MAX_FAILED_CHECKS]],
        }

    return {}


def load_fixture(path: str) -> dict[str, Any]:
    if not path:
        return {}
    with Path(path).open("r", encoding="utf-8") as fixture:
        data = json.load(fixture)
    if not isinstance(data, dict):
        raise ValueError("fixture root must be an object")
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--task-id", default="")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    parser.add_argument("--token", default=os.environ.get("GITHUB_API_TOKEN") or os.environ.get("GH_TOKEN") or "")
    parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = Path(args.output)
    try:
        fixture_path = os.environ.get("JULES_FAILED_PR_CONTEXT_FIXTURE", "")
        if fixture_path:
            context = load_fixture(fixture_path)
        elif not args.token or not args.repo:
            context = {}
        else:
            context = collect_context(
                GitHubClient(api_url=args.api_url, repo=args.repo, token=args.token),
                task_id=args.task_id,
                session_id=args.session_id,
            )
        output.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")
        return 0
    except Exception as exc:  # noqa: BLE001 - fail open for unattended recovery.
        print(f"::warning::Could not collect failed PR context: {type(exc).__name__}: {exc}", file=sys.stderr)
        output.write_text("{}", encoding="utf-8")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
