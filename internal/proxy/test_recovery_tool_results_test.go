package proxy

import (
	"strings"
	"testing"
)

func TestRecoveryToolResultsPreservation(t *testing.T) {
	messages := []ChatMessage{
		{Role: "user", Content: "Original query: fix the bug"},
		{Role: "assistant", Content: "I will use the tool", ToolCalls: []ToolCall{
			{ID: "call_1", Function: ToolCallFunction{Name: "Bash", Arguments: "{}"}},
		}},
		{Role: "tool", ToolCallID: "call_1", Name: "Bash", Content: "line1\nline2"},
		{Role: "user", Content: "Here is the result of the tool run:\nWhat should we do next?"},
	}

	recovery := buildFreshThreadRecoveryMessages(messages)

	if len(recovery) == 0 {
		t.Fatalf("no recovery messages")
	}

	content := recovery[0].Content

	// Ensure there is a newline after the colon for tool names
	if !strings.Contains(content, "Tool (Bash):\nline1") {
		t.Errorf("Missing newline after tool label. Content:\n%s", content)
	}

	// Also check history logic
	messagesHistory := []ChatMessage{
		{Role: "user", Content: "Query 1"},
		{Role: "assistant", Content: "I will use the tool", ToolCalls: []ToolCall{
			{ID: "call_1", Function: ToolCallFunction{Name: "Bash", Arguments: "{}"}},
		}},
		{Role: "tool", ToolCallID: "call_1", Name: "Bash", Content: "line1\nline2"},
		{Role: "user", Content: "Query 2"},
	}

	recoveryHistory := buildFreshThreadRecoveryMessages(messagesHistory)
	contentHistory := recoveryHistory[0].Content

	if !strings.Contains(contentHistory, "Tool (Bash):\nline1") {
		t.Errorf("Missing newline after tool label in history. Content:\n%s", contentHistory)
	}
}
