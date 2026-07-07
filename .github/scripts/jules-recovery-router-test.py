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
WORKFLOW_PATH = Path(__file__).parents[1] / "workflows" / "jules_recovery_router.yml"
BURST_WORKFLOW_PATH = Path(__file__).parents[1] / "workflows" / "jules_burst_monitor.yml"
UNATTENDED_WORKFLOW_PATH = Path(__file__).parents[1] / "workflows" / "jules_unattended_monitor.yml"
CI_WORKFLOW_PATH = Path(__file__).parents[1] / "workflows" / "ci.yml"
AUTOMERGE_WORKFLOW_PATH = Path(__file__).parents[1] / "workflows" / "jules_automerge.yml"
NEXT_TASK_WORKFLOW_PATH = Path(__file__).parents[1] / "workflows" / "jules_next_task.yml"
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
    def __init__(self, responses: list[dict], text_responses: list[str] | None = None) -> None:
        self.api_url = "https://api.github.com"
        self.repo = REPO
        self.responses = list(responses)
        self.text_responses = list(text_responses or [])
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

    def request_text(
        self,
        method: str,
        path: str,
        ok: tuple[int, ...] = (200,),
    ) -> str:
        self.calls.append((method, path))
        if not self.text_responses:
            raise AssertionError(f"unexpected text request: {method} {path}")
        return self.text_responses.pop(0)


class FakeJulesClient:
    def __init__(self, responses: list[dict], *, label: str = "primary") -> None:
        self.label = label
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        ok: tuple[int, ...] = (200,),
    ) -> dict:
        self.calls.append((method, path))
        if not self.responses:
            raise AssertionError(f"unexpected Jules request: {method} {path}")
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
    pr_body_file: str = "",
) -> dict:
    data = {
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
    if pr_body_file:
        data["pr_body_file"] = pr_body_file
    return data


def state(
    *,
    open_pulls: list[dict] | None = None,
    selector: dict | None = None,
    jules_sessions: list[dict] | None = None,
    task_statuses: dict[str, str] | None = None,
    task_metrics: dict[str, int] | None = None,
    task_details: dict[str, dict] | None = None,
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
        "task_metrics": task_metrics or {},
        "task_details": task_details or {},
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
    wait_reason: str = "unknown_continue",
    prompt_action: str = "continue_safely",
    latest_agent_excerpt: str = "",
    continue_token_count: int = 0,
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
            "continue_token_count": continue_token_count,
            "wait_kind": wait_kind,
            "wait_reason": wait_reason,
            "prompt_action": prompt_action,
            "latest_agent_excerpt": latest_agent_excerpt,
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
    def test_action_limit_keeps_diagnostic_and_first_executable(self) -> None:
        diagnostic = router.RecoveryAction(
            type="quality_fix_recovery_cooldown",
            dedupe_key="diagnostic",
            reason="diagnostic",
            ttl_minutes=0,
            payload={},
        )
        first = router.RecoveryAction(
            type="dispatch_workflow",
            dedupe_key="first",
            reason="first",
            ttl_minutes=10,
            payload={"workflow": "automation_health.yml"},
        )
        second = router.RecoveryAction(
            type="dispatch_workflow",
            dedupe_key="second",
            reason="second",
            ttl_minutes=10,
            payload={"workflow": "jules_next_task.yml"},
        )

        actions = router.limit_planned_actions([diagnostic, first, second])

        self.assertEqual([action.dedupe_key for action in actions], ["diagnostic", "first"])

    def test_collect_jules_sessions_uses_fresh_recent_task_mapping(self) -> None:
        source = f"sources/github/{REPO}"
        session_update = (NOW - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        map_update = (NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        jules = FakeJulesClient(
            [
                {
                    "sessions": [
                        {
                            "name": "sessions/1111111111111",
                            "state": "IN_PROGRESS",
                            "sourceContext": {"source": source},
                            "updateTime": session_update,
                        }
                    ]
                },
                {"activities": []},
            ]
        )

        result = router.collect_jules_sessions(
            [jules],
            source=source,
            lookback_hours=24,
            recent_session_tasks={
                "1111111111111": {
                    "task_id": "proxy-runtime-fix",
                    "updateTime": map_update,
                }
            },
            now=NOW,
        )

        self.assertEqual(result["sessions"][0]["task_id"], "proxy-runtime-fix")
        self.assertEqual(result["sessions"][0]["task_id_source"], "recent_session_tasks")
        self.assertEqual(result["sessions"][0]["recent_task_mapping_updateTime"], map_update)
        self.assertEqual(len(router.active_jules_sessions(state(
            jules_sessions=result["sessions"],
            task_statuses={"proxy-runtime-fix": "todo"},
            task_details={"proxy-runtime-fix": {"id": "proxy-runtime-fix", "status": "todo"}},
        ))), 1)

    def test_collect_jules_sessions_ignores_stale_recent_task_mapping_for_in_progress(self) -> None:
        source = f"sources/github/{REPO}"
        session_update = (NOW - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        map_update = (NOW - timedelta(minutes=61)).isoformat().replace("+00:00", "Z")
        jules = FakeJulesClient(
            [
                {
                    "sessions": [
                        {
                            "name": "sessions/1111111111111",
                            "state": "IN_PROGRESS",
                            "sourceContext": {"source": source},
                            "updateTime": session_update,
                        }
                    ]
                },
                {"activities": []},
            ]
        )

        result = router.collect_jules_sessions(
            [jules],
            source=source,
            lookback_hours=24,
            recent_session_tasks={
                "1111111111111": {
                    "task_id": "proxy-runtime-fix",
                    "updateTime": map_update,
                }
            },
            now=NOW,
        )

        collected = result["sessions"][0]
        self.assertEqual(collected["task_id"], "")
        self.assertTrue(collected["recent_task_mapping_stale"])
        self.assertEqual(collected["recent_task_id"], "proxy-runtime-fix")
        self.assertEqual(router.active_jules_sessions(state(jules_sessions=result["sessions"])), [])

        actions = plan(
            state(
                selector={
                    "selected": True,
                    "task_id": "automation-health-failed-session-86122315",
                },
                jules_sessions=result["sessions"],
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "dispatch_workflow")
        self.assertEqual(actions[0].payload["workflow"], "jules_next_task.yml")

    def test_workflow_reruns_router_after_pr_checks_finish(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("workflow_run:", text)
        self.assertIn("- CI", text)
        self.assertIn("- RDSH Local Live Smoke", text)
        self.assertIn("- 1. Auto-Validate and Merge Jules PRs", text)
        self.assertIn("- 4. Advisory Critic Review", text)
        self.assertIn("- completed", text)

    def test_workflow_can_push_manifest_only_recovery_prs(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")
        script_text = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn("contents: write", text)
        self.assertIn("pull-requests: write", text)
        self.assertIn("create-circuit-breaker-followup-task-pr.py", script_text)

    def test_pull_request_router_concurrency_is_pr_scoped(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("jules-recovery-router-${{", text)
        self.assertIn("github.event_name == 'pull_request_target'", text)
        self.assertIn("github.event.pull_request.number", text)
        self.assertIn("|| 'global'", text)

    def test_pull_request_target_router_runs_in_plan_mode(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("RECOVERY_ROUTER_MODE: ${{ github.event_name == 'pull_request_target' && 'plan' || inputs.mode || 'act' }}", text)
        self.assertIn('--mode "${RECOVERY_ROUTER_MODE}"', text)
        self.assertIn('echo "- Mode:', text)
        self.assertIn("github.event_name == 'pull_request_target' && 'plan' || inputs.mode || 'act'", text)

    def test_next_task_dispatches_health_for_no_todo_and_no_eligible_queues(self) -> None:
        text = NEXT_TASK_WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("Dispatch automation health recovery for starved queue", text)
        self.assertIn("steps.select-task.outputs.reason_code == 'no_eligible_autonomous_task'", text)
        self.assertIn("steps.select-task.outputs.reason_code == 'no_todo_tasks'", text)
        self.assertIn("actions/workflows/automation_health.yml/dispatches", text)

    def test_burst_monitor_dispatches_next_after_touching_last_session(self) -> None:
        text = BURST_WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn('touched_sessions="$(get_output touched_sessions "$output_file")"', text)
        self.assertIn('echo "Touched Jules sessions: ${touched_sessions:-0}"', text)
        self.assertIn('if [ "${touched_sessions:-0}" != "0" ]; then', text)
        self.assertIn('saw_active_sessions="1"', text)

    def test_burst_monitor_runtime_is_bounded(self) -> None:
        text = BURST_WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn('default: "6"', text)
        self.assertIn('default: "30"', text)
        self.assertIn("cancel-in-progress: true", text)
        self.assertIn("timeout-minutes: 10", text)
        self.assertIn("vars.JULES_BURST_MONITOR_CYCLES || '6'", text)
        self.assertIn("vars.JULES_BURST_MONITOR_INTERVAL_SECONDS || '30'", text)
        self.assertIn("STALE_IN_PROGRESS_MINUTES", text)
        self.assertIn("NO_AGENT_IN_PROGRESS_MINUTES", text)
        self.assertIn("vars.JULES_BURST_NO_AGENT_IN_PROGRESS_MINUTES || '4'", text)
        self.assertIn("NO_AGENT_STALE_IN_PROGRESS_MINUTES", text)
        self.assertIn("vars.JULES_BURST_NO_AGENT_STALE_IN_PROGRESS_MINUTES || '5'", text)
        self.assertIn("stale_in_progress_count", text)
        self.assertIn("Stale in-progress sessions", text)

    def test_unattended_monitor_summarizes_stale_in_progress_sessions(self) -> None:
        text = UNATTENDED_WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("STALE_IN_PROGRESS_MINUTES", text)
        self.assertIn("NO_AGENT_STALE_IN_PROGRESS_MINUTES", text)
        self.assertIn("vars.JULES_NO_AGENT_STALE_IN_PROGRESS_MINUTES || '5'", text)
        self.assertIn("stale_in_progress_count", text)
        self.assertIn("stale_in_progress_sessions", text)

    def test_go_formatting_failures_report_actionable_file_list(self) -> None:
        for path in (CI_WORKFLOW_PATH, AUTOMERGE_WORKFLOW_PATH):
            text = path.read_text(encoding="utf-8")
            self.assertIn('files="$(gofmt -l .)"', text)
            self.assertIn("gofmt required for:", text)
            self.assertNotIn('run: test -z "$(gofmt -l .)"', text)

        next_task_text = NEXT_TASK_WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn('files="\\$(gofmt -l .)"', next_task_text)
        self.assertIn("gofmt required for:", next_task_text)

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

    def test_git_conflict_fallback_unshallows_unrelated_histories(self) -> None:
        open_pulls = [
            pr(
                labels=["jules"],
                head_ref="jules/task-1234567890123456789",
                mergeable=None,
                mergeable_state="unknown",
            )
        ]
        fetch = router.subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        unrelated = router.subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="fatal: refusing to merge unrelated histories",
        )
        unshallow = router.subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        fetch_full = router.subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        conflict = router.subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="CONFLICT (content): Merge conflict in agent_tasks.json",
            stderr="",
        )

        with patch.object(
            router.subprocess,
            "run",
            side_effect=[fetch, unrelated, unshallow, fetch_full, conflict],
        ) as run:
            router.enrich_open_pull_git_conflicts(open_pulls, repo=REPO)

        self.assertEqual(run.call_count, 5)
        self.assertIn("--unshallow", run.call_args_list[2].args[0])
        self.assertFalse(open_pulls[0]["mergeable"])
        self.assertEqual(open_pulls[0]["mergeable_state"], "dirty")

    def test_quality_fix_posts_comment_and_sends_session_message(self) -> None:
        actions = plan(state(open_pulls=[pr(labels=["jules", "needs-quality-fix"])]))

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "quality_fix_recovery")
        self.assertEqual(actions[0].payload["pr_number"], 10)
        self.assertTrue(actions[0].payload["comment_needed"])
        self.assertIn("исправь этот же PR #10", actions[0].payload["body"])
        self.assertNotIn("follow-up", actions[0].payload["body"].lower())
        self.assertIn("GitHub PR description/body", actions[0].payload["body"])
        self.assertIn("pr-body.md", actions[0].payload["body"])
        self.assertEqual(actions[0].payload["session_id"], "1234567890123456789")

    def test_quality_fix_syncs_pr_body_from_file_before_prompting_jules(self) -> None:
        body_file = (
            "Summary\n\n"
            "<!-- AUTONOMOUS_TASK_EVIDENCE\n"
            "task_id: task-one\n"
            "status: done\n"
            "-->\n"
        )
        actions = plan(
            state(
                open_pulls=[
                    pr(
                        labels=["jules", "needs-quality-fix"],
                        body="Summary without evidence",
                        pr_body_file=body_file,
                    )
                ]
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "sync_pr_body_from_file")
        self.assertEqual(actions[0].payload["pr_number"], 10)
        self.assertEqual(actions[0].payload["body"], body_file)
        self.assertEqual(actions[0].payload["retry_label"], "needs-quality-fix")

    def test_quality_fix_syncs_pr_body_from_file_when_evidence_differs(self) -> None:
        current_body = (
            "<!-- AUTONOMOUS_TASK_EVIDENCE\n"
            "task_id: task-one\n"
            "status: done\n"
            "acceptance:\n"
            "- stale evidence\n"
            "-->\n"
        )
        body_file = current_body.replace("stale evidence", "corrected evidence")
        actions = plan(
            state(
                open_pulls=[
                    pr(
                        labels=["jules", "needs-quality-fix"],
                        body=current_body,
                        pr_body_file=body_file,
                    )
                ]
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "sync_pr_body_from_file")
        self.assertEqual(actions[0].payload["body"], body_file)

    def test_quality_fix_does_not_sync_when_pr_body_file_matches(self) -> None:
        evidence_body = (
            "<!-- AUTONOMOUS_TASK_EVIDENCE\n"
            "task_id: task-one\n"
            "status: done\n"
            "-->\n"
        )
        actions = plan(
            state(
                open_pulls=[
                    pr(
                        labels=["jules", "needs-quality-fix"],
                        body=evidence_body,
                        pr_body_file=evidence_body,
                    )
                ]
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "quality_fix_recovery")

    def test_execute_sync_pr_body_updates_body_and_relabels_for_retry(self) -> None:
        class FakeClient:
            repo = REPO

            def __init__(self) -> None:
                self.calls: list[tuple[str, str, dict | None]] = []

            def request(self, method, path, body=None, ok=(200, 201, 204)):
                self.calls.append((method, path, body))
                return {}

        client = FakeClient()
        action = router.RecoveryAction(
            type="sync_pr_body_from_file",
            dedupe_key="sync-pr-body:10:abc123:deadbeef",
            reason="sync body",
            ttl_minutes=24 * 60,
            payload={
                "pr_number": 10,
                "body": "new body",
                "retry_label": "needs-quality-fix",
            },
        )

        router.execute_action(client, action)

        self.assertEqual(
            client.calls,
            [
                ("PATCH", f"/repos/{REPO}/pulls/10", {"body": "new body"}),
                ("DELETE", f"/repos/{REPO}/issues/10/labels/needs-quality-fix", None),
                ("POST", f"/repos/{REPO}/issues/10/labels", {"labels": ["needs-quality-fix"]}),
            ],
        )

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
        self.assertNotIn("follow-up", body.lower())
        self.assertIn("PR body repeatedly mentions [deferred-task marker] tasks", body)
        self.assertIn("proxy-observability-json-tool-call-mode-loss-test-more", body)

    def test_quality_fix_prompt_ignores_nested_marker_inside_recovery_comments(self) -> None:
        quality_comment = """<!-- AUTONOMOUS_QUALITY_FIX_REQUEST pr-level -->

# Autonomous PR quality gate

Blocking reasons:
- real quality gate reason
"""
        recovery_comment = """<!-- AUTONOMOUS_RECOVERY_ROUTER action=quality-fix sha=old -->

Previous router prompt:

```text
<!-- AUTONOMOUS_QUALITY_FIX_REQUEST pr-level -->
- stale quoted reason
```
"""
        actions = plan(
            state(
                open_pulls=[
                    pr(
                        labels=["jules", "needs-quality-fix"],
                        comments=[quality_comment, recovery_comment],
                        sha="newsha",
                    )
                ]
            )
        )

        self.assertEqual(len(actions), 1)
        body = actions[0].payload["body"]
        self.assertIn("real quality gate reason", body)
        self.assertNotIn("stale quoted reason", body)

    def test_quality_fix_comment_marker_reports_recovery_cooldown(self) -> None:
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

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "quality_fix_recovery_cooldown")
        self.assertFalse(router.is_executable_action(actions[0]))
        self.assertEqual(actions[0].payload["pr_number"], 10)
        self.assertEqual(actions[0].payload["sha"], "abc123")
        self.assertEqual(actions[0].payload["label"], "needs-quality-fix")
        self.assertTrue(actions[0].payload["marker_present"])
        self.assertTrue(actions[0].payload["recovery_recently_done"])
        self.assertEqual(actions[0].payload["cooldown_minutes"], 30)
        self.assertIn("already ran", actions[0].payload["wait_reason"])

    def test_quality_fix_cooldown_allows_low_todo_health_dispatch(self) -> None:
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
            state(
                open_pulls=[pr(labels=["jules", "needs-quality-fix"], comments=[marker])],
                selector={
                    "selected": True,
                    "task_id": "proxy-runtime-fix",
                    "todo_count": 4,
                    "eligible_count": 1,
                    "reason_code": "selected",
                },
                task_metrics={"todo_count": 4, "minimum_todo_tasks": 5},
            ),
            ledger=ledger,
        )

        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0].type, "quality_fix_recovery_cooldown")
        self.assertFalse(router.is_executable_action(actions[0]))
        self.assertEqual(actions[1].type, "dispatch_workflow")
        self.assertTrue(router.is_executable_action(actions[1]))
        self.assertEqual(actions[1].payload["workflow"], "automation_health.yml")
        self.assertEqual(actions[1].payload["inputs"]["mode"], "enforce")
        self.assertIn("4/5", actions[1].reason)

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

    def test_quality_fix_under_attempt_limit_still_sends_recovery(self) -> None:
        comments = [
            "<!-- AUTONOMOUS_RECOVERY_ROUTER action=quality-fix sha=aaa111 -->",
            "<!-- AUTONOMOUS_RECOVERY_ROUTER action=quality-fix sha=bbb222 -->",
        ]

        actions = plan(
            state(
                open_pulls=[
                    pr(labels=["jules", "needs-quality-fix"], sha="ccc333", comments=comments)
                ]
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "quality_fix_recovery")
        self.assertEqual(actions[0].payload["pr_number"], 10)

    def test_quality_fix_circuit_breaker_stops_repeated_recovery_loop(self) -> None:
        comments = [
            "<!-- AUTONOMOUS_RECOVERY_ROUTER action=quality-fix sha=aaa111 -->",
            "<!-- AUTONOMOUS_RECOVERY_ROUTER action=quality-fix sha=bbb222 -->",
        ]
        ledger = {
            "version": 1,
            "actions": {
                "quality-fix:10:ccc333": {
                    "time": (NOW - timedelta(minutes=31)).isoformat().replace("+00:00", "Z"),
                    "type": "quality_fix_recovery",
                }
            },
        }

        actions = plan(
            state(
                open_pulls=[
                    pr(labels=["jules", "needs-quality-fix"], sha="ddd444", comments=comments)
                ]
            ),
            ledger=ledger,
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "quality_fix_circuit_breaker")
        self.assertEqual(
            actions[0].payload["labels"],
            ["human-review", "no-automerge", "stop-loop"],
        )
        self.assertIn("circuit breaker", actions[0].payload["body"])
        self.assertIn("aaa111, bbb222, ccc333", actions[0].payload["body"])

    def test_quality_fix_circuit_breaker_execution_adds_labels_and_comment(self) -> None:
        action = router.RecoveryAction(
            type="quality_fix_circuit_breaker",
            dedupe_key="quality-fix-circuit-breaker:10:abc123",
            reason="too many attempts",
            ttl_minutes=router.QUALITY_FIX_CIRCUIT_BREAKER_TTL_MINUTES,
            payload={
                "pr_number": 10,
                "labels": ["human-review", "no-automerge", "stop-loop"],
                "body": "stop",
            },
        )
        client = FakeGitHubClient([{}, {}])

        with patch.object(router, "ensure_repository_labels") as ensure_labels:
            router.execute_action(client, action)

        ensure_labels.assert_called_once_with(
            client,
            ["human-review", "no-automerge", "stop-loop"],
        )
        self.assertEqual(
            client.calls,
            [
                ("POST", f"/repos/{REPO}/issues/10/comments"),
                ("POST", f"/repos/{REPO}/issues/10/labels"),
            ],
        )

    def test_quality_fix_followup_task_execution_calls_helper(self) -> None:
        action = router.RecoveryAction(
            type="quality_fix_followup_task",
            dedupe_key="quality-fix-followup-task:10:abc123:followup",
            reason="stopped quality loop",
            ttl_minutes=router.QUALITY_FIX_FOLLOWUP_TTL_MINUTES,
            payload={
                "pr_number": 10,
                "source_sha": "abc123",
                "source_task_id": "proxy-runtime-fix",
                "reason": "quality gate did not converge",
            },
        )
        client = FakeGitHubClient([])

        with patch.object(router.subprocess, "run") as run:
            router.execute_action(client, action)

        args = run.call_args.args[0]
        self.assertIn(".github/scripts/create-circuit-breaker-followup-task-pr.py", args)
        self.assertIn("--pr-number", args)
        self.assertIn("10", args)
        self.assertIn("--source-sha", args)
        self.assertIn("abc123", args)
        self.assertIn("--source-task-id", args)
        self.assertIn("proxy-runtime-fix", args)
        self.assertIn("--source-finding-id", args)
        self.assertIn("quality_fix_circuit_breaker", args)
        self.assertTrue(run.call_args.kwargs["check"])

    def test_conflict_recovery_followup_task_execution_calls_helper(self) -> None:
        action = router.RecoveryAction(
            type="conflict_recovery_followup_task",
            dedupe_key="conflict-recovery-followup-task:400:abc123:followup",
            reason="stopped conflict loop",
            ttl_minutes=router.QUALITY_FIX_FOLLOWUP_TTL_MINUTES,
            payload={
                "pr_number": 400,
                "source_sha": "abc123",
                "source_task_id": "proxy-runtime-fix",
                "reason": "dirty PR stayed unresolved",
            },
        )
        client = FakeGitHubClient([])

        with patch.object(router.subprocess, "run") as run:
            router.execute_action(client, action)

        args = run.call_args.args[0]
        self.assertIn(".github/scripts/create-circuit-breaker-followup-task-pr.py", args)
        self.assertIn("--pr-number", args)
        self.assertIn("400", args)
        self.assertIn("--source-sha", args)
        self.assertIn("abc123", args)
        self.assertIn("--source-finding-id", args)
        self.assertIn("conflict_recovery_circuit_breaker", args)
        self.assertTrue(run.call_args.kwargs["check"])

    def test_failed_check_recovery_execution_comments_and_messages_jules(self) -> None:
        action = router.RecoveryAction(
            type="failed_check_recovery",
            dedupe_key="failed-check-recovery:10:abc123:fingerprint",
            reason="failed checks",
            ttl_minutes=router.FAILED_CHECK_RECOVERY_COOLDOWN_MINUTES,
            payload={
                "pr_number": 10,
                "body": "fix failed checks",
                "comment_needed": True,
                "session_id": "1234567890123456789",
            },
        )
        client = FakeGitHubClient([{}])
        jules_clients = [object()]

        with patch.object(router, "jules_request_any") as send:
            router.execute_action(client, action, jules_clients=jules_clients)

        self.assertEqual(client.calls, [("POST", f"/repos/{REPO}/issues/10/comments")])
        send.assert_called_once_with(
            jules_clients,
            "POST",
            "sessions/1234567890123456789:sendMessage",
            {"prompt": "fix failed checks"},
        )

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

    def test_stopped_autonomous_pr_does_not_block_next_task(self) -> None:
        actions = plan(
            state(
                open_pulls=[pr(labels=["jules", "human-review"])],
                selector={"selected": True, "task_id": "automation-health-failed-session-86122315"},
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "dispatch_workflow")
        self.assertEqual(actions[0].payload["workflow"], "jules_next_task.yml")

    def test_stopped_circuit_breaker_pr_creates_followup_task(self) -> None:
        stopped = pr(
            labels=["jules", "human-review", "no-automerge", "stop-loop"],
            sha="ddd444",
            body="<!-- AUTONOMOUS_TASK_EVIDENCE\ntask_id: proxy-runtime-fix\n-->",
            comments=[
                "<!-- AUTONOMOUS_RECOVERY_ROUTER action=quality-fix-circuit-breaker sha=ddd444 -->",
                "<!-- AUTONOMOUS_QUALITY_FIX_REQUEST pr-level -->\nBlocking reasons:\n- repeated evidence mismatch",
            ],
        )

        actions = plan(
            state(
                open_pulls=[stopped],
                selector={"selected": True, "task_id": "automation-health-failed-session-86122315"},
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "quality_fix_followup_task")
        self.assertEqual(actions[0].payload["pr_number"], 10)
        self.assertEqual(actions[0].payload["source_sha"], "ddd444")
        self.assertEqual(actions[0].payload["source_task_id"], "proxy-runtime-fix")
        self.assertTrue(actions[0].payload["task_id"].startswith("automation-quality-loop-pr-10-"))
        self.assertIn("repeated evidence mismatch", actions[0].payload["reason"])

    def test_stopped_circuit_breaker_pr_creates_followup_despite_unrelated_active_session(self) -> None:
        stopped = pr(
            labels=["jules", "human-review", "no-automerge", "stop-loop"],
            sha="ddd444",
            body="<!-- AUTONOMOUS_TASK_EVIDENCE\ntask_id: proxy-runtime-fix\n-->",
            comments=[
                "<!-- AUTONOMOUS_RECOVERY_ROUTER action=quality-fix-circuit-breaker sha=ddd444 -->",
                "<!-- AUTONOMOUS_QUALITY_FIX_REQUEST pr-level -->\nBlocking reasons:\n- repeated evidence mismatch",
            ],
        )

        actions = plan(
            state(
                open_pulls=[stopped],
                jules_sessions=[
                    session(
                        state="IN_PROGRESS",
                        task_id="automation-health-failed-session-86122315",
                    )
                ],
                selector={"selected": True, "task_id": "automation-health-failed-session-86122315"},
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "quality_fix_followup_task")
        self.assertEqual(actions[0].payload["pr_number"], 10)
        self.assertEqual(actions[0].payload["source_task_id"], "proxy-runtime-fix")

    def test_stopped_circuit_breaker_followup_is_not_masked_by_fresh_waiting_continue(self) -> None:
        latest_agent = epoch(5)
        stopped = pr(
            labels=["jules", "human-review", "no-automerge", "stop-loop"],
            sha="ddd444",
            body="<!-- AUTONOMOUS_TASK_EVIDENCE\ntask_id: proxy-runtime-fix\n-->",
            comments=[
                "<!-- AUTONOMOUS_RECOVERY_ROUTER action=quality-fix-circuit-breaker sha=ddd444 -->",
                "<!-- AUTONOMOUS_QUALITY_FIX_REQUEST pr-level -->\nBlocking reasons:\n- repeated evidence mismatch",
            ],
        )
        ledger = {
            "version": 1,
            "actions": {
                f"continue:1234567890123456789:{latest_agent}": {
                    "time": (NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                    "type": "jules_send_message",
                }
            },
        }

        actions = plan(
            state(
                open_pulls=[stopped],
                jules_sessions=[
                    session(
                        state="AWAITING_USER_FEEDBACK",
                        task_id="automation-health-failed-session-86122315",
                        latest_agent_epoch=latest_agent,
                        latest_user_epoch=0,
                        latest_token_epoch=0,
                    )
                ],
                selector={"selected": True, "task_id": "automation-health-failed-session-86122315"},
            ),
            ledger=ledger,
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "quality_fix_followup_task")
        self.assertEqual(actions[0].payload["pr_number"], 10)

    def test_existing_circuit_breaker_followup_task_is_not_recreated(self) -> None:
        stopped = pr(
            labels=["jules", "human-review", "no-automerge", "stop-loop"],
            sha="ddd444",
            body="<!-- AUTONOMOUS_TASK_EVIDENCE\ntask_id: proxy-runtime-fix\n-->",
            comments=["<!-- AUTONOMOUS_RECOVERY_ROUTER action=quality-fix-circuit-breaker sha=ddd444 -->"],
        )
        followup_task_id = router.quality_fix_followup_task_id(stopped, TASK_IDS)

        actions = plan(
            state(
                open_pulls=[stopped],
                selector={"selected": True, "task_id": "automation-health-failed-session-86122315"},
                task_details={followup_task_id: {"id": followup_task_id, "status": "todo"}},
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "dispatch_workflow")
        self.assertEqual(actions[0].payload["workflow"], "jules_next_task.yml")

    def test_stopped_conflict_circuit_breaker_pr_creates_followup_task(self) -> None:
        stopped = pr(
            number=400,
            labels=["jules", "human-review", "no-automerge", "stop-loop"],
            sha="bfe8471",
            body="<!-- AUTONOMOUS_TASK_EVIDENCE\ntask_id: proxy-runtime-fix\n-->",
            comments=[
                "<!-- AUTONOMOUS_RECOVERY_ROUTER action=conflict-recovery-circuit-breaker sha=bfe8471 -->",
            ],
        )

        actions = plan(
            state(
                open_pulls=[stopped],
                selector={"selected": True, "task_id": "automation-health-failed-session-86122315"},
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "conflict_recovery_followup_task")
        self.assertEqual(actions[0].payload["pr_number"], 400)
        self.assertEqual(actions[0].payload["source_sha"], "bfe8471")
        self.assertEqual(actions[0].payload["source_task_id"], "proxy-runtime-fix")
        self.assertTrue(actions[0].payload["task_id"].startswith("automation-conflict-loop-pr-400-"))

    def test_existing_conflict_circuit_breaker_followup_task_is_not_recreated(self) -> None:
        stopped = pr(
            number=400,
            labels=["jules", "human-review", "no-automerge", "stop-loop"],
            sha="bfe8471",
            body="<!-- AUTONOMOUS_TASK_EVIDENCE\ntask_id: proxy-runtime-fix\n-->",
            comments=[
                "<!-- AUTONOMOUS_RECOVERY_ROUTER action=conflict-recovery-circuit-breaker sha=bfe8471 -->",
            ],
        )
        followup_task_id = router.conflict_recovery_followup_task_id(stopped, TASK_IDS)

        actions = plan(
            state(
                open_pulls=[stopped],
                selector={"selected": True, "task_id": "automation-health-failed-session-86122315"},
                task_details={followup_task_id: {"id": followup_task_id, "status": "todo"}},
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "dispatch_workflow")
        self.assertEqual(actions[0].payload["workflow"], "jules_next_task.yml")

    def test_stopped_task_ids_are_extracted_for_selector_exclusion(self) -> None:
        stopped = pr(
            labels=["jules", "stop-loop"],
            body="<!-- AUTONOMOUS_TASK_EVIDENCE\ntask_id: proxy-runtime-fix\n-->",
            head_ref="jules/proxy-runtime-fix-1234567890123456789",
        )
        active = pr(
            labels=["jules"],
            body="task_id: automation-health-failed-session-86122315",
        )

        self.assertEqual(
            router.stopped_task_ids_from_prs([stopped, active], TASK_IDS),
            ["proxy-runtime-fix"],
        )

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
        self.assertEqual(actions[0].dedupe_key, "conflict-recovery:10:abc123:attempt-2")
        self.assertFalse(actions[0].payload["comment_needed"])
        self.assertEqual(actions[0].ttl_minutes, 30)

    def test_conflict_recovery_cooldown_is_reported_as_diagnostic_action(self) -> None:
        marker = "<!-- AUTONOMOUS_RECOVERY_ROUTER action=conflict-recovery sha=abc123 -->"
        ledger = {
            "version": 1,
            "actions": {
                "conflict-recovery:10:abc123": {
                    "time": (NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
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
                ],
                selector={
                    "selected": True,
                    "task_id": "automation-health-failed-session-86122315",
                },
            ),
            ledger=ledger,
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "conflict_recovery_cooldown")
        self.assertFalse(router.is_executable_action(actions[0]))
        self.assertEqual(actions[0].payload["attempt_count"], 1)
        self.assertTrue(actions[0].payload["recovery_recently_done"])
        self.assertIn("within 30 minutes", actions[0].payload["wait_reason"])

    def test_conflict_recovery_circuit_breaker_stops_repeated_prompt_loop(self) -> None:
        comments = [
            "<!-- AUTONOMOUS_RECOVERY_ROUTER action=conflict-recovery sha=aaa111 -->",
            "<!-- AUTONOMOUS_RECOVERY_ROUTER action=conflict-recovery sha=bbb222 -->",
        ]
        ledger = {
            "version": 1,
            "actions": {
                "conflict-recovery:10:ccc333": {
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
                        sha="ddd444",
                        comments=comments,
                        mergeable=False,
                        mergeable_state="dirty",
                    )
                ]
            ),
            ledger=ledger,
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "conflict_recovery_circuit_breaker")
        self.assertEqual(
            actions[0].payload["labels"],
            ["human-review", "no-automerge", "stop-loop"],
        )
        self.assertIn("conflict recovery", actions[0].payload["body"])
        self.assertIn("pull_request", actions[0].payload["body"])
        self.assertIn("aaa111, bbb222, ccc333", actions[0].payload["body"])

    def test_conflict_recovery_circuit_breaker_counts_repeated_same_sha_attempts(self) -> None:
        marker = "<!-- AUTONOMOUS_RECOVERY_ROUTER action=conflict-recovery sha=abc123 -->"
        ledger = {
            "version": 1,
            "actions": {
                "conflict-recovery:10:abc123": {
                    "time": (NOW - timedelta(minutes=95)).isoformat().replace("+00:00", "Z"),
                    "type": "conflict_recovery",
                },
                "conflict-recovery:10:abc123:attempt-2": {
                    "time": (NOW - timedelta(minutes=65)).isoformat().replace("+00:00", "Z"),
                    "type": "conflict_recovery",
                },
                "conflict-recovery:10:abc123:attempt-3": {
                    "time": (NOW - timedelta(minutes=35)).isoformat().replace("+00:00", "Z"),
                    "type": "conflict_recovery",
                },
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
                ],
            ),
            ledger=ledger,
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "conflict_recovery_circuit_breaker")
        self.assertEqual(actions[0].payload["labels"], ["human-review", "no-automerge", "stop-loop"])
        self.assertIn("abc123", actions[0].payload["body"])

    def test_conflict_recovery_circuit_breaker_execution_adds_labels_and_comment(self) -> None:
        action = router.RecoveryAction(
            type="conflict_recovery_circuit_breaker",
            dedupe_key="conflict-recovery-circuit-breaker:10:abc123",
            reason="too many conflict prompts",
            ttl_minutes=router.CONFLICT_RECOVERY_CIRCUIT_BREAKER_TTL_MINUTES,
            payload={
                "pr_number": 10,
                "labels": ["human-review", "no-automerge", "stop-loop"],
                "body": "stop conflict loop",
            },
        )
        client = FakeGitHubClient([{}, {}])

        with patch.object(router, "ensure_repository_labels") as ensure_labels:
            router.execute_action(client, action)

        ensure_labels.assert_called_once_with(
            client,
            ["human-review", "no-automerge", "stop-loop"],
        )
        self.assertEqual(
            client.calls,
            [
                ("POST", f"/repos/{REPO}/issues/10/comments"),
                ("POST", f"/repos/{REPO}/issues/10/labels"),
            ],
        )

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

    def test_failed_automerge_after_rerun_prompts_jules_with_failed_check_context(self) -> None:
        ledger = {
            "version": 1,
            "actions": {
                "rerun-automerge:10:abc123:12345": {
                    "time": (NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                    "type": "rerun_workflow",
                }
            },
        }
        actions = plan(
            state(
                open_pulls=[
                    pr(
                        labels=["jules"],
                        check_runs=[
                            {
                                "name": "test-and-merge",
                                "workflowName": "1. Auto-Validate and Merge Jules PRs",
                                "status": "completed",
                                "conclusion": "failure",
                                "details_url": "https://github.com/o/r/actions/runs/12345/job/9",
                            }
                        ],
                    )
                ]
            ),
            ledger=ledger,
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "failed_check_recovery")
        self.assertEqual(actions[0].payload["pr_number"], 10)
        self.assertEqual(actions[0].payload["session_id"], "1234567890123456789")
        self.assertIn("Failed checks:", actions[0].payload["body"])
        self.assertIn("1. Auto-Validate and Merge Jules PRs / test-and-merge", actions[0].payload["body"])
        self.assertIn("https://github.com/o/r/actions/runs/12345/job/9", actions[0].payload["body"])
        self.assertIn("gofmt required for:", actions[0].payload["body"])

    def test_failed_check_evidence_enrichment_collects_files_annotations_and_log_excerpt(self) -> None:
        failed_pr = pr(
            check_runs=[
                {
                    "id": 987,
                    "name": "test-and-merge",
                    "workflowName": "1. Auto-Validate and Merge Jules PRs",
                    "status": "completed",
                    "conclusion": "failure",
                    "details_url": "https://github.com/o/r/actions/runs/12345/job/987",
                    "output": {
                        "annotations_url": (
                            "https://api.github.com/repos/Omnividente/notion-abuz_ai/"
                            "check-runs/987/annotations"
                        )
                    },
                }
            ],
        )
        client = FakeGitHubClient(
            responses=[
                [
                    {"filename": ".github/scripts/jules-recovery-router.py"},
                    {"filename": "pr_body.txt"},
                ],
                [
                    {
                        "path": ".github",
                        "start_line": 26,
                        "message": (
                            "PR changes protected runtime, token=ghp_abcdef1234567890, "
                            "or temporary scratch files."
                        ),
                    }
                ],
            ],
            text_responses=[
                "\n".join(
                    [
                        "2026-07-07T06:08:03Z .github/scripts/jules-recovery-router.py",
                        "2026-07-07T06:08:03Z pr_body.txt",
                        "2026-07-07T06:08:03Z ##[error]PR changes protected runtime, secret, binary, log, data, account, workflow, or temporary scratch files.",
                        "2026-07-07T06:08:03Z ##[error]Process completed with exit code 1.",
                    ]
                )
            ],
        )

        router.enrich_failed_check_evidence(client, failed_pr)
        context = router.failed_check_prompt_context(failed_pr, repo=REPO)
        prompt = router.failed_check_recovery_prompt(failed_pr)

        self.assertEqual(
            client.calls,
            [
                ("GET", "/repos/Omnividente/notion-abuz_ai/pulls/10/files?per_page=100"),
                (
                    "GET",
                    "/repos/Omnividente/notion-abuz_ai/check-runs/987/annotations?per_page=5",
                ),
                ("GET", "/repos/Omnividente/notion-abuz_ai/actions/jobs/987/logs"),
            ],
        )
        self.assertIn("pr_body.txt", context["changed_files"])
        self.assertIn("pr_body.txt", context["failed_checks"][0]["log_excerpt"])
        self.assertIn("temporary scratch files", context["failed_checks"][0]["annotations"][0])
        self.assertIn("Changed files from this PR:", prompt)
        self.assertIn("pr_body.txt", prompt)
        self.assertIn("annotation:", prompt)
        self.assertIn("log_excerpt:", prompt)
        self.assertIn("[REDACTED]", prompt)
        self.assertNotIn("ghp_abcdef1234567890", prompt)

    def test_failed_ci_check_prompts_jules_without_waiting_for_automerge_rerun(self) -> None:
        actions = plan(
            state(
                open_pulls=[
                    pr(
                        labels=["jules"],
                        check_runs=[
                            {
                                "name": "validate",
                                "workflowName": "CI",
                                "status": "completed",
                                "conclusion": "failure",
                                "details_url": "https://github.com/o/r/actions/runs/222/job/3",
                            }
                        ],
                    )
                ]
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "failed_check_recovery")
        self.assertIn("CI / validate", actions[0].payload["body"])

    def test_failed_check_recovery_waits_for_pending_checks(self) -> None:
        actions = plan(
            state(
                open_pulls=[
                    pr(
                        labels=["jules"],
                        check_runs=[
                            {
                                "name": "validate",
                                "workflowName": "CI",
                                "status": "completed",
                                "conclusion": "failure",
                            },
                            {"name": "test-and-merge", "status": "in_progress"},
                        ],
                    )
                ]
            )
        )

        self.assertEqual(actions, [])

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

    def test_selected_task_with_thin_queue_dispatches_health_recovery_first(self) -> None:
        actions = plan(
            state(
                selector={
                    "selected": True,
                    "task_id": "automation-health-failed-session-86122315",
                    "todo_count": 4,
                },
                task_metrics={"todo_count": 4, "minimum_todo_tasks": 5},
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "dispatch_workflow")
        self.assertEqual(actions[0].payload["workflow"], "automation_health.yml")
        self.assertEqual(actions[0].payload["inputs"]["mode"], "enforce")
        self.assertIn("4/5", actions[0].reason)

    def test_recent_health_recovery_allows_selected_task_dispatch(self) -> None:
        actions = plan(
            state(
                recent_health=True,
                selector={
                    "selected": True,
                    "task_id": "automation-health-failed-session-86122315",
                    "todo_count": 4,
                },
                task_metrics={"todo_count": 4, "minimum_todo_tasks": 5},
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "dispatch_workflow")
        self.assertEqual(actions[0].payload["workflow"], "jules_next_task.yml")

    def test_disabled_health_recovery_allows_selected_task_dispatch(self) -> None:
        actions = plan(
            state(
                selector={
                    "selected": True,
                    "task_id": "automation-health-failed-session-86122315",
                    "todo_count": 4,
                },
                task_metrics={"todo_count": 4, "minimum_todo_tasks": 5},
            ),
            health_mode="disabled",
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

    def test_known_active_jules_session_prevents_next_task_noise(self) -> None:
        actions = plan(
            state(
                jules_sessions=[session(state="IN_PROGRESS")],
                selector={"selected": True, "task_id": "automation-health-failed-session-86122315"},
                task_statuses={"automation-health-failed-session-86122315": "todo"},
                task_details={
                    "automation-health-failed-session-86122315": {
                        "id": "automation-health-failed-session-86122315",
                        "status": "todo",
                    }
                },
            )
        )

        self.assertEqual(actions, [])

    def test_no_task_in_progress_session_does_not_block_next_task_dispatch(self) -> None:
        actions = plan(
            state(
                jules_sessions=[session(state="IN_PROGRESS", task_id="")],
                selector={"selected": True, "task_id": "automation-health-failed-session-86122315"},
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "dispatch_workflow")
        self.assertEqual(actions[0].payload["workflow"], "jules_next_task.yml")

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

    def test_awaiting_user_feedback_prompt_includes_dynamic_task_context(self) -> None:
        task_id = "automation-health-failed-session-86122315"
        actions = plan(
            state(
                jules_sessions=[
                    session(
                        state="AWAITING_USER_FEEDBACK",
                        task_id=task_id,
                        wait_reason="transient_api_or_partial_context",
                        prompt_action="repeat_targeted_context_collection",
                        latest_agent_excerpt="API error left partial context. password=[REDACTED]",
                    )
                ],
                task_details={
                    task_id: {
                        "status": "todo",
                        "area": "automation",
                        "risk": "medium",
                        "title": "Recover failed session",
                        "allowed_paths": [".github/scripts/jules-recovery-router.py"],
                        "acceptance": ["Prompt repeats targeted context collection"],
                    }
                },
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "jules_send_message")
        self.assertEqual(actions[0].payload["wait_reason"], "transient_api_or_partial_context")
        self.assertEqual(actions[0].payload["prompt_action"], "repeat_targeted_context_collection")
        prompt = actions[0].payload["prompt"]
        self.assertIn(f"task_id: {task_id}", prompt)
        self.assertIn("wait_reason: transient_api_or_partial_context", prompt)
        self.assertIn("allowed_paths: .github/scripts/jules-recovery-router.py", prompt)
        self.assertIn("Повтори targeted search/read", prompt)

    def test_awaiting_user_feedback_prompt_includes_associated_failed_check_context(self) -> None:
        failed_pr = pr(
            labels=["jules"],
            comments=["<!-- AUTONOMOUS_RECOVERY_ROUTER action=failed-check sha=abc123 -->"],
            check_runs=[
                {
                    "name": "validate",
                    "workflowName": "CI",
                    "status": "completed",
                    "conclusion": "failure",
                    "details_url": (
                        "https://github.com/o/r/actions/runs/222/job/3"
                        "?token=ghp_abcdef1234567890"
                    ),
                    "annotations": [
                        "pr_body.txt: PR changes protected scratch file with token=ghp_abcdef1234567890"
                    ],
                    "log_excerpt": (
                        "pr_body.txt\n"
                        "##[error]PR changes protected runtime, secret, binary, log, data, account, "
                        "workflow, or temporary scratch files."
                    ),
                }
            ],
        )
        failed_pr["changed_files"] = [".github/scripts/jules-recovery-router.py", "pr_body.txt"]
        fingerprint = router.failed_check_fingerprint(failed_pr)
        ledger = {
            "version": 1,
            "actions": {
                f"failed-check-recovery:10:abc123:{fingerprint}": {
                    "time": (NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                    "type": "failed_check_recovery",
                }
            },
        }
        actions = plan(
            state(
                open_pulls=[failed_pr],
                jules_sessions=[
                    session(
                        state="AWAITING_USER_FEEDBACK",
                        wait_reason="unknown_continue",
                        prompt_action="continue_safely",
                        latest_agent_excerpt="I am waiting for input before fixing the failed check.",
                    )
                ],
                task_details={
                    "automation-health-failed-session-86122315": {
                        "status": "todo",
                        "area": "automation",
                        "risk": "low",
                        "title": "Recover stuck session",
                        "allowed_paths": [".github/scripts/jules-recovery-router.py"],
                        "acceptance": ["Prompt includes failed check context"],
                    }
                },
            ),
            ledger=ledger,
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "jules_send_message")
        prompt = actions[0].payload["prompt"]
        self.assertIn(f"repo: {REPO}", prompt)
        self.assertIn("session_id: 1234567890123456789", prompt)
        self.assertIn("session_state: AWAITING_USER_FEEDBACK", prompt)
        self.assertIn("pr_context: available", prompt)
        self.assertIn("pr_number: #10", prompt)
        self.assertIn("pr_head_sha: abc123", prompt)
        self.assertIn("changed_files:", prompt)
        self.assertIn("pr_body.txt", prompt)
        self.assertIn("CI / validate: failure", prompt)
        self.assertIn("annotation:", prompt)
        self.assertIn("log_excerpt:", prompt)
        self.assertIn("используй annotations/log_excerpt/changed_files", prompt)
        self.assertNotIn("ghp_abcdef1234567890", prompt)
        self.assertEqual(actions[0].payload["pr_context"]["pr_number"], "#10")
        self.assertEqual(actions[0].payload["repo"], REPO)
        self.assertEqual(actions[0].payload["session_id"], "1234567890123456789")
        self.assertEqual(actions[0].payload["session_state"], "AWAITING_USER_FEEDBACK")
        self.assertIn("changed_files", actions[0].payload["pr_context"])
        self.assertIn("[REDACTED]", actions[0].payload["pr_context"]["failed_checks"][0]["details_url"])
        self.assertIn("[REDACTED]", actions[0].payload["pr_context"]["failed_checks"][0]["annotations"][0])
        self.assertNotIn(
            "ghp_abcdef1234567890",
            actions[0].payload["pr_context"]["failed_checks"][0]["details_url"],
        )

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

    def test_awaiting_user_feedback_token_becomes_stale_after_ten_minutes(self) -> None:
        fresh_actions = plan(
            state(
                jules_sessions=[
                    session(
                        state="AWAITING_USER_FEEDBACK",
                        latest_agent_epoch=epoch(20),
                        latest_user_epoch=epoch(9),
                        latest_token_epoch=epoch(9),
                    )
                ]
            )
        )
        self.assertEqual(fresh_actions, [])

        stale_actions = plan(
            state(
                jules_sessions=[
                    session(
                        state="AWAITING_USER_FEEDBACK",
                        latest_agent_epoch=epoch(20),
                        latest_user_epoch=epoch(11),
                        latest_token_epoch=epoch(11),
                    )
                ]
            )
        )
        self.assertEqual(len(stale_actions), 1)
        self.assertEqual(stale_actions[0].type, "jules_send_message")
        self.assertIn("continue уже был отправлен", stale_actions[0].payload["prompt"])

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
                    "time": (NOW - timedelta(minutes=15 + attempt)).isoformat().replace("+00:00", "Z"),
                    "type": "jules_send_message",
                }
                for attempt in range(1, 3)
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

    def test_no_todo_task_dispatches_health_enforce(self) -> None:
        actions = plan(
            state(
                selector={
                    "selected": False,
                    "reason_code": "no_todo_tasks",
                    "reason": "no todo tasks are available",
                    "todo_count": 0,
                    "eligible_count": 0,
                }
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].payload["workflow"], "automation_health.yml")
        self.assertEqual(actions[0].payload["inputs"]["mode"], "enforce")
        self.assertIn("No eligible autonomous task", actions[0].reason)

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
