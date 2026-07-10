"""Observe-Orient-Decide-Act helper for the notion-abuz_ai task loop.

This is a repository-local adaptation of the Magda-agent Rool loop. It does not
call Jules or external APIs; it helps an autonomous worker select a task,
inspect its allowed paths, and optionally run local validation commands.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


SAFE_RISKS = {"low", "medium"}


@dataclass(frozen=True)
class SelectedTask:
    """A selected autonomous task."""

    task_id: str
    title: str
    description: str
    risk: str
    area: str
    allowed_paths: list[str]
    acceptance: list[str]


def load_manifest(path: Path) -> dict[str, Any]:
    """Load the autonomous task manifest."""
    with path.open("r", encoding="utf-8") as manifest_file:
        data = json.load(manifest_file)
    if not isinstance(data, dict):
        raise ValueError("manifest root must be an object")
    return data


def observe(
    data: dict[str, Any], task_id: str | None, risk_ceiling: str
) -> SelectedTask | None:
    """Observe the manifest and select a task."""
    print("--- OBSERVE ---")
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("manifest tasks must be an array")

    allowed_risks = {"low"} if risk_ceiling == "low" else SAFE_RISKS
    todos = [
        task
        for task in tasks
        if isinstance(task, dict) and task.get("status") == "todo"
    ]
    print(f"Todo tasks: {len(todos)}")

    for task in todos:
        current_id = task.get("id")
        risk = task.get("risk")
        if task_id and current_id != task_id:
            continue
        if risk not in allowed_risks:
            print(
                f"Skipping {current_id}: risk {risk!r} exceeds ceiling {risk_ceiling!r}"
            )
            continue
        return task_from_dict(task)

    return None


def task_from_dict(task: dict[str, Any]) -> SelectedTask:
    """Convert a manifest task object to SelectedTask."""
    return SelectedTask(
        task_id=str(task.get("id", "")),
        title=str(task.get("title", "")),
        description=str(task.get("description", "")),
        risk=str(task.get("risk", "")),
        area=str(task.get("area", "")),
        allowed_paths=[str(path) for path in task.get("allowed_paths", [])],
        acceptance=[str(item) for item in task.get("acceptance", [])],
    )


def orient(task: SelectedTask, repo_root: Path) -> None:
    """Print task context and allowed path status."""
    print("--- ORIENT ---")
    print(f"Task: {task.task_id}")
    print(f"Title: {task.title}")
    print(f"Area: {task.area}")
    print(f"Risk: {task.risk}")
    print(f"Goal: {task.description}")
    print("Allowed paths:")
    for allowed_path in task.allowed_paths:
        matches = (
            list(repo_root.glob(allowed_path))
            if any(ch in allowed_path for ch in "*?[")
            else []
        )
        literal = repo_root / allowed_path
        if literal.exists():
            print(f"  EXISTS  {allowed_path}")
        elif matches:
            print(f"  GLOB    {allowed_path} ({len(matches)} match(es))")
        else:
            print(f"  MISSING {allowed_path}")
    print("Acceptance:")
    for item in task.acceptance:
        print(f"  - {item}")


def decide(task: SelectedTask, validation: str) -> str:
    """Choose the local action to run."""
    print("--- DECIDE ---")
    if validation == "none":
        action = "report"
    elif validation == "manifest":
        action = "validate-manifest"
    elif validation == "fast":
        action = "validate-fast"
    else:
        action = "validate-full"
    print(f"Action: {action} for {task.task_id}")
    return action


def run_command(args: Sequence[str], cwd: Path) -> int:
    """Run one command and stream output."""
    print(f"$ {' '.join(args)}")
    completed = subprocess.run(args, cwd=cwd, text=True)
    return completed.returncode


def check_gofmt(repo_root: Path) -> int:
    """Return non-zero when gofmt would change files."""
    print("$ gofmt -l .")
    completed = subprocess.run(
        ["gofmt", "-l", "."], cwd=repo_root, text=True, capture_output=True
    )
    if completed.stdout.strip():
        print(completed.stdout, end="")
        print("gofmt reported unformatted files", file=sys.stderr)
        return 1
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    return completed.returncode


def copy_dashboard_dist(repo_root: Path) -> int:
    """Copy web/dist to internal/web/dist using Python stdlib."""
    source = repo_root / "web" / "dist"
    target = repo_root / "internal" / "web" / "dist"
    if not source.exists():
        print(f"missing dashboard build output: {source}", file=sys.stderr)
        return 1
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    print(f"copied {source} -> {target}")
    return 0


def act(action: str, repo_root: Path) -> int:
    """Run the selected local action."""
    print("--- ACT ---")
    if action == "report":
        print("No validation requested.")
        return 0

    commands: list[tuple[str, Sequence[str] | None, Path]] = [
        (
            "validate manifest",
            [sys.executable, "scripts/validate_agent_tasks.py", "agent_tasks.json"],
            repo_root,
        ),
    ]

    if action in {"validate-fast", "validate-full"}:
        commands.extend(
            [
                ("go test", ["go", "test", "./..."], repo_root),
            ]
        )

    if action == "validate-full":
        commands = [
            (
                "validate manifest",
                [sys.executable, "scripts/validate_agent_tasks.py", "agent_tasks.json"],
                repo_root,
            ),
            ("gofmt", None, repo_root),
            ("npm ci", ["npm", "ci"], repo_root / "web"),
            ("npm build", ["npm", "run", "build"], repo_root / "web"),
            ("copy dashboard", None, repo_root),
            ("go vet", ["go", "vet", "./..."], repo_root),
            ("go test", ["go", "test", "./..."], repo_root),
            (
                "go build",
                [
                    "go",
                    "build",
                    "-ldflags=-s -w",
                    "-o",
                    "notion-manager",
                    "./cmd/notion-manager",
                ],
                repo_root,
            ),
        ]

    for name, command, cwd in commands:
        print(f"--- {name} ---")
        if name == "gofmt":
            code = check_gofmt(repo_root)
        elif name == "copy dashboard":
            code = copy_dashboard_dist(repo_root)
        elif command is not None:
            code = run_command(command, cwd)
        else:
            code = 1
        if code != 0:
            print(f"{name} failed with exit code {code}", file=sys.stderr)
            return code
    return 0


def run_loop(args: argparse.Namespace) -> int:
    """Run one local observe-orient-decide-act cycle."""
    repo_root = Path(args.repo_root).resolve()
    manifest = load_manifest(repo_root / args.manifest)
    task = observe(manifest, task_id=args.task_id, risk_ceiling=args.risk_ceiling)
    if task is None:
        print("No matching safe todo task found.")
        return 0
    orient(task, repo_root)
    action = decide(task, validation=args.validation)
    return act(action, repo_root)


def main(argv: list[str] | None = None) -> int:
    """Run the local Rool loop helper."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".", help="repository root")
    parser.add_argument(
        "--manifest",
        default="agent_tasks.json",
        help="task manifest path relative to repo root",
    )
    parser.add_argument("--task-id", default=None, help="optional exact task id")
    parser.add_argument("--risk-ceiling", choices=["low", "medium"], default="medium")
    parser.add_argument(
        "--validation", choices=["none", "manifest", "fast", "full"], default="manifest"
    )
    return run_loop(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
