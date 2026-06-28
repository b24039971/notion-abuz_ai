#!/usr/bin/env bash
set -euo pipefail

: "${LIVE_NOTION_ACCOUNTS_B64:?LIVE_NOTION_ACCOUNTS_B64 is required}"

SMOKE_MODEL="${SMOKE_MODEL:-opus-4.8}"
SMOKE_PORT="${SMOKE_PORT:-31081}"
SMOKE_API_KEY="${SMOKE_API_KEY:-local-live-smoke-key}"

python3 - <<'PY'
import base64
import json
import os
import pathlib
import zipfile

raw = os.environ["LIVE_NOTION_ACCOUNTS_B64"]
data = base64.b64decode("".join(raw.split()))
accounts_dir = pathlib.Path("accounts")
accounts_dir.mkdir(exist_ok=True)
payload = pathlib.Path("/tmp/live-notion-accounts.payload")
payload.write_bytes(data)

written = []
if zipfile.is_zipfile(payload):
    with zipfile.ZipFile(payload) as archive:
        for member in archive.namelist():
            name = pathlib.PurePosixPath(member).name
            if not name.endswith(".json") or name.startswith("."):
                continue
            target = accounts_dir / name
            target.write_bytes(archive.read(member))
            written.append(target)
else:
    obj = json.loads(data.decode("utf-8"))
    if isinstance(obj, list):
        for index, account in enumerate(obj, start=1):
            target = accounts_dir / f"account-{index}.json"
            target.write_text(json.dumps(account), encoding="utf-8")
            written.append(target)
    elif isinstance(obj, dict) and "token_v2" not in obj and all(isinstance(v, dict) for v in obj.values()):
        for index, account in enumerate(obj.values(), start=1):
            target = accounts_dir / f"account-{index}.json"
            target.write_text(json.dumps(account), encoding="utf-8")
            written.append(target)
    elif isinstance(obj, dict):
        target = accounts_dir / "account-1.json"
        target.write_text(json.dumps(obj), encoding="utf-8")
        written.append(target)
    else:
        raise SystemExit("LIVE_NOTION_ACCOUNTS_B64 decoded JSON is not an object or array")

if not written:
    raise SystemExit("LIVE_NOTION_ACCOUNTS_B64 did not contain any account JSON files")

print(f"Decoded {len(written)} live account file(s).")
PY

cat > config.yaml <<'YAML'
server:
  port: "31081"
  accounts_dir: "accounts"
  token_file: "token.txt"
  api_key: "local-live-smoke-key"
  admin_password: "local-live-smoke-admin"
  log_file: ""
  debug_logging: false
  api_log_input: false
  api_log_output: false
  notion_log_request: false
  notion_log_response: false
  dump_api_input: false
proxy:
  default_model: "opus-4.8"
  disable_notion_prompt: true
  enable_web_search: false
  enable_workspace_search: false
  ask_mode_default: false
timeouts:
  inference_timeout: 180
  research_timeout: 180
  api_timeout: 30
  tls_dial_timeout: 30
refresh:
  interval_minutes: 60
  quota_recheck_minutes: 30
  concurrency: 1
  live_check_seconds: 5
YAML

if [ ! -x ./notion-manager ]; then
  go build -ldflags="-s -w" -o notion-manager ./cmd/notion-manager
fi

cleanup() {
  if [ -f /tmp/notion-manager.pid ]; then
    kill "$(cat /tmp/notion-manager.pid)" 2>/dev/null || true
  fi
}
trap cleanup EXIT

PORT="$SMOKE_PORT" \
API_KEY="$SMOKE_API_KEY" \
DEBUG_LOGGING=false \
API_LOG_INPUT=false \
API_LOG_OUTPUT=false \
NOTION_LOG_REQUEST=false \
NOTION_LOG_RESPONSE=false \
./notion-manager > /tmp/notion-manager.stdout.log 2> /tmp/notion-manager.stderr.log &
echo "$!" > /tmp/notion-manager.pid

for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${SMOKE_PORT}/health" > /tmp/health.json; then
    cat /tmp/health.json | jq '{status, accounts, available}'
    jq -e '.accounts > 0' /tmp/health.json >/dev/null
    break
  fi
  sleep 2
done

if ! curl -fsS "http://127.0.0.1:${SMOKE_PORT}/health" > /tmp/health.json; then
  echo "::error::local notion-manager did not become healthy."
  tail -100 /tmp/notion-manager.stderr.log || true
  exit 1
fi

openai_body="$(jq -n \
  --arg model "$SMOKE_MODEL" \
  '{
    model: $model,
    stream: false,
    max_tokens: 64,
    messages: [
      {
        role: "system",
        content: "You are a Claude Code compatible coding assistant behind an OpenAI-compatible proxy. Do not mention Notion, pages, workspaces, or documents. Follow the user instruction exactly."
      },
      {
        role: "user",
        content: "Reply exactly with this token and nothing else: OK_CLAUDE_PROXY_OPENAI"
      }
    ]
  }')"

openai_response="$(curl -fsS --max-time 180 \
  -X POST "http://127.0.0.1:${SMOKE_PORT}/v1/chat/completions" \
  -H "Authorization: Bearer ${SMOKE_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "$openai_body")"

openai_content="$(echo "$openai_response" | jq -r '.choices[0].message.content // ""')"
echo "OpenAI-compatible smoke content: $openai_content"

if ! grep -q 'OK_CLAUDE_PROXY_OPENAI' <<<"$openai_content"; then
  echo "::error::OpenAI-compatible local smoke did not contain the expected token."
  echo "Diagnostic (first 256 chars): ${openai_content:0:256}"
  grep -E '\[bridge\] decision:|\[session\] decision:' /tmp/notion-manager.stderr.log || true
  exit 1
fi
if grep -Eiq 'notion workspace|notion context|our workspace|reframe the workspace|switch workspace|your notion workspace' <<<"$openai_content"; then
  echo "::error::OpenAI-compatible local smoke drifted due to workspace reframing."
  echo "Diagnostic (first 256 chars): ${openai_content:0:256}"
  grep -E '\[bridge\] decision:|\[session\] decision:' /tmp/notion-manager.stderr.log || true
  exit 1
fi
if grep -Eiq "don't have access to your local machine|cannot run commands directly|cannot access your local system|unable to execute code|you will need to run this|don't have direct access|cannot execute commands directly" <<<"$openai_content"; then
  echo "::error::OpenAI-compatible local smoke drifted due to tool-call refusal."
  echo "Diagnostic (first 256 chars): ${openai_content:0:256}"
  grep -E '\[bridge\] decision:|\[session\] decision:' /tmp/notion-manager.stderr.log || true
  exit 1
fi
if grep -Eiq 'notion|workspace|page|document' <<<"$openai_content"; then
  echo "::error::OpenAI-compatible local smoke leaked Notion/workspace/page/document persona text."
  echo "Diagnostic (first 256 chars): ${openai_content:0:256}"
  grep -E '\[bridge\] decision:|\[session\] decision:' /tmp/notion-manager.stderr.log || true
  exit 1
fi

anthropic_body="$(jq -n \
  --arg model "$SMOKE_MODEL" \
  '{
    model: $model,
    stream: false,
    max_tokens: 64,
    system: "You are Claude Code, Anthropic'\''s official CLI for coding. You are behind a compatibility proxy. Do not mention Notion, pages, workspaces, or documents. Follow the user instruction exactly.",
    messages: [
      {
        role: "user",
        content: "Reply exactly with this token and nothing else: OK_CLAUDE_PROXY_ANTHROPIC"
      }
    ]
  }')"

anthropic_response="$(curl -fsS --max-time 180 \
  -X POST "http://127.0.0.1:${SMOKE_PORT}/v1/messages" \
  -H "Authorization: Bearer ${SMOKE_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "$anthropic_body")"

anthropic_content="$(echo "$anthropic_response" | jq -r '[.content[]? | select(.type == "text") | .text] | join("\n")')"
echo "Anthropic smoke content: $anthropic_content"

if ! grep -q 'OK_CLAUDE_PROXY_ANTHROPIC' <<<"$anthropic_content"; then
  echo "::error::Anthropic local smoke did not contain the expected token."
  echo "Diagnostic (first 256 chars): ${anthropic_content:0:256}"
  grep -E '\[bridge\] decision:|\[session\] decision:' /tmp/notion-manager.stderr.log || true
  exit 1
fi
if grep -Eiq 'notion workspace|notion context|our workspace|reframe the workspace|switch workspace|your notion workspace' <<<"$anthropic_content"; then
  echo "::error::Anthropic local smoke drifted due to workspace reframing."
  echo "Diagnostic (first 256 chars): ${anthropic_content:0:256}"
  grep -E '\[bridge\] decision:|\[session\] decision:' /tmp/notion-manager.stderr.log || true
  exit 1
fi
if grep -Eiq "don't have access to your local machine|cannot run commands directly|cannot access your local system|unable to execute code|you will need to run this|don't have direct access|cannot execute commands directly" <<<"$anthropic_content"; then
  echo "::error::Anthropic local smoke drifted due to tool-call refusal."
  echo "Diagnostic (first 256 chars): ${anthropic_content:0:256}"
  grep -E '\[bridge\] decision:|\[session\] decision:' /tmp/notion-manager.stderr.log || true
  exit 1
fi
if grep -Eiq 'notion|workspace|page|document' <<<"$anthropic_content"; then
  echo "::error::Anthropic local smoke leaked Notion/workspace/page/document persona text."
  echo "Diagnostic (first 256 chars): ${anthropic_content:0:256}"
  grep -E '\[bridge\] decision:|\[session\] decision:' /tmp/notion-manager.stderr.log || true
  exit 1
fi
