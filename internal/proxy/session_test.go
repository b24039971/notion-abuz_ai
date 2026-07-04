package proxy

import (
	"bytes"
	"log"
	"os"
	"strings"
	"testing"
)

func TestBuildRecoveryMessages_InstructionPreservation_Short(t *testing.T) {
	var buf bytes.Buffer
	log.SetOutput(&buf)
	defer log.SetOutput(os.Stderr)

	messages := []ChatMessage{
		{Role: "user", Content: "This is the original subagent instruction."},
		{Role: "assistant", Content: "I will do it."},
		{Role: "user", Content: "Latest query"},
	}

	buildFreshThreadRecoveryMessages(messages)

	logOutput := buf.String()
	if !strings.Contains(logOutput, "[bridge] diagnostic: instruction preservation during handoff - first user message included: true") {
		t.Errorf("Expected diagnostic log indicating first user message was preserved, got: %s", logOutput)
	}
}

func TestBuildRecoveryMessages_ContextLoss_SystemInstruction(t *testing.T) {
	var buf bytes.Buffer
	log.SetOutput(&buf)
	defer log.SetOutput(os.Stderr)

	messages := []ChatMessage{
		{Role: "system", Content: strings.Repeat("a", 1500)}, // Exceeds maxSystemChars (1200)
		{Role: "user", Content: "First query to trigger needsFreshThreadRecovery"},
		{Role: "assistant", Content: "ack"},
		{Role: "user", Content: "Latest query"},
	}

	buildFreshThreadRecoveryMessages(messages)

	logOutput := buf.String()
	if !strings.Contains(logOutput, "[metrics] context_loss: system_instruction_truncated") {
		t.Errorf("Expected context loss metric for system instruction truncation, got: %s", logOutput)
	}
}

func TestBuildRecoveryMessages_ContextLoss_ToolResult(t *testing.T) {
	var buf bytes.Buffer
	log.SetOutput(&buf)
	defer log.SetOutput(os.Stderr)

	messages := []ChatMessage{
		{Role: "user", Content: "Original query"},
		{Role: "assistant", Content: "Running tool", ToolCalls: []ToolCall{{ID: "1", Function: ToolCallFunction{Name: "bash", Arguments: "{}"}}}},
		{Role: "tool", ToolCallID: "1", Content: strings.Repeat("a", 1500)}, // Exceeds maxEntryChars (900)
		{Role: "user", Content: "Latest query"},
	}

	buildFreshThreadRecoveryMessages(messages)

	logOutput := buf.String()
	if !strings.Contains(logOutput, "[metrics] context_loss: tool_result_truncated") {
		t.Errorf("Expected context loss metric for tool result truncation, got: %s", logOutput)
	}
}

func TestBuildRecoveryMessages_ContextLoss_HistoryDropped(t *testing.T) {
	var buf bytes.Buffer
	log.SetOutput(&buf)
	defer log.SetOutput(os.Stderr)

	messages := []ChatMessage{
		{Role: "user", Content: "This is the original subagent instruction that should be tracked."},
	}

	// Add a huge amount of history to push the original message out of the window.
	// 4000 char max limit, each of these is ~500 chars, so 10 of them is ~5000 chars.
	longContent := strings.Repeat("a", 500)
	for i := 0; i < 10; i++ {
		messages = append(messages, ChatMessage{Role: "assistant", Content: longContent})
		messages = append(messages, ChatMessage{Role: "user", Content: "Continue workspace reframing"})
	}

	messages = append(messages, ChatMessage{Role: "user", Content: "Latest query"})

	buildFreshThreadRecoveryMessages(messages)

	logOutput := buf.String()
	if !strings.Contains(logOutput, "[metrics] context_loss: conversation_history_dropped") {
		t.Errorf("Expected context loss metric for dropped conversation history, got: %s", logOutput)
	}
	if !strings.Contains(logOutput, "[metrics] context_loss: first_user_message_dropped") {
		t.Errorf("Expected context loss metric for dropped first user message, got: %s", logOutput)
	}
}

func TestBuildRecoveryMessages_InstructionPreservation_LongHistoryLost(t *testing.T) {
	var buf bytes.Buffer
	log.SetOutput(&buf)
	defer log.SetOutput(os.Stderr)

	messages := []ChatMessage{
		{Role: "user", Content: "This is the original subagent instruction that should be tracked."},
	}

	// Add a huge amount of history (e.g. workspace reframing loop) to push the original message out of the window.
	// 4000 char max limit, each of these is ~500 chars, so 10 of them is ~5000 chars.
	longContent := strings.Repeat("a", 500)
	for i := 0; i < 10; i++ {
		messages = append(messages, ChatMessage{Role: "assistant", Content: longContent})
		messages = append(messages, ChatMessage{Role: "user", Content: "Continue workspace reframing"})
	}

	messages = append(messages, ChatMessage{Role: "user", Content: "Latest query"})

	buildFreshThreadRecoveryMessages(messages)

	logOutput := buf.String()
	if !strings.Contains(logOutput, "[bridge] diagnostic: instruction preservation during handoff - first user message included: false") {
		t.Errorf("Expected diagnostic log indicating first user message was lost due to truncation, got: %s", logOutput)
	}
}

func TestBuildRecoveryMessages_DiagnosticLogging_SkippedEntries(t *testing.T) {
	var buf bytes.Buffer
	log.SetOutput(&buf)
	defer log.SetOutput(os.Stderr)

	messages := []ChatMessage{
		{Role: "user", Content: "Do something"},
		{Role: "assistant", Content: "I will do it."},
		{Role: "user", Content: "Another query"},
		{Role: "assistant", Content: "Okay"},
		{Role: "tool", Content: "(empty output)", Name: "bash"},
	}

	buildRecoveryMessages(messages, func(msg ChatMessage, content string) bool {
		if msg.Role == "tool" && content == "(empty output)" {
			return true
		}
		return false
	})

	logOutput := buf.String()
	if !strings.Contains(logOutput, "[bridge] diagnostic: skipped entry during recovery traversal") {
		t.Errorf("Expected skipped entry log, got: %s", logOutput)
	}
}

func TestBuildRecoveryMessages_ContextLoss_TrailingDropped(t *testing.T) {
	var buf bytes.Buffer
	log.SetOutput(&buf)
	defer log.SetOutput(os.Stderr)

	messages := []ChatMessage{
		{Role: "user", Content: "Latest query"},
		{Role: "assistant", Content: strings.Repeat("A", 800), ToolCalls: []ToolCall{{ID: "1", Function: ToolCallFunction{Name: "bash", Arguments: "{}"}}}},
		{Role: "tool", ToolCallID: "1", Content: strings.Repeat("a", 800)},
		{Role: "assistant", Content: strings.Repeat("B", 800), ToolCalls: []ToolCall{{ID: "2", Function: ToolCallFunction{Name: "bash", Arguments: "{}"}}}},
		{Role: "tool", ToolCallID: "2", Content: strings.Repeat("b", 800)},
		{Role: "assistant", Content: strings.Repeat("C", 800), ToolCalls: []ToolCall{{ID: "3", Function: ToolCallFunction{Name: "bash", Arguments: "{}"}}}},
		{Role: "tool", ToolCallID: "3", Content: strings.Repeat("c", 800)},
		{Role: "assistant", Content: strings.Repeat("D", 800), ToolCalls: []ToolCall{{ID: "4", Function: ToolCallFunction{Name: "bash", Arguments: "{}"}}}},
		{Role: "tool", ToolCallID: "4", Content: strings.Repeat("d", 800)},
	}

	buildFreshThreadRecoveryMessages(messages)

	logOutput := buf.String()
	if !strings.Contains(logOutput, "[metrics] context_loss: trailing_progress_dropped") {
		t.Errorf("Expected context loss metric for trailing_progress_dropped, got: %s", logOutput)
	}
}

func TestBuildRecoveryMessages_ContextLoss_EmptySystemMessage(t *testing.T) {
	var buf bytes.Buffer
	log.SetOutput(&buf)
	defer log.SetOutput(os.Stderr)

	messages := []ChatMessage{
		{Role: "system", Content: "   \n "}, // Empty after trim
		{Role: "user", Content: "First query to trigger needsFreshThreadRecovery"},
		{Role: "assistant", Content: "ack"},
		{Role: "user", Content: "Latest query"},
	}

	buildFreshThreadRecoveryMessages(messages)

	logOutput := buf.String()
	if !strings.Contains(logOutput, "[metrics] context_loss: empty_system_prompt_dropped") {
		t.Errorf("Expected context loss metric for empty_system_prompt_dropped, got: %s", logOutput)
	}
}

func TestBuildRecoveryMessages_ContextLoss_EmptyEntry(t *testing.T) {
	var buf bytes.Buffer
	log.SetOutput(&buf)
	defer log.SetOutput(os.Stderr)

	messages := []ChatMessage{
		{Role: "user", Content: "First query to trigger needsFreshThreadRecovery"},
		{Role: "assistant", Content: "   "}, // Empty after trim
		{Role: "user", Content: "Latest query"},
	}

	buildFreshThreadRecoveryMessages(messages)

	logOutput := buf.String()
	if !strings.Contains(logOutput, "[metrics] context_loss: recovery_empty_entry_dropped") {
		t.Errorf("Expected context loss metric for recovery_empty_entry_dropped, got: %s", logOutput)
	}
}
func TestBuildRecoveryMessages_ContextLoss_LatestUserMessage(t *testing.T) {
	var buf bytes.Buffer
	log.SetOutput(&buf)
	defer log.SetOutput(os.Stderr)

	messages := []ChatMessage{
		{Role: "user", Content: "Original query"},
		{Role: "assistant", Content: "ack"},
		{Role: "user", Content: strings.Repeat("a", 8500)}, // Exceeds 8000
	}

	buildFreshThreadRecoveryMessages(messages)

	logOutput := buf.String()
	if !strings.Contains(logOutput, "[metrics] context_loss: latest_user_message_truncated") {
		t.Errorf("Expected context loss metric for latest user message truncation, got: %s", logOutput)
	}
}
func TestBuildRecoveryMessages_ContextLoss_HistoryEntry(t *testing.T) {
	var buf bytes.Buffer
	log.SetOutput(&buf)
	defer log.SetOutput(os.Stderr)

	messages := []ChatMessage{
		{Role: "user", Content: "Original query"},
		{Role: "assistant", Content: strings.Repeat("b", 1500)}, // Exceeds maxEntryChars (900)
		{Role: "user", Content: "Latest query"},
	}

	buildFreshThreadRecoveryMessages(messages)

	logOutput := buf.String()
	if !strings.Contains(logOutput, "[metrics] context_loss: history_entry_truncated") {
		t.Errorf("Expected context loss metric for history entry truncation, got: %s", logOutput)
	}
}
