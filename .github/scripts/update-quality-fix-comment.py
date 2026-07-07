#!/usr/bin/env python3
"""Build a single updatable autonomous quality-fix PR comment."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


COMMENT_MARKER = "<!-- AUTONOMOUS_QUALITY_FIX_REQUEST pr-level -->"
HISTORY_RE = re.compile(r"^-\s+`(?P<sha>[0-9a-f]{7,40})`:\s+(?P<reason>.+)$")
MAX_HISTORY = 5
DEFERRED_TASK_MARKER_RE = re.compile(r"\bfollow-?up\b", re.IGNORECASE)


def load_comments(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    raise ValueError(f"{path} must contain a JSON array")


def find_existing_comment(comments: list[dict[str, Any]]) -> dict[str, Any] | None:
    for comment in comments:
        if COMMENT_MARKER in str(comment.get("body") or ""):
            return comment
    return None


def clean_summary(value: str) -> str:
    one_line = " ".join(value.split())
    sanitized = sanitize_quality_text(one_line)
    return sanitized[:500] if sanitized else "quality gate failed"


def sanitize_quality_text(value: str) -> str:
    return DEFERRED_TASK_MARKER_RE.sub("[deferred-task marker]", value)


def history_from_body(body: str) -> list[tuple[str, str]]:
    history: list[tuple[str, str]] = []
    for line in body.splitlines():
        match = HISTORY_RE.match(line.strip())
        if match:
            history.append((match.group("sha"), sanitize_quality_text(match.group("reason"))))
    return history


def merged_history(existing_body: str, *, head_sha: str, summary: str) -> list[tuple[str, str]]:
    normalized_sha = head_sha.strip()
    current = (normalized_sha, clean_summary(summary))
    history = [
        (sha, reason)
        for sha, reason in history_from_body(existing_body)
        if sha != normalized_sha
    ]
    return [current, *history][:MAX_HISTORY]


def build_body(
    *,
    pr_number: int,
    head_sha: str,
    summary: str,
    report: str,
    existing_body: str = "",
) -> str:
    history = merged_history(existing_body, head_sha=head_sha, summary=summary)
    lines = [
        COMMENT_MARKER,
        "",
        f"Jules, исправь этот же PR #{pr_number}; не открывай новый PR и не создавай отдельную задачу на потом.",
        "",
        "Что нужно сделать:",
        "- исправь deterministic autonomous quality gate failure ниже;",
        "- синхронизируй AUTONOMOUS_TASK_EVIDENCE с фактическим статусом задачи в agent_tasks.json;",
        "- если задача в manifest имеет status blocked, evidence тоже должен иметь status: blocked и concrete blocked_reason;",
        "- для blocked задачи, указывай в evidence_files только те файлы, которые реально изменены (чаще всего только agent_tasks.json);",
        "- если задача реально выполнена, manifest должен быть status done и evidence status done с changed evidence files;",
        "- убери временные scratch-файлы из PR, если они не являются частью acceptance/evidence;",
        "- push исправление в эту же PR ветку и дождись повторных checks.",
        "",
        "История последних failed SHA/reasons:",
    ]
    lines.extend(f"- `{sha}`: {reason}" for sha, reason in history)
    lines.extend(["", sanitize_quality_text(report.strip()), ""])
    return "\n".join(lines)


def write_github_outputs(path: str, *, comment_id: str) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as output:
        output.write(f"comment_id={comment_id}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comments", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--output-body", required=True, type=Path)
    parser.add_argument("--github-output", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    comments = load_comments(args.comments)
    existing = find_existing_comment(comments)
    report = args.report.read_text(encoding="utf-8")
    body = build_body(
        pr_number=args.pr_number,
        head_sha=args.head_sha,
        summary=args.summary,
        report=report,
        existing_body=str((existing or {}).get("body") or ""),
    )
    args.output_body.write_text(body, encoding="utf-8")
    write_github_outputs(args.github_output, comment_id=str((existing or {}).get("id") or ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
