# Jules Autonomous Improvement Loop

This document defines the autonomous improvement loop for notion-abuz_ai.
The goal is to improve Claude and Claude Code compatibility through the
RDSH/notion-abuz_ai proxy without touching secrets, runtime account data,
deployment state, or broad unrelated code.

## Loop

```text
manual dispatch or pull_request.closed
  -> trigger Jules API session
  -> Jules reads project rules
  -> Jules selects one safe task
  -> Jules implements a bounded change
  -> Jules updates tests and docs
  -> Jules marks the task done
  -> Jules opens one PR
  -> CI and automerge validate/merge
  -> pull_request.closed starts the next task
```

This is an event-driven loop. The GitHub workflow does not run forever; it
starts a Jules session and exits. The loop continues when Jules opens a PR and
the PR is merged.

## Main Rule

Do not ask the user what to do next when a safe `todo` task exists.
Ask for human review only when the change requires secrets, production access,
deployment changes, workflow permission changes, or high/critical-risk work.

For low/medium-risk tasks, do not ask the user to choose between implementation
approaches. If multiple safe approaches exist, choose the smallest reversible
change that satisfies the selected task's acceptance criteria. If unsure, add
focused tests first and then implement the smallest passing fix.

## Product Goal

The proxy should behave like a stable coding-assistant API for Claude and
Claude Code clients. Treat these as compatibility bugs:

- Notion persona leakage.
- Notion workspace/page/document refusals in coding-assistant requests.
- Claude-style coding prompts answered as if the user is inside Notion.
- Model drift caused by lossy OpenAI-compatible or Anthropic-compatible request
  translation.

## Task Sources

Priority:

1. `agent_tasks.json`
2. Failing CI from the current PR
3. `AGENTS.md`
4. `README.md`
5. `docs/api.md`
6. `docs/configuration.md`
7. TODO/FIXME comments
8. Repeated runtime or test failures visible in the repository

## Local Helper Scripts

Two helper scripts are available for local agent work:

```bash
python3 scripts/rool_cognitive_loop.py --validation manifest
python3 scripts/dedupe_agent_tasks.py agent_tasks.json
```

`rool_cognitive_loop.py` implements a small Observe-Orient-Decide-Act cycle for
selecting the first safe todo task, printing its allowed paths, and optionally
running validation. It does not call Jules or any external API.

`dedupe_agent_tasks.py` detects duplicate todo tasks. It is dry-run by default.
Use `--write` only when the selected task allows manifest cleanup.

## Replenishment Policy

Keep at least `replenishment_policy.minimum_todo_tasks` tasks with status
`todo`.

When the queue is low:

1. Prefer stabilization, tests, and compatibility work over feature expansion.
2. Generate low/medium-risk tasks only.
3. Each new task must include:
   - stable `id`
   - `area`
   - `risk`
   - `title`
   - `description`
   - `allowed_paths`
   - `acceptance`
4. Do not duplicate done or existing todo tasks.
5. Keep each task small enough for one PR.

## Proxy Priorities

Prefer improvements in this order:

1. Claude Code behavior that avoids Notion persona leakage.
2. OpenAI-compatible API correctness.
3. Anthropic Messages API compatibility.
4. Model mapping and alias behavior.
5. Streaming and tool-call regression tests.
6. Error normalization and retry behavior.
7. Dashboard visibility into runtime state.
8. Documentation that prevents misconfiguration.

For `claude-code-notion-persona-leakage-regression`, prefer a narrow
coding-assistant detection helper plus a short proxy compatibility instruction.
Do not preserve the full Claude Code system prompt in tool-heavy requests.

## Protected Files

Autonomous PRs must not edit:

- `.github/workflows/**`
- `data/**`
- `accounts/**`
- `config.yaml`
- `token.txt`
- `pass.txt`
- `*.log`
- built binaries
- real account/session dumps

Workflow changes must be performed manually or through a dedicated human-reviewed
task.

## Validation Contract

Before opening a PR, run or reason through:

```bash
python3 scripts/validate_agent_tasks.py agent_tasks.json
test -z "$(gofmt -l .)"
cd web && npm ci && npm run build
rm -rf internal/web/dist && cp -r web/dist internal/web/dist
go vet ./...
go test ./...
go build -ldflags="-s -w" -o notion-manager ./cmd/notion-manager
```

If validation fails, fix the failure inside the current task scope when possible.
If the failure is unrelated, add a follow-up task and explain it in the PR body.

## Live RDSH Smoke Tests

The repository may define `RDSH_API_KEY` as a GitHub secret. Live checks must use
that secret only through GitHub Actions environment variables and must not print
or store it. Live network checks belong in `.github/workflows/rdsh_live_smoke.yml`;
unit tests must stay offline and deterministic.

## Local Live Account Smoke Tests

Real account checks can be run against the code from a PR through
`.github/workflows/rdsh_local_live_smoke.yml`. That workflow decodes
`LIVE_NOTION_ACCOUNTS_B64` from the protected `live-rdsh` GitHub environment,
starts `notion-manager` locally, and verifies OpenAI-compatible and Anthropic
Claude Code style requests against `127.0.0.1`.

Use this workflow for integration validation only. Do not add live Notion calls
to Go unit tests, and do not commit account files or generated runtime configs.
See `docs/live_smoke_secrets.md` for setup and operating details.
