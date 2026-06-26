"""Validate the notion-abuz_ai autonomous task manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


VALID_STATUSES = {"todo", "in_progress", "done", "blocked"}
VALID_RISKS = {"low", "medium", "high", "critical"}
REQUIRED_TASK_FIELDS = {
    "id",
    "status",
    "area",
    "risk",
    "title",
    "description",
    "allowed_paths",
    "acceptance",
}


class ValidationError(ValueError):
    """Raised when the manifest violates the expected schema."""


def load_manifest(path: Path) -> dict[str, Any]:
    """Load a JSON manifest from disk."""
    try:
        with path.open("r", encoding="utf-8") as manifest_file:
            data = json.load(manifest_file)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{path}: invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValidationError("manifest root must be an object")
    return data


def require_string(value: Any, field: str) -> str:
    """Return a non-empty string or raise a validation error."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    return value


def require_string_list(value: Any, field: str) -> list[str]:
    """Return a non-empty list of non-empty strings or raise an error."""
    if not isinstance(value, list) or not value:
        raise ValidationError(f"{field} must be a non-empty array")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValidationError(f"{field}[{index}] must be a non-empty string")
        result.append(item)
    return result


def validate_replenishment_policy(policy: Any) -> int:
    """Validate the replenishment policy and return the minimum todo count."""
    if not isinstance(policy, dict):
        raise ValidationError("replenishment_policy must be an object")

    minimum = policy.get("minimum_todo_tasks")
    if not isinstance(minimum, int) or minimum < 1:
        raise ValidationError("replenishment_policy.minimum_todo_tasks must be a positive integer")

    batch_size = policy.get("batch_size")
    if not isinstance(batch_size, int) or batch_size < 1:
        raise ValidationError("replenishment_policy.batch_size must be a positive integer")

    max_todo = policy.get("max_todo_tasks")
    if max_todo is not None and (not isinstance(max_todo, int) or max_todo < minimum):
        raise ValidationError("replenishment_policy.max_todo_tasks must be >= minimum_todo_tasks")

    allowed = policy.get("allowed_risks_for_generated_tasks")
    if not isinstance(allowed, list) or not allowed:
        raise ValidationError("replenishment_policy.allowed_risks_for_generated_tasks must be a non-empty array")
    invalid = set(allowed).difference(VALID_RISKS)
    if invalid:
        raise ValidationError(f"invalid generated task risks: {sorted(invalid)}")

    require_string(policy.get("instruction"), "replenishment_policy.instruction")
    return minimum


def validate_task(task: Any, index: int, seen_ids: set[str]) -> str:
    """Validate one task and return its status."""
    if not isinstance(task, dict):
        raise ValidationError(f"tasks[{index}] must be an object")

    missing = REQUIRED_TASK_FIELDS.difference(task)
    if missing:
        raise ValidationError(f"tasks[{index}] missing required fields: {sorted(missing)}")

    task_id = require_string(task.get("id"), f"tasks[{index}].id")
    if task_id in seen_ids:
        raise ValidationError(f"duplicate task id: {task_id}")
    seen_ids.add(task_id)

    status = require_string(task.get("status"), f"tasks[{index}].status")
    if status not in VALID_STATUSES:
        raise ValidationError(f"task {task_id}: invalid status {status!r}")

    risk = require_string(task.get("risk"), f"tasks[{index}].risk")
    if risk not in VALID_RISKS:
        raise ValidationError(f"task {task_id}: invalid risk {risk!r}")

    require_string(task.get("area"), f"task {task_id}.area")
    require_string(task.get("title"), f"task {task_id}.title")
    require_string(task.get("description"), f"task {task_id}.description")
    require_string_list(task.get("allowed_paths"), f"task {task_id}.allowed_paths")
    require_string_list(task.get("acceptance"), f"task {task_id}.acceptance")

    return status


def validate_manifest(data: dict[str, Any]) -> list[str]:
    """Validate a task manifest and return non-fatal warnings."""
    warnings: list[str] = []

    if data.get("schema_version") != 1:
        raise ValidationError("schema_version must be 1")

    require_string(data.get("project"), "project")
    require_string_list(data.get("task_source_priority"), "task_source_priority")

    risk_levels = data.get("risk_levels")
    if set(risk_levels or []) != VALID_RISKS:
        raise ValidationError(f"risk_levels must be exactly {sorted(VALID_RISKS)}")

    merge_policy = data.get("merge_policy")
    if not isinstance(merge_policy, dict):
        raise ValidationError("merge_policy must be an object")
    missing_policy = VALID_RISKS.difference(merge_policy)
    if missing_policy:
        raise ValidationError(f"merge_policy missing risk levels: {sorted(missing_policy)}")

    minimum_todo_tasks = validate_replenishment_policy(data.get("replenishment_policy"))

    loop_policy = data.get("autonomous_loop_policy")
    if not isinstance(loop_policy, dict):
        raise ValidationError("autonomous_loop_policy must be an object")
    for field in ("operating_model", "selection_rule", "anti_stall_rule", "max_pr_scope", "failure_rule"):
        require_string(loop_policy.get(field), f"autonomous_loop_policy.{field}")

    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValidationError("tasks must be a non-empty array")

    seen_ids: set[str] = set()
    todo_count = 0
    for index, task in enumerate(tasks):
        status = validate_task(task, index, seen_ids)
        if status == "todo":
            todo_count += 1

    if todo_count < minimum_todo_tasks:
        warnings.append(
            f"todo task count {todo_count} is below replenishment minimum {minimum_todo_tasks}"
        )

    return warnings


def main(argv: list[str] | None = None) -> int:
    """Run the manifest validator CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="Path to agent_tasks.json")
    args = parser.parse_args(argv)

    try:
        manifest = load_manifest(args.manifest)
        warnings = validate_manifest(manifest)
    except ValidationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)

    print(f"OK: {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
