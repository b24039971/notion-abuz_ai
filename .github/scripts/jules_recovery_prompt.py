#!/usr/bin/env python3
"""Build situation-aware recovery prompts for stuck Jules sessions."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


AUTONOMOUS_CONTINUE_TOKEN = "AUTONOMOUS_CONTINUE_TOKEN"
MAX_EXCERPT_CHARS = 700
MAX_LIST_ITEMS = 8

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

ROUTINE_QUESTION_MARKERS = (
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

VALIDATION_MARKERS = (
    "test",
    "tests",
    "go test",
    "pytest",
    "npm test",
    "run validation",
    "run checks",
    "запустить тест",
    "проверк",
    "валидац",
)

TRANSIENT_CONTEXT_MARKERS = (
    "api error",
    "api failure",
    "rate limit",
    "timeout",
    "timed out",
    "transient",
    "partial",
    "truncated",
    "could not read",
    "couldn't read",
    "search failed",
    "list files",
    "список файлов",
    "ошибка api",
    "ошибка апи",
    "непол",
    "обрез",
)

MISSING_SECRET_MARKERS = (
    "secret",
    "secrets",
    "credential",
    "credentials",
    "password",
    "access token",
    "auth token",
    "api token",
    "github token",
    "api key",
    "permission",
    "permissions",
    "production access",
    "prod access",
    "live secret",
    "секрет",
    "парол",
    "ключ",
    "доступ",
    "разрешени",
)

HIGH_RISK_MARKERS = (
    "high risk",
    "critical risk",
    "destructive",
    "delete production",
    "drop table",
    "wipe",
    "force push",
    "опасн",
    "критич",
    "destructive action",
)

CONFLICT_SYNC_MARKERS = (
    "merge conflict",
    "conflict",
    "behind master",
    "behind main",
    "rebase",
    "sync with master",
    "sync latest master",
    "конфликт",
    "синхрониз",
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

SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)\b(authorization|api[\s_-]?key|token|secret|password|passwd|cookie)"
            r"(\s*[:=]\s*)([^\s,;]+)"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1[REDACTED]"),
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]+)\b"), "[REDACTED]"),
    (re.compile(r"(?i)(https?://[^/\s:@]+:)[^@\s/]+(@)"), r"\1[REDACTED]\2"),
)


def parse_epoch(value: Any) -> int:
    if not value:
        return 0
    try:
        normalized = str(value).replace("Z", "+00:00")
        return int(datetime.fromisoformat(normalized).timestamp())
    except ValueError:
        return 0


def compact_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def sanitize_text(value: str, *, limit: int = MAX_EXCERPT_CHARS) -> str:
    sanitized = value
    for pattern, replacement in SECRET_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    sanitized = compact_space(sanitized)
    if len(sanitized) > limit:
        sanitized = sanitized[:limit].rstrip() + "..."
    return sanitized


def strings_from(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        preferred = ("text", "content", "message", "body", "summary")
        for key in preferred:
            if key in value:
                found.extend(strings_from(value[key]))
        for key, item in value.items():
            if key not in preferred:
                found.extend(strings_from(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(strings_from(item))
    return found


def activity_text(activity: dict[str, Any]) -> str:
    text = " ".join(strings_from(activity))
    if text:
        return text
    return json.dumps(activity, ensure_ascii=False)


def extract_task_id_from_blob(data: Any) -> str:
    text = json.dumps(data, ensure_ascii=False)
    for match in TASK_ID_RE.finditer(text):
        task_id = match.group(1).strip().strip('"')
        if task_id and task_id.lower() not in {"null", "none", "task_id"}:
            return task_id
    return ""


def classify_wait(latest_agent_text: str) -> tuple[str, str, str]:
    lower = latest_agent_text.lower()
    has_question = "?" in latest_agent_text or "？" in latest_agent_text

    if any(marker in lower for marker in FINALIZE_MARKERS):
        return "finalize", "finalize", "finalize_pr"
    if any(marker in lower for marker in MISSING_SECRET_MARKERS):
        return "continue", "missing_secret_or_permission", "block_with_reason"
    if any(marker in lower for marker in HIGH_RISK_MARKERS):
        return "continue", "high_risk_confirmation", "block_or_limit_scope"
    if any(marker in lower for marker in CONFLICT_SYNC_MARKERS):
        return "continue", "conflict_or_sync", "sync_branch"
    if any(marker in lower for marker in TRANSIENT_CONTEXT_MARKERS):
        return "continue", "transient_api_or_partial_context", "repeat_targeted_context_collection"
    if has_question and any(marker in lower for marker in ROUTINE_QUESTION_MARKERS):
        return "continue", "routine_question", "choose_safe_next_step"
    if any(marker in lower for marker in VALIDATION_MARKERS):
        return "continue", "validation_request", "run_safe_validation"
    return "continue", "unknown_continue", "continue_safely"


def summarize_activities(activities: list[dict[str, Any]]) -> dict[str, Any]:
    latest_agent_epoch = 0
    latest_user_epoch = 0
    latest_token_epoch = 0
    continue_token_count = 0
    latest_agent_text = ""

    for activity in activities:
        if not isinstance(activity, dict):
            continue
        originator = str(activity.get("originator", "")).lower()
        is_user = "user" in originator
        epoch = parse_epoch(activity.get("createTime"))
        blob = json.dumps(activity, ensure_ascii=False)

        if is_user:
            latest_user_epoch = max(latest_user_epoch, epoch)
            if AUTONOMOUS_CONTINUE_TOKEN in blob:
                continue_token_count += 1
                latest_token_epoch = max(latest_token_epoch, epoch)
            continue

        if epoch >= latest_agent_epoch:
            latest_agent_epoch = epoch
            latest_agent_text = activity_text(activity)

    wait_kind, wait_reason, prompt_action = classify_wait(latest_agent_text)
    return {
        "latest_agent_epoch": latest_agent_epoch,
        "latest_user_epoch": latest_user_epoch,
        "latest_token_epoch": latest_token_epoch,
        "continue_token_count": continue_token_count,
        "wait_kind": wait_kind,
        "wait_reason": wait_reason,
        "prompt_action": prompt_action,
        "latest_agent_excerpt": sanitize_text(latest_agent_text),
        "task_id": extract_task_id_from_blob({"activities": activities}),
    }


def load_manifest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as manifest_file:
        data = json.load(manifest_file)
    if not isinstance(data, dict):
        raise ValueError("manifest root must be an object")
    return data


def task_details_from_manifest(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    details: dict[str, dict[str, Any]] = {}
    for task in manifest.get("tasks", []):
        if isinstance(task, dict) and task.get("id"):
            details[str(task["id"])] = {
                "id": str(task.get("id") or ""),
                "status": str(task.get("status") or ""),
                "area": str(task.get("area") or ""),
                "risk": str(task.get("risk") or ""),
                "title": str(task.get("title") or ""),
                "allowed_paths": [str(item) for item in task.get("allowed_paths", []) if item],
                "acceptance": [str(item) for item in task.get("acceptance", []) if item],
            }
    return details


def compact_list(items: list[str], *, limit: int = MAX_LIST_ITEMS) -> list[str]:
    cleaned = [sanitize_text(str(item), limit=220) for item in items if str(item).strip()]
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + [f"... еще {len(cleaned) - limit}"]


def task_lines(task: dict[str, Any] | None) -> list[str]:
    if not task:
        return ["- task_details: not found in agent_tasks.json"]

    lines = [
        f"- task_status: {task.get('status') or 'unknown'}",
        f"- task_risk: {task.get('risk') or 'unknown'}",
        f"- task_area: {task.get('area') or 'unknown'}",
        f"- task_title: {sanitize_text(str(task.get('title') or ''), limit=220) or 'unknown'}",
    ]
    allowed_paths = compact_list(list(task.get("allowed_paths") or []))
    acceptance = compact_list(list(task.get("acceptance") or []), limit=6)
    if allowed_paths:
        lines.append("- allowed_paths: " + "; ".join(allowed_paths))
    if acceptance:
        lines.append("- acceptance: " + " | ".join(acceptance))
    return lines


def pr_check_context_lines(pr_context: dict[str, Any] | None) -> list[str]:
    if not pr_context:
        return []

    lines = ["- pr_context: available"]
    repo = sanitize_text(str(pr_context.get("repo") or ""), limit=180)
    pr_number = sanitize_text(str(pr_context.get("pr_number") or ""), limit=80)
    head_sha = sanitize_text(str(pr_context.get("head_sha") or ""), limit=80)
    if repo:
        lines.append(f"- repo: {repo}")
    if pr_number:
        lines.append(f"- pr_number: {pr_number}")
    if head_sha:
        lines.append(f"- pr_head_sha: {head_sha}")

    changed_files = pr_context.get("changed_files") or []
    if isinstance(changed_files, list) and changed_files:
        lines.append("- changed_files:")
        for path in changed_files[:MAX_LIST_ITEMS]:
            clean = sanitize_text(str(path), limit=220)
            if clean:
                lines.append(f"  - {clean}")
        if len(changed_files) > MAX_LIST_ITEMS:
            lines.append(f"  - ... еще {len(changed_files) - MAX_LIST_ITEMS}")

    failed_checks = pr_context.get("failed_checks") or []
    if not isinstance(failed_checks, list) or not failed_checks:
        return lines

    lines.append("- failed_checks:")
    for item in failed_checks[:MAX_LIST_ITEMS]:
        if not isinstance(item, dict):
            continue
        name = sanitize_text(str(item.get("name") or "unknown"), limit=180)
        conclusion = sanitize_text(str(item.get("conclusion") or "failure"), limit=80)
        run_id = sanitize_text(str(item.get("run_id") or ""), limit=80)
        details_url = sanitize_text(str(item.get("details_url") or ""), limit=240)
        detail = f"details: {details_url}" if details_url else f"run_id: {run_id}" if run_id else "details: unavailable"
        lines.append(f"  - {name}: {conclusion}; {detail}")
        annotations = item.get("annotations") or []
        if isinstance(annotations, list):
            for annotation in annotations[:MAX_LIST_ITEMS]:
                clean = sanitize_text(str(annotation), limit=360)
                if clean:
                    lines.append(f"    annotation: {clean}")
        log_excerpt = sanitize_text(str(item.get("log_excerpt") or ""), limit=900)
        if log_excerpt:
            lines.append(f"    log_excerpt: {log_excerpt}")
        annotations_error = sanitize_text(str(item.get("annotations_error") or ""), limit=260)
        if annotations_error:
            lines.append(f"    annotations_unavailable: {annotations_error}")
        log_excerpt_error = sanitize_text(str(item.get("log_excerpt_error") or ""), limit=260)
        if log_excerpt_error:
            lines.append(f"    log_excerpt_unavailable: {log_excerpt_error}")
    if len(failed_checks) > MAX_LIST_ITEMS:
        lines.append(f"  - ... еще {len(failed_checks) - MAX_LIST_ITEMS}")
    return lines


def action_instruction(prompt_action: str, *, mode: str) -> str:
    if mode == "stale" and prompt_action in {"continue_safely", "choose_safe_next_step"}:
        return (
            "Предыдущий autonomous continue уже был отправлен, но сессия всё ещё ждёт пользователя. "
            "Не повторяй вопрос. Выбери безопасный исход: завершить задачу в текущем scope либо оформить blocked."
        )
    if prompt_action == "finalize_pr":
        return (
            "Финализируй задачу: синхронизируй ветку с последним master, запусти релевантные проверки, "
            "обнови agent_tasks.json и открой один PR с label `jules` и корректным AUTONOMOUS_TASK_EVIDENCE."
        )
    if prompt_action == "run_safe_validation":
        return (
            "Запусти безопасные локальные/offline проверки внутри allowed_paths. Если проверка требует секреты, "
            "production-доступ или destructive action, оформи задачу как blocked с concrete blocked_reason."
        )
    if prompt_action == "repeat_targeted_context_collection":
        return (
            "Не продолжай по частичным данным. Повтори targeted search/read для недополученного контекста, "
            "зафиксируй transient API/search failure в выводе и продолжай только после повторной проверки."
        )
    if prompt_action == "block_with_reason":
        return (
            "Не запрашивай секреты или production-доступ в этой сессии. Отметь задачу blocked в agent_tasks.json, "
            "добавь concrete blocked_reason и открой manifest-only PR, если продолжение невозможно без этих данных."
        )
    if prompt_action == "block_or_limit_scope":
        return (
            "Если это bounded high-risk legacy/offline/lab task, продолжай только внутри task scope/allowed_paths "
            "и только с concrete CI/smoke/artifact/self-hosted evidence, rollback plan и обратимыми шагами. "
            "Critical, destructive или unguarded high-risk действие не выполняй: оформи blocked с concrete blocked_reason."
        )
    if prompt_action == "sync_branch":
        return (
            "Синхронизируйся с последним master/default branch. При конфликте сохрани актуальную очередь из master "
            "и наложи только изменения текущей задачи; если конфликт небезопасен, оформи blocked."
        )
    if prompt_action == "choose_safe_next_step":
        return (
            "Выбери сам самый маленький безопасный обратимый шаг внутри scope/allowed_paths и продолжай. "
            "Не задавай пользователю вопрос-подтверждение."
        )
    return (
        "Продолжай без ожидания ответа пользователя. Если можно безопасно завершить задачу внутри scope, сделай это; "
        "если нельзя без секретов, production-доступа, destructive или unguarded high-risk действия, оформи blocked."
    )


def build_prompt_payload(
    *,
    summary: dict[str, Any],
    task: dict[str, Any] | None = None,
    task_id: str = "",
    repo: str = "",
    session_id: str = "",
    session_state: str = "",
    mode: str = "continue",
    stale_reason: str = "",
    max_continue_attempts: int = 2,
    pr_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_task_id = task_id or str(summary.get("task_id") or "")
    wait_reason = str(summary.get("wait_reason") or "unknown_continue")
    prompt_action = str(summary.get("prompt_action") or "continue_safely")
    if mode == "stale" and prompt_action == "continue_safely":
        prompt_action = "choose_safe_next_step"

    continue_attempts = int(summary.get("continue_token_count") or 0)
    excerpt = str(summary.get("latest_agent_excerpt") or "")
    context_lines = [
        "AUTONOMOUS_CONTINUE_TOKEN",
        "",
        "Recovery context для зависшей Jules-сессии:",
    ]
    repo = sanitize_text(repo, limit=180)
    session_id = sanitize_text(session_id, limit=120)
    session_state = sanitize_text(session_state, limit=80)
    if repo:
        context_lines.append(f"- repo: {repo}")
    if session_id:
        context_lines.append(f"- session_id: {session_id}")
    if session_state:
        context_lines.append(f"- session_state: {session_state}")
    context_lines.extend([
        f"- task_id: {resolved_task_id or 'unknown'}",
        f"- wait_reason: {wait_reason}",
        f"- prompt_action: {prompt_action}",
        f"- continue_attempts: {continue_attempts}/{max_continue_attempts}",
    ])
    if stale_reason:
        context_lines.append(f"- stale_reason: {sanitize_text(stale_reason, limit=240)}")
    if excerpt:
        context_lines.append(f"- sanitized_latest_jules_message: {excerpt}")
    context_lines.extend(task_lines(task))
    context_lines.extend(pr_check_context_lines(pr_context))

    prompt = "\n".join(
        context_lines
        + [
            "",
            "Что сделать сейчас:",
            action_instruction(prompt_action, mode=mode),
            (
                "Если pr_context содержит failed_checks: используй annotations/log_excerpt/changed_files как первичный "
                "recovery packet; если excerpts недостаточно, открой/read linked job logs и артефакты этих checks; "
                "если лог не содержит конкретных файлов, воспроизведи failing command локально в текущей PR branch; "
                "исправь причину в этом же PR внутри allowed_paths."
                if pr_context and pr_context.get("failed_checks")
                else ""
            ),
            "",
            "Ограничения:",
            "- Пиши пользовательские сообщения, PR title/body и финальные summary на русском.",
            "- Оставайся внутри выбранного task_id, scope и allowed_paths.",
            "- Не расширяй права, не запрашивай секреты и не логируй raw credentials/account data.",
            "- Не создавай новый PR или новую Jules-сессию ради micro follow-up; заверши текущую задачу одним PR.",
            "- В PR body добавь ровно один AUTONOMOUS_TASK_EVIDENCE. Для blocked обязательно добавь blocked_reason.",
            "- В следующем сообщении кратко укажи: этап плана, что сделано, что дальше, зачем, почему так, проверки/риски.",
        ]
    )

    return {
        "task_id": resolved_task_id,
        "wait_reason": wait_reason,
        "prompt_action": prompt_action,
        "continue_attempts": continue_attempts,
        "max_continue_attempts": max_continue_attempts,
        "repo": repo,
        "session_id": session_id,
        "session_state": session_state,
        "summary": summary,
        "pr_context": pr_context or {},
        "prompt": prompt,
    }


def build_from_activities(
    *,
    activities: list[dict[str, Any]],
    manifest: dict[str, Any] | None = None,
    task_id: str = "",
    repo: str = "",
    session_id: str = "",
    session_state: str = "",
    mode: str = "continue",
    stale_reason: str = "",
    max_continue_attempts: int = 2,
    pr_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = summarize_activities(activities)
    resolved_task_id = task_id or str(summary.get("task_id") or "")
    task = None
    if manifest:
        task = task_details_from_manifest(manifest).get(resolved_task_id)
    return build_prompt_payload(
        summary=summary,
        task=task,
        task_id=resolved_task_id,
        repo=repo,
        session_id=session_id,
        session_state=session_state,
        mode=mode,
        stale_reason=stale_reason,
        max_continue_attempts=max_continue_attempts,
        pr_context=pr_context,
    )


def command_build(args: argparse.Namespace) -> int:
    with Path(args.activities).open("r", encoding="utf-8") as activities_file:
        activities_data = json.load(activities_file)
    activities = activities_data.get("activities", [])
    if not isinstance(activities, list):
        raise ValueError("activities root must contain an activities list")

    manifest = None
    if args.manifest:
        manifest_path = Path(args.manifest)
        if manifest_path.exists():
            manifest = load_manifest(manifest_path)

    pr_context = None
    if args.pr_context_file:
        pr_context_path = Path(args.pr_context_file)
        if pr_context_path.exists():
            with pr_context_path.open("r", encoding="utf-8") as pr_context_file:
                loaded_pr_context = json.load(pr_context_file)
            if not isinstance(loaded_pr_context, dict):
                raise ValueError("pr context root must be an object")
            pr_context = loaded_pr_context

    payload = build_from_activities(
        activities=activities,
        manifest=manifest,
        task_id=args.task_id,
        repo=args.repo,
        session_id=args.session_id,
        session_state=args.session_state,
        mode=args.mode,
        stale_reason=args.stale_reason,
        max_continue_attempts=args.max_continue_attempts,
        pr_context=pr_context,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activities", required=True)
    parser.add_argument("--manifest", default="agent_tasks.json")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--repo", default="")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--session-state", default="")
    parser.add_argument("--mode", choices=("continue", "stale"), default="continue")
    parser.add_argument("--stale-reason", default="")
    parser.add_argument("--max-continue-attempts", type=int, default=2)
    parser.add_argument("--pr-context-file", default="")
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return command_build(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
