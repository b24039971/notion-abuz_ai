package proxy

import (
	"strings"
	"testing"
)

func TestRecoveryToolResultsPreservation(t *testing.T) {
	// Simulate an Anthropic tool chain where the last turn had a user message
	// containing both text and a tool result.
	// In convertAnthropicMessages, this produces:
	// assistant (tool_use)
	// tool (tool_result)
	// user (text blocks from the same message)

	messages := []ChatMessage{
		{Role: "user", Content: "Original query: fix the bug"},
		{Role: "assistant", Content: "I will use the tool", ToolCalls: []ToolCall{
			{ID: "call_1", Function: ToolCallFunction{Name: "Bash", Arguments: "{}"}},
		}},
		{Role: "tool", ToolCallID: "call_1", Name: "Bash", Content: "tool output here"},
		{Role: "user", Content: "Here is the result of the tool run:\nWhat should we do next?"},
	}

	recovery := buildFreshThreadRecoveryMessages(messages)

	if len(recovery) == 0 {
		t.Fatalf("no recovery messages")
	}

	content := recovery[0].Content

	// The original query should be the latest user message
	if !strings.Contains(content, "Latest user message:\nOriginal query: fix the bug") {
		t.Errorf("Latest user message is incorrect. Content:\n%s", content)
	}

	// The partial progress should contain the tool calls and results
	if !strings.Contains(content, "Partial progress") {
		t.Errorf("Missing partial progress. Content:\n%s", content)
	}

	if !strings.Contains(content, "tool output here") {
		t.Errorf("Missing tool output. Content:\n%s", content)
	}
}
