#!/usr/bin/env bash
set -euo pipefail

API_BASE="${JULES_API_BASE:-https://jules.googleapis.com/v1alpha}"
SOURCE="${JULES_SOURCE:-sources/github/${GITHUB_REPOSITORY:-Omnividente/notion-abuz_ai}}"
LOOKBACK_HOURS="${LOOKBACK_HOURS:-24}"
MIN_USER_REPLY_INTERVAL_MINUTES="${MIN_USER_REPLY_INTERVAL_MINUTES:-2}"
STALE_AWAITING_FEEDBACK_MINUTES="${STALE_AWAITING_FEEDBACK_MINUTES:-10}"
MAX_STALE_AWAITING_FEEDBACK_ESCALATIONS="${MAX_STALE_AWAITING_FEEDBACK_ESCALATIONS:-2}"
STALE_IN_PROGRESS_MINUTES="${STALE_IN_PROGRESS_MINUTES:-45}"
MAX_IN_PROGRESS_SESSION_MINUTES="${MAX_IN_PROGRESS_SESSION_MINUTES:-180}"
NO_AGENT_IN_PROGRESS_MINUTES="${NO_AGENT_IN_PROGRESS_MINUTES:-$MAX_IN_PROGRESS_SESSION_MINUTES}"
NO_AGENT_STALE_IN_PROGRESS_MINUTES="${NO_AGENT_STALE_IN_PROGRESS_MINUTES:-$STALE_IN_PROGRESS_MINUTES}"
MAX_STALE_IN_PROGRESS_ESCALATIONS="${MAX_STALE_IN_PROGRESS_ESCALATIONS:-2}"

if [ -z "${JULES_API_KEY:-}" ] && [ -z "${JULES_API_KEY_BACKUP:-}" ]; then
  echo "::warning::No Jules API keys configured; unattended monitor cannot inspect sessions."
  {
    echo "active_sessions=0"
    echo "touched_sessions=0"
    echo "api_available=false"
    echo "session_ids="
    echo "failed_sessions="
    echo "failed_task_id="
    echo "failed_session_id="
    echo "failed_count_for_task=0"
    echo "failed_recovery_action=none"
    echo "failed_recovery_reason=no Jules API keys configured"
  } >> "${GITHUB_OUTPUT:-/dev/null}"
  exit 0
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
failed_sessions_file="${tmpdir}/failed-sessions.tsv"
active_task_ids_file="${tmpdir}/active-task-ids.txt"
stopped_task_ids_file="${tmpdir}/stopped-task-ids.txt"
: > "$failed_sessions_file"
: > "$active_task_ids_file"
: > "$stopped_task_ids_file"

declare -a key_labels=()
declare -a key_values=()

if [ -n "${JULES_API_KEY:-}" ]; then
  key_labels+=("primary")
  key_values+=("${JULES_API_KEY}")
fi

if [ -n "${JULES_API_KEY_BACKUP:-}" ]; then
  key_labels+=("backup")
  key_values+=("${JULES_API_KEY_BACKUP}")
fi

now_epoch="$(date -u +%s)"
cutoff_epoch="$((now_epoch - LOOKBACK_HOURS * 3600))"
reply_cooldown_seconds="$((MIN_USER_REPLY_INTERVAL_MINUTES * 60))"
stale_feedback_seconds="$((STALE_AWAITING_FEEDBACK_MINUTES * 60))"
stale_in_progress_seconds="$((STALE_IN_PROGRESS_MINUTES * 60))"
max_in_progress_session_seconds="$((MAX_IN_PROGRESS_SESSION_MINUTES * 60))"
no_agent_in_progress_seconds="$((NO_AGENT_IN_PROGRESS_MINUTES * 60))"
no_agent_stale_in_progress_seconds="$((NO_AGENT_STALE_IN_PROGRESS_MINUTES * 60))"
max_stale_feedback_escalations="$MAX_STALE_AWAITING_FEEDBACK_ESCALATIONS"
max_stale_in_progress_escalations="$MAX_STALE_IN_PROGRESS_ESCALATIONS"

active_sessions=0
touched_sessions=0
api_available=false
session_ids=()
stale_waiting_sessions=()
stale_in_progress_sessions=()
skipped_stopped_sessions=()
wait_reason_details=()
prompt_action_details=()
prompt_task_id_details=()
continue_attempt_details=()
declare -A seen_sessions=()

jules_get() {
  local key="$1"
  local path="$2"
  local out="$3"
  curl -fsS \
    -H "X-Goog-Api-Key: ${key}" \
    "${API_BASE}/${path}" \
    -o "$out"
}

jules_post() {
  local key="$1"
  local path="$2"
  local body="$3"
  local out="$4"
  curl -fsS \
    -X POST \
    -H "Content-Type: application/json" \
    -H "X-Goog-Api-Key: ${key}" \
    "${API_BASE}/${path}" \
    -d "$body" \
    -o "$out"
}

jules_delete() {
  local key="$1"
  local path="$2"
  local out="$3"
  curl -fsS \
    -X DELETE \
    -H "X-Goog-Api-Key: ${key}" \
    "${API_BASE}/${path}" \
    -o "$out"
}

extract_session_task_id() {
  local key="$1"
  local session_name="$2"
  local prefix="$3"
  local activities_file="${tmpdir}/activities-${prefix}-${session_name//\//-}.json"

  if ! jules_get "$key" "${session_name}/activities?pageSize=50" "$activities_file"; then
    echo "::warning::Could not list activities for ${session_name}; task id cannot be extracted." >&2
    return 0
  fi

  python3 .github/scripts/summarize-jules-failures.py extract-task-id "$activities_file" || true
}

extract_failed_session_context() {
  local key="$1"
  local session_name="$2"
  local activities_file="${tmpdir}/activities-failed-${session_name//\//-}.json"

  if ! jules_get "$key" "${session_name}/activities?pageSize=50" "$activities_file"; then
    echo "::warning::Could not list activities for ${session_name}; failed context cannot be extracted." >&2
    printf '\t\n'
    return 0
  fi

  python3 .github/scripts/summarize-jules-failures.py failed-context "$activities_file" || printf '\t\n'
}

resolve_session_task_id_from_recent_map() {
  local session_name="$1"

  if [ -z "${GITHUB_API_TOKEN:-}" ]; then
    return 0
  fi
  if [ -z "${GITHUB_API_URL:-}" ] || [ -z "${GITHUB_REPOSITORY:-}" ]; then
    return 0
  fi

  local out="${tmpdir}/jules-session-task-map.json"
  local status
  status="$(curl -sS \
    -H "Authorization: Bearer ${GITHUB_API_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    -o "$out" \
    -w "%{http_code}" \
    "${GITHUB_API_URL}/repos/${GITHUB_REPOSITORY}/actions/variables/JULES_RECENT_SESSION_TASKS" || echo "000")"

  if [[ ! "$status" =~ ^2[0-9][0-9]$ ]]; then
    return 0
  fi

  jq -r --arg sid "${session_name##*/}" '
    (.value // "{}" | fromjson? // {} | .[$sid].task_id // "")
  ' "$out" || true
}

load_stopped_task_ids() {
  if [ -z "${GITHUB_API_TOKEN:-}" ]; then
    return 0
  fi
  if [ -z "${GITHUB_API_URL:-}" ] || [ -z "${GITHUB_REPOSITORY:-}" ]; then
    return 0
  fi

  local pulls_file="${tmpdir}/open-pulls.json"
  local status
  status="$(curl -sS \
    -H "Authorization: Bearer ${GITHUB_API_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    -o "$pulls_file" \
    -w "%{http_code}" \
    "${GITHUB_API_URL}/repos/${GITHUB_REPOSITORY}/pulls?state=open&per_page=100" || echo "000")"

  if [[ ! "$status" =~ ^2[0-9][0-9]$ ]]; then
    echo "::warning::Could not list open PRs to identify stopped autonomous tasks; status ${status}."
    return 0
  fi

  if ! python3 - "$pulls_file" agent_tasks.json > "$stopped_task_ids_file" <<'PY'
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


STOP_LABELS = {"human-review", "no-automerge", "stop-loop"}
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


def labels_of(pr: dict) -> set[str]:
    labels = pr.get("labels") or []
    return {
        str(label.get("name") if isinstance(label, dict) else label)
        for label in labels
        if label
    }


def head_ref(pr: dict) -> str:
    head = pr.get("head") or {}
    return str(head.get("ref") or "")


def task_id_from_pr(pr: dict, task_ids: list[str]) -> str:
    fields = [
        str(pr.get("body") or ""),
        str(pr.get("title") or ""),
        head_ref(pr),
    ]
    for field in fields:
        match = TASK_ID_RE.search(field)
        if match:
            return match.group(1)
    for task_id in task_ids:
        if any(task_id and task_id in field for field in fields):
            return task_id
    return ""


pulls_path = Path(sys.argv[1])
manifest_path = Path(sys.argv[2])
pulls = json.loads(pulls_path.read_text(encoding="utf-8"))
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
task_ids = [
    str(task.get("id") or "")
    for task in manifest.get("tasks", [])
    if isinstance(task, dict) and task.get("id")
]

seen: set[str] = set()
for pr in pulls:
    if not isinstance(pr, dict):
        continue
    if not (labels_of(pr) & STOP_LABELS):
        continue
    task_id = task_id_from_pr(pr, task_ids)
    if not task_id or task_id in seen:
        continue
    seen.add(task_id)
    print(task_id)
PY
  then
    echo "::warning::Could not extract stopped autonomous task ids from open PRs."
    : > "$stopped_task_ids_file"
  fi

  if [ -s "$stopped_task_ids_file" ]; then
    echo "Stopped autonomous task ids: $(paste -sd, "$stopped_task_ids_file")"
  fi
}

record_active_task_id() {
  local task_id="$1"
  if [ -n "$task_id" ]; then
    echo "$task_id" >> "$active_task_ids_file"
  fi
}

task_is_stopped() {
  local task_id="$1"
  if [ -z "$task_id" ] || [ ! -s "$stopped_task_ids_file" ]; then
    return 1
  fi
  grep -Fxq -- "$task_id" "$stopped_task_ids_file"
}

skip_stopped_active_session() {
  local session_name="$1"
  local task_id="$2"
  if ! task_is_stopped "$task_id"; then
    return 1
  fi

  echo "Skipped ${session_name}; task ${task_id} is represented by a stopped autonomous PR awaiting review."
  skipped_stopped_sessions+=("${session_name##*/}:${task_id}")
  if [ "$active_sessions" -gt 0 ]; then
    active_sessions=$((active_sessions - 1))
  fi
  return 0
}

build_recovery_prompt() {
  local activities_file="$1"
  local session_id="$2"
  local session_state="$3"
  local task_id="$4"
  local mode="$5"
  local stale_reason="$6"
  local out="$7"
  local max_attempts="${8:-$max_stale_feedback_escalations}"

  python3 .github/scripts/jules_recovery_prompt.py \
    --activities "$activities_file" \
    --manifest agent_tasks.json \
    --task-id "$task_id" \
    --repo "${GITHUB_REPOSITORY:-}" \
    --session-id "$session_id" \
    --session-state "$session_state" \
    --mode "$mode" \
    --stale-reason "$stale_reason" \
    --max-continue-attempts "$max_attempts" \
    > "$out"
}

record_prompt_detail() {
  local session_id="$1"
  local prompt_json="$2"
  local max_attempts="${3:-$max_stale_feedback_escalations}"
  local wait_reason
  local prompt_action
  local prompt_task_id
  local continue_attempts

  wait_reason="$(jq -r '.wait_reason // "unknown_continue"' "$prompt_json")"
  prompt_action="$(jq -r '.prompt_action // "continue_safely"' "$prompt_json")"
  prompt_task_id="$(jq -r '.task_id // ""' "$prompt_json")"
  continue_attempts="$(jq -r '.continue_attempts // 0' "$prompt_json")"

  wait_reason_details+=("${session_id}:${wait_reason}")
  prompt_action_details+=("${session_id}:${prompt_action}")
  if [ -n "$prompt_task_id" ]; then
    prompt_task_id_details+=("${session_id}:${prompt_task_id}")
  fi
  continue_attempt_details+=("${session_id}:${continue_attempts}/${max_attempts}")
}

session_epoch_filter='
  def ts:
    ((.updateTime // .createTime // "1970-01-01T00:00:00Z")
      | sub("\\.[0-9]+Z$"; "Z")
      | fromdateiso8601? // 0);
  .sessions[]?
  | select(.sourceContext.source == $source)
  | select(ts >= $cutoff)
'

load_stopped_task_ids

for i in "${!key_labels[@]}"; do
  label="${key_labels[$i]}"
  key="${key_values[$i]}"
  sessions_file="${tmpdir}/sessions-${label}.json"

  if ! jules_get "$key" "sessions?pageSize=100" "$sessions_file"; then
    echo "::warning::Could not list Jules sessions with ${label} key."
    continue
  fi

  api_available=true

  while IFS= read -r session_json; do
    session_name="$(jq -r '.name' <<<"$session_json")"
    session_state="$(jq -r '.state // "STATE_UNSPECIFIED"' <<<"$session_json")"
    session_create_epoch="$(jq -r '((.createTime // .updateTime // "1970-01-01T00:00:00Z") | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601? // 0)' <<<"$session_json")"
    session_update_epoch="$(jq -r '((.updateTime // .createTime // "1970-01-01T00:00:00Z") | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601? // 0)' <<<"$session_json")"
    if [ "${session_create_epoch:-0}" -le 0 ]; then
      session_create_epoch="$session_update_epoch"
    fi

    if [ -z "$session_name" ] || [ "$session_name" = "null" ]; then
      continue
    fi

    if [ -n "${seen_sessions[$session_name]+x}" ]; then
      continue
    fi
    seen_sessions[$session_name]=1
    session_ids+=("${session_name##*/}")
    echo "Jules session ${session_name} state: ${session_state}"
    active_task_id=""

    case "$session_state" in
      QUEUED|PLANNING|IN_PROGRESS|AWAITING_PLAN_APPROVAL|AWAITING_USER_FEEDBACK)
        active_sessions=$((active_sessions + 1))
        active_task_id="$(extract_session_task_id "$key" "$session_name" "active")"
        if [ -z "$active_task_id" ]; then
          active_task_id="$(resolve_session_task_id_from_recent_map "$session_name")"
        fi
        if skip_stopped_active_session "$session_name" "$active_task_id"; then
          continue
        fi
        ;;
    esac

    if [ "$session_state" = "FAILED" ]; then
      failed_context="$(extract_failed_session_context "$key" "$session_name")"
      if [[ "$failed_context" == *$'\t'* ]]; then
        failed_task_id="${failed_context%%$'\t'*}"
        failed_kind="${failed_context#*$'\t'}"
      else
        failed_task_id="$failed_context"
        failed_kind=""
      fi
      if [ -z "$failed_task_id" ]; then
        failed_task_id="$(resolve_session_task_id_from_recent_map "$session_name")"
      fi
      if [ -n "$failed_task_id" ]; then
        echo "Detected failed Jules session ${session_name} for task ${failed_task_id}."
        if [ -n "$failed_kind" ]; then
          echo "Failed Jules session ${session_name} classified as ${failed_kind}."
        fi
      else
        echo "::warning::Detected failed Jules session ${session_name}, but no task id was found in activities."
      fi
      printf '%s\t%s\t%s\n' "${session_name##*/}" "$failed_task_id" "$failed_kind" >> "$failed_sessions_file"
      continue
    fi

    if [ "$session_state" = "AWAITING_PLAN_APPROVAL" ]; then
      record_active_task_id "$active_task_id"
      out="${tmpdir}/approve-${session_name//\//-}.json"
      if jules_post "$key" "${session_name}:approvePlan" '{}' "$out"; then
        echo "Approved Jules plan for ${session_name}."
        touched_sessions=$((touched_sessions + 1))
      else
        echo "::warning::Could not approve Jules plan for ${session_name}."
      fi
      continue
    fi

    if [ "$session_state" != "AWAITING_USER_FEEDBACK" ]; then
      if [ "$session_state" = "IN_PROGRESS" ]; then
        activities_file="${tmpdir}/activities-${session_name//\//-}.json"
        if ! jules_get "$key" "${session_name}/activities?pageSize=30" "$activities_file"; then
          echo "::warning::Could not list activities for ${session_name}; stale in-progress recovery skipped."
          record_active_task_id "$active_task_id"
          continue
        fi

        prompt_json="${tmpdir}/prompt-${session_name//\//-}.json"
        if ! build_recovery_prompt \
          "$activities_file" \
          "${session_name##*/}" \
          "$session_state" \
          "$active_task_id" \
          "stale" \
          "Jules session stayed IN_PROGRESS without recent activity" \
          "$prompt_json" \
          "$max_stale_in_progress_escalations"; then
          echo "::warning::Could not build stale in-progress recovery prompt for ${session_name}; skipping."
          record_active_task_id "$active_task_id"
          continue
        fi

        latest_agent_epoch="$(jq -r '.summary.latest_agent_epoch // 0' "$prompt_json")"
        last_user_epoch="$(jq -r '.summary.latest_user_epoch // 0' "$prompt_json")"
        latest_token_epoch="$(jq -r '.summary.latest_token_epoch // 0' "$prompt_json")"
        continue_token_count="$(jq -r '.summary.continue_token_count // 0' "$prompt_json")"
        continue_token_count="${continue_token_count:-0}"
        prompt_task_id="$(jq -r '.task_id // ""' "$prompt_json")"
        if [ -z "$active_task_id" ] && [ -n "$prompt_task_id" ]; then
          active_task_id="$prompt_task_id"
        fi
        if skip_stopped_active_session "$session_name" "$active_task_id"; then
          continue
        fi

        newest_activity_epoch="$session_update_epoch"
        for candidate in "$latest_agent_epoch" "$last_user_epoch" "$latest_token_epoch"; do
          if [ "${candidate:-0}" -gt "$newest_activity_epoch" ]; then
            newest_activity_epoch="$candidate"
          fi
        done
        idle_age="$((now_epoch - newest_activity_epoch))"
        session_age="$((now_epoch - session_create_epoch))"

        if [ "${latest_agent_epoch:-0}" -eq 0 ]; then
          if [ -z "$active_task_id" ]; then
            active_task_id="$(resolve_session_task_id_from_recent_map "$session_name")"
          fi
          if [ -z "$active_task_id" ]; then
            echo "::warning::No task id found for no-agent IN_PROGRESS session ${session_name}; recovery prompt will carry task_id=unknown."
          fi

          if [ "$continue_token_count" -ge "$max_stale_in_progress_escalations" ]; then
            echo "Autonomous in-progress recovery limit reached for ${session_name} without agent activity; deleting stale session and blocking task."
            record_prompt_detail "${session_name##*/}" "$prompt_json" "$max_stale_in_progress_escalations"
            if [ -n "$active_task_id" ]; then
              printf '%s\t%s\t%s\n' "${session_name##*/}" "$active_task_id" "repeated_stale_in_progress" >> "$failed_sessions_file"
            fi
            out="${tmpdir}/delete-${session_name//\//-}.json"
            if jules_delete "$key" "$session_name" "$out"; then
              active_sessions=$((active_sessions - 1))
              touched_sessions=$((touched_sessions + 1))
              echo "Deleted no-agent stale in-progress Jules session ${session_name} after ${continue_token_count} autonomous recovery messages."
            else
              echo "::warning::Could not delete no-agent stale in-progress Jules session ${session_name}."
              record_active_task_id "$active_task_id"
            fi
            continue
          fi

          if [ "${latest_token_epoch:-0}" -gt 0 ]; then
            token_age="$((now_epoch - latest_token_epoch))"
            if [ "$token_age" -lt "$no_agent_stale_in_progress_seconds" ]; then
              echo "Skipped ${session_name}; no agent activity yet, but in-progress recovery prompt is still fresh (${token_age}s old, no-agent stale threshold ${no_agent_stale_in_progress_seconds}s, continue tokens ${continue_token_count}/${max_stale_in_progress_escalations})."
              record_prompt_detail "${session_name##*/}" "$prompt_json" "$max_stale_in_progress_escalations"
              stale_in_progress_sessions+=("${session_name##*/}:no-agent-token:${token_age}s/${no_agent_stale_in_progress_seconds}s:${continue_token_count}/${max_stale_in_progress_escalations}")
              record_active_task_id "$active_task_id"
              continue
            fi
            echo "Previous no-agent in-progress recovery for ${session_name} is stale after $((token_age / 60)) minute(s); sending escalation ${continue_token_count}/${max_stale_in_progress_escalations}."
            stale_in_progress_sessions+=("${session_name##*/}:no-agent-stale-token:${continue_token_count}/${max_stale_in_progress_escalations}")
            if ! build_recovery_prompt \
              "$activities_file" \
              "${session_name##*/}" \
              "$session_state" \
              "$active_task_id" \
              "stale" \
              "previous no-agent in-progress autonomous recovery token is stale" \
              "$prompt_json" \
              "$max_stale_in_progress_escalations"; then
              echo "::warning::Could not build no-agent in-progress escalation prompt for ${session_name}; skipping."
              record_active_task_id "$active_task_id"
              continue
            fi
          elif [ "$no_agent_in_progress_seconds" -gt 0 ] && [ "$session_age" -ge "$no_agent_in_progress_seconds" ]; then
            echo "Detected long-running IN_PROGRESS Jules session ${session_name} without agent activity after $((session_age / 60)) minute(s); sending dynamic recovery prompt."
            stale_in_progress_sessions+=("${session_name##*/}:no-agent-long-running:${continue_token_count}/${max_stale_in_progress_escalations}")
            if ! build_recovery_prompt \
              "$activities_file" \
              "${session_name##*/}" \
              "$session_state" \
              "$active_task_id" \
              "stale" \
              "Jules session stayed IN_PROGRESS for $((session_age / 60)) minute(s) without any agent activity or PR" \
              "$prompt_json" \
              "$max_stale_in_progress_escalations"; then
              echo "::warning::Could not build long-running no-agent in-progress recovery prompt for ${session_name}; skipping."
              record_active_task_id "$active_task_id"
              continue
            fi
          else
            echo "Skipped ${session_name}; no agent activity found for stale in-progress recovery (session age ${session_age}s, no-agent threshold ${no_agent_in_progress_seconds}s)."
            record_active_task_id "$active_task_id"
            continue
          fi

          record_active_task_id "$active_task_id"
          record_prompt_detail "${session_name##*/}" "$prompt_json" "$max_stale_in_progress_escalations"
          prompt="$(jq -r '.prompt // ""' "$prompt_json")"
          body="$(jq -n --arg prompt "$prompt" '{prompt: $prompt}')"
          out="${tmpdir}/send-${session_name//\//-}.json"
          if jules_post "$key" "${session_name}:sendMessage" "$body" "$out"; then
            echo "Sent dynamic no-agent in-progress recovery message to ${session_name}."
            touched_sessions=$((touched_sessions + 1))
          else
            echo "::warning::Could not send no-agent in-progress recovery message to ${session_name}."
          fi
          continue
        fi

        if [ "$continue_token_count" -ge "$max_stale_in_progress_escalations" ]; then
          echo "Autonomous in-progress recovery limit reached for ${session_name}; deleting stale session and blocking task."
          record_prompt_detail "${session_name##*/}" "$prompt_json" "$max_stale_in_progress_escalations"
          if [ -n "$active_task_id" ]; then
            printf '%s\t%s\t%s\n' "${session_name##*/}" "$active_task_id" "repeated_stale_in_progress" >> "$failed_sessions_file"
          fi
          out="${tmpdir}/delete-${session_name//\//-}.json"
          if jules_delete "$key" "$session_name" "$out"; then
            active_sessions=$((active_sessions - 1))
            touched_sessions=$((touched_sessions + 1))
            echo "Deleted stale in-progress Jules session ${session_name} after ${continue_token_count} autonomous recovery messages."
          else
            echo "::warning::Could not delete stale in-progress Jules session ${session_name}."
            record_active_task_id "$active_task_id"
          fi
          continue
        fi

        if [ "${latest_token_epoch:-0}" -ge "${latest_agent_epoch:-0}" ]; then
          token_age="$((now_epoch - latest_token_epoch))"
          if [ "$token_age" -lt "$stale_in_progress_seconds" ]; then
            echo "Skipped ${session_name}; in-progress recovery prompt is still fresh (${token_age}s old, stale threshold ${stale_in_progress_seconds}s, continue tokens ${continue_token_count}/${max_stale_in_progress_escalations})."
            record_prompt_detail "${session_name##*/}" "$prompt_json" "$max_stale_in_progress_escalations"
            stale_in_progress_sessions+=("${session_name##*/}:${token_age}s/${stale_in_progress_seconds}s:${continue_token_count}/${max_stale_in_progress_escalations}")
            record_active_task_id "$active_task_id"
            continue
          fi
          echo "Previous in-progress recovery for ${session_name} is stale after $((token_age / 60)) minute(s); sending escalation ${continue_token_count}/${max_stale_in_progress_escalations}."
          stale_in_progress_sessions+=("${session_name##*/}:stale-token:${continue_token_count}/${max_stale_in_progress_escalations}")
          if ! build_recovery_prompt \
            "$activities_file" \
            "${session_name##*/}" \
            "$session_state" \
            "$active_task_id" \
            "stale" \
            "previous in-progress autonomous recovery token is stale" \
            "$prompt_json" \
            "$max_stale_in_progress_escalations"; then
            echo "::warning::Could not build stale in-progress escalation prompt for ${session_name}; skipping."
            record_active_task_id "$active_task_id"
            continue
          fi
        elif [ "$max_in_progress_session_seconds" -gt 0 ] && [ "$session_age" -ge "$max_in_progress_session_seconds" ]; then
          echo "Detected long-running IN_PROGRESS Jules session ${session_name} after $((session_age / 60)) minute(s); sending dynamic recovery prompt."
          stale_in_progress_sessions+=("${session_name##*/}:long-running:${continue_token_count}/${max_stale_in_progress_escalations}")
          if ! build_recovery_prompt \
            "$activities_file" \
            "${session_name##*/}" \
            "$session_state" \
            "$active_task_id" \
            "stale" \
            "Jules session stayed IN_PROGRESS for $((session_age / 60)) minute(s) without opening a PR" \
            "$prompt_json" \
            "$max_stale_in_progress_escalations"; then
            echo "::warning::Could not build long-running in-progress recovery prompt for ${session_name}; skipping."
            record_active_task_id "$active_task_id"
            continue
          fi
        elif [ "$idle_age" -lt "$stale_in_progress_seconds" ]; then
          echo "Skipped ${session_name}; IN_PROGRESS activity is still fresh (${idle_age}s old, stale threshold ${stale_in_progress_seconds}s)."
          record_active_task_id "$active_task_id"
          continue
        else
          echo "Detected stale IN_PROGRESS Jules session ${session_name} after $((idle_age / 60)) minute(s); sending dynamic recovery prompt."
          stale_in_progress_sessions+=("${session_name##*/}:stale:${continue_token_count}/${max_stale_in_progress_escalations}")
        fi

        record_active_task_id "$active_task_id"
        record_prompt_detail "${session_name##*/}" "$prompt_json" "$max_stale_in_progress_escalations"
        prompt="$(jq -r '.prompt // ""' "$prompt_json")"
        body="$(jq -n --arg prompt "$prompt" '{prompt: $prompt}')"
        out="${tmpdir}/send-${session_name//\//-}.json"
        if jules_post "$key" "${session_name}:sendMessage" "$body" "$out"; then
          echo "Sent dynamic stale in-progress recovery message to ${session_name}."
          touched_sessions=$((touched_sessions + 1))
        else
          echo "::warning::Could not send stale in-progress recovery message to ${session_name}."
        fi
        continue
      fi
      record_active_task_id "$active_task_id"
      continue
    fi

    activities_file="${tmpdir}/activities-${session_name//\//-}.json"
    if ! jules_get "$key" "${session_name}/activities?pageSize=30" "$activities_file"; then
      echo "::warning::Could not list activities for ${session_name}; skipping auto-continue to avoid duplicate prompts."
      continue
    fi

    prompt_json="${tmpdir}/prompt-${session_name//\//-}.json"
    if ! build_recovery_prompt "$activities_file" "${session_name##*/}" "$session_state" "$active_task_id" "continue" "" "$prompt_json"; then
      echo "::warning::Could not build recovery prompt for ${session_name}; skipping auto-continue."
      record_active_task_id "$active_task_id"
      continue
    fi

    latest_agent_epoch="$(jq -r '.summary.latest_agent_epoch // 0' "$prompt_json")"
    last_user_epoch="$(jq -r '.summary.latest_user_epoch // 0' "$prompt_json")"
    latest_token_epoch="$(jq -r '.summary.latest_token_epoch // 0' "$prompt_json")"
    wait_kind="$(jq -r '.summary.wait_kind // "continue"' "$prompt_json")"
    continue_token_count="$(jq -r '.summary.continue_token_count // 0' "$prompt_json")"
    continue_token_count="${continue_token_count:-0}"
    prompt_task_id="$(jq -r '.task_id // ""' "$prompt_json")"
    if [ -z "$active_task_id" ] && [ -n "$prompt_task_id" ]; then
      active_task_id="$prompt_task_id"
    fi
    if skip_stopped_active_session "$session_name" "$active_task_id"; then
      continue
    fi
    prompt="$(jq -r '.prompt // ""' "$prompt_json")"

    if [ "${latest_agent_epoch:-0}" -eq 0 ]; then
      echo "Skipped ${session_name}; no agent activity found to answer."
      record_active_task_id "$active_task_id"
      continue
    fi

    if [ "${latest_token_epoch:-0}" -ge "${latest_agent_epoch:-0}" ]; then
      token_age="$((now_epoch - latest_token_epoch))"
      if [ "${last_user_epoch:-0}" -gt "${latest_token_epoch:-0}" ]; then
        echo "Skipped ${session_name}; a newer user message already answers the latest wait state."
        record_active_task_id "$active_task_id"
        continue
      fi
      if [ "$continue_token_count" -ge "$max_stale_feedback_escalations" ]; then
        echo "Autonomous continue limit reached for ${session_name}; deleting stale session and blocking task."
        record_prompt_detail "${session_name##*/}" "$prompt_json"
        if [ -n "$active_task_id" ]; then
          printf '%s\t%s\t%s\n' "${session_name##*/}" "$active_task_id" "repeated_stale_feedback" >> "$failed_sessions_file"
        fi
        out="${tmpdir}/delete-${session_name//\//-}.json"
        if jules_delete "$key" "$session_name" "$out"; then
          active_sessions=$((active_sessions - 1))
          touched_sessions=$((touched_sessions + 1))
          echo "Deleted stale Jules session ${session_name} after ${continue_token_count} autonomous continue messages."
        else
          echo "::warning::Could not delete stale Jules session ${session_name}."
          record_active_task_id "$active_task_id"
        fi
        continue
      fi
      if [ "$token_age" -lt "$stale_feedback_seconds" ]; then
        echo "Skipped ${session_name}; autonomous continue already answers the latest wait state (${token_age}s old, stale threshold ${stale_feedback_seconds}s, continue tokens ${continue_token_count}/${max_stale_feedback_escalations})."
        record_prompt_detail "${session_name##*/}" "$prompt_json"
        stale_waiting_sessions+=("${session_name##*/}:${token_age}s/${stale_feedback_seconds}s:${continue_token_count}/${max_stale_feedback_escalations}")
        record_active_task_id "$active_task_id"
        continue
      fi
      echo "Previous autonomous continue for ${session_name} is stale after $((token_age / 60)) minute(s); sending escalation ${continue_token_count}/${max_stale_feedback_escalations}."
      stale_waiting_sessions+=("${session_name##*/}:stale:${continue_token_count}/${max_stale_feedback_escalations}")
      if ! build_recovery_prompt "$activities_file" "${session_name##*/}" "$session_state" "$active_task_id" "stale" "latest autonomous continue token is stale" "$prompt_json"; then
        echo "::warning::Could not build stale recovery prompt for ${session_name}; skipping auto-continue."
        record_active_task_id "$active_task_id"
        continue
      fi
      prompt="$(jq -r '.prompt // ""' "$prompt_json")"
    elif [ "${last_user_epoch:-0}" -ge "${latest_agent_epoch:-0}" ]; then
      echo "Skipped ${session_name}; a user message already answers the latest wait state."
      record_active_task_id "$active_task_id"
      continue
    fi

    if [ "$continue_token_count" -ge "$max_stale_feedback_escalations" ]; then
      echo "Autonomous continue limit reached for ${session_name}; deleting repeated feedback session and blocking task."
      record_prompt_detail "${session_name##*/}" "$prompt_json"
      if [ -n "$active_task_id" ]; then
        printf '%s\t%s\t%s\n' "${session_name##*/}" "$active_task_id" "repeated_stale_feedback" >> "$failed_sessions_file"
      fi
      out="${tmpdir}/delete-${session_name//\//-}.json"
      if jules_delete "$key" "$session_name" "$out"; then
        active_sessions=$((active_sessions - 1))
        touched_sessions=$((touched_sessions + 1))
        echo "Deleted repeated-feedback Jules session ${session_name} after ${continue_token_count} autonomous continue messages."
      else
        echo "::warning::Could not delete repeated-feedback Jules session ${session_name}."
        record_active_task_id "$active_task_id"
      fi
      continue
    fi

    if [ "$((now_epoch - last_user_epoch))" -lt "$reply_cooldown_seconds" ]; then
      echo "Skipped ${session_name}; a user message was already sent recently."
      record_active_task_id "$active_task_id"
      continue
    fi

    record_active_task_id "$active_task_id"
    record_prompt_detail "${session_name##*/}" "$prompt_json"
    body="$(jq -n --arg prompt "$prompt" '{prompt: $prompt}')"
    out="${tmpdir}/send-${session_name//\//-}.json"
    if jules_post "$key" "${session_name}:sendMessage" "$body" "$out"; then
      echo "Sent dynamic autonomous ${wait_kind:-continue} recovery message to ${session_name}."
      touched_sessions=$((touched_sessions + 1))
    else
      echo "::warning::Could not send autonomous continue message to ${session_name}."
    fi
  done < <(jq -c --arg source "$SOURCE" --argjson cutoff "$cutoff_epoch" "$session_epoch_filter" "$sessions_file")
done

echo "Failed Jules recovery decision:"
python3 .github/scripts/summarize-jules-failures.py decide \
  --manifest agent_tasks.json \
  --failed-sessions "$failed_sessions_file" \
  --active-task-ids "$active_task_ids_file" \
  --github-output "${GITHUB_OUTPUT:-/dev/null}" \
  --json

echo "Active recent Jules sessions for ${SOURCE}: ${active_sessions}"
echo "Touched Jules sessions: ${touched_sessions}"
echo "Stale waiting Jules sessions: ${#stale_waiting_sessions[@]}"
echo "Stale in-progress Jules sessions: ${#stale_in_progress_sessions[@]}"
echo "Skipped stopped Jules sessions: ${#skipped_stopped_sessions[@]}"
session_ids_csv="$(IFS=,; echo "${session_ids[*]}")"
stale_waiting_csv="$(IFS=,; echo "${stale_waiting_sessions[*]}")"
stale_in_progress_csv="$(IFS=,; echo "${stale_in_progress_sessions[*]}")"
skipped_stopped_csv="$(IFS=,; echo "${skipped_stopped_sessions[*]}")"
wait_reason_csv="$(IFS=,; echo "${wait_reason_details[*]}")"
prompt_action_csv="$(IFS=,; echo "${prompt_action_details[*]}")"
prompt_task_id_csv="$(IFS=,; echo "${prompt_task_id_details[*]}")"
continue_attempt_csv="$(IFS=,; echo "${continue_attempt_details[*]}")"

{
  echo "active_sessions=${active_sessions}"
  echo "touched_sessions=${touched_sessions}"
  echo "api_available=${api_available}"
  echo "session_ids=${session_ids_csv}"
  echo "stale_waiting_sessions=${stale_waiting_csv}"
  echo "stale_waiting_count=${#stale_waiting_sessions[@]}"
  echo "stale_in_progress_sessions=${stale_in_progress_csv}"
  echo "stale_in_progress_count=${#stale_in_progress_sessions[@]}"
  echo "skipped_stopped_sessions=${skipped_stopped_csv}"
  echo "skipped_stopped_count=${#skipped_stopped_sessions[@]}"
  echo "wait_reason=${wait_reason_csv}"
  echo "prompt_action=${prompt_action_csv}"
  echo "prompt_task_id=${prompt_task_id_csv}"
  echo "continue_attempts=${continue_attempt_csv}"
} >> "${GITHUB_OUTPUT:-/dev/null}"
