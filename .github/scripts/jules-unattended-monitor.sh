#!/usr/bin/env bash
set -euo pipefail

API_BASE="${JULES_API_BASE:-https://jules.googleapis.com/v1alpha}"
SOURCE="${JULES_SOURCE:-sources/github/${GITHUB_REPOSITORY:-Omnividente/notion-abuz_ai}}"
LOOKBACK_HOURS="${LOOKBACK_HOURS:-24}"
MIN_USER_REPLY_INTERVAL_MINUTES="${MIN_USER_REPLY_INTERVAL_MINUTES:-2}"
STALE_AWAITING_FEEDBACK_MINUTES="${STALE_AWAITING_FEEDBACK_MINUTES:-30}"
MAX_STALE_AWAITING_FEEDBACK_ESCALATIONS="${MAX_STALE_AWAITING_FEEDBACK_ESCALATIONS:-3}"

read -r -d '' CONTINUE_PROMPT <<'EOF' || true
AUTONOMOUS_CONTINUE_TOKEN

Продолжай без ожидания ответа пользователя.

Пиши все пользовательские сообщения, объяснения плана, PR title/body и финальные summary на русском. Оставляй file paths, task ids, команды, API names, code identifiers и quoted errors на языке оригинала.

Если последнее сообщение просит выбрать между безопасными вариантами, выбери сам самый маленький обратимый low/medium-risk шаг и продолжай.
Если последнее сообщение спрашивает, нужно ли запустить локальный сервер, локальные тесты, offline reproduction или изучить логи/artifacts, ответ: да, сделай это сам, если действие безопасно, недеструктивно, остается внутри scope/allowed_paths и не требует секретов.
Если для воспроизведения нужны live secrets, реальные credentials или production-доступ, не спрашивай пользователя в этой сессии. Вместо этого зафиксируй точный blocker в agent_tasks.json и открой manifest-only PR.
Оставайся внутри scope выбранной задачи и allowed_paths.
Не дроби работу на отдельные micro-PR: если связанный тест, лог, doc или artifact-capture проверяет тот же failure mode и остается в allowed_paths, заверши это в текущем PR. Не создавай новую Jules-сессию, PR или follow-up задачу ради одного маленького edge-case теста без live smoke, transcript, CI или offline reproduction evidence.
Перед открытием PR синхронизируйся с последним master/default branch. Если master изменился во время сессии, rebase/merge latest master перед PR. При конфликте в agent_tasks.json сохрани актуальную очередь из master и наложи только статус выбранной задачи и конкретные follow-up задачи этой сессии.
Не открывай PR, если он заведомо dirty/conflicting с master.
Запусти нужную валидацию.
Когда задача готова, открой один PR с русским title/body и label `jules`.
В PR body обязательно добавь ровно один блок:
<!-- AUTONOMOUS_TASK_EVIDENCE
task_id: <selected task id>
status: done|blocked
acceptance:
- <acceptance criterion> -> <changed file/test/artifact evidence>
evidence_files:
- <repo-relative changed file path>
checks:
- <validation command that was run>
micro_pr_justification: <why this is one complete task theme, not a micro PR>
-->
Если status: blocked, добавь строку `blocked_reason: <concrete blocker>`.
Все `evidence_files` должны быть файлами, измененными этим PR.
Не задавай новый вопрос-подтверждение, если нет блокера из-за missing permissions, missing secrets, high/critical risk или неизбежного destructive action.

В следующем сообщении обязательно кратко укажи:
- Этап плана
- Что сделано
- Что дальше
- Зачем
- Почему так
- Проверки/риски
EOF

read -r -d '' FINALIZE_PROMPT <<'EOF' || true
AUTONOMOUS_CONTINUE_TOKEN

Дополнительное ревью не требуется.

Финализируй эту задачу сейчас:
- Отметь выбранную задачу как завершенную в agent_tasks.json.
- Не добавляй новую follow-up задачу ради одного маленького теста/лога/doc-строки, если нет конкретного live smoke, transcript, CI или offline reproduction evidence.
- Синхронизируй ветку с последним master/default branch перед PR.
- Если master изменился во время сессии, rebase/merge latest master. При конфликте в agent_tasks.json сохрани актуальную очередь из master и наложи только статус выбранной задачи и конкретные follow-up задачи этой сессии.
- Открой один pull request для готовых изменений.
- Дай PR русский title/body.
- Поставь label `jules`.
- В PR body добавь ровно один блок `AUTONOMOUS_TASK_EVIDENCE` с `task_id`, `status`, mapping всех acceptance criteria к evidence, измененными `evidence_files`, выполненными `checks` и `micro_pr_justification`. Для blocked добавь `blocked_reason`.
- Не задавай новый вопрос-подтверждение.

В финальном сообщении на русском укажи:
- Этап плана
- Что сделано
- Что дальше
- Зачем
- Почему так
- Проверки/риски
EOF

read -r -d '' STALE_FEEDBACK_PROMPT <<'EOF' || true
AUTONOMOUS_CONTINUE_TOKEN

Предыдущий autonomous continue уже был отправлен, но сессия всё ещё ждёт пользователя.

Не жди дополнительного подтверждения. Выбери один безопасный исход:
- если задачу можно завершить внутри scope/allowed_paths, синхронизируйся с master, запусти нужную валидацию и открой один PR с label `jules` и корректным AUTONOMOUS_TASK_EVIDENCE;
- если продолжение требует missing secrets, production-доступ, high/critical risk или destructive action, отметь задачу `blocked` в agent_tasks.json, добавь concrete blocked_reason и открой manifest-only PR.

Не задавай новый вопрос-подтверждение и не оставляй сессию в ожидании пользователя.

В финальном сообщении на русском укажи:
- Этап плана
- Что сделано
- Что дальше
- Зачем
- Почему так
- Проверки/риски
EOF

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
: > "$failed_sessions_file"
: > "$active_task_ids_file"

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
max_stale_escalations="$MAX_STALE_AWAITING_FEEDBACK_ESCALATIONS"

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

record_active_task_id() {
  local task_id="$1"
  if [ -n "$task_id" ]; then
    echo "$task_id" >> "$active_task_ids_file"
  fi
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
    active_task_id=""

    case "$session_state" in
      QUEUED|PLANNING|IN_PROGRESS|AWAITING_PLAN_APPROVAL|AWAITING_USER_FEEDBACK)
        active_sessions=$((active_sessions + 1))
        active_task_id="$(extract_session_task_id "$key" "$session_name" "active")"
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
      record_active_task_id "$active_task_id"
      continue
    fi

    activities_file="${tmpdir}/activities-${session_name//\//-}.json"
    if ! jules_get "$key" "${session_name}/activities?pageSize=30" "$activities_file"; then
      echo "::warning::Could not list activities for ${session_name}; skipping auto-continue to avoid duplicate prompts."
      continue
    else
      activity_summary="$(python3 .github/scripts/summarize-jules-activities.py "$activities_file")"
    fi

    IFS=$'\t' read -r latest_agent_epoch last_user_epoch latest_token_epoch wait_kind continue_token_count <<<"$activity_summary"
    continue_token_count="${continue_token_count:-0}"

    if [ "${latest_agent_epoch:-0}" -eq 0 ]; then
      echo "Skipped ${session_name}; no agent activity found to answer."
      record_active_task_id "$active_task_id"
      continue
    fi

    prompt="$CONTINUE_PROMPT"
    if [ "${wait_kind:-continue}" = "finalize" ]; then
      prompt="$FINALIZE_PROMPT"
    fi

    if [ "${latest_token_epoch:-0}" -ge "${latest_agent_epoch:-0}" ]; then
      token_age="$((now_epoch - latest_token_epoch))"
      if [ "$token_age" -lt "$stale_feedback_seconds" ]; then
        echo "Skipped ${session_name}; autonomous continue already answers the latest wait state."
        record_active_task_id "$active_task_id"
        continue
      fi
      if [ "${last_user_epoch:-0}" -gt "${latest_token_epoch:-0}" ]; then
        echo "Skipped ${session_name}; a newer user message already answers the latest wait state."
        record_active_task_id "$active_task_id"
        continue
      fi
      if [ "$continue_token_count" -ge "$max_stale_escalations" ]; then
        echo "Autonomous continue limit reached for ${session_name}; deleting stale session and blocking task."
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
      echo "Previous autonomous continue for ${session_name} is stale after $((token_age / 60)) minute(s); sending escalation."
      prompt="$STALE_FEEDBACK_PROMPT"
    elif [ "${last_user_epoch:-0}" -ge "${latest_agent_epoch:-0}" ]; then
      echo "Skipped ${session_name}; a user message already answers the latest wait state."
      record_active_task_id "$active_task_id"
      continue
    fi

    if [ "$continue_token_count" -ge "$max_stale_escalations" ]; then
      echo "Autonomous continue limit reached for ${session_name}; deleting repeated feedback session and blocking task."
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
    body="$(jq -n --arg prompt "$prompt" '{prompt: $prompt}')"
    out="${tmpdir}/send-${session_name//\//-}.json"
    if jules_post "$key" "${session_name}:sendMessage" "$body" "$out"; then
      echo "Sent autonomous ${wait_kind:-continue} message to ${session_name}."
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
session_ids_csv="$(IFS=,; echo "${session_ids[*]}")"

{
  echo "active_sessions=${active_sessions}"
  echo "touched_sessions=${touched_sessions}"
  echo "api_available=${api_available}"
  echo "session_ids=${session_ids_csv}"
} >> "${GITHUB_OUTPUT:-/dev/null}"
