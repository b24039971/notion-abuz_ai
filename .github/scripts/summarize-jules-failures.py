#!/usr/bin/env python3
"""Summarize recent failed Jules sessions into one recovery action."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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


@dataclass(frozen=True)
class FailedSession:
    session_id: str
    task_id: str
    failure_kind: str = ""


@dataclass(frozen=True)
class RecoveryDecision:
    action: str
    task_id: str = ""
    session_id: str = ""
    sessions: tuple[str, ...] = ()
    count_for_task: int = 0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "failed_recovery_action": self.action,
            "failed_task_id": self.task_id,
            "failed_session_id": self.session_id,
            "failed_sessions": ",".join(self.sessions),
            "failed_count_for_task": self.count_for_task,
            "failed_recovery_reason": self.reason,
        }


def load_manifest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as manifest_file:
        data = json.load(manifest_file)
    if not isinstance(data, dict):
        raise ValueError("manifest root must be an object")
    return data


def task_statuses(manifest: dict[str, Any]) -> dict[str, str]:
    return {
        str(task.get("id", "")): str(task.get("status", ""))
        for task in manifest.get("tasks", [])
        if isinstance(task, dict)
    }


def extract_task_id_from_activities(data: dict[str, Any]) -> str:
    text = json.dumps(data, ensure_ascii=False)
    for match in TASK_ID_RE.finditer(text):
        task_id = match.group(1).strip().strip('"')
        if task_id and task_id.lower() not in {"null", "none", "task_id"}:
            return task_id
    return ""


def latest_agent_text_from_activities(data: dict[str, Any]) -> str:
    latest_epoch = ""
    latest_blob = ""
    for activity in data.get("activities", []):
        if not isinstance(activity, dict):
            continue
        originator = str(activity.get("originator", "")).lower()
        if "user" in originator:
            continue
        epoch = str(activity.get("createTime", ""))
        blob = json.dumps(activity, ensure_ascii=False)
        if not latest_blob or epoch >= latest_epoch:
            latest_epoch = epoch
            latest_blob = blob
    return latest_blob


def classify_failed_activities(data: dict[str, Any]) -> str:
    latest = latest_agent_text_from_activities(data).lower()
    if not latest:
        return ""

    routine_question_markers = (
        "пожалуйста, подскажите",
        "подскажите",
        "нужно ли",
        "нужен ли",
        "стоит ли",
        "запустить локальный сервер",
        "запустить сервер",
        "запустить модульные тесты",
        "воспроизвести",
        "should i",
        "do you want me",
        "would you like me",
        "need me to",
        "run the local server",
        "run local",
        "reproduce",
    )
    if "?" in latest and any(marker in latest for marker in routine_question_markers):
        return "routine_question"
    return ""


def read_failed_sessions(path: Path) -> list[FailedSession]:
    sessions: list[FailedSession] = []
    if not path.exists():
        return sessions
    with path.open("r", encoding="utf-8") as sessions_file:
        for line in sessions_file:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            session_id = parts[0].strip()
            task_id = parts[1].strip() if len(parts) > 1 else ""
            failure_kind = parts[2].strip() if len(parts) > 2 else ""
            if session_id:
                sessions.append(
                    FailedSession(
                        session_id=session_id,
                        task_id=task_id,
                        failure_kind=failure_kind,
                    )
                )
    return sessions


def read_active_task_ids(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    with path.open("r", encoding="utf-8") as active_file:
        return {line.strip() for line in active_file if line.strip()}


def decide_recovery(
    manifest: dict[str, Any],
    failed_sessions: list[FailedSession],
    active_task_ids: set[str] | None = None,
) -> RecoveryDecision:
    active_task_ids = active_task_ids or set()
    statuses = task_statuses(manifest)
    known_failed = [session for session in failed_sessions if session.task_id]
    counts = Counter(session.task_id for session in known_failed)

    not_todo_decision = None
    active_decision = None

    for session in known_failed:
        status = statuses.get(session.task_id)
        if status is None:
            continue
        if status != "todo":
            if not not_todo_decision:
                not_todo_decision = RecoveryDecision(
                    action="none",
                    task_id=session.task_id,
                    session_id=session.session_id,
                    count_for_task=counts[session.task_id],
                    reason=f"task status is {status}, not todo",
                )
            continue
        if session.task_id in active_task_ids:
            if not active_decision:
                active_decision = RecoveryDecision(
                    action="none",
                    task_id=session.task_id,
                    session_id=session.session_id,
                    count_for_task=counts[session.task_id],
                    reason="an active Jules session already targets this task",
                )
            continue

        sessions_for_task = tuple(
            item.session_id for item in known_failed if item.task_id == session.task_id
        )
        count_for_task = counts[session.task_id]
        if session.failure_kind == "repeated_stale_feedback":
            return RecoveryDecision(
                action="block",
                task_id=session.task_id,
                session_id=session.session_id,
                sessions=sessions_for_task,
                count_for_task=count_for_task,
                reason=(
                    "Jules session exhausted autonomous stale-feedback continuations "
                    "without opening a PR or unblocking itself"
                ),
            )
        if count_for_task <= 1:
            reason = "first failed session for this task"
            if session.failure_kind == "routine_question":
                reason = (
                    "failed after asking a routine implementation question; "
                    "auto-answer: do not ask, run safe local/offline reproduction "
                    "inside the selected task scope; if live secrets or credentials "
                    "are required, block the task with a concrete blocked_reason"
                )
            return RecoveryDecision(
                action="retry",
                task_id=session.task_id,
                session_id=session.session_id,
                sessions=sessions_for_task,
                count_for_task=count_for_task,
                reason=reason,
            )
        return RecoveryDecision(
            action="block",
            task_id=session.task_id,
            session_id=session.session_id,
            sessions=sessions_for_task,
            count_for_task=count_for_task,
            reason="repeated failed sessions for this task",
        )

    if active_decision:
        return active_decision
    if not_todo_decision:
        return not_todo_decision

    if any(not session.task_id for session in failed_sessions):
        return RecoveryDecision(action="none", reason="failed sessions did not expose a task id")
    return RecoveryDecision(action="none", reason="no failed sessions need recovery")


def write_github_outputs(path: Path, decision: RecoveryDecision) -> None:
    data = decision.to_dict()
    with path.open("a", encoding="utf-8") as output_file:
        for key, value in data.items():
            output_file.write(f"{key}={value}\n")


def command_extract_task_id(args: argparse.Namespace) -> int:
    with Path(args.activities).open("r", encoding="utf-8") as activities_file:
        data = json.load(activities_file)
    print(extract_task_id_from_activities(data))
    return 0


def command_failed_context(args: argparse.Namespace) -> int:
    with Path(args.activities).open("r", encoding="utf-8") as activities_file:
        data = json.load(activities_file)
    print(f"{extract_task_id_from_activities(data)}\t{classify_failed_activities(data)}")
    return 0


def command_decide(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    failed_sessions = read_failed_sessions(Path(args.failed_sessions))
    active_task_ids = read_active_task_ids(Path(args.active_task_ids) if args.active_task_ids else None)
    decision = decide_recovery(manifest, failed_sessions, active_task_ids)

    if args.github_output:
        write_github_outputs(Path(args.github_output), decision)
    if args.json:
        print(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(decision.action)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser("extract-task-id")
    extract.add_argument("activities")
    extract.set_defaults(func=command_extract_task_id)

    failed_context = subparsers.add_parser("failed-context")
    failed_context.add_argument("activities")
    failed_context.set_defaults(func=command_failed_context)

    decide = subparsers.add_parser("decide")
    decide.add_argument("--manifest", default="agent_tasks.json")
    decide.add_argument("--failed-sessions", required=True)
    decide.add_argument("--active-task-ids", default="")
    decide.add_argument("--github-output", default="")
    decide.add_argument("--json", action="store_true")
    decide.set_defaults(func=command_decide)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
