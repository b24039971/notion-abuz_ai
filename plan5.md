Ah! `log.SetOutput(&buf)` is used to capture logs in tests, as seen in `anthropic_observability_test.go`.
But my task's `allowed_paths` is:
- `internal/proxy/anthropic_bridge_test.go`
- `internal/proxy/anthropic.go`
- `agent_tasks.json`

I should add a test in `internal/proxy/anthropic_bridge_test.go` that calls `handleAnthropicStream` (or similar) using a mocked `getChromeHTTPClient` to return a fake Notion response that contains the tool-call refusal prose, and then check `buf.String()` to ensure the log message `[bridge] %s decision: tool-call refusal explicitly detected` is recorded.
Wait, let's see how `handleAnthropicNonStream` or `handleAnthropicStream` is tested. Let's look at `anthropic_observability_test.go` to see how the mock server is set up.
