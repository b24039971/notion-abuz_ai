#!/usr/bin/env python3
"""Regression tests for jules-unattended-monitor.sh."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github" / "scripts" / "jules-unattended-monitor.sh"
TASK_ID = "proxy-runtime-final-answer-mode-stability"
INACTIVE_TASK_ID = "proxy-runtime-final-answer-mode-stability-blocked"


FAKE_CURL = r"""#!/usr/bin/env bash
exec "$PYTHON_FOR_FAKE_CURL" - "$@" <<'PY'
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


args = sys.argv[1:]
method = "GET"
out = ""
url = ""
body = ""
writeout = ""
i = 0
while i < len(args):
    arg = args[i]
    if arg == "-X":
        method = args[i + 1]
        i += 2
        continue
    if arg == "-H":
        i += 2
        continue
    if arg == "-d":
        body = args[i + 1]
        i += 2
        continue
    if arg == "-o":
        out = args[i + 1]
        i += 2
        continue
    if arg == "-w":
        writeout = args[i + 1]
        i += 2
        continue
    if arg.startswith("http://") or arg.startswith("https://"):
        url = arg
    i += 1

if not out:
    print("fake curl requires -o", file=sys.stderr)
    raise SystemExit(2)

now = int(os.environ["FAKE_NOW_EPOCH"])
scenario = os.environ.get("FAKE_SCENARIO", "repeat_feedback")
if scenario == "routine_question":
    session_name = "sessions/test-routine"
elif scenario == "in_progress_stale":
    session_name = "sessions/test-in-progress-stale"
elif scenario == "in_progress_long_running_fresh":
    session_name = "sessions/test-in-progress-long-running"
elif scenario == "in_progress_no_agent_long_running":
    session_name = "sessions/test-in-progress-no-agent"
elif scenario == "in_progress_no_agent_short_burst":
    session_name = "sessions/test-in-progress-no-agent-short"
elif scenario == "in_progress_no_agent_token_stale":
    session_name = "sessions/test-in-progress-no-agent-token-stale"
elif scenario == "in_progress_inactive_manifest":
    session_name = "sessions/test-in-progress-inactive"
elif scenario == "in_progress_unknown_task":
    session_name = "sessions/test-in-progress-unknown"
elif scenario == "in_progress_no_agent_repeat":
    session_name = "sessions/test-in-progress-no-agent-repeat"
elif scenario == "in_progress_no_agent_repeat_grace_period":
    session_name = "sessions/test-in-progress-no-agent-repeat-grace"
elif scenario == "in_progress_repeat":
    session_name = "sessions/test-in-progress-repeat"
elif scenario == "in_progress_repeat_grace_period":
    session_name = "sessions/test-in-progress-repeat-grace"
elif scenario == "stopped_in_progress":
    session_name = "sessions/test-stopped-in-progress"
elif scenario == "repeat_feedback_grace_period":
    session_name = "sessions/test-repeat-feedback-grace"
else:
    session_name = "sessions/test-repeat-feedback"
task_id = os.environ.get("FAKE_TASK_ID", "proxy-runtime-final-answer-mode-stability")
if scenario == "in_progress_inactive_manifest":
    task_id = os.environ.get("FAKE_INACTIVE_TASK_ID", "proxy-runtime-final-answer-mode-stability-blocked")

if method == "GET" and url.endswith("/sessions?pageSize=100"):
    state = "IN_PROGRESS" if scenario.startswith("in_progress_") or scenario == "stopped_in_progress" else "AWAITING_USER_FEEDBACK"
    update_time = iso(now - 5) if scenario in {"in_progress_long_running_fresh", "in_progress_no_agent_long_running", "in_progress_no_agent_short_burst", "in_progress_no_agent_token_stale"} else iso(now - 4000) if scenario.startswith("in_progress_") or scenario == "stopped_in_progress" else iso(now - 60)
    create_time = iso(now - 300) if scenario == "in_progress_no_agent_short_burst" else iso(now - 20000) if scenario in {"in_progress_long_running_fresh", "in_progress_no_agent_long_running", "in_progress_no_agent_token_stale", "in_progress_no_agent_repeat"} else iso(now - 900)
    payload = {
        "sessions": [
            {
                "name": session_name,
                "state": state,
                "sourceContext": {"source": "sources/github/Omnividente/notion-abuz_ai"},
                "createTime": create_time,
                "updateTime": update_time,
            }
        ]
    }
elif method == "GET" and "/pulls?state=open" in url:
    payload = []
    if scenario == "stopped_in_progress":
        payload = [
            {
                "number": 421,
                "title": task_id,
                "body": "",
                "head": {"ref": f"{task_id}-{session_name.split('/')[-1]}"},
                "labels": [
                    {"name": "jules"},
                    {"name": "human-review"},
                    {"name": "no-automerge"},
                    {"name": "stop-loop"},
                ],
            }
        ]
elif method == "GET" and "/actions/variables/JULES_RECENT_SESSION_TASKS" in url:
    if scenario == "in_progress_unknown_task":
        payload = {"value": "{}"}
    else:
        payload = {
            "value": json.dumps(
                {
                    session_name.split("/")[-1]: {
                        "task_id": task_id,
                        "updateTime": iso(now - 60),
                    }
                }
            )
        }
elif method == "GET" and f"/{session_name}/activities?" in url and scenario == "stopped_in_progress":
    payload = {
        "activities": [
            {
                "originator": "AGENT",
                "createTime": iso(now - 3900),
                "message": {
                    "text": (
                        f"selected task id: {task_id}\n"
                        "Still waiting on an old stopped autonomous PR."
                    )
                },
            }
        ]
    }
elif method == "GET" and f"/{session_name}/activities?" in url and scenario == "routine_question":
    payload = {
        "activities": [
            {
                "originator": "AGENT",
                "createTime": iso(now - 300),
                "message": {
                    "text": (
                        f"selected task id: {task_id}\n"
                        "Should I run local tests before opening the PR? "
                        "ghp_abcdef1234567890"
                    )
                },
            }
        ]
    }
elif method == "GET" and f"/{session_name}/activities?" in url and scenario == "in_progress_stale":
    payload = {
        "activities": [
            {
                "originator": "AGENT",
                "createTime": iso(now - 3900),
                "message": {
                    "text": (
                        f"selected task id: {task_id}\n"
                        "API error left me with partial context from the previous search."
                    )
                },
            }
        ]
    }
elif method == "GET" and f"/{session_name}/activities?" in url and scenario == "in_progress_inactive_manifest":
    payload = {
        "activities": [
            {
                "originator": "AGENT",
                "createTime": iso(now - 3900),
                "message": {
                    "text": (
                        f"selected task id: {task_id}\n"
                        "Old inactive task session should not receive another recovery prompt."
                    )
                },
            }
        ]
    }
elif method == "GET" and f"/{session_name}/activities?" in url and scenario == "in_progress_unknown_task":
    payload = {
        "activities": [
            {
                "originator": "AGENT",
                "createTime": iso(now - 3900),
                "message": {
                    "text": "I am still working, but this old transcript has no selected task id."
                },
            }
        ]
    }
elif method == "GET" and f"/{session_name}/activities?" in url and scenario == "in_progress_long_running_fresh":
    payload = {
        "activities": [
            {
                "originator": "AGENT",
                "createTime": iso(now - 10),
                "message": {
                    "text": (
                        f"selected task id: {task_id}\n"
                        "Still validating recovery evidence before opening a PR."
                    )
                },
            }
        ]
    }
elif method == "GET" and f"/{session_name}/activities?" in url and scenario in {"in_progress_no_agent_long_running", "in_progress_no_agent_short_burst"}:
    payload = {"activities": []}
elif method == "GET" and f"/{session_name}/activities?" in url and scenario == "in_progress_no_agent_token_stale":
    payload = {
        "activities": [
            {
                "originator": "USER",
                "createTime": iso(now - 360),
                "message": {"text": "AUTONOMOUS_CONTINUE_TOKEN\nRecover stalled no-agent work."},
            },
        ]
    }
elif method == "GET" and f"/{session_name}/activities?" in url and scenario == "in_progress_no_agent_repeat":
    payload = {
        "activities": [
            {
                "originator": "USER",
                "createTime": iso(now - 4900),
                "message": {"text": "AUTONOMOUS_CONTINUE_TOKEN\nRecover stalled no-agent work."},
            },
            {
                "originator": "USER",
                "createTime": iso(now - 4500),
                "message": {"text": "AUTONOMOUS_CONTINUE_TOKEN\nRecover stalled no-agent work again."},
            },
        ]
    }
elif method == "GET" and f"/{session_name}/activities?" in url and scenario == "in_progress_no_agent_repeat_grace_period":
    payload = {
        "activities": [
            {
                "originator": "USER",
                "createTime": iso(now - 4900),
                "message": {"text": "AUTONOMOUS_CONTINUE_TOKEN\nRecover stalled no-agent work."},
            },
            {
                "originator": "USER",
                "createTime": iso(now - 120),
                "message": {"text": "AUTONOMOUS_CONTINUE_TOKEN\nRecover stalled no-agent work again in grace period."},
            },
        ]
    }
elif method == "GET" and f"/{session_name}/activities?" in url and scenario == "in_progress_repeat":
    payload = {
        "activities": [
            {
                "originator": "AGENT",
                "createTime": iso(now - 5000),
                "message": {
                    "text": f"selected task id: {task_id}\nStill working."
                },
            },
            {
                "originator": "USER",
                "createTime": iso(now - 4900),
                "message": {"text": "AUTONOMOUS_CONTINUE_TOKEN\nRecover stalled work."},
            },
            {
                "originator": "USER",
                "createTime": iso(now - 4500),
                "message": {"text": "AUTONOMOUS_CONTINUE_TOKEN\nRecover stalled work again."},
            },
        ]
    }
elif method == "GET" and f"/{session_name}/activities?" in url and scenario == "in_progress_repeat_grace_period":
    payload = {
        "activities": [
            {
                "originator": "AGENT",
                "createTime": iso(now - 5000),
                "message": {
                    "text": f"selected task id: {task_id}\nStill working."
                },
            },
            {
                "originator": "USER",
                "createTime": iso(now - 4900),
                "message": {"text": "AUTONOMOUS_CONTINUE_TOKEN\nRecover stalled work."},
            },
            {
                "originator": "USER",
                "createTime": iso(now - 120),
                "message": {"text": "AUTONOMOUS_CONTINUE_TOKEN\nRecover stalled work again in grace period."},
            },
        ]
    }
elif method == "GET" and f"/{session_name}/activities?" in url and scenario == "repeat_feedback_grace_period":
    payload = {
        "activities": [
            {
                "originator": "AGENT",
                "createTime": iso(now - 500),
                "message": {
                    "text": f"selected task id: {task_id}\nI need input before continuing."
                },
            },
            {
                "originator": "USER",
                "createTime": iso(now - 490),
                "message": {"text": "AUTONOMOUS_CONTINUE_TOKEN\nContinue."},
            },
            {
                "originator": "USER",
                "createTime": iso(now - 120),
                "message": {"text": "AUTONOMOUS_CONTINUE_TOKEN\nContinue again in grace period."},
            },
        ]
    }
elif method == "GET" and f"/{session_name}/activities?" in url:
    payload = {
        "activities": [
            {
                "originator": "AGENT",
                "createTime": iso(now - 500),
                "message": {
                    "text": f"selected task id: {task_id}\nI need input before continuing."
                },
            },
            {
                "originator": "USER",
                "createTime": iso(now - 490),
                "message": {"text": "AUTONOMOUS_CONTINUE_TOKEN\nContinue."},
            },
            {
                "originator": "USER",
                "createTime": iso(now - 450),
                "message": {"text": "AUTONOMOUS_CONTINUE_TOKEN\nContinue again."},
            },
            {
                "originator": "USER",
                "createTime": iso(now - 300),
                "message": {"text": "AUTONOMOUS_CONTINUE_TOKEN\nStill continue."},
            },
        ]
    }
elif method == "POST" and url.endswith(f"/{session_name}:sendMessage"):
    Path(os.environ["FAKE_CURL_LOG"]).write_text(f"POST {session_name}\n", encoding="utf-8")
    Path(os.environ["FAKE_SEND_BODY"]).write_text(body, encoding="utf-8")
    payload = {}
elif method == "DELETE" and url.endswith(f"/{session_name}"):
    Path(os.environ["FAKE_CURL_LOG"]).write_text(f"DELETE {session_name}\n", encoding="utf-8")
    payload = {}
else:
    print(f"unexpected fake curl call: method={method} url={url}", file=sys.stderr)
    raise SystemExit(22)

Path(out).write_text(json.dumps(payload), encoding="utf-8")
if "%{http_code}" in writeout:
    print("200", end="")
PY
"""


class JulesUnattendedMonitorTest(unittest.TestCase):
    def run_monitor(
        self,
        tmp_path: Path,
        *,
        scenario: str,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
        if not shutil.which("bash"):
            self.skipTest("bash is required for jules-unattended-monitor.sh")
        if not shutil.which("jq"):
            self.skipTest("jq is required for jules-unattended-monitor.sh")

        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        fake_curl = fake_bin / "curl"
        fake_curl.write_text(FAKE_CURL, encoding="utf-8", newline="\n")
        fake_curl.chmod(0o755)

        output_path = tmp_path / "github-output.txt"
        curl_log = tmp_path / "curl.log"
        send_body = tmp_path / "send-body.json"
        empty_pr_context = tmp_path / "empty-pr-context.json"
        empty_pr_context.write_text("{}", encoding="utf-8")
        manifest_path = tmp_path / "agent_tasks.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "tasks": [
                        {
                            "id": TASK_ID,
                            "status": "todo",
                            "area": "proxy",
                            "risk": "medium",
                            "title": "Exercise active monitor task",
                            "description": "Runtime failure evidence from a transcript.",
                            "allowed_paths": [".github/scripts/jules-unattended-monitor.sh"],
                            "acceptance": ["Recovery prompt behavior is verified."],
                        },
                        {
                            "id": INACTIVE_TASK_ID,
                            "status": "blocked",
                            "blocked_reason": "Superseded inactive monitor test fixture task.",
                            "area": "proxy",
                            "risk": "medium",
                            "title": "Inactive monitor task",
                            "description": "Blocked task must not receive recovery prompts.",
                            "allowed_paths": [".github/scripts/jules-unattended-monitor.sh"],
                            "acceptance": ["Inactive task session is skipped."],
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.update(
            {
                "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
                "PYTHON_FOR_FAKE_CURL": sys.executable,
                "FAKE_TASK_ID": TASK_ID,
                "FAKE_INACTIVE_TASK_ID": INACTIVE_TASK_ID,
                "FAKE_NOW_EPOCH": str(int(time.time())),
                "FAKE_SCENARIO": scenario,
                "FAKE_CURL_LOG": str(curl_log),
                "FAKE_SEND_BODY": str(send_body),
                "JULES_FAILED_PR_CONTEXT_FIXTURE": str(empty_pr_context),
                "AGENT_TASKS_MANIFEST": str(manifest_path),
                "GITHUB_OUTPUT": str(output_path),
                "GITHUB_REPOSITORY": "Omnividente/notion-abuz_ai",
                "JULES_API_KEY": "fake-key",
                "LOOKBACK_HOURS": "24",
                "MIN_USER_REPLY_INTERVAL_MINUTES": "0",
                "STALE_AWAITING_FEEDBACK_MINUTES": "10",
                "MAX_STALE_AWAITING_FEEDBACK_ESCALATIONS": "2",
                "STALE_IN_PROGRESS_MINUTES": "45",
                "MAX_IN_PROGRESS_SESSION_MINUTES": "180",
                "MAX_STALE_IN_PROGRESS_ESCALATIONS": "2",
            }
        )
        env.update(extra_env or {})
        for name in ("JULES_API_KEY_BACKUP", "GITHUB_API_TOKEN", "GITHUB_API_URL"):
            if not extra_env or name not in extra_env:
                env.pop(name, None)
        if scenario.startswith("in_progress_no_agent_") or scenario == "stopped_in_progress":
            env["GITHUB_API_TOKEN"] = "fake-gh-token"
            env["GITHUB_API_URL"] = "https://api.github.test"

        result = subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        return result, output_path, send_body

    def test_dynamic_prompt_answers_routine_question_with_task_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result, output_path, send_body = self.run_monitor(tmp_path, scenario="routine_question")

            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn("Sent dynamic autonomous continue recovery message", result.stdout)
            body = json.loads(send_body.read_text(encoding="utf-8"))
            prompt = body["prompt"]
            self.assertIn("repo: Omnividente/notion-abuz_ai", prompt)
            self.assertIn("session_id: test-routine", prompt)
            self.assertIn("session_state: AWAITING_USER_FEEDBACK", prompt)
            self.assertIn("task_id: proxy-runtime-final-answer-mode-stability", prompt)
            self.assertIn("wait_reason: routine_question", prompt)
            self.assertIn("prompt_action: choose_safe_next_step", prompt)
            self.assertIn("allowed_paths:", prompt)
            self.assertIn("[REDACTED]", prompt)
            self.assertNotIn("ghp_abcdef1234567890", prompt)

            outputs = dict(
                line.split("=", 1)
                for line in output_path.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            self.assertEqual(outputs["active_sessions"], "1")
            self.assertEqual(outputs["touched_sessions"], "1")
            self.assertIn("routine_question", outputs["wait_reason"])
            self.assertIn("choose_safe_next_step", outputs["prompt_action"])
            self.assertIn(TASK_ID, outputs["prompt_task_id"])
            self.assertIn("0/2", outputs["continue_attempts"])

    def test_dynamic_prompt_includes_failed_pr_context_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fixture = tmp_path / "failed-pr-context.json"
            fixture.write_text(
                json.dumps(
                    {
                        "repo": "Omnividente/notion-abuz_ai",
                        "pr_number": "#401",
                        "head_sha": "abc123",
                        "changed_files": [".github/scripts/jules-unattended-monitor.sh"],
                        "failed_checks": [
                            {
                                "name": "CI / validate",
                                "conclusion": "failure",
                                "details_url": (
                                    "https://github.com/Omnividente/notion-abuz_ai/actions/runs/123/job/9"
                                    "?token=ghp_abcdef1234567890"
                                ),
                                "annotations": [
                                    "monitor.sh: failed with token=ghp_abcdef1234567890"
                                ],
                                "log_excerpt": "##[error]Process completed with exit code 1.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result, _output_path, send_body = self.run_monitor(
                tmp_path,
                scenario="routine_question",
                extra_env={
                    "GITHUB_API_TOKEN": "fake-gh-token",
                    "GITHUB_API_URL": "https://api.github.test",
                    "JULES_FAILED_PR_CONTEXT_FIXTURE": str(fixture),
                },
            )

            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            body = json.loads(send_body.read_text(encoding="utf-8"))
            prompt = body["prompt"]
            self.assertIn("pr_context: available", prompt)
            self.assertIn("pr_number: #401", prompt)
            self.assertIn("changed_files:", prompt)
            self.assertIn("CI / validate: failure", prompt)
            self.assertIn("annotation:", prompt)
            self.assertIn("log_excerpt:", prompt)
            self.assertIn("используй annotations/log_excerpt/changed_files", prompt)
            self.assertIn("[REDACTED]", prompt)
            self.assertNotIn("ghp_abcdef1234567890", prompt)

    def test_repeated_autonomous_continue_limit_deletes_without_waiting_for_stale_age(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result, output_path, _send_body = self.run_monitor(tmp_path, scenario="repeat_feedback")
            curl_log = tmp_path / "curl.log"

            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn(
                "Autonomous continue limit reached for sessions/test-repeat-feedback",
                result.stdout,
            )
            self.assertNotIn("autonomous continue already answers the latest wait state", result.stdout)
            self.assertEqual(curl_log.read_text(encoding="utf-8"), "DELETE sessions/test-repeat-feedback\n")

            outputs = dict(
                line.split("=", 1)
                for line in output_path.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            self.assertEqual(outputs["active_sessions"], "0")
            self.assertEqual(outputs["touched_sessions"], "1")
            self.assertEqual(outputs["stale_waiting_count"], "0")
            self.assertEqual(outputs["failed_recovery_action"], "block")
            self.assertEqual(outputs["failed_task_id"], TASK_ID)
            self.assertEqual(outputs["failed_session_id"], "test-repeat-feedback")

    def test_stale_in_progress_session_gets_dynamic_recovery_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result, output_path, send_body = self.run_monitor(tmp_path, scenario="in_progress_stale")

            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn("Sent dynamic stale in-progress recovery message", result.stdout)
            body = json.loads(send_body.read_text(encoding="utf-8"))
            prompt = body["prompt"]
            self.assertIn("repo: Omnividente/notion-abuz_ai", prompt)
            self.assertIn("session_id: test-in-progress-stale", prompt)
            self.assertIn("session_state: IN_PROGRESS", prompt)
            self.assertIn(f"task_id: {TASK_ID}", prompt)
            self.assertIn("wait_reason: transient_api_or_partial_context", prompt)
            self.assertIn("prompt_action: repeat_targeted_context_collection", prompt)
            self.assertIn("Jules session stayed IN_PROGRESS without recent activity", prompt)
            self.assertIn("Не продолжай по частичным данным", prompt)

            outputs = dict(
                line.split("=", 1)
                for line in output_path.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            self.assertEqual(outputs["active_sessions"], "1")
            self.assertEqual(outputs["touched_sessions"], "1")
            self.assertEqual(outputs["stale_in_progress_count"], "1")
            self.assertIn("transient_api_or_partial_context", outputs["wait_reason"])
            self.assertIn("repeat_targeted_context_collection", outputs["prompt_action"])

    def test_stopped_task_session_does_not_block_or_receive_recovery_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result, output_path, send_body = self.run_monitor(
                tmp_path,
                scenario="stopped_in_progress",
            )

            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn(f"Stopped autonomous task ids: {TASK_ID}", result.stdout)
            self.assertIn("Skipped sessions/test-stopped-in-progress", result.stdout)
            self.assertIn("stopped autonomous PR awaiting review", result.stdout)
            self.assertFalse(send_body.exists())

            outputs = dict(
                line.split("=", 1)
                for line in output_path.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            self.assertEqual(outputs["active_sessions"], "0")
            self.assertEqual(outputs["touched_sessions"], "0")
            self.assertEqual(outputs["stale_in_progress_count"], "0")
            self.assertEqual(outputs["skipped_stopped_count"], "1")
            self.assertIn(f"test-stopped-in-progress:{TASK_ID}", outputs["skipped_stopped_sessions"])

    def test_inactive_manifest_task_session_does_not_receive_recovery_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result, output_path, send_body = self.run_monitor(
                tmp_path,
                scenario="in_progress_inactive_manifest",
            )

            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn("Skipped sessions/test-in-progress-inactive", result.stdout)
            self.assertIn(f"task {INACTIVE_TASK_ID} is blocked", result.stdout)
            self.assertFalse(send_body.exists())

            outputs = dict(
                line.split("=", 1)
                for line in output_path.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            self.assertEqual(outputs["active_sessions"], "0")
            self.assertEqual(outputs["touched_sessions"], "0")
            self.assertEqual(outputs["stale_in_progress_count"], "0")
            self.assertEqual(outputs["skipped_inactive_count"], "1")
            self.assertIn(
                f"test-in-progress-inactive:{INACTIVE_TASK_ID}:blocked",
                outputs["skipped_inactive_sessions"],
            )

    def test_unknown_in_progress_session_does_not_receive_recovery_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result, output_path, send_body = self.run_monitor(
                tmp_path,
                scenario="in_progress_unknown_task",
            )

            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn("Skipped sessions/test-in-progress-unknown", result.stdout)
            self.assertIn("IN_PROGRESS recovery has no task_id", result.stdout)
            self.assertFalse(send_body.exists())

            outputs = dict(
                line.split("=", 1)
                for line in output_path.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            self.assertEqual(outputs["active_sessions"], "0")
            self.assertEqual(outputs["touched_sessions"], "0")
            self.assertEqual(outputs["stale_in_progress_count"], "0")
            self.assertEqual(outputs["skipped_unknown_in_progress_count"], "1")
            self.assertIn(
                "test-in-progress-unknown",
                outputs["skipped_unknown_in_progress_sessions"],
            )

    def test_long_running_in_progress_session_gets_recovery_prompt_even_with_fresh_activity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result, output_path, send_body = self.run_monitor(
                tmp_path,
                scenario="in_progress_long_running_fresh",
            )

            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn("Detected long-running IN_PROGRESS Jules session", result.stdout)
            self.assertIn("Sent dynamic stale in-progress recovery message", result.stdout)
            body = json.loads(send_body.read_text(encoding="utf-8"))
            prompt = body["prompt"]
            self.assertIn("session_id: test-in-progress-long-running", prompt)
            self.assertIn(f"task_id: {TASK_ID}", prompt)
            self.assertIn("Jules session stayed IN_PROGRESS for", prompt)
            self.assertIn("without opening a PR", prompt)

            outputs = dict(
                line.split("=", 1)
                for line in output_path.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            self.assertEqual(outputs["active_sessions"], "1")
            self.assertEqual(outputs["touched_sessions"], "1")
            self.assertEqual(outputs["stale_in_progress_count"], "1")
            self.assertIn("test-in-progress-long-running:long-running", outputs["stale_in_progress_sessions"])

    def test_long_running_in_progress_without_agent_activity_gets_recovery_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result, output_path, send_body = self.run_monitor(
                tmp_path,
                scenario="in_progress_no_agent_long_running",
            )

            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn("without agent activity", result.stdout)
            self.assertIn("Sent dynamic no-agent in-progress recovery message", result.stdout)
            body = json.loads(send_body.read_text(encoding="utf-8"))
            prompt = body["prompt"]
            self.assertIn("session_id: test-in-progress-no-agent", prompt)
            self.assertIn(f"task_id: {TASK_ID}", prompt)
            self.assertIn("wait_reason: unknown_continue", prompt)
            self.assertIn("without any agent activity or PR", prompt)

            outputs = dict(
                line.split("=", 1)
                for line in output_path.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            self.assertEqual(outputs["active_sessions"], "1")
            self.assertEqual(outputs["touched_sessions"], "1")
            self.assertEqual(outputs["stale_in_progress_count"], "1")
            self.assertIn("test-in-progress-no-agent:no-agent-long-running", outputs["stale_in_progress_sessions"])
            self.assertIn(TASK_ID, outputs["prompt_task_id"])

    def test_no_agent_in_progress_under_default_threshold_waits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result, output_path, send_body = self.run_monitor(
                tmp_path,
                scenario="in_progress_no_agent_short_burst",
            )

            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn("no-agent threshold 10800s", result.stdout)
            self.assertFalse(send_body.exists())

            outputs = dict(
                line.split("=", 1)
                for line in output_path.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            self.assertEqual(outputs["active_sessions"], "1")
            self.assertEqual(outputs["touched_sessions"], "0")
            self.assertEqual(outputs["stale_in_progress_count"], "0")

    def test_burst_no_agent_threshold_sends_recovery_before_three_hours(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result, output_path, send_body = self.run_monitor(
                tmp_path,
                scenario="in_progress_no_agent_short_burst",
                extra_env={"NO_AGENT_IN_PROGRESS_MINUTES": "4"},
            )

            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn("without agent activity after 5 minute(s)", result.stdout)
            self.assertIn("Sent dynamic no-agent in-progress recovery message", result.stdout)
            body = json.loads(send_body.read_text(encoding="utf-8"))
            prompt = body["prompt"]
            self.assertIn("session_id: test-in-progress-no-agent-short", prompt)
            self.assertIn(f"task_id: {TASK_ID}", prompt)
            self.assertIn("without any agent activity or PR", prompt)

            outputs = dict(
                line.split("=", 1)
                for line in output_path.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            self.assertEqual(outputs["active_sessions"], "1")
            self.assertEqual(outputs["touched_sessions"], "1")
            self.assertEqual(outputs["stale_in_progress_count"], "1")
            self.assertIn("test-in-progress-no-agent-short:no-agent-long-running", outputs["stale_in_progress_sessions"])

    def test_no_agent_recovery_token_under_default_stale_threshold_waits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result, output_path, send_body = self.run_monitor(
                tmp_path,
                scenario="in_progress_no_agent_token_stale",
            )

            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn("no-agent stale threshold 2700s", result.stdout)
            self.assertFalse(send_body.exists())

            outputs = dict(
                line.split("=", 1)
                for line in output_path.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            self.assertEqual(outputs["active_sessions"], "1")
            self.assertEqual(outputs["touched_sessions"], "0")
            self.assertEqual(outputs["stale_in_progress_count"], "1")
            self.assertIn("test-in-progress-no-agent-token-stale:no-agent-token", outputs["stale_in_progress_sessions"])

    def test_no_agent_recovery_token_uses_dedicated_stale_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result, output_path, send_body = self.run_monitor(
                tmp_path,
                scenario="in_progress_no_agent_token_stale",
                extra_env={"NO_AGENT_STALE_IN_PROGRESS_MINUTES": "5"},
            )

            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn("Previous no-agent in-progress recovery", result.stdout)
            self.assertIn("Sent dynamic no-agent in-progress recovery message", result.stdout)
            body = json.loads(send_body.read_text(encoding="utf-8"))
            prompt = body["prompt"]
            self.assertIn("session_id: test-in-progress-no-agent-token-stale", prompt)
            self.assertIn("previous no-agent in-progress autonomous recovery token is stale", prompt)

            outputs = dict(
                line.split("=", 1)
                for line in output_path.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            self.assertEqual(outputs["active_sessions"], "1")
            self.assertEqual(outputs["touched_sessions"], "1")
            self.assertEqual(outputs["stale_in_progress_count"], "1")
            self.assertIn("test-in-progress-no-agent-token-stale:no-agent-stale-token", outputs["stale_in_progress_sessions"])

    def test_repeated_no_agent_in_progress_limit_deletes_and_blocks_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result, output_path, _send_body = self.run_monitor(
                tmp_path,
                scenario="in_progress_no_agent_repeat",
            )
            curl_log = tmp_path / "curl.log"

            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn("without agent activity; deleting stale session", result.stdout)
            self.assertEqual(curl_log.read_text(encoding="utf-8"), "DELETE sessions/test-in-progress-no-agent-repeat\n")

            outputs = dict(
                line.split("=", 1)
                for line in output_path.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            self.assertEqual(outputs["active_sessions"], "0")
            self.assertEqual(outputs["touched_sessions"], "1")
            self.assertEqual(outputs["failed_recovery_action"], "block")
            self.assertEqual(outputs["failed_task_id"], TASK_ID)
            self.assertEqual(outputs["failed_session_id"], "test-in-progress-no-agent-repeat")

    def test_repeated_stale_in_progress_limit_deletes_and_blocks_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result, output_path, _send_body = self.run_monitor(tmp_path, scenario="in_progress_repeat")
            curl_log = tmp_path / "curl.log"

            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn(
                "Autonomous in-progress recovery limit reached for sessions/test-in-progress-repeat",
                result.stdout,
            )
            self.assertEqual(curl_log.read_text(encoding="utf-8"), "DELETE sessions/test-in-progress-repeat\n")

            outputs = dict(
                line.split("=", 1)
                for line in output_path.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            self.assertEqual(outputs["active_sessions"], "0")
            self.assertEqual(outputs["touched_sessions"], "1")
            self.assertEqual(outputs["failed_recovery_action"], "block")
            self.assertEqual(outputs["failed_task_id"], TASK_ID)
            self.assertEqual(outputs["failed_session_id"], "test-in-progress-repeat")


    def test_repeated_autonomous_continue_limit_waits_for_grace_period(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result, output_path, send_body = self.run_monitor(tmp_path, scenario="repeat_feedback_grace_period")
            curl_log = tmp_path / "curl.log"

            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn("Skipped deletion of sessions/test-repeat-feedback-grace; session is in its 5 minute termination grace period (120s old).", result.stdout)
            self.assertNotIn("Autonomous continue limit reached", result.stdout)
            if curl_log.exists():
                self.assertNotIn("DELETE", curl_log.read_text(encoding="utf-8"))
            self.assertFalse(send_body.exists())

            outputs = dict(
                line.split("=", 1)
                for line in output_path.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            self.assertEqual(outputs["active_sessions"], "1")
            self.assertEqual(outputs["touched_sessions"], "0")
            self.assertEqual(outputs.get("stale_waiting_count", "0"), "0")

    def test_repeated_no_agent_in_progress_limit_waits_for_grace_period(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result, output_path, send_body = self.run_monitor(
                tmp_path,
                scenario="in_progress_no_agent_repeat_grace_period",
            )
            curl_log = tmp_path / "curl.log"

            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn("Skipped deletion of sessions/test-in-progress-no-agent-repeat-grace; session is in its 5 minute termination grace period (120s old).", result.stdout)
            if curl_log.exists():
                self.assertNotIn("DELETE", curl_log.read_text(encoding="utf-8"))

            outputs = dict(
                line.split("=", 1)
                for line in output_path.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            self.assertEqual(outputs["active_sessions"], "1")
            self.assertEqual(outputs["touched_sessions"], "0")

    def test_repeated_stale_in_progress_limit_waits_for_grace_period(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result, output_path, send_body = self.run_monitor(tmp_path, scenario="in_progress_repeat_grace_period")
            curl_log = tmp_path / "curl.log"

            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn("Skipped deletion of sessions/test-in-progress-repeat-grace; session is in its 5 minute termination grace period (120s old).", result.stdout)
            if curl_log.exists():
                self.assertNotIn("DELETE", curl_log.read_text(encoding="utf-8"))
            self.assertFalse(send_body.exists())


if __name__ == "__main__":
    unittest.main()
