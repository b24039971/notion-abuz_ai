1. **Analyze the CI Failure**:
   - Error: `Autonomous PR quality gate failed: Task proxy-claude-code-tool-call-refusal-prose-coverage-701fc235 is operational/diagnostic but the PR changed only tests and agent_tasks.json.; Task proxy-claude-code-tool-call-refusal-prose-coverage-701fc235 requires observability/logging proof, but the diff has no runtime/script change and no direct log-capture assertion.`
   - The task `proxy-claude-code-tool-call-refusal-prose-coverage-701fc235` explicitly says: "ensuring these are safely detected." The PR quality gate requires proof that we are asserting on the observability/logging, meaning we need to assert that the logging or observability actually triggers, not just that `detectToolBridgeNoToolResponse` returns true.
   - However, `detectToolBridgeNoToolResponse` is a pure function. The observability (logging) happens in `extractToolActionsFromClaudeCodeBridge` and `extractJSONToolActionsFromClaudeCodeBridge` where it calls `detectToolBridgeNoToolResponse` and then logs `[bridge] %s decision: tool-call refusal explicitly detected ...`.
   - The PR gate looks for "runtime/script change" OR "direct log-capture assertion" in the tests. I only added a unit test for `detectToolBridgeNoToolResponse`. To pass the PR gate, I need a test that captures logs or asserts on the bridge decision.

2. **Understand the Code**:
   - `anthropic_bridge_test.go` likely has tests for `extractToolActionsFromClaudeCodeBridge` or similar that assert on the `ErrToolBridgeNoTool` error. If we look at other tests, they might be capturing logs or testing the higher-level function.

3. **Check `anthropic_bridge_test.go` for log capture or higher-level tests**:
