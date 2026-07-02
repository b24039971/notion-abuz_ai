#!/usr/bin/env python3
"""Route stuck autonomous-loop states to one deterministic recovery action."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


LEDGER_VARIABLE = "JULES_RECOVERY_ROUTER_LEDGER"
ROUTER_MARKER = "AUTONOMOUS_RECOVERY_ROUTER"
QUALITY_FIX_MARKER = "AUTONOMOUS_QUALITY_FIX_REQUEST"
ACTIVE_NEXT_TASK_COOLDOWN_MINUTES = 10
HEALTH_ENFORCE_COOLDOWN_MINUTES = 20
RERUN_AUTOMERGE_COOLDOWN_MINUTES = 120
MONITOR_COOLDOWN_MINUTES = 7
QUALITY_FIX_RECOVERY_COOLDOWN_MINUTES = 30
CONFLICT_RECOVERY_COOLDOWN_MINUTES = 30
MAX_QUALITY_FIX_DETAILS_CHARS = 5000
STALE_AWAITING_FEEDBACK_MINUTES = 30
STALE_AWAITING_FEEDBACK_COOLDOWN_MINUTES = 30
MAX_STALE_AWAITING_FEEDBACK_ESCALATIONS = 3
SESSION_ID_RE = re.compile(r"(?<!\d)(\d{12,})(?!\d)")
AUTONOMOUS_CONTINUE_TOKEN = "AUTONOMOUS_CONTINUE_TOKEN"
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


FINALIZE_MARKERS = (
    "before i wrap up",
    "wrap up my work",
    "ready for review",
    "ready to finalize",
    "ready for submission",
    "all plan steps completed",
    "open a new pull request",
    "open the pull request",
    "open/finalize the pr",
    "anything else you'd like me to review",
    "anything else you would like me to review",
)
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
CONTINUE_PROMPT = """AUTONOMOUS_CONTINUE_TOKEN

Продолжай без ожидания ответа пользователя.

Если последнее сообщение просит выбрать между безопасными вариантами, выбери сам самый маленький обратимый low/medium-risk шаг и продолжай.
Если последнее сообщение спрашивает, нужно ли запустить локальный сервер, локальные тесты, offline reproduction или изучить логи/artifacts, ответ: да, сделай это сам, если действие безопасно, недеструктивно, остается внутри scope/allowed_paths и не требует секретов.
Если нужны live secrets, реальные credentials, production-доступ, high/critical risk или destructive action, не жди пользователя: зафиксируй concrete blocked_reason в agent_tasks.json и открой manifest-only PR.
Оставайся внутри scope выбранной задачи и allowed_paths.
Не создавай micro-PR/follow-up без live smoke, transcript, CI или offline reproduction evidence.
Когда задача готова, открой один PR с label `jules` и корректным AUTONOMOUS_TASK_EVIDENCE.
"""
FINALIZE_PROMPT = """AUTONOMOUS_CONTINUE_TOKEN

Дополнительное ревью не требуется. Финализируй эту задачу сейчас.

Синхронизируй ветку с последним master, запусти нужную валидацию, отметь выбранную задачу в agent_tasks.json и открой один PR с label `jules`.
В PR body добавь корректный AUTONOMOUS_TASK_EVIDENCE. Для blocked task обязательно добавь concrete blocked_reason.
Не задавай новый вопрос-подтверждение.
"""
STALE_FEEDBACK_PROMPT = """AUTONOMOUS_CONTINUE_TOKEN

Предыдущий autonomous continue уже был отправлен, но сессия всё ещё ждёт пользователя.

Не жди дополнительного подтверждения. Выбери один безопасный исход:
- если задачу можно завершить внутри scope/allowed_paths, синхронизируйся с master, запусти нужную валидацию и открой один PR с label `jules` и корректным AUTONOMOUS_TASK_EVIDENCE;
- если продолжение требует missing secrets, production-доступ, high/critical risk или destructive action, отметь задачу `blocked` в agent_tasks.json, добавь concrete blocked_reason и открой manifest-only PR.

Не задавай новый вопрос-подтверждение и не оставляй сессию в ожидании пользователя.
"""


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


def extract_task_id_from_blob(data: Any) -> str:
    text = json.dumps(data, ensure_ascii=False)
    for match in TASK_ID_RE.finditer(text):
        task_id = match.group(1).strip().strip('"')
        if task_id and task_id.lower() not in {"null", "none", "task_id"}:
            return task_id
    return ""


def summarize_activities(activities: list[dict[str, Any]]) -> dict[str, Any]:
    latest_agent_epoch = 0
    latest_user_epoch = 0
    latest_token_epoch = 0
    latest_agent_blob = ""

    for activity in activities:
        if not isinstance(activity, dict):
            continue
        originator = str(activity.get("originator", "")).lower()
        epoch = parse_epoch(activity.get("createTime"))
        blob = json.dumps(activity, ensure_ascii=False)
        if "user" in originator:
            latest_user_epoch = max(latest_user_epoch, epoch)
            if AUTONOMOUS_CONTINUE_TOKEN in blob:
                latest_token_epoch = max(latest_token_epoch, epoch)
            continue
        if epoch >= latest_agent_epoch:
            latest_agent_epoch = epoch
            latest_agent_blob = blob

    latest_agent_lower = latest_agent_blob.lower()
    wait_kind = "finalize" if any(marker in latest_agent_lower for marker in FINALIZE_MARKERS) else "continue"
    failure_kind = ""
    routine_markers = (
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
    if "?" in latest_agent_lower and any(marker in latest_agent_lower for marker in routine_markers):
        failure_kind = "routine_question"

    return {
        "latest_agent_epoch": latest_agent_epoch,
        "latest_user_epoch": latest_user_epoch,
        "latest_token_epoch": latest_token_epoch,
        "wait_kind": wait_kind,
        "failure_kind": failure_kind,
        "task_id": extract_task_id_from_blob({"activities": activities}),
    }


def is_autonomous_pr(pr: dict[str, Any], *, repo: str, task_ids: list[str]) -> bool:
    labels = labels_of(pr)
    head = pr.get("head") or {}
    head_ref = str(head.get("ref") or "")
    head_repo = str((head.get("repo") or {}).get("full_name") or "")
    user = str((pr.get("user") or {}).get("login") or "")
    title = str(pr.get("title") or "")
    body = str(pr.get("body") or "")

    if user == "google-jules[bot]" or "jules" in labels:
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
        or task_id in title
        or task_id in body
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


def latest_failed_automerge_run(pr: dict[str, Any]) -> str:
    for check_run in pr.get("check_runs", []):
        name = str(check_run.get("name") or "")
        conclusion = str(check_run.get("conclusion") or "")
        if name in {"test-and-merge", "validate-and-merge"} and conclusion == "failure":
            return check_run_run_id(check_run)
    return ""


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
        if QUALITY_FIX_MARKER not in body:
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
        return details
    return ""


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
        f"Jules, исправь этот же PR #{number}; не открывай новый PR и не создавай follow-up задачу.\n\n"
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
        "- синхронизируй PR branch с последним `master`;\n"
        "- resolve merge conflicts внутри scope текущей задачи и allowed_paths;\n"
        "- сохрани один task status/evidence в agent_tasks.json и AUTONOMOUS_TASK_EVIDENCE;\n"
        "- если конфликт нельзя безопасно решить внутри scope, отметь текущую задачу `blocked` с concrete blocked_reason;\n"
        "- запусти релевантные проверки и push исправление в эту же PR ветку.\n\n"
        "Не жди ответа пользователя, если действие безопасное и не требует секретов, production-доступа или destructive changes."
        f"{details_block}"
    )


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


def plan_stale_feedback_action(
    session: dict[str, Any],
    ledger: dict[str, Any],
    *,
    now: datetime,
    reason: str,
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
    return RecoveryAction(
        type="jules_send_message",
        dedupe_key=f"{prefix}attempt-{attempt}",
        reason=f"Jules session {session_id} is still awaiting user feedback after autonomous continue: {reason}",
        ttl_minutes=24 * 60,
        payload={"session": session_name, "prompt": STALE_FEEDBACK_PROMPT},
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
    ]
    open_prs.sort(key=lambda item: int(item.get("number") or 0))

    for pr in open_prs:
        labels = labels_of(pr)
        number = pr.get("number")
        sha = str((pr.get("head") or {}).get("sha") or "")
        if "stop-loop" in labels or "human-review" in labels or "no-automerge" in labels:
            continue

        if is_conflicting_pr(pr):
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

    if open_prs:
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
        if session_state == "AWAITING_USER_FEEDBACK" and should_continue_session(session):
            summary = session.get("activity_summary") or {}
            wait_kind = str(summary.get("wait_kind") or "continue")
            prompt = FINALIZE_PROMPT if wait_kind == "finalize" else CONTINUE_PROMPT
            dedupe_key = f"continue:{session_id}:{summary.get('latest_agent_epoch') or update_marker}"
            if not action_recently_done(ledger, dedupe_key, now=now, ttl_minutes=24 * 60):
                actions.append(
                    RecoveryAction(
                        type="jules_send_message",
                        dedupe_key=dedupe_key,
                        reason=f"Jules session {session_id} is awaiting user feedback",
                        ttl_minutes=24 * 60,
                        payload={"session": session_name, "prompt": prompt},
                    )
                )
            elif stale_after_recorded_continue(ledger, dedupe_key, now=now):
                action = plan_stale_feedback_action(
                    session,
                    ledger,
                    now=now,
                    reason="recorded continue did not produce new agent activity",
                )
                if action:
                    actions.append(action)
            return actions
        if session_state == "AWAITING_USER_FEEDBACK" and stale_after_autonomous_continue(session, now=now):
            action = plan_stale_feedback_action(
                session,
                ledger,
                now=now,
                reason="latest autonomous continue token is stale",
            )
            if action:
                actions.append(action)
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
        if health_mode == "disabled" or workflow_recently_created(
            state,
            "automation_health.yml",
            now=now,
            minutes=HEALTH_ENFORCE_COOLDOWN_MINUTES,
        ):
            return actions

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


def execute_action(
    client: GitHubClient,
    action: RecoveryAction,
    *,
    jules_clients: list[JulesClient] | None = None,
) -> None:
    payload = action.payload
    jules_clients = jules_clients or []
    if action.type in {"quality_fix_recovery", "conflict_recovery"}:
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
    raise ValueError(f"unsupported action type: {action.type}")


def run_selector(manifest: Path, *, focus: str, risk_ceiling: str) -> dict[str, Any]:
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
    open_pulls = client.request("GET", f"/repos/{client.repo}/pulls?state=open&per_page=100") or []
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
        "selector": run_selector(manifest, focus=focus, risk_ceiling=risk_ceiling),
        "task_statuses": task_statuses_from_manifest(manifest_data),
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
