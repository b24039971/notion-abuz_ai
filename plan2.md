The CI says: `Task proxy-claude-code-tool-call-refusal-prose-coverage-701fc235 requires observability/logging proof, but the diff has no runtime/script change and no direct log-capture assertion.`

To fix this, we need a test that captures logs or we need to add a direct log-capture assertion in our new test. Let's look at `extractToolActionsFromClaudeCodeBridge` which does the logging. It takes a raw string, and returns an error `ErrToolBridgeNoTool` while logging `[bridge] %s decision: tool-call refusal explicitly detected...`.

Wait, the memory rule says: `When writing tests for proxy observability or logging behavior, assert on the actual returned failure reasons or upstream handling logic rather than manually invoking log.Printf in the test, which results in a tautological test.`

Wait, maybe we need to test `extractToolActionsFromClaudeCodeBridge` instead of just `detectToolBridgeNoToolResponse`?
Let's see what functions are available in `internal/proxy/anthropic.go` that we can call and assert on.
