#!/usr/bin/env bash
set -euo pipefail

API_BASE="${JULES_API_BASE:-https://jules.googleapis.com/v1alpha}"
SOURCE="${JULES_SOURCE:-sources/github/${GITHUB_REPOSITORY:-Omnividente/notion-abuz_ai}}"
LOOKBACK_HOURS="${LOOKBACK_HOURS:-24}"
MIN_USER_REPLY_INTERVAL_MINUTES="${MIN_USER_REPLY_INTERVAL_MINUTES:-2}"

read -r -d '' CONTINUE_PROMPT <<'EOF' || true
AUTONOMOUS_CONTINUE_TOKEN: notion-abuz_ai-unattended-v1

Continue without waiting for human input.

Decision policy:
- If your current message asks whether to proceed, treat this message as explicit approval to proceed.
- If your current message asks the user to choose between options, choose the smallest reversible low/medium-risk option yourself.
- If your current message says the work is ready for review or asks whether to wrap up, open/finalize the PR now.
- Do not ask the user to choose between safe implementation approaches.
- For low/medium-risk tasks, choose the smallest reversible change that satisfies the selected task acceptance criteria.
- If a possible change belongs to a separate task, exclude it from the current PR, add or keep a follow-up task, and finish the current task.
- If tests fail because of the current change, fix them inside the selected task scope.
- If validation exposes unrelated failures, document them and add a bounded follow-up task instead of stopping.
- Keep one task id per PR, stay inside allowed_paths, run the required validation, open one PR, and label it jules.
- If your work is ready for review or ready to finalize, open/finalize the PR now instead of asking for confirmation.
- Do not ask more questions unless blocked by missing permissions, missing secrets, high/critical risk, or an unavoidable destructive action.
- Do not repeat the question that caused this wait state.
EOF

if [ -z "${JULES_API_KEY:-}" ] && [ -z "${JULES_API_KEY_BACKUP:-}" ]; then
  echo "::warning::No Jules API keys configured; unattended monitor cannot inspect sessions."
  {
    echo "active_sessions=0"
    echo "touched_sessions=0"
    echo "api_available=false"
    echo "session_ids="
  } >> "${GITHUB_OUTPUT:-/dev/null}"
  exit 0
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

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

active_sessions=0
touched_sessions=0
api_available=false
session_ids=()
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

session_epoch_filter='
  def ts:
    ((.updateTime // .createTime // "1970-01-01T00:00:00Z")
      | sub("\\.[0-9]+Z$"; "Z")
      | fromdateiso8601? // 0);
  .sessions[]?
  | select(.sourceContext.source == $source)
  | select(ts >= $cutoff)
'

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

    if [ -z "$session_name" ] || [ "$session_name" = "null" ]; then
      continue
    fi

    if [ -n "${seen_sessions[$session_name]+x}" ]; then
      continue
    fi
    seen_sessions[$session_name]=1
    session_ids+=("${session_name##*/}")
    echo "Jules session ${session_name} state: ${session_state}"

    case "$session_state" in
      QUEUED|PLANNING|IN_PROGRESS|AWAITING_PLAN_APPROVAL|AWAITING_USER_FEEDBACK)
        active_sessions=$((active_sessions + 1))
        ;;
    esac

    if [ "$session_state" = "AWAITING_PLAN_APPROVAL" ]; then
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
      continue
    fi

    activities_file="${tmpdir}/activities-${session_name//\//-}.json"
    if ! jules_get "$key" "${session_name}/activities?pageSize=30" "$activities_file"; then
      echo "::warning::Could not list activities for ${session_name}; sending continue message anyway."
      last_user_epoch=0
    else
      last_user_epoch="$(
        jq -r '
          [
            .activities[]?
            | select(.originator == "user")
            | ((.createTime // "1970-01-01T00:00:00Z")
              | sub("\\.[0-9]+Z$"; "Z")
              | fromdateiso8601? // 0)
          ]
          | max // 0
        ' "$activities_file"
      )"
    fi

    if [ "$((now_epoch - last_user_epoch))" -lt "$reply_cooldown_seconds" ]; then
      echo "Skipped ${session_name}; a user message was already sent recently."
      continue
    fi

    body="$(jq -n --arg prompt "$CONTINUE_PROMPT" '{prompt: $prompt}')"
    out="${tmpdir}/send-${session_name//\//-}.json"
    if jules_post "$key" "${session_name}:sendMessage" "$body" "$out"; then
      echo "Sent autonomous continue message to ${session_name}."
      touched_sessions=$((touched_sessions + 1))
    else
      echo "::warning::Could not send autonomous continue message to ${session_name}."
    fi
  done < <(jq -c --arg source "$SOURCE" --argjson cutoff "$cutoff_epoch" "$session_epoch_filter" "$sessions_file")
done

echo "Active recent Jules sessions for ${SOURCE}: ${active_sessions}"
echo "Touched Jules sessions: ${touched_sessions}"
session_ids_csv="$(IFS=,; echo "${session_ids[*]}")"

{
  echo "active_sessions=${active_sessions}"
  echo "touched_sessions=${touched_sessions}"
  echo "api_available=${api_available}"
  echo "session_ids=${session_ids_csv}"
} >> "${GITHUB_OUTPUT:-/dev/null}"
