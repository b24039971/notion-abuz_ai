The function testing this logic is `handleAnthropicStream` (around line 1732), or maybe there's a higher level test.
Instead of calling `handleAnthropicStream` which takes HTTP response writers and does network requests, let's look at `extractToolActionsFromClaudeCodeBridge`. Wait, the logic is inline in `handleAnthropicStream`.

Is there a `TestHandleAnthropicStream...` in `anthropic_bridge_test.go`?
Let's search for "TestHandleAnthropicStream" or "ErrToolBridgeNoTool" in `anthropic_bridge_test.go`.
