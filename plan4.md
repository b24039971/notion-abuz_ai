The CI explicitly says: "requires observability/logging proof, but the diff has no runtime/script change and no direct log-capture assertion."

If we look at `TestWorkspaceReframingDetection_BubblesUpReason`, it does exactly what our new test does: it calls `detectToolBridgeNoToolResponse` and asserts the reason.
But perhaps we can capture logs in the new test or we need to add a test for `handleAnthropicStream` that captures logs, OR we can capture logs in a mock HTTP handler test.

Wait, looking at my task's `allowed_paths`, I have `internal/proxy/anthropic_bridge_test.go` and `internal/proxy/anthropic.go`.
If I look at the rules for tests: "When writing tests for proxy observability or logging behavior, assert on the actual returned failure reasons or upstream handling logic rather than manually invoking log.Printf in the test, which results in a tautological test."

How can we test the proxy observability/logging behavior? We can start a mock server, send a request that triggers this, and check the logs. But the proxy might be complex to start.

Let's look at `session_multiturn_test.go` or `claude_code_final_answer_test.go` which were mentioned in memory. They have tests that might be similar.
