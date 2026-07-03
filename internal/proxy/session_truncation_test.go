package proxy

import (
	"bytes"
	"log"
	"strings"
	"testing"
)

func TestSessionRecoveryTruncation(t *testing.T) {
	// Capture log output
	var buf bytes.Buffer
	originalLogOutput := log.Writer()
	defer log.SetOutput(originalLogOutput)
	log.SetOutput(&buf)

	// Build a large system prompt
	systemPrompt := strings.Repeat("a", 1500)

	// Build a large history
	messages := []ChatMessage{
		{Role: "system", Content: systemPrompt},
		{Role: "user", Content: "initial user message"},
		{Role: "assistant", Content: strings.Repeat("b", 1000)},
		{Role: "user", Content: "another user message"},
		{Role: "assistant", Content: strings.Repeat("c", 1000)},
		{Role: "user", Content: "latest user message"},
	}

	buildFreshThreadRecoveryMessages(messages)

	output := buf.String()

	if !strings.Contains(output, "diagnostic: session recovery truncated system context") {
		t.Errorf("Expected diagnostic log for system context truncation, got: %s", output)
	}

	if !strings.Contains(output, "diagnostic: session recovery truncated Assistant context") {
		t.Errorf("Expected diagnostic log for Assistant context truncation, got: %s", output)
	}

	// Test history length truncation
	buf.Reset()
	var longHistory []ChatMessage
	longHistory = append(longHistory, ChatMessage{Role: "system", Content: "system"})
	longHistory = append(longHistory, ChatMessage{Role: "user", Content: "first"})
	for i := 0; i < 10; i++ {
		longHistory = append(longHistory, ChatMessage{Role: "assistant", Content: strings.Repeat("a", 500)})
		longHistory = append(longHistory, ChatMessage{Role: "user", Content: "user"})
	}
	longHistory = append(longHistory, ChatMessage{Role: "user", Content: "last"})

	buildFreshThreadRecoveryMessages(longHistory)
	output = buf.String()

	if !strings.Contains(output, "diagnostic: session recovery truncated conversation history") {
		t.Errorf("Expected diagnostic log for history truncation, got: %s", output)
	}
}
