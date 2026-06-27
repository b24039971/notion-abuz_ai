package proxy

import (
	"encoding/json"
	"testing"
)

func TestParseToolCalls_NestedObjects(t *testing.T) {
	tests := []struct {
		name     string
		content  string
		wantName string
		wantArgs string
		wantBool bool
	}{
		{
			name: "xml format with nested object",
			content: `<tool_call>
{
  "name": "deep_nested_tool",
  "arguments": {"config": {"features": {"enable_x": true, "details": {"version": 1.2, "tags": ["a", "b"]}}}}
}
</tool_call>`,
			wantName: "deep_nested_tool",
			wantArgs: `{"config": {"features": {"enable_x": true, "details": {"version": 1.2, "tags": ["a", "b"]}}}}`,
			wantBool: true,
		},
		{
			name: "markdown fenced json with nested object",
			content: "Some text before\n```json\n" +
				`{"name": "complex_action", "arguments": {"user": {"profile": {"preferences": {"theme": "dark"}}}}}` +
				"\n```",
			wantName: "complex_action",
			wantArgs: `{"user": {"profile": {"preferences": {"theme": "dark"}}}}`,
			wantBool: true,
		},
		{
			name:     "direct json format with nested object",
			content:  `{"name": "direct_call", "arguments": {"data": {"nested": {"level3": {"level4": "value"}}}}}`,
			wantName: "direct_call",
			wantArgs: `{"data": {"nested": {"level3": {"level4": "value"}}}}`,
			wantBool: true,
		},
		{
			name:     "tool_call wrapper format with nested object",
			content:  `{"tool_call": {"name": "wrapper_call", "arguments": {"root": {"child": {"grandchild": null}}}}}`,
			wantName: "wrapper_call",
			wantArgs: `{"root": {"child": {"grandchild": null}}}`,
			wantBool: true,
		},
		{
			name:     "multi-line json format with nested objects",
			content:  `{"name": "call_1", "arguments": {"a": {"b": 1}}}` + "\n" + `{"name": "call_2", "arguments": {"x": {"y": {"z": true}}}}`,
			wantName: "call_1",
			wantArgs: `{"a": {"b": 1}}`,
			wantBool: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			calls, _, hasCalls := parseToolCalls(tt.content)
			if hasCalls != tt.wantBool {
				t.Fatalf("parseToolCalls() hasCalls = %v, want %v", hasCalls, tt.wantBool)
			}
			if tt.wantBool {
				if len(calls) == 0 {
					t.Fatalf("expected at least 1 tool call, got %d", len(calls))
				}
				if calls[0].Function.Name != tt.wantName {
					t.Errorf("expected name %q, got %q", tt.wantName, calls[0].Function.Name)
				}

				var gotArgs, wantArgs interface{}
				if err := json.Unmarshal([]byte(calls[0].Function.Arguments), &gotArgs); err != nil {
					t.Fatalf("failed to unmarshal got arguments: %v", err)
				}
				if err := json.Unmarshal([]byte(tt.wantArgs), &wantArgs); err != nil {
					t.Fatalf("failed to unmarshal want arguments: %v", err)
				}

				gotBytes, _ := json.Marshal(gotArgs)
				wantBytes, _ := json.Marshal(wantArgs)
				if string(gotBytes) != string(wantBytes) {
					t.Errorf("arguments mismatch.\ngot:  %s\nwant: %s", string(gotBytes), string(wantBytes))
				}

				if tt.name == "multi-line json format with nested objects" {
					if len(calls) < 2 {
						t.Fatalf("expected 2 tool calls, got %d", len(calls))
					}
					if calls[1].Function.Name != "call_2" {
						t.Errorf("expected name %q, got %q", "call_2", calls[1].Function.Name)
					}

					var gotArgs2, wantArgs2 interface{}
					_ = json.Unmarshal([]byte(calls[1].Function.Arguments), &gotArgs2)
					_ = json.Unmarshal([]byte(`{"x": {"y": {"z": true}}}`), &wantArgs2)

					gotBytes2, _ := json.Marshal(gotArgs2)
					wantBytes2, _ := json.Marshal(wantArgs2)
					if string(gotBytes2) != string(wantBytes2) {
						t.Errorf("arguments mismatch for second call.\ngot:  %s\nwant: %s", string(gotBytes2), string(wantBytes2))
					}
				}
			}
		})
	}
}

func TestIsCodingAssistantRequest(t *testing.T) {
	tests := []struct {
		name     string
		messages []ChatMessage
		want     bool
	}{
		{
			name: "claude code system message",
			messages: []ChatMessage{
				{Role: "system", Content: "You are Claude Code, Anthropic's official CLI."},
				{Role: "user", Content: "Fix the bug."},
			},
			want: true,
		},
		{
			name: "cursor system message",
			messages: []ChatMessage{
				{Role: "system", Content: "You are an expert software engineer using Cursor."},
			},
			want: true,
		},
		{
			name: "developer message with coding words",
			messages: []ChatMessage{
				{Role: "developer", Content: "Please write some tests for this repository."},
			},
			want: true,
		},
		{
			name: "normal request",
			messages: []ChatMessage{
				{Role: "system", Content: "You are a helpful assistant."},
				{Role: "user", Content: "What is the capital of France?"},
			},
			want: false,
		},
		{
			name:     "empty messages",
			messages: []ChatMessage{},
			want:     false,
		},
		{
			name: "coding words in user message only",
			messages: []ChatMessage{
				{Role: "system", Content: "You are a helpful assistant."},
				{Role: "user", Content: "Help me write some tests using Claude Code."},
			},
			want: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := isCodingAssistantRequest(tt.messages); got != tt.want {
				t.Errorf("isCodingAssistantRequest() = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestInjectCodingAssistantInstruction(t *testing.T) {
	messages := []ChatMessage{
		{Role: "user", Content: "Hello"},
	}

	injected := injectCodingAssistantInstruction(messages)
	if len(injected) != 2 {
		t.Fatalf("expected 2 messages, got %d", len(injected))
	}
	if injected[0].Role != "system" {
		t.Errorf("expected first message role to be system, got %s", injected[0].Role)
	}
	if injected[0].Content != "You are acting as a coding assistant API behind a compatibility proxy. Follow the user's coding instructions directly. Do not answer as Notion AI, and do not refer to Notion pages, workspaces, or documents unless the user explicitly asks about Notion." {
		t.Errorf("unexpected instruction content: %s", injected[0].Content)
	}
	if injected[1].Role != "user" || injected[1].Content != "Hello" {
		t.Errorf("expected original message to be preserved")
	}
}

func TestStripSystemReminders_PreservesCodingIntent(t *testing.T) {
	tests := []struct {
		name  string
		input string
		want  string
	}{
		{
			name:  "preserves command name intent",
			input: "Run <command-name>/test</command-name> to verify.",
			want:  "Run /test to verify.",
		},
		{
			name:  "preserves file intent",
			input: "Look at <file>src/main.go</file>",
			want:  "Look at src/main.go",
		},
		{
			name:  "strips blocks but keeps inline",
			input: "Here is a <local-command-caveat>DO NOT respond</local-command-caveat> rule. Use <package>gin</package>.",
			want:  "Here is a  rule. Use gin.",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := stripSystemReminders(tt.input)
			if got != tt.want {
				t.Errorf("stripSystemReminders() = %q, want %q", got, tt.want)
			}
		})
	}
}
