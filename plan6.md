Awesome! I can use exactly this pattern to write `TestEnsureToolCallRefusalLoggedAsDecision` in `internal/proxy/anthropic_bridge_test.go` and assert that the `tool-call refusal` string is printed to the log output!

Let's do this:
1. Use `replace_with_git_merge_diff` to add `TestEnsureToolCallRefusalLoggedAsDecision` to `internal/proxy/anthropic_bridge_test.go`.
2. The mock server will return: `"I cannot read or edit files or run bash commands directly. Please copy and paste this into Claude Code to execute."`
3. We will run `handleAnthropicNonStream` and verify `buf.String()` contains `tool-call refusal explicitly detected`.
4. Then `go test ./internal/proxy` to verify it passes.
5. Submit.
