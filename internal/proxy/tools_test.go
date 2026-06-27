package proxy

import (
	"encoding/json"
	"strings"
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

func TestStripClaudeCodeInstructions_PreservesCodingIntent(t *testing.T) {
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
		{
			name:  "preserves MCP server tags with attributes",
			input: "Context from <mcp-server name=\"github\">Provides github tools</mcp-server> for PRs.",
			want:  "Context from Provides github tools for PRs.",
		},
		{
			name:  "preserves project instructions (CLAUDE.md)",
			input: "Follow these <project-instructions>Use tabs instead of spaces</project-instructions>.",
			want:  "Follow these Use tabs instead of spaces.",
		},
		{
			name:  "preserves hook-style reminders",
			input: "Remember: <hook-reminder>run tests after edits</hook-reminder>.",
			want:  "Remember: run tests after edits.",
		},
		{
			name:  "preserves subagent-style prompts",
			input: "Task context: <subagent-task id=\"123\">Fix the auth bug</subagent-task>",
			want:  "Task context: Fix the auth bug",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := stripClaudeCodeInstructions(tt.input)
			if got != tt.want {
				t.Errorf("stripClaudeCodeInstructions() = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestParseToolCalls_RobustJSONExtraction(t *testing.T) {
	tests := []struct {
		name      string
		content   string
		wantCalls int
		wantRem   string
		wantHas   bool
	}{
		{
			name:      "conversational preamble with __done__ tool call",
			content:   "Here is the requested tool call:\n{\"name\": \"__done__\", \"arguments\": {\"result\": \"Done!\"}}",
			wantCalls: 1,
			wantRem:   "Here is the requested tool call:",
			wantHas:   true,
		},
		{
			name:      "postamble text",
			content:   "{\"name\": \"Bash\", \"arguments\": {\"command\": \"ls\"}}\nI hope this helps!",
			wantCalls: 1,
			wantRem:   "I hope this helps!",
			wantHas:   true,
		},
		{
			name:      "multiple tool calls",
			content:   "{\"name\": \"Read\", \"arguments\": {\"path\": \"main.go\"}}\n{\"name\": \"Bash\", \"arguments\": {\"command\": \"cat main.go\"}}",
			wantCalls: 2,
			wantRem:   "",
			wantHas:   true,
		},
		{
			name:      "json wrapper format",
			content:   "{\"tool_call\": {\"name\": \"Edit\", \"arguments\": {\"path\": \"main.go\"}}}",
			wantCalls: 1,
			wantRem:   "",
			wantHas:   true,
		},
		{
			name:      "no valid tool calls",
			content:   "Just a regular message with no json tools.",
			wantCalls: 0,
			wantRem:   "Just a regular message with no json tools.",
			wantHas:   false,
		},
		{
			name:      "tool call with empty function name",
			content:   `{"name": "", "arguments": {"foo": "bar"}}`,
			wantCalls: 0,
			wantRem:   `{"name": "", "arguments": {"foo": "bar"}}`,
			wantHas:   false,
		},
		{
			name:      "tool call wrapper with empty function name",
			content:   `{"tool_call": {"name": "", "arguments": {"foo": "bar"}}}`,
			wantCalls: 0,
			wantRem:   `{"tool_call": {"name": "", "arguments": {"foo": "bar"}}}`,
			wantHas:   false,
		},
		{
			name:      "refusal prose with identity drift",
			content:   `I am Notion AI, and I don't have access to your coding assistant. Therefore, I cannot run edit or bash to modify those files.`,
			wantCalls: 0,
			wantRem:   `I am Notion AI, and I don't have access to your coding assistant. Therefore, I cannot run edit or bash to modify those files.`,
			wantHas:   false,
		},
		{
			name:      "refusal prose with workspace reframing",
			content:   `I cannot run bash commands to modify files. However, I can help you create a Notion page or search the Notion workspace.`,
			wantCalls: 0,
			wantRem:   `I cannot run bash commands to modify files. However, I can help you create a Notion page or search the Notion workspace.`,
			wantHas:   false,
		},
		{
			name:      "refusal prose with identity handoff in chinese",
			content:   "抱歉，我理解你希望我直接帮你修改文件，但**我是 Notion AI，无法访问你的本地文件系统**。我没有 Read、Edit、Bash 这些工具的能力。\n\n把下面这段话直接发给你的编码助手（Cursor / Claude Code），它就能帮你操作。",
			wantCalls: 0,
			wantRem:   "抱歉，我理解你希望我直接帮你修改文件，但**我是 Notion AI，无法访问你的本地文件系统**。我没有 Read、Edit、Bash 这些工具的能力。\n\n把下面这段话直接发给你的编码助手（Cursor / Claude Code），它就能帮你操作。",
			wantHas:   false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			calls, rem, hasCalls := parseToolCalls(tt.content)
			if hasCalls != tt.wantHas {
				t.Fatalf("hasCalls = %v, want %v", hasCalls, tt.wantHas)
			}
			if len(calls) != tt.wantCalls {
				t.Fatalf("len(calls) = %v, want %v", len(calls), tt.wantCalls)
			}
			if rem != tt.wantRem {
				t.Errorf("remaining = %q, want %q", rem, tt.wantRem)
			}
		})
	}
}

// We want to test parseToolCallJSON which is an internal function in package proxy
func TestParseToolCallJSON_WrapperFormats(t *testing.T) {
	tests := []struct {
		name     string
		jsonStr  string
		wantNil  bool
		wantName string
		wantArgs string
	}{
		{
			name:     "standard flat format",
			jsonStr:  `{"name": "test_func", "arguments": {"a": 1}}`,
			wantNil:  false,
			wantName: "test_func",
			wantArgs: `{"a": 1}`,
		},
		{
			name:     "wrapper format",
			jsonStr:  `{"tool_call": {"name": "test_wrapper", "arguments": {"b": 2}}}`,
			wantNil:  false,
			wantName: "test_wrapper",
			wantArgs: `{"b": 2}`,
		},
		{
			name:    "invalid json",
			jsonStr: `{"name": "test", "arguments":`,
			wantNil: true,
		},
		{
			name:     "wrapper format with null tool_call",
			jsonStr:  `{"tool_call": null}`,
			wantNil:  false,
			wantName: "",
			wantArgs: `{}`,
		},
		{
			name:     "wrapper format with array tool_call",
			jsonStr:  `{"tool_call": [{"name": "test_wrapper", "arguments": {"b": 2}}]}`,
			wantNil:  false,
			wantName: "",
			wantArgs: `{}`,
		},
		{
			name:     "wrapper format string instead of object",
			jsonStr:  `{"tool_call": "string not object"}`,
			wantNil:  false,
			wantName: "",
			wantArgs: `{}`,
		},
		{
			name:     "deeply nested unknown wrapper",
			jsonStr:  `{"some_other_key": {"name": "not_extracted", "arguments": {}}}`,
			wantNil:  false,
			wantName: "",
			wantArgs: `{}`,
		},
		{
			name:     "wrapper format with empty arguments",
			jsonStr:  `{"tool_call": {"name": "test_empty_args", "arguments": {}}}`,
			wantNil:  false,
			wantName: "test_empty_args",
			wantArgs: `{}`,
		},
		{
			name:     "wrapper format with invalid string arguments (should be json.RawMessage)",
			jsonStr:  `{"tool_call": {"name": "test_invalid_args", "arguments": "invalid string not json"}}`,
			wantNil:  false,
			wantName: "test_invalid_args",
			wantArgs: `{}`,
		},
		{
			name:     "missing function name in flat format",
			jsonStr:  `{"name": "", "arguments": {"foo": "bar"}}`,
			wantNil:  false,
			wantName: "",
			wantArgs: `{"foo": "bar"}`,
		},
		{
			name:     "missing function name in wrapper format",
			jsonStr:  `{"tool_call": {"name": "", "arguments": {"foo": "bar"}}}`,
			wantNil:  false,
			wantName: "",
			wantArgs: `{}`,
		},
		{
			name:    "malformed json - missing quotes on keys",
			jsonStr: `{name: "test_missing_quotes", arguments: {}}`,
			wantNil: true,
		},
		{
			name:    "malformed json - improperly escaped character",
			jsonStr: `{"name": "test_escape", "arguments": {"path": "C:\Program Files"}}`,
			wantNil: true,
		},
		{
			name:    "malformed json - wrapper missing quotes on keys",
			jsonStr: `{tool_call: {"name": "test", "arguments": {}}}`,
			wantNil: true,
		},
		{
			name:    "malformed json - wrapper improperly escaped character",
			jsonStr: `{"tool_call": {"name": "test", "arguments": {"text": "hello \x00 world"}}}`,
			wantNil: true,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			res := parseToolCallJSON(tt.jsonStr, 0)
			if tt.wantNil {
				if res != nil {
					t.Errorf("expected nil result, got %+v", res)
				}
				return
			}
			if res == nil {
				t.Fatalf("expected non-nil result")
			}
			if res.Function.Name != tt.wantName {
				t.Errorf("expected name %q, got %q", tt.wantName, res.Function.Name)
			}

			// To compare args ignoring space
			var gotArgs, wantArgs interface{}
			_ = json.Unmarshal([]byte(res.Function.Arguments), &gotArgs)
			_ = json.Unmarshal([]byte(tt.wantArgs), &wantArgs)

			gb, _ := json.Marshal(gotArgs)
			wb, _ := json.Marshal(wantArgs)
			if string(gb) != string(wb) {
				t.Errorf("expected args %q, got %q (unmarshalled %s != %s)", tt.wantArgs, res.Function.Arguments, string(gb), string(wb))
			}
		})
	}
}

func TestBuildToolsBlocks_EmptySchemaFallback(t *testing.T) {
	tools := []Tool{
		{
			Type: "function",
			Function: ToolFunction{
				Name:        "test_tool",
				Description: "A tool with no schema",
				Parameters:  nil,
			},
		},
	}

	anthropicBlock := buildAnthropicToolsBlock(tools)
	openAIBlock := buildOpenAIToolsBlock(tools)
	geminiBlock := buildGeminiToolsBlock(tools)

	expectedSchemaSnippet := `"type": "object"`
	expectedPropsSnippet := `"properties": {}`

	if !strings.Contains(anthropicBlock, expectedSchemaSnippet) || !strings.Contains(anthropicBlock, expectedPropsSnippet) {
		t.Errorf("buildAnthropicToolsBlock missing empty schema fallback:\n%s", anthropicBlock)
	}

	if !strings.Contains(openAIBlock, expectedSchemaSnippet) || !strings.Contains(openAIBlock, expectedPropsSnippet) {
		t.Errorf("buildOpenAIToolsBlock missing empty schema fallback:\n%s", openAIBlock)
	}

	if !strings.Contains(geminiBlock, expectedSchemaSnippet) || !strings.Contains(geminiBlock, expectedPropsSnippet) {
		t.Errorf("buildGeminiToolsBlock missing empty schema fallback:\n%s", geminiBlock)
	}
}
