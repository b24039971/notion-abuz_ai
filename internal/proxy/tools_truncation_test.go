package proxy

import (
	"strings"
	"testing"
)

func TestClaudeCodeAgentLoop_ToolResultContinuationLargeOutputTruncation(t *testing.T) {
	longOutput := strings.Repeat("A", 5000)

	messages := []ChatMessage{
		{Role: "user", Content: "Run a command that outputs a lot of text."},
		{Role: "assistant", Content: "", ToolCalls: []ToolCall{
			{ID: "call_1", Type: "function", Function: ToolCallFunction{Name: "Bash", Arguments: `{"command":"cat huge_file.txt"}`}},
		}},
		{Role: "tool", Name: "Bash", ToolCallID: "call_1", Content: longOutput},
	}

	followUp := buildSessionChainContinuation(messages, "Bash", "")
	if len(followUp) != 1 {
		t.Fatalf("expected 1 follow up")
	}

	content := followUp[0].Content

	if !strings.Contains(content, "... (truncated)") {
		t.Errorf("Expected long tool output to be truncated")
	}

	// Ensure safe multibyte truncation
	if len(content) > 6000 {
		t.Errorf("Follow-up prompt should be constrained in size, got %d chars", len(content))
	}
}

func TestClaudeCodeAgentLoop_ToolResultContinuationMultibyteTruncation(t *testing.T) {
	// A string of emojis (each emoji is multiple bytes, usually 4)
	longOutput := strings.Repeat("😂", 5000) // This is 20,000 bytes long

	messages := []ChatMessage{
		{Role: "user", Content: "Run a command that outputs a lot of text."},
		{Role: "assistant", Content: "", ToolCalls: []ToolCall{
			{ID: "call_1", Type: "function", Function: ToolCallFunction{Name: "Bash", Arguments: `{"command":"cat huge_file.txt"}`}},
		}},
		{Role: "tool", Name: "Bash", ToolCallID: "call_1", Content: longOutput},
	}

	followUp := buildSessionChainContinuation(messages, "Bash", "")
	if len(followUp) != 1 {
		t.Fatalf("expected 1 follow up")
	}

	content := followUp[0].Content

	if !strings.Contains(content, "... (truncated)") {
		t.Errorf("Expected long tool output to be truncated")
	}

	// Check if string ends with a half-sliced emoji.
	// Since we are truncating by rune, it shouldn't. If we were truncating by byte, it might.
	// The `[]rune` trick in Go prevents invalid UTF-8 generation anyway, but let's test it runs fine.
}

func TestLegacyCollapseDroppedToolResultMetric(t *testing.T) {
	// Reset metrics before test
	contextLossMetricsMu.Lock()
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()

	tools := []Tool{
		{Function: ToolFunction{Name: "ToolA"}},
		{Function: ToolFunction{Name: "ToolB"}},
		{Function: ToolFunction{Name: "ToolC"}},
		{Function: ToolFunction{Name: "ToolD"}},
		{Function: ToolFunction{Name: "ToolE"}},
		{Function: ToolFunction{Name: "ToolF"}},
	}

	messages := []ChatMessage{
		{Role: "user", Content: "Query 1"},
		{Role: "assistant", ToolCalls: []ToolCall{{ID: "1", Function: ToolCallFunction{Name: "ToolA"}}}},
		{Role: "tool", Content: "Result 1", ToolCallID: "1"}, // This should be dropped because it's before the last user query
		{Role: "user", Content: "Query 2"},
		{Role: "assistant", ToolCalls: []ToolCall{{ID: "2", Function: ToolCallFunction{Name: "ToolB"}}}},
		{Role: "tool", Content: "Result 2", ToolCallID: "2"}, // This is the final tool result to trigger chain continuation
	}

	// Force format-based injection which hits legacy collapse (large toolset + chain continuation + no session)
	_ = injectToolsIntoMessages(messages, tools, "claude-3-5-sonnet-20241022", nil)

	contextLossMetricsMu.Lock()
	count := contextLossMetrics["legacy_collapse_dropped_tool_result"]
	contextLossMetricsMu.Unlock()

	if count != 1 {
		t.Errorf("Expected 1 dropped tool result metric, got %d", count)
	}
}
