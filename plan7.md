Wait, `anthropic_observability_test.go` *already* has `TestEnsureToolCallRefusalLoggedAsDecision`.
But wait! My task says: "Expand internal/proxy/anthropic_bridge_test.go to add test coverage for complex "tool-call refusal" behaviors where Claude Code declines tool execution and instead outputs conversational prose explaining how to manually execute commands, ensuring these are safely detected."
And my task's `allowed_paths` includes `internal/proxy/anthropic_bridge_test.go`. The test I added (`TestDetectToolBridgeNoToolResponse_MatchesComplexToolCallRefusals`) only checks `detectToolBridgeNoToolResponse`, not logging!
Wait, but if I add logging check in `TestDetectToolBridgeNoToolResponse_MatchesComplexToolCallRefusals`, `detectToolBridgeNoToolResponse` doesn't log anything. The logging happens in `handleAnthropicNonStream` or `handleAnthropicStream`.
Can I add a log-capture test in `internal/proxy/anthropic_bridge_test.go`? Yes, I can copy the test from `anthropic_observability_test.go` or just add a new mock test in `anthropic_bridge_test.go` for the *complex* tool call refusals!

Let's look at what `TestEnsureToolCallRefusalLoggedAsDecision` tests: it tests `"I do not have access to run terminal commands such as bash or read or edit local files. You will need to copy and paste this into your coding assistant."`.
I should write `TestEnsureComplexToolCallRefusalLoggedAsDecision` in `anthropic_bridge_test.go` and use one of my complex refusal strings.
