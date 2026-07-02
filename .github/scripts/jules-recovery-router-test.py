#!/usr/bin/env python3
"""Unit tests for jules-recovery-router.py planning logic."""

from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).with_name("jules-recovery-router.py")
SPEC = importlib.util.spec_from_file_location("jules_recovery_router", SCRIPT_PATH)
router = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = router
SPEC.loader.exec_module(router)


NOW = datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)
REPO = "Omnividente/notion-abuz_ai"
TASK_IDS = ["automation-health-failed-session-86122315", "proxy-runtime-fix"]


class FakeHTTPResponse:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self._body = body

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class FakeGitHubClient:
    def __init__(self, responses: list[dict]) -> None:
        self.repo = REPO
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        ok: tuple[int, ...] = (200, 201, 204),
    ) -> dict:
        self.calls.append((method, path))
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {path}")
        return self.responses.pop(0)


def epoch(minutes_ago: int) -> int:
    return int((NOW - timedelta(minutes=minutes_ago)).timestamp())


def pr(
    *,
    number: int = 10,
    labels: list[str] | None = None,
    head_ref: str = "jules/proxy-runtime-fix-1234567890123456789",
    sha: str = "abc123",
    user: str = "google-jules[bot]",
    body: str = "",
    comments: list[str] | None = None,
    check_runs: list[dict] | None = None,
    mergeable: bool | None = True,
    mergeable_state: str = "clean",
) -> dict:
    return {
        "number": number,
        "title": "Autonomous PR",
        "body": body,
        "mergeable": mergeable,
        "mergeable_state": mergeable_state,
        "labels": [{"name": label} for label in labels or []],
        "user": {"login": user},
        "head": {
            "ref": head_ref,
            "sha": sha,
            "repo": {"full_name": REPO},
        },
        "comments": [{"body": comment} for comment in comments or []],
        "check_runs": check_runs or [],
    }


def state(
    *,
    open_pulls: list[dict] | None = None,
    selector: dict | None = None,
    jules_sessions: list[dict] | None = None,
    task_statuses: dict[str, str] | None = None,
    recent_unattended: bool = True,
    recent_next: bool = False,
    burst_in_progress: bool = False,
    recent_health: bool = False,
) -> dict:
    workflow_runs: dict[str, list[dict]] = {
        "jules_next_task.yml": [],
        "jules_unattended_monitor.yml": [],
        "jules_burst_monitor.yml": [],
        "automation_health.yml": [],
        "jules_automerge.yml": [],
    }
    if recent_unattended:
        workflow_runs["jules_unattended_monitor.yml"].append(
            {"created_at": (NOW - timedelta(minutes=1)).isoformat(), "status": "completed"}
        )
    if recent_next:
        workflow_runs["jules_next_task.yml"].append(
            {"created_at": (NOW - timedelta(minutes=1)).isoformat(), "status": "completed"}
        )
    if burst_in_progress:
        workflow_runs["jules_burst_monitor.yml"].append(
            {"created_at": (NOW - timedelta(minutes=1)).isoformat(), "status": "in_progress"}
        )
    if recent_health:
        workflow_runs["automation_health.yml"].append(
            {"created_at": (NOW - timedelta(minutes=1)).isoformat(), "status": "completed"}
        )
    return {
        "open_pulls": open_pulls or [],
        "workflow_runs": workflow_runs,
        "selector": selector if selector is not None else {"selected": False, "reason": "none"},
        "jules": {"api_available": True, "sessions": jules_sessions or []},
        "task_statuses": task_statuses or {},
    }


def session(
    *,
    session_id: str = "1234567890123456789",
    state: str = "IN_PROGRESS",
    task_id: str = "automation-health-failed-session-86122315",
    latest_agent_epoch: int = 100,
    latest_user_epoch: int = 0,
    latest_token_epoch: int = 0,
    wait_kind: str = "continue",
) -> dict:
    return {
        "name": f"sessions/{session_id}",
        "session_id": session_id,
        "state": state,
        "task_id": task_id,
        "createTime": "2026-06-29T11:00:00Z",
        "updateTime": "2026-06-29T11:10:00Z",
        "activity_summary": {
            "latest_agent_epoch": latest_agent_epoch,
            "latest_user_epoch": latest_user_epoch,
            "latest_token_epoch": latest_token_epoch,
            "wait_kind": wait_kind,
            "task_id": task_id,
        },
    }


def plan(input_state: dict, ledger: dict | None = None, health_mode: str = "enforce") -> list:
    return router.plan_recovery_actions(
        input_state,
        ledger or {"version": 1, "actions": {}},
        repo=REPO,
        task_ids=TASK_IDS,
        now=NOW,
        health_mode=health_mode,
    )


class RecoveryRouterTest(unittest.TestCase):
    def test_github_get_retries_transient_503(self) -> None:
        client = router.GitHubClient(api_url="https://api.github.test", repo=REPO, token="token")
        transient = router.urllib.error.HTTPError(
            "https://api.github.test/repos/example/actions/runs",
            503,
            "Service Unavailable",
            {},
            io.BytesIO(b"temporary"),
        )

        with patch.object(router.urllib.request, "urlopen") as urlopen:
            with patch.object(router.time, "sleep") as sleep:
                urlopen.side_effect = [
                    transient,
                    FakeHTTPResponse(200, b'{"ok": true}'),
                ]

                result = client.request("GET", "/repos/example/actions/runs")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_github_post_does_not_retry_transient_503(self) -> None:
        client = router.GitHubClient(api_url="https://api.github.test", repo=REPO, token="token")
        transient = router.urllib.error.HTTPError(
            "https://api.github.test/repos/example/issues/1/comments",
            503,
            "Service Unavailable",
            {},
            io.BytesIO(b"temporary"),
        )

        with patch.object(router.urllib.request, "urlopen", side_effect=transient) as urlopen:
            with self.assertRaises(RuntimeError):
                client.request("POST", "/repos/example/issues/1/comments", {"body": "comment"})

        self.assertEqual(urlopen.call_count, 1)

    def test_open_pull_details_enrich_mergeability(self) -> None:
        open_pulls = [pr(labels=["jules"], mergeable=None, mergeable_state="")]
        client = FakeGitHubClient(
            [
                {
                    "number": 10,
                    "title": "Detailed PR",
                    "mergeable": False,
                    "mergeable_state": "dirty",
                    "labels": [{"name": "jules"}],
                    "head": {"ref": "jules/task-1234567890123456789", "sha": "def456"},
                }
            ]
        )

        router.enrich_open_pull_details(client, open_pulls)

        self.assertEqual(client.calls, [("GET", f"/repos/{REPO}/pulls/10")])
        self.assertEqual(open_pulls[0]["title"], "Detailed PR")
        self.assertFalse(open_pulls[0]["mergeable"])
        self.assertEqual(open_pulls[0]["mergeable_state"], "dirty")
        self.assertEqual(open_pulls[0]["head"]["sha"], "def456")

    def test_open_pull_details_retries_unknown_mergeability_once(self) -> None:
        open_pulls = [pr(labels=["jules"], mergeable=None, mergeable_state="")]
        client = FakeGitHubClient(
            [
                {"number": 10, "mergeable": None, "mergeable_state": "unknown"},
                {"number": 10, "mergeable": False, "mergeable_state": "dirty"},
            ]
        )

        with patch.object(router.time, "sleep") as sleep:
            router.enrich_open_pull_details(client, open_pulls)

        self.assertEqual(
            client.calls,
            [
                ("GET", f"/repos/{REPO}/pulls/10"),
                ("GET", f"/repos/{REPO}/pulls/10"),
            ],
        )
        sleep.assert_called_once_with(1)
        self.assertFalse(open_pulls[0]["mergeable"])
        self.assertEqual(open_pulls[0]["mergeable_state"], "dirty")

    def test_git_conflict_fallback_marks_unknown_pr_dirty(self) -> None:
        open_pulls = [
            pr(
                labels=["jules"],
                head_ref="jules/task-1234567890123456789",
                mergeable=None,
                mergeable_state="unknown",
            )
        ]
        fetch = router.subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        merge = router.subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="Auto-merging agent_tasks.json\nCONFLICT (content): Merge conflict in agent_tasks.json",
            stderr="",
        )

        with patch.object(router.subprocess, "run", side_effect=[fetch, merge]) as run:
            router.enrich_open_pull_git_conflicts(open_pulls, repo=REPO)

        self.assertEqual(run.call_count, 2)
        fetch_cmd = run.call_args_list[0].args[0]
        self.assertIn("--depth=2000", fetch_cmd)
        self.assertIn("master:refs/remotes/origin/master", fetch_cmd)
        self.assertIn("jules/task-1234567890123456789:refs/remotes/origin/jules/task-1234567890123456789", fetch_cmd)
        self.assertFalse(open_pulls[0]["mergeable"])
        self.assertEqual(open_pulls[0]["mergeable_state"], "dirty")
        self.assertEqual(open_pulls[0]["mergeability_source"], "git-merge-tree")

    def test_git_conflict_fallback_skips_computed_mergeability(self) -> None:
        open_pulls = [pr(labels=["jules"], mergeable=True, mergeable_state="clean")]

        with patch.object(router.subprocess, "run") as run:
            router.enrich_open_pull_git_conflicts(open_pulls, repo=REPO)

        run.assert_not_called()
        self.assertTrue(open_pulls[0]["mergeable"])
        self.assertEqual(open_pulls[0]["mergeable_state"], "clean")

    def test_quality_fix_posts_comment_and_sends_session_message(self) -> None:
        actions = plan(state(open_pulls=[pr(labels=["jules", "needs-quality-fix"])]))

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "quality_fix_recovery")
        self.assertEqual(actions[0].payload["pr_number"], 10)
        self.assertTrue(actions[0].payload["comment_needed"])
        self.assertIn("исправь этот же PR #10", actions[0].payload["body"])
        self.assertEqual(actions[0].payload["session_id"], "1234567890123456789")

    def test_quality_fix_prompt_includes_latest_quality_gate_details(self) -> None:
        quality_comment = """<!-- AUTONOMOUS_QUALITY_FIX_REQUEST pr-level -->

# Autonomous PR quality gate

Status: failed

Blocking reasons:
- PR body repeatedly mentions follow-up tasks.

New task ids:
- proxy-observability-json-tool-call-mode-loss-test-more
"""
        actions = plan(
            state(open_pulls=[pr(labels=["jules", "needs-quality-fix"], comments=[quality_comment])])
        )

        self.assertEqual(len(actions), 1)
        body = actions[0].payload["body"]
        self.assertIn("Детали текущего quality gate failure", body)
        self.assertIn("PR body repeatedly mentions follow-up tasks", body)
        self.assertIn("proxy-observability-json-tool-call-mode-loss-test-more", body)

    def test_quality_fix_comment_marker_prevents_duplicate(self) -> None:
        marker = "<!-- AUTONOMOUS_RECOVERY_ROUTER action=quality-fix sha=abc123 -->"
        ledger = {
            "version": 1,
            "actions": {
                "quality-fix:10:abc123": {
                    "time": (NOW - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                    "type": "quality_fix_recovery",
                }
            },
        }
        actions = plan(
            state(open_pulls=[pr(labels=["jules", "needs-quality-fix"], comments=[marker])]),
            ledger=ledger,
        )

        self.assertEqual(actions, [])

    def test_quality_fix_marker_without_ledger_still_sends_session_message(self) -> None:
        marker = "<!-- AUTONOMOUS_RECOVERY_ROUTER action=quality-fix sha=abc123 -->"
        actions = plan(state(open_pulls=[pr(labels=["jules", "needs-quality-fix"], comments=[marker])]))

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "quality_fix_recovery")
        self.assertFalse(actions[0].payload["comment_needed"])
        self.assertEqual(actions[0].payload["session_id"], "1234567890123456789")

    def test_quality_fix_recovery_retries_after_cooldown(self) -> None:
        marker = "<!-- AUTONOMOUS_RECOVERY_ROUTER action=quality-fix sha=abc123 -->"
        ledger = {
            "version": 1,
            "actions": {
                "quality-fix:10:abc123": {
                    "time": (NOW - timedelta(minutes=31)).isoformat().replace("+00:00", "Z"),
                    "type": "quality_fix_recovery",
                }
            },
        }

        actions = plan(
            state(open_pulls=[pr(labels=["jules", "needs-quality-fix"], comments=[marker])]),
            ledger=ledger,
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "quality_fix_recovery")
        self.assertFalse(actions[0].payload["comment_needed"])
        self.assertEqual(actions[0].ttl_minutes, 30)

    def test_quality_fix_waits_for_pending_checks_on_new_head(self) -> None:
        actions = plan(
            state(
                open_pulls=[
                    pr(
                        labels=["jules", "needs-quality-fix"],
                        check_runs=[{"name": "validate", "status": "in_progress"}],
                    )
                ]
            )
        )

        self.assertEqual(actions, [])

    def test_conflicting_jules_pr_sends_conflict_recovery(self) -> None:
        actions = plan(
            state(
                open_pulls=[
                    pr(
                        labels=["jules"],
                        mergeable=False,
                        mergeable_state="dirty",
                    )
                ]
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "conflict_recovery")
        self.assertEqual(actions[0].payload["pr_number"], 10)
        self.assertTrue(actions[0].payload["comment_needed"])
        self.assertEqual(actions[0].payload["session_id"], "1234567890123456789")
        self.assertIn("конфликтует с текущим `master`", actions[0].payload["body"])
        self.assertIn("не открывай новый PR", actions[0].payload["body"])

    def test_conflicting_quality_fix_pr_prioritizes_branch_sync(self) -> None:
        quality_comment = """<!-- AUTONOMOUS_QUALITY_FIX_REQUEST pr-level -->

# Autonomous PR quality gate

Status: failed

Blocking reasons:
- More than one task was marked done.
"""
        actions = plan(
            state(
                open_pulls=[
                    pr(
                        labels=["jules", "needs-quality-fix"],
                        comments=[quality_comment],
                        mergeable=False,
                        mergeable_state="dirty",
                    )
                ]
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "conflict_recovery")
        self.assertIn("unresolved quality gate details", actions[0].payload["body"])
        self.assertIn("More than one task was marked done", actions[0].payload["body"])

    def test_conflict_recovery_retries_after_cooldown(self) -> None:
        marker = "<!-- AUTONOMOUS_RECOVERY_ROUTER action=conflict-recovery sha=abc123 -->"
        ledger = {
            "version": 1,
            "actions": {
                "conflict-recovery:10:abc123": {
                    "time": (NOW - timedelta(minutes=31)).isoformat().replace("+00:00", "Z"),
                    "type": "conflict_recovery",
                }
            },
        }

        actions = plan(
            state(
                open_pulls=[
                    pr(
                        labels=["jules"],
                        comments=[marker],
                        mergeable=False,
                        mergeable_state="dirty",
                    )
                ]
            ),
            ledger=ledger,
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "conflict_recovery")
        self.assertFalse(actions[0].payload["comment_needed"])
        self.assertEqual(actions[0].ttl_minutes, 30)

    def test_missing_jules_label_is_repaired(self) -> None:
        actions = plan(
            state(
                open_pulls=[
                    pr(
                        labels=[],
                        user="someone",
                        head_ref="proxy-runtime-fix-branch",
                        body="task proxy-runtime-fix",
                    )
                ]
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "add_label")
        self.assertEqual(actions[0].payload["labels"], ["jules"])

    def test_failed_automerge_is_rerun_once(self) -> None:
        actions = plan(
            state(
                open_pulls=[
                    pr(
                        labels=["jules"],
                        check_runs=[
                            {
                                "name": "test-and-merge",
                                "conclusion": "failure",
                                "details_url": "https://github.com/o/r/actions/runs/12345/job/9",
                            }
                        ],
                    )
                ]
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "rerun_workflow")
        self.assertEqual(actions[0].payload["run_id"], "12345")

    def test_selected_task_beats_stale_unattended_monitor(self) -> None:
        actions = plan(
            state(
                recent_unattended=False,
                selector={"selected": True, "task_id": "automation-health-failed-session-86122315"},
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "dispatch_workflow")
        self.assertEqual(actions[0].payload["workflow"], "jules_next_task.yml")

    def test_idle_selected_task_dispatches_next_task_when_monitor_recent(self) -> None:
        actions = plan(
            state(
                selector={"selected": True, "task_id": "automation-health-failed-session-86122315"}
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "dispatch_workflow")
        self.assertEqual(actions[0].payload["workflow"], "jules_next_task.yml")

    def test_recent_next_task_dispatch_prevents_duplicate(self) -> None:
        actions = plan(
            state(
                recent_next=True,
                selector={"selected": True, "task_id": "automation-health-failed-session-86122315"},
            )
        )

        self.assertEqual(actions, [])

    def test_in_progress_burst_monitor_prevents_next_task_noise(self) -> None:
        actions = plan(
            state(
                burst_in_progress=True,
                selector={"selected": True, "task_id": "automation-health-failed-session-86122315"},
            )
        )

        self.assertEqual(actions, [])

    def test_active_jules_session_prevents_next_task_noise(self) -> None:
        actions = plan(
            state(
                jules_sessions=[session(state="IN_PROGRESS")],
                selector={"selected": True, "task_id": "automation-health-failed-session-86122315"},
            )
        )

        self.assertEqual(actions, [])

    def test_awaiting_plan_approval_is_approved_directly(self) -> None:
        actions = plan(state(jules_sessions=[session(state="AWAITING_PLAN_APPROVAL")]))

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "jules_approve_plan")
        self.assertEqual(actions[0].payload["session"], "sessions/1234567890123456789")

    def test_awaiting_user_feedback_sends_continue_directly(self) -> None:
        actions = plan(state(jules_sessions=[session(state="AWAITING_USER_FEEDBACK")]))

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "jules_send_message")
        self.assertIn("AUTONOMOUS_CONTINUE_TOKEN", actions[0].payload["prompt"])

    def test_awaiting_user_feedback_token_prevents_duplicate_continue(self) -> None:
        actions = plan(
            state(
                jules_sessions=[
                    session(
                        state="AWAITING_USER_FEEDBACK",
                        latest_agent_epoch=epoch(5),
                        latest_user_epoch=epoch(4),
                        latest_token_epoch=epoch(4),
                    )
                ]
            )
        )

        self.assertEqual(actions, [])

    def test_stale_awaiting_user_feedback_after_continue_escalates(self) -> None:
        actions = plan(
            state(
                jules_sessions=[
                    session(
                        state="AWAITING_USER_FEEDBACK",
                        latest_agent_epoch=epoch(90),
                        latest_user_epoch=epoch(55),
                        latest_token_epoch=epoch(55),
                    )
                ]
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "jules_send_message")
        self.assertTrue(actions[0].dedupe_key.startswith("stale-continue:1234567890123456789:"))
        self.assertIn("continue уже был отправлен", actions[0].payload["prompt"])

    def test_stale_recorded_continue_escalates_when_token_missing_from_activity(self) -> None:
        latest_agent = epoch(90)
        ledger = {
            "version": 1,
            "actions": {
                f"continue:1234567890123456789:{latest_agent}": {
                    "time": (NOW - timedelta(minutes=55)).isoformat().replace("+00:00", "Z"),
                    "type": "jules_send_message",
                }
            },
        }
        actions = plan(
            state(
                jules_sessions=[
                    session(
                        state="AWAITING_USER_FEEDBACK",
                        latest_agent_epoch=latest_agent,
                        latest_user_epoch=0,
                        latest_token_epoch=0,
                    )
                ]
            ),
            ledger=ledger,
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "jules_send_message")
        self.assertTrue(actions[0].dedupe_key.endswith(":attempt-1"))

    def test_stale_continue_escalation_has_cooldown_and_then_deletes_stale_session(self) -> None:
        latest_agent = epoch(90)
        prefix = f"stale-continue:1234567890123456789:{latest_agent}:"
        recent_ledger = {
            "version": 1,
            "actions": {
                f"{prefix}attempt-1": {
                    "time": (NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                    "type": "jules_send_message",
                }
            },
        }
        actions = plan(
            state(
                jules_sessions=[
                    session(
                        state="AWAITING_USER_FEEDBACK",
                        latest_agent_epoch=latest_agent,
                        latest_user_epoch=epoch(55),
                        latest_token_epoch=epoch(55),
                    )
                ]
            ),
            ledger=recent_ledger,
        )
        self.assertEqual(actions, [])

        exhausted_ledger = {
            "version": 1,
            "actions": {
                f"{prefix}attempt-{attempt}": {
                    "time": (NOW - timedelta(minutes=35 + attempt)).isoformat().replace("+00:00", "Z"),
                    "type": "jules_send_message",
                }
                for attempt in range(1, 4)
            },
        }
        actions = plan(
            state(
                jules_sessions=[
                    session(
                        state="AWAITING_USER_FEEDBACK",
                        latest_agent_epoch=latest_agent,
                        latest_user_epoch=epoch(55),
                        latest_token_epoch=epoch(55),
                    )
                ]
            ),
            ledger=exhausted_ledger,
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "jules_delete_session")
        self.assertEqual(actions[0].payload["session"], "sessions/1234567890123456789")

    def test_failed_session_retries_same_task_once(self) -> None:
        actions = plan(
            state(
                jules_sessions=[session(state="FAILED", session_id="1111111111111")],
                task_statuses={"automation-health-failed-session-86122315": "todo"},
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "dispatch_workflow")
        self.assertEqual(actions[0].payload["workflow"], "jules_next_task.yml")
        self.assertEqual(
            actions[0].payload["inputs"]["task_id"],
            "automation-health-failed-session-86122315",
        )

    def test_failed_session_for_unknown_task_is_ignored(self) -> None:
        actions = plan(
            state(
                jules_sessions=[session(state="FAILED", session_id="1111111111111")],
                task_statuses={},
            ),
            health_mode="disabled",
        )

        self.assertEqual(actions, [])

    def test_repeated_failed_sessions_block_task(self) -> None:
        actions = plan(
            state(
                jules_sessions=[
                    session(state="FAILED", session_id="1111111111111"),
                    session(state="FAILED", session_id="2222222222222"),
                ],
                task_statuses={"automation-health-failed-session-86122315": "todo"},
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "block_failed_task")
        self.assertEqual(actions[0].payload["task_id"], "automation-health-failed-session-86122315")

    def test_no_eligible_task_dispatches_health_enforce(self) -> None:
        actions = plan(
            state(
                selector={
                    "selected": False,
                    "reason_code": "no_eligible_autonomous_task",
                    "reason": "no eligible todo task matched the risk ceiling",
                }
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].payload["workflow"], "automation_health.yml")
        self.assertEqual(actions[0].payload["inputs"]["mode"], "enforce")

    def test_no_eligible_task_beats_stale_unattended_monitor(self) -> None:
        actions = plan(
            state(
                recent_unattended=False,
                selector={
                    "selected": False,
                    "reason_code": "no_eligible_autonomous_task",
                    "reason": "no eligible todo task matched the risk ceiling",
                },
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].payload["workflow"], "automation_health.yml")
        self.assertEqual(actions[0].payload["inputs"]["mode"], "enforce")

    def test_no_eligible_task_can_dispatch_health_shadow(self) -> None:
        actions = plan(
            state(selector={"selected": False, "reason": "no eligible task"}),
            health_mode="shadow",
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].payload["workflow"], "automation_health.yml")
        self.assertEqual(actions[0].payload["inputs"]["mode"], "shadow")

    def test_no_eligible_task_can_disable_health_dispatch(self) -> None:
        actions = plan(
            state(selector={"selected": False, "reason": "no eligible task"}),
            health_mode="disabled",
        )

        self.assertEqual(actions, [])

    def test_recent_health_dispatch_prevents_duplicate(self) -> None:
        actions = plan(
            state(
                recent_health=True,
                selector={"selected": False, "reason": "no eligible task"},
            )
        )

        self.assertEqual(actions, [])

    def test_ledger_prevents_duplicate_action_within_ttl(self) -> None:
        dedupe = "automation-health-enforce:no-eligible-task"
        ledger = {
            "version": 1,
            "actions": {
                dedupe: {
                    "time": (NOW - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                    "type": "dispatch_workflow",
                }
            },
        }
        actions = plan(
            state(selector={"selected": False, "reason": "no eligible task"}),
            ledger=ledger,
        )

        self.assertEqual(actions, [])


if __name__ == "__main__":
    unittest.main()
