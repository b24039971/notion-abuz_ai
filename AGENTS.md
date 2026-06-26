# notion-abuz_ai Autonomous Agent Guide

This repository can be improved by Jules/Codex-style autonomous PR agents.
Use `agent_tasks.json` as the machine-readable source of truth.

## Primary Objective

Improve Claude and Claude Code compatibility through the RDSH/notion-abuz_ai
proxy. The service should behave like a coding assistant API, not like the
Notion product UI. Treat Notion persona leakage, Notion workspace/page/document
refusals, and Claude-style coding requests answered as Notion UI requests as
proxy compatibility bugs.

## Source Priority

Before changing code, read these files in order:

1. `agent_tasks.json`
2. `docs/jules_autonomous_loop.md`
3. `README.md`
4. `docs/api.md`
5. `docs/configuration.md`
6. Relevant package manifests and local tests

## Project Map

- `cmd/notion-manager/`: server entrypoint.
- `internal/proxy/`: proxy core, API compatibility, accounts, uploads, model mapping, stats.
- `internal/regjob/`: bulk registration jobs.
- `internal/msalogin/`: Microsoft SSO onboarding flow.
- `internal/netutil/`: proxy and network helpers.
- `internal/web/`: embedded dashboard assets.
- `web/`: React + TypeScript + Vite dashboard source.
- `docs/`: API and operating documentation.

## Task Selection

Pick exactly one task per PR.

Default selection rule:

1. Pick the exact requested `task_id` when provided.
2. Otherwise pick the first `todo` task in `agent_tasks.json`.
3. Implement only tasks with risk `low` or `medium` autonomously.
4. For `high` or `critical` tasks, create or refine a human-review task instead of implementing it.

If the todo queue is below `replenishment_policy.minimum_todo_tasks`, add a small batch of low/medium-risk tasks with concrete `allowed_paths` and `acceptance` criteria.

Do not ask the user to choose between implementation approaches for low/medium
tasks. If multiple safe approaches exist, choose the smallest reversible change
that satisfies the selected task's acceptance criteria. If unsure, write focused
tests first and then implement the smallest passing fix.

Local helper scripts:

```bash
python3 scripts/rool_cognitive_loop.py --validation manifest
python3 scripts/dedupe_agent_tasks.py agent_tasks.json
```

`rool_cognitive_loop.py` is an Observe-Orient-Decide-Act helper for selecting
and validating one local task. `dedupe_agent_tasks.py` is dry-run by default;
use `--write` only in a task that explicitly permits manifest cleanup.

## Safety Rules

Do not modify or commit:

- Real account JSON files
- `data/**`
- `accounts/**`
- `config.yaml` with local secrets
- `token.txt`
- `pass.txt`
- Logs
- Built binaries
- `.github/workflows/**`, unless the selected task explicitly allows workflow work
- Deployment files, unless the selected task explicitly allows deployment work

Unit tests must not call real Notion, Google, OpenAI, Anthropic, GitHub, or
Microsoft APIs. Use local fakes, fixtures, and mocks.

Live RDSH checks belong in `.github/workflows/rdsh_live_smoke.yml` and may use
the repository secret `RDSH_API_KEY`. Do not print, persist, or copy that secret.

Real-account local integration checks belong only in
`.github/workflows/rdsh_local_live_smoke.yml`. That workflow may use the
protected `live-rdsh` environment secret `LIVE_NOTION_ACCOUNTS_B64` to start the
PR code locally and test it through `127.0.0.1`. Do not move those live checks
into regular Go tests.

## Validation

Run the relevant subset first, then full validation before opening a PR:

```bash
python3 scripts/validate_agent_tasks.py agent_tasks.json
test -z "$(gofmt -l .)"
cd web && npm ci && npm run build
rm -rf internal/web/dist && cp -r web/dist internal/web/dist
go vet ./...
go test ./...
go build -ldflags="-s -w" -o notion-manager ./cmd/notion-manager
```

## PR Rules

- One task id per PR.
- Keep changes inside the task's `allowed_paths`.
- Update `agent_tasks.json` to mark the selected task as `done`.
- Add follow-up tasks for newly discovered bugs or improvements.
- Label autonomous PRs with `jules`.
- Use focused commit/PR descriptions that mention the completed task id and validation run.
