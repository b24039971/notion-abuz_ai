#!/usr/bin/env python3
"""Filter Jules sessions that should actually block next-task dispatch."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_RECENT_MAP_TTL_MINUTES = 60
DEFAULT_ACTIVE_STATES = {
    "QUEUED",
    "PLANNING",
    "IN_PROGRESS",
    "AWAITING_PLAN_APPROVAL",
    "AWAITING_USER_FEEDBACK",
}


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_json(path: Path) -> Any:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as data_file:
            return json.load(data_file)
    except (json.JSONDecodeError, OSError):
        return {}


def load_manifest_statuses(path: Path) -> dict[str, str]:
    data = load_json(path)
    statuses: dict[str, str] = {}
    for task in data.get("tasks", []):
        if not isinstance(task, dict):
            continue
        task_id = task.get("id")
        if isinstance(task_id, str) and task_id:
            statuses[task_id] = str(task.get("status") or "")
    return statuses


def parse_recent_map_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def load_recent_map(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    data = load_json(path)
    if isinstance(data, dict) and "value" in data:
        return parse_recent_map_value(data.get("value"))
    return parse_recent_map_value(data)


def session_id(session: dict[str, Any]) -> str:
    raw = session.get("id")
    if isinstance(raw, str) and raw:
        return raw.split("/")[-1]
    raw = session.get("name")
    if isinstance(raw, str) and raw:
        return raw.split("/")[-1]
    return ""


def session_name(session: dict[str, Any]) -> str:
    raw = session.get("name")
    if isinstance(raw, str) and raw:
        return raw
    raw = session.get("id")
    if isinstance(raw, str) and raw:
        return raw
    return ""


def recent_task_for_session(
    session: dict[str, Any],
    recent_map: dict[str, Any],
    *,
    now: datetime,
    ttl_minutes: int,
) -> tuple[str, bool, str]:
    sid = session_id(session)
    entry = recent_map.get(sid)
    if isinstance(entry, dict):
        value = entry.get("task_id")
        task_id = str(value or "")
        if not task_id:
            return "", False, ""
        update_time = str(entry.get("updateTime") or "")
        updated_at = parse_time(update_time)
        if not updated_at:
            return task_id, True, update_time
        if updated_at < now - timedelta(minutes=ttl_minutes):
            return task_id, True, update_time
        return task_id, False, update_time
    if isinstance(entry, str):
        task_id = entry.strip()
        return (task_id, True, "") if task_id else ("", False, "")
    return "", False, ""


def stopped_task_ids(raw: str) -> set[str]:
    return {item.strip() for item in raw.split(",") if item.strip()}


def filter_sessions(
    sessions_data: dict[str, Any],
    *,
    source: str,
    active_states: set[str],
    task_statuses: dict[str, str],
    recent_map: dict[str, Any],
    stopped_tasks: set[str],
    now: datetime | None = None,
    recent_map_ttl_minutes: int = DEFAULT_RECENT_MAP_TTL_MINUTES,
) -> dict[str, Any]:
    blocking: list[dict[str, str]] = []
    ignored: list[dict[str, str]] = []
    active_total = 0
    current_time = now or now_utc()

    for session in sessions_data.get("sessions", []):
        if not isinstance(session, dict):
            continue
        if str((session.get("sourceContext") or {}).get("source") or "") != source:
            continue
        state = str(session.get("state") or "")
        if state not in active_states:
            continue

        active_total += 1
        sid = session_id(session)
        task_id, stale_recent_map, recent_map_update_time = recent_task_for_session(
            session,
            recent_map,
            now=current_time,
            ttl_minutes=recent_map_ttl_minutes,
        )
        item = {
            "session_id": sid,
            "session_name": session_name(session),
            "state": state,
            "task_id": task_id,
        }
        if stale_recent_map:
            item["recent_map_stale"] = "true"
            if recent_map_update_time:
                item["recent_map_updateTime"] = recent_map_update_time

        if task_id:
            status = task_statuses.get(task_id, "")
            if status in {"done", "blocked"}:
                item["reason"] = f"manifest_status:{status}"
                ignored.append(item)
                continue
            if task_id in stopped_tasks:
                item["reason"] = "stopped_autonomous_pr"
                ignored.append(item)
                continue

            if stale_recent_map:
                if state == "IN_PROGRESS":
                    item["reason"] = "stale_recent_task_mapping"
                    ignored.append(item)
                    continue
                else:
                    item["reason"] = "stale_recent_task_mapping"
                    blocking.append(item)
                    continue

            item["reason"] = "active_task"
            blocking.append(item)
            continue

        if state == "IN_PROGRESS":
            item["reason"] = (
                "stale_recent_task_mapping"
                if stale_recent_map
                else "unknown_in_progress_task_id"
            )
            ignored.append(item)
            continue

        item["reason"] = (
            "stale_recent_task_mapping" if stale_recent_map else "unknown_task_id"
        )
        blocking.append(item)

    return {
        "active_total": active_total,
        "blocking_count": len(blocking),
        "ignored_count": len(ignored),
        "blocking_sessions": blocking,
        "ignored_sessions": ignored,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=Path("agent_tasks.json"))
    parser.add_argument("--recent-map", type=Path)
    parser.add_argument(
        "--recent-map-ttl-minutes", type=int, default=DEFAULT_RECENT_MAP_TTL_MINUTES
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--stopped-task-ids", default="")
    parser.add_argument(
        "--active-state",
        action="append",
        default=[],
        help="Active Jules state. Defaults to the known active states.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print debug list of blocking and ignored sessions",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    active_states = set(args.active_state or DEFAULT_ACTIVE_STATES)
    result = filter_sessions(
        load_json(args.sessions),
        source=args.source,
        active_states=active_states,
        task_statuses=load_manifest_statuses(args.manifest),
        recent_map=load_recent_map(args.recent_map),
        stopped_tasks=stopped_task_ids(args.stopped_task_ids),
        recent_map_ttl_minutes=max(1, args.recent_map_ttl_minutes),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if args.debug:
            import sys

            for item in result.get("blocking_sessions", []):
                print(
                    f"Blocking session: {item.get('session_name')} (reason: {item.get('reason')})",
                    file=sys.stderr,
                )
            for item in result.get("ignored_sessions", []):
                print(
                    f"Ignored session: {item.get('session_name')} (reason: {item.get('reason')})",
                    file=sys.stderr,
                )
        print(result["blocking_count"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
