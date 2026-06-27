package proxy

import (
	"strings"
	"testing"
	"time"
)

// TestBuildSessionChainFollowUp verifies that the session-based chain follow-up
// builds a concise message with only the latest tool results.
func TestBuildSessionChainFollowUp(t *testing.T) {
	messages := []ChatMessage{
		{Role: "user", Content: "list files in the current directory"},
		{Role: "assistant", Content: "I'll help with that.", ToolCalls: []ToolCall{
			{ID: "call_1", Type: "function", Function: ToolCallFunction{Name: "Bash", Arguments: `{"command":"ls"}`}},
		}},
		{Role: "tool", ToolCallID: "call_1", Name: "Bash", Content: "file1.txt\nfile2.txt\nREADME.md"},
	}

	compactList := "- Bash(command: str) — Execute shell command\n- Read(file_path: str) — Read a file\n"
	result := buildSessionChainFollowUp(messages, compactList, "/home/user/project")

	if len(result) != 1 {
		t.Fatalf("expected 1 message, got %d", len(result))
	}
	if result[0].Role != "user" {
		t.Fatalf("expected user role, got %s", result[0].Role)
	}

	content := result[0].Content
	// Should contain tool results
	if !strings.Contains(content, "[Bash]: file1.txt") {
		t.Errorf("expected tool results in follow-up, got: %s", content)
	}
	// Should contain CWD
	if !strings.Contains(content, "Working directory: /home/user/project") {
		t.Errorf("expected CWD in follow-up, got: %s", content)
	}
	// Should contain available functions
	if !strings.Contains(content, "Available functions:") {
		t.Errorf("expected function list in follow-up, got: %s", content)
	}
	// Should contain __done__
	if !strings.Contains(content, "__done__") {
		t.Errorf("expected __done__ in follow-up, got: %s", content)
	}
	// Note: We now actively include the original query to prevent tool-result continuation loss,
	// where Notion's system prompt would otherwise override the thread's coding context.
	if !strings.Contains(content, "list files in the current directory") {
		t.Errorf("follow-up should include the original query to preserve coding intent")
	}
}

// TestBuildSessionChainFollowUp_MultipleToolResults verifies handling of parallel tool calls.
func TestBuildSessionChainFollowUp_MultipleToolResults(t *testing.T) {
	messages := []ChatMessage{
		{Role: "user", Content: "check both files"},
		{Role: "assistant", Content: "I'll read both.", ToolCalls: []ToolCall{
			{ID: "call_1", Type: "function", Function: ToolCallFunction{Name: "Read", Arguments: `{"file_path":"a.txt"}`}},
			{ID: "call_2", Type: "function", Function: ToolCallFunction{Name: "Read", Arguments: `{"file_path":"b.txt"}`}},
		}},
		{Role: "tool", ToolCallID: "call_1", Name: "Read", Content: "content of a"},
		{Role: "tool", ToolCallID: "call_2", Name: "Read", Content: "content of b"},
	}

	result := buildSessionChainFollowUp(messages, "- Read(file_path: str)\n", "")

	content := result[0].Content
	if !strings.Contains(content, "[Read]: content of a") {
		t.Errorf("expected first tool result, got: %s", content)
	}
	if !strings.Contains(content, "[Read]: content of b") {
		t.Errorf("expected second tool result, got: %s", content)
	}
}

// TestBuildSessionChainFollowUp_TruncatesLargeOutput verifies truncation of large tool output.
func TestBuildSessionChainFollowUp_TruncatesLargeOutput(t *testing.T) {
	largeOutput := strings.Repeat("x", 5000)
	messages := []ChatMessage{
		{Role: "user", Content: "read large file"},
		{Role: "assistant", Content: "Reading.", ToolCalls: []ToolCall{
			{ID: "call_1", Type: "function", Function: ToolCallFunction{Name: "Read", Arguments: `{"file_path":"big.txt"}`}},
		}},
		{Role: "tool", ToolCallID: "call_1", Name: "Read", Content: largeOutput},
	}

	result := buildSessionChainFollowUp(messages, "- Read(file_path: str)\n", "")

	content := result[0].Content
	if !strings.Contains(content, "... (truncated)") {
		t.Errorf("expected truncation marker in large output")
	}
	// Should be well under the original 5000 chars
	if len(content) > 4500 {
		t.Errorf("follow-up too large: %d chars (expected < 4500)", len(content))
	}
}

func TestBuildSessionChainFollowUp_ReadOversizeGuard(t *testing.T) {
	messages := []ChatMessage{
		{Role: "user", Content: "检查为什么 copy 按钮不显示"},
		{Role: "assistant", Content: "I'll inspect the file.", ToolCalls: []ToolCall{
			{ID: "call_1", Type: "function", Function: ToolCallFunction{Name: "Read", Arguments: `{"file_path":"src/content.js"}`}},
		}},
		{Role: "tool", ToolCallID: "call_1", Name: "Read", Content: "File content (31582 tokens) exceeds maximum allowed tokens (10000). Use offset and limit parameters to read specific portions of the file."},
	}

	result := buildSessionChainFollowUp(messages, "- Read(file_path: str, offset?: num, limit?: num)\n- Grep(pattern: str)\n", "")
	content := result[0].Content
	if !strings.Contains(content, "Do NOT repeat the same full-file Read") {
		t.Fatalf("expected oversize read guard in follow-up, got: %s", content)
	}
}

// TestCountNonSystemMessages verifies the new helper function.
func TestCountNonSystemMessages(t *testing.T) {
	tests := []struct {
		name     string
		messages []ChatMessage
		want     int
	}{
		{
			name:     "empty",
			messages: nil,
			want:     0,
		},
		{
			name: "system only",
			messages: []ChatMessage{
				{Role: "system", Content: "you are helpful"},
			},
			want: 0,
		},
		{
			name: "first turn",
			messages: []ChatMessage{
				{Role: "system", Content: "system prompt"},
				{Role: "user", Content: "hello"},
			},
			want: 1,
		},
		{
			name: "chain continuation",
			messages: []ChatMessage{
				{Role: "system", Content: "system prompt"},
				{Role: "user", Content: "hello"},
				{Role: "assistant", Content: "tool call"},
				{Role: "tool", Content: "result"},
			},
			want: 3,
		},
		{
			name: "multi-round chain",
			messages: []ChatMessage{
				{Role: "system", Content: "system prompt"},
				{Role: "user", Content: "hello"},
				{Role: "assistant", Content: "tool call 1"},
				{Role: "tool", Content: "result 1"},
				{Role: "assistant", Content: "tool call 2"},
				{Role: "tool", Content: "result 2"},
			},
			want: 5,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := countNonSystemMessages(tt.messages)
			if got != tt.want {
				t.Errorf("countNonSystemMessages() = %d, want %d", got, tt.want)
			}
		})
	}
}

// TestSessionFingerprintStability verifies that the session fingerprint is stable
// across turns when computed on raw (pre-injection) messages.
func TestSessionFingerprintStability(t *testing.T) {
	systemPrompt := "You are Claude Code, a CLI assistant..."

	// Turn 1: just system + user
	turn1 := []ChatMessage{
		{Role: "system", Content: systemPrompt},
		{Role: "user", Content: "list files here"},
	}

	// Turn 2: system + user + assistant + tool (chain continuation)
	turn2 := []ChatMessage{
		{Role: "system", Content: systemPrompt},
		{Role: "user", Content: "list files here"},
		{Role: "assistant", Content: `{"name":"Bash","arguments":{"command":"ls"}}`},
		{Role: "tool", Content: "file1.txt\nfile2.txt"},
	}

	// Turn 3: system + user + assistant + tool + assistant + tool
	turn3 := []ChatMessage{
		{Role: "system", Content: systemPrompt},
		{Role: "user", Content: "list files here"},
		{Role: "assistant", Content: `{"name":"Bash","arguments":{"command":"ls"}}`},
		{Role: "tool", Content: "file1.txt\nfile2.txt"},
		{Role: "assistant", Content: `{"name":"Read","arguments":{"file_path":"file1.txt"}}`},
		{Role: "tool", Content: "content of file1"},
	}

	fp1 := computeSessionFingerprint(turn1)
	fp2 := computeSessionFingerprint(turn2)
	fp3 := computeSessionFingerprint(turn3)

	if fp1 != fp2 {
		t.Errorf("fingerprint changed between turn 1 and 2: %s vs %s", fp1, fp2)
	}
	if fp2 != fp3 {
		t.Errorf("fingerprint changed between turn 2 and 3: %s vs %s", fp2, fp3)
	}
}

// TestSessionContinuationDetection verifies that rawMsgCount correctly
// distinguishes first turn, continuation, and repeat.
func TestSessionContinuationDetection(t *testing.T) {
	sm := NewSessionManager(5 * time.Minute)

	systemPrompt := "You are Claude Code..."
	fingerprint := "test-fingerprint-123456789012"

	// Turn 1: first turn
	turn1Msgs := []ChatMessage{
		{Role: "system", Content: systemPrompt},
		{Role: "user", Content: "hello"},
	}
	rawMsgCount1 := countNonSystemMessages(turn1Msgs)
	if rawMsgCount1 != 1 {
		t.Fatalf("expected 1, got %d", rawMsgCount1)
	}

	session := sm.Get(fingerprint)
	if session != nil {
		t.Fatal("expected nil session for first turn")
	}

	// Save session after turn 1
	sm.Set(fingerprint, &Session{
		ThreadID:        "thread-1",
		TurnCount:       1,
		RawMessageCount: rawMsgCount1,
		AccountEmail:    "test@example.com",
		CreatedAt:       time.Now(),
		LastUsedAt:      time.Now(),
	})

	// Turn 2: chain continuation (rawMsgCount increases)
	turn2Msgs := []ChatMessage{
		{Role: "system", Content: systemPrompt},
		{Role: "user", Content: "hello"},
		{Role: "assistant", Content: "tool call"},
		{Role: "tool", Content: "result"},
	}
	rawMsgCount2 := countNonSystemMessages(turn2Msgs)
	if rawMsgCount2 != 3 {
		t.Fatalf("expected 3, got %d", rawMsgCount2)
	}

	session = sm.Get(fingerprint)
	if session == nil {
		t.Fatal("expected existing session")
	}
	if rawMsgCount2 <= session.RawMessageCount {
		t.Error("expected continuation detection (rawMsgCount > session.RawMessageCount)")
	}

	// Simulate saving after turn 2
	session.TurnCount++
	session.RawMessageCount = rawMsgCount2

	// Retry of turn 2 (same messages): repeat detection
	rawMsgCountRetry := countNonSystemMessages(turn2Msgs)
	if rawMsgCountRetry != session.RawMessageCount {
		t.Errorf("expected repeat detection: rawMsgCount=%d, session.RawMessageCount=%d",
			rawMsgCountRetry, session.RawMessageCount)
	}

	// Rollback: user removes the last tool result and assistant call, and asks something else
	turn3Msgs := []ChatMessage{
		{Role: "system", Content: systemPrompt},
		{Role: "user", Content: "hello"},
		{Role: "user", Content: "never mind, let's do something else"},
	}
	rawMsgCountRollback := countNonSystemMessages(turn3Msgs)
	if rawMsgCountRollback >= session.RawMessageCount {
		t.Errorf("expected rollback detection (rawMsgCount < session.RawMessageCount): got %d, want < %d",
			rawMsgCountRollback, session.RawMessageCount)
	}
}

// TestClaudeCodeAgentLoop_RetryBehavior verifies that identical repeated turns
// yield the exact same fingerprint and message count.
func TestClaudeCodeAgentLoop_RetryBehavior(t *testing.T) {
	systemPrompt := "You are Claude Code..."

	// Turn A
	turnAMsgs := []ChatMessage{
		{Role: "system", Content: systemPrompt},
		{Role: "user", Content: "run test"},
		{Role: "assistant", Content: "", ToolCalls: []ToolCall{{ID: "call_1", Function: ToolCallFunction{Name: "Bash", Arguments: `{"command":"go test ."}`}}}},
		{Role: "tool", ToolCallID: "call_1", Name: "Bash", Content: "timeout"},
	}

	// Turn B (exact same payload, typical of a client retry on a 502/timeout from the API)
	turnBMsgs := []ChatMessage{
		{Role: "system", Content: systemPrompt},
		{Role: "user", Content: "run test"},
		{Role: "assistant", Content: "", ToolCalls: []ToolCall{{ID: "call_1", Function: ToolCallFunction{Name: "Bash", Arguments: `{"command":"go test ."}`}}}},
		{Role: "tool", ToolCallID: "call_1", Name: "Bash", Content: "timeout"},
	}

	fpA := computeSessionFingerprint(turnAMsgs)
	fpB := computeSessionFingerprint(turnBMsgs)

	if fpA != fpB {
		t.Errorf("fingerprint mismatch on retry: %s != %s", fpA, fpB)
	}

	countA := countNonSystemMessages(turnAMsgs)
	countB := countNonSystemMessages(turnBMsgs)

	if countA != countB {
		t.Errorf("message count mismatch on retry: %d != %d", countA, countB)
	}
}

// TestClaudeCodeAgentLoop_MultipleToolCallsContinuation validates rawMsgCount
// correctly identifies multiple tool calls and responses in a single turn.
func TestClaudeCodeAgentLoop_MultipleToolCallsContinuation(t *testing.T) {
	systemPrompt := "You are Claude Code..."

	msgs := []ChatMessage{
		{Role: "system", Content: systemPrompt},
		{Role: "user", Content: "read both files"},
		{Role: "assistant", Content: "", ToolCalls: []ToolCall{
			{ID: "call_1", Function: ToolCallFunction{Name: "Read", Arguments: `{"file_path":"a.go"}`}},
			{ID: "call_2", Function: ToolCallFunction{Name: "Read", Arguments: `{"file_path":"b.go"}`}},
		}},
		{Role: "tool", ToolCallID: "call_1", Name: "Read", Content: "content A"},
		{Role: "tool", ToolCallID: "call_2", Name: "Read", Content: "content B"},
	}

	rawMsgCount := countNonSystemMessages(msgs)

	// Expect 1 user message, 1 assistant message, 2 tool messages = 4
	if rawMsgCount != 4 {
		t.Errorf("expected 4 non-system messages, got %d", rawMsgCount)
	}

	followUp := buildSessionChainFollowUp(msgs, "Read", "")
	if len(followUp) != 1 {
		t.Fatalf("expected 1 follow up, got %d", len(followUp))
	}
	content := followUp[0].Content

	if !strings.Contains(content, "[Read]: content A") {
		t.Errorf("expected followUp to contain first tool result")
	}
	if !strings.Contains(content, "[Read]: content B") {
		t.Errorf("expected followUp to contain second tool result")
	}
}

// TestInjectToolsSessionVsLegacy verifies that injectToolsIntoMessages takes
// the session-based path when a session is provided, and the legacy collapse
// path when no session exists.
func TestInjectToolsSessionVsLegacy(t *testing.T) {
	// Build a chain continuation scenario with >5 tools (triggers useLargeToolSet)
	tools := []Tool{
		{Type: "function", Function: ToolFunction{Name: "Bash", Description: "Execute shell command", Parameters: map[string]interface{}{"type": "object"}}},
		{Type: "function", Function: ToolFunction{Name: "Read", Description: "Read a file", Parameters: map[string]interface{}{"type": "object"}}},
		{Type: "function", Function: ToolFunction{Name: "Write", Description: "Write a file", Parameters: map[string]interface{}{"type": "object"}}},
		{Type: "function", Function: ToolFunction{Name: "Edit", Description: "Edit a file", Parameters: map[string]interface{}{"type": "object"}}},
		{Type: "function", Function: ToolFunction{Name: "Glob", Description: "Find files", Parameters: map[string]interface{}{"type": "object"}}},
		{Type: "function", Function: ToolFunction{Name: "Grep", Description: "Search files", Parameters: map[string]interface{}{"type": "object"}}},
	}

	messages := []ChatMessage{
		{Role: "user", Content: "list all go files"},
		{Role: "assistant", Content: "", ToolCalls: []ToolCall{
			{ID: "call_1", Type: "function", Function: ToolCallFunction{Name: "Bash", Arguments: `{"command":"find . -name '*.go'"}`}},
		}},
		{Role: "tool", ToolCallID: "call_1", Name: "Bash", Content: "main.go\ntools.go\nserver.go"},
	}

	// With session: should use session-based follow-up (shorter, no original query)
	session := &Session{TurnCount: 1, RawMessageCount: 1}
	resultWithSession := injectToolsIntoMessages(messages, tools, "claude-sonnet-4-20250514", session)

	if len(resultWithSession) != 1 {
		t.Fatalf("session path: expected 1 message, got %d", len(resultWithSession))
	}
	if !strings.Contains(resultWithSession[0].Content, "Results from executed function") {
		t.Error("session path: expected 'Results from executed function' prefix")
	}
	if strings.Contains(resultWithSession[0].Content, "I'm writing a unit test") {
		t.Error("session path: should NOT contain unit test framing (context is in thread)")
	}

	// Without session: should use legacy collapse (includes original query + unit test framing)
	resultNoSession := injectToolsIntoMessages(messages, tools, "claude-sonnet-4-20250514", nil)

	if len(resultNoSession) != 1 {
		t.Fatalf("legacy path: expected 1 message, got %d", len(resultNoSession))
	}
	if !strings.Contains(resultNoSession[0].Content, "I'm writing a unit test") {
		t.Error("legacy path: expected 'unit test' framing")
	}
	if !strings.Contains(resultNoSession[0].Content, "list all go files") {
		t.Error("legacy path: expected original query in collapsed message")
	}
}

// TestClaudeCodeAgentLoop_MultiTurnReadEditTest verifies that a multi-turn
// agentic loop simulating Read -> Edit -> Test tools properly generates session follow-ups
// without including Notion persona leakage or losing intent.
func TestClaudeCodeAgentLoop_MultiTurnReadEditTest(t *testing.T) {
	messages := []ChatMessage{
		{Role: "user", Content: "Update the tests and verify."},
		{Role: "assistant", Content: "", ToolCalls: []ToolCall{
			{ID: "call_1", Type: "function", Function: ToolCallFunction{Name: "Read", Arguments: `{"file_path":"test.go"}`}},
		}},
		{Role: "tool", Name: "Read", ToolCallID: "call_1", Content: "func TestA() {}"},
		{Role: "assistant", Content: "", ToolCalls: []ToolCall{
			{ID: "call_2", Type: "function", Function: ToolCallFunction{Name: "Edit", Arguments: `{"file_path":"test.go","content":"func TestA() { } func TestB() { }"}`}},
		}},
		{Role: "tool", Name: "Edit", ToolCallID: "call_2", Content: "File updated"},
		{Role: "assistant", Content: "", ToolCalls: []ToolCall{
			{ID: "call_3", Type: "function", Function: ToolCallFunction{Name: "Bash", Arguments: `{"command":"go test ."}`}},
		}},
		{Role: "tool", Name: "Bash", ToolCallID: "call_3", Content: "PASS\nok  test.go\t0.001s"},
	}

	followUp := buildSessionChainFollowUp(messages, "Bash, Read, Edit", "")

	if len(followUp) != 1 {
		t.Fatalf("expected 1 follow-up message, got %d", len(followUp))
	}
	content := followUp[0].Content

	// Verify that only the latest tool result is included in the follow-up.
	if strings.Contains(content, "[Read]: func TestA()") {
		t.Errorf("follow-up should not contain earlier Read tool result")
	}
	if strings.Contains(content, "[Edit]: File updated") {
		t.Errorf("follow-up should not contain earlier Edit tool result")
	}
	if !strings.Contains(content, "[Bash]: PASS") {
		t.Errorf("follow-up should contain the latest Bash tool result, got: %s", content)
	}
	if strings.Contains(content, "Notion AI") {
		t.Errorf("follow-up should not contain Notion persona leakage")
	}
}

func TestClaudeCodeAgentLoop_ToolResultContinuationPreservesIntent(t *testing.T) {
	messages := []ChatMessage{
		{Role: "user", Content: "Find the bug in login and fix it."},
		{Role: "assistant", Content: "", ToolCalls: []ToolCall{
			{ID: "call_1", Type: "function", Function: ToolCallFunction{Name: "Grep", Arguments: `{"pattern":"login"}`}},
		}},
		{Role: "tool", Name: "Grep", ToolCallID: "call_1", Content: "login.go:10: func Login() {}"},
	}

	followUp := buildSessionChainFollowUp(messages, "Grep", "")
	if len(followUp) != 1 {
		t.Fatalf("expected 1 follow up")
	}

	content := followUp[0].Content
	// Must contain the original request
	if !strings.Contains(content, "Find the bug in login and fix it.") {
		t.Errorf("Continuation prompt did not preserve the original coding intent. Content: %s", content)
	}
}

func TestClaudeCodeAgentLoop_FinalAnswerAvoidsNotionPersona(t *testing.T) {
	messages := []ChatMessage{
		{Role: "user", Content: "Update tests, verify, and tell me when you are done."},
		{Role: "assistant", Content: "", ToolCalls: []ToolCall{
			{ID: "call_1", Type: "function", Function: ToolCallFunction{Name: "Read", Arguments: `{"file_path":"test.go"}`}},
		}},
		{Role: "tool", Name: "Read", ToolCallID: "call_1", Content: "func TestA() {}"},
		{Role: "assistant", Content: "", ToolCalls: []ToolCall{
			{ID: "call_2", Type: "function", Function: ToolCallFunction{Name: "Bash", Arguments: `{"command":"go test ."}`}},
		}},
		{Role: "tool", Name: "Bash", ToolCallID: "call_2", Content: "PASS"},
	}

	followUp := buildSessionChainFollowUp(messages, "Read, Bash", "")
	if len(followUp) != 1 {
		t.Fatalf("expected 1 follow up")
	}

	content := followUp[0].Content

	// Must have the `__done__` directive for final answers
	if !strings.Contains(content, "__done__") {
		t.Errorf("Continuation prompt did not include __done__ instructions. Content: %s", content)
	}

	// Must verify that the system is properly rejecting Notion persona
	// By asserting that our detect function correctly flags a fake bad response
	badResponse := `I am Notion AI, and I don't have access to your coding assistant. Therefore, I cannot run edit or bash to modify those files.`
	if !detectToolBridgeNoToolResponse(badResponse) {
		t.Errorf("detectToolBridgeNoToolResponse failed to catch a Notion persona final answer drift")
	}

	goodResponse := `{"name": "__done__", "arguments": {"result": "I have updated and verified the tests. They all pass."}}`
	if detectToolBridgeNoToolResponse(goodResponse) {
		t.Errorf("detectToolBridgeNoToolResponse incorrectly flagged a valid JSON final answer")
	}
}

// TestClaudeCodeAgentLoop_ToolResultContinuationComplexMarkdown validates
// that tool results containing complex markdown or extreme lengths are properly
// formatted and truncated if needed, without breaking continuation.
func TestClaudeCodeAgentLoop_ToolResultContinuationComplexMarkdown(t *testing.T) {
	longMarkdown := "# Title\n\n## Subtitle\n\n```json\n{\"key\": \"value\"}\n```\n\n" + strings.Repeat("A long paragraph. ", 500)

	messages := []ChatMessage{
		{Role: "user", Content: "Read the complex file."},
		{Role: "assistant", Content: "", ToolCalls: []ToolCall{
			{ID: "call_1", Type: "function", Function: ToolCallFunction{Name: "Read", Arguments: `{"file_path":"complex.md"}`}},
		}},
		{Role: "tool", Name: "Read", ToolCallID: "call_1", Content: longMarkdown},
	}

	followUp := buildSessionChainFollowUp(messages, "Read", "")
	if len(followUp) != 1 {
		t.Fatalf("expected 1 follow up")
	}

	content := followUp[0].Content

	if !strings.Contains(content, "[Read]: # Title") {
		t.Errorf("Expected markdown title to be present")
	}
	if !strings.Contains(content, "... (truncated)") {
		t.Errorf("Expected long tool output to be truncated")
	}
	if len(content) > 5000 {
		t.Errorf("Follow-up prompt should be constrained in size, got %d chars", len(content))
	}
}

// TestClaudeCodeAgentLoop_ToolResultContinuationInterleaved tests handling of
// interleaved text and tool calls within assistant messages.
func TestClaudeCodeAgentLoop_ToolResultContinuationInterleaved(t *testing.T) {
	messages := []ChatMessage{
		{Role: "user", Content: "Do a bunch of stuff."},
		{Role: "assistant", Content: "First, I'll search for the file.", ToolCalls: []ToolCall{
			{ID: "call_1", Type: "function", Function: ToolCallFunction{Name: "Grep", Arguments: `{"pattern":"foo"}`}},
		}},
		{Role: "tool", Name: "Grep", ToolCallID: "call_1", Content: "found foo in bar.go"},
		{Role: "assistant", Content: "Now I'll read it.", ToolCalls: []ToolCall{
			{ID: "call_2", Type: "function", Function: ToolCallFunction{Name: "Read", Arguments: `{"file_path":"bar.go"}`}},
		}},
		{Role: "tool", Name: "Read", ToolCallID: "call_2", Content: "content of bar.go"},
	}

	followUp := buildSessionChainFollowUp(messages, "Grep, Read", "")
	if len(followUp) != 1 {
		t.Fatalf("expected 1 follow up")
	}

	content := followUp[0].Content

	// Should only include the latest tool results after the last assistant message
	if strings.Contains(content, "[Grep]: found foo in bar.go") {
		t.Errorf("Follow-up should not contain earlier Grep tool result")
	}
	if !strings.Contains(content, "[Read]: content of bar.go") {
		t.Errorf("Follow-up should contain the latest Read tool result")
	}
}
