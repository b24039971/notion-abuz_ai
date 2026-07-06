package proxy

import (
	"bytes"
	"encoding/json"
	"log"
	"strings"
	"testing"
	"unicode/utf8"
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
		{
			name:  "preserves nested and attributed XML tags",
			input: "Payload: <mcp-server name=\"github\">{\"status\": \"ok\", \"nested\": <inner>content</inner>}</mcp-server>",
			want:  "Payload: {\"status\": \"ok\", \"nested\": content}",
		},
		{
			name:  "strips multiline system reminders with other tags",
			input: "Start <system-reminder>\nline 1\nline 2\n<nested>foo</nested>\n</system-reminder>\nkeep this <other>tag</other>",
			want:  "Start \nkeep this tag",
		},
		{
			name:  "preserves hook-style messages",
			input: "Hook: <hook name=\"pre-commit\">Please check code</hook>",
			want:  "Hook: Please check code",
		},
		{
			name:  "preserves slash command intent",
			input: "Run <command name=\"/test\">/test --all</command>",
			want:  "Run /test --all",
		},
		{
			name:  "preserves user HTML intent",
			input: "Fix the `<button class=\"test\">` tag rendering.",
			want:  "Fix the `<button class=\"test\">` tag rendering.",
		},
		{
			name:  "preserves comparison operators",
			input: "Ensure `if x < y { ... }` is evaluated.",
			want:  "Ensure `if x < y { ... }` is evaluated.",
		},
		{
			name:  "strips plain subagent tag",
			input: "Here is context <subagent>write a test</subagent>",
			want:  "Here is context write a test",
		},
		{
			name:  "cleans up extra newlines left by tag removal",
			input: "Hello\n\n<system-reminder>\nsome internal logic\n</system-reminder>\n\n\nWorld",
			want:  "Hello\n\nWorld",
		},
		{
			name:  "cleans up extra newlines at string boundaries",
			input: "\n\n\n\nStart\n\n\n\nEnd\n\n\n\n",
			want:  "Start\n\nEnd",
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
			name:      "array format within tool_call xml",
			content:   "<tool_call>[{\"name\": \"Bash\", \"arguments\": {\"command\": \"ls\"}}]</tool_call>",
			wantCalls: 1,
			wantRem:   "",
			wantHas:   true,
		},
		{
			name:      "array format with multiple calls in xml",
			content:   "<tool_call>[{\"name\": \"Bash\", \"arguments\": {\"command\": \"ls\"}}, {\"name\": \"Read\", \"arguments\": {\"path\": \"main.go\"}}]</tool_call>",
			wantCalls: 2,
			wantRem:   "",
			wantHas:   true,
		},
		{
			name:      "array format within markdown fences",
			content:   "Here are the tools:\n```json\n[{\"name\": \"Bash\", \"arguments\": {\"command\": \"ls\"}}]\n```",
			wantCalls: 1,
			wantRem:   "Here are the tools:",
			wantHas:   true,
		},
		{
			name:      "Unfenced JSON object format",
			content:   "Here is the object you requested: {\"name\": \"my_tool_obj\", \"arguments\": {\"foo\": \"baz\"}}",
			wantCalls: 1,
			wantRem:   "Here is the object you requested:",
			wantHas:   true,
		},
		{
			name:      "Unfenced JSON array format",
			content:   "Here is the tool call you requested: [{\"name\": \"my_tool\", \"arguments\": {\"foo\": \"bar\"}}]",
			wantCalls: 1,
			wantRem:   "Here is the tool call you requested:",
			wantHas:   true,
		},
		{
			name:      "Unfenced malformed arguments",
			content:   "Here is the tool call you requested: [{\"name\": \"my_tool\", \"arguments\": 123}]",
			wantCalls: 1,
			wantRem:   "Here is the tool call you requested:",
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
		{
			name:      "JSON array mode multi-call",
			content:   "Here are the tools:\n[{\"name\": \"Bash\", \"arguments\": {\"command\": \"ls\"}}, {\"name\": \"Bash\", \"arguments\": {\"command\": \"echo\"}}]",
			wantCalls: 2,
			wantRem:   "Here are the tools:",
			wantHas:   true,
		},
		{
			name:      "Markdown fence array mode",
			content:   "```json\n[\n  {\"name\": \"bash\", \"arguments\": {}}\n]\n```\nSome text.",
			wantCalls: 1,
			wantRem:   "Some text.",
			wantHas:   true,
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

	t.Run("Exactly801RunesUnicode", func(t *testing.T) {
		toolModeLossMetricsMu.Lock()
		toolModeLossMetrics = make(map[string]int)
		toolModeLossMetricsMu.Unlock()

		// "абвгд" is 5 runes. Repeat 159 times (795 runes) + "абвг" (4 runes) = 799 runes.
		// Enclose in "{...}" -> 799 + 2 = 801 runes.
		innerStr := strings.Repeat("абвгд", 159) + "абвг"
		content := "{" + innerStr + "}"

		parseToolCalls(content)

		toolModeLossMetricsMu.Lock()
		count, exists := toolModeLossMetrics["unparseable_json_candidate_truncated"]
		toolModeLossMetricsMu.Unlock()

		if !exists || count != 1 {
			t.Fatalf("Expected metric unparseable_json_candidate_truncated to be 1 and exist, got %d (exists: %v)", count, exists)
		}
	})
}

func TestParseToolCalls_JSON_Array_AdvancedEdgeCases(t *testing.T) {
	tests := []struct {
		name      string
		content   string
		wantCalls int
		wantNames []string
		wantArgs  []string
	}{
		{
			name:      "deeply nested json strings in array",
			content:   "I'll use the tools now.\n```json\n[\n  {\n    \"name\": \"Write\",\n    \"arguments\": {\n      \"path\": \"test.json\",\n      \"content\": \"{\\\"nested\\\": {\\\"array\\\": [1, 2, 3], \\\"string\\\": \\\"value\\\"}}\"\n    }\n  },\n  {\n    \"name\": \"Bash\",\n    \"arguments\": {\n      \"command\": \"cat test.json | jq '.nested.string'\"\n    }\n  }\n]\n```\nHope this works!",
			wantCalls: 2,
			wantNames: []string{"Write", "Bash"},
			wantArgs: []string{
				`{"path": "test.json", "content": "{\"nested\": {\"array\": [1, 2, 3], \"string\": \"value\"}}"}`,
				`{"command": "cat test.json | jq '.nested.string'"}`,
			},
		},
		{
			name:      "unfenced array with nested structures",
			content:   "Executing:\n[\n  {\"name\": \"Edit\", \"arguments\": {\"path\": \"main.go\", \"diff\": \"{\\\"old\\\": \\\"func A()\\\", \\\"new\\\": \\\"func B()\\\"}\"}}\n]",
			wantCalls: 1,
			wantNames: []string{"Edit"},
			wantArgs: []string{
				`{"path": "main.go", "diff": "{\"old\": \"func A()\", \"new\": \"func B()\"}"}`,
			},
		},
		{
			name:      "array of tool wrapper objects (tool_calls field)",
			content:   "I lost my JSON mode, but here are my calls:\n```json\n[{\"tool_calls\": [{\"name\": \"Write\", \"arguments\": {\"path\": \"foo.txt\"}}, {\"name\": \"Bash\", \"arguments\": {\"command\": \"ls\"}}]}]\n```",
			wantCalls: 2,
			wantNames: []string{"Write", "Bash"},
			wantArgs: []string{
				`{"path": "foo.txt"}`,
				`{"command": "ls"}`,
			},
		},
		{
			name:      "single tool wrapper object with array of calls (tool_calls field)",
			content:   "Here is what I want to do:\n```json\n{\"tool_calls\": [{\"name\": \"Write\", \"arguments\": {\"path\": \"bar.txt\"}}, {\"name\": \"Bash\", \"arguments\": {\"command\": \"pwd\"}}]}\n```",
			wantCalls: 2,
			wantNames: []string{"Write", "Bash"},
			wantArgs: []string{
				`{"path": "bar.txt"}`,
				`{"command": "pwd"}`,
			},
		},
		{
			name:      "array with malformed elements safely skipped",
			content:   "Here is my response:\n```json\n[\n  {\"name\": \"Write\", \"arguments\": {\"path\": \"ok.txt\"}},\n  {\"tool_call\": \"invalid string because model lost mode\"},\n  {\"tool_call\": {\"name\": \"Bash\", \"arguments\": {\"command\": \"ls\"}}},\n  {\"broken\": }\n]\n```",
			wantCalls: 2,
			wantNames: []string{"Write", "Bash"},
			wantArgs: []string{
				`{"path": "ok.txt"}`,
				`{"command": "ls"}`,
			},
		},
		{
			name:      "wrapper array with malformed string skipped",
			content:   "Here is the array:\n```json\n{\"tool_calls\": [\n  {\"name\": \"Read\", \"arguments\": {\"path\": \"a.txt\"}},\n  \"a malformed hallucinated string instead of an object\",\n  {\"name\": \"Grep\", \"arguments\": {\"pattern\": \"foo\"}}\n]}\n```",
			wantCalls: 2,
			wantNames: []string{"Read", "Grep"},
			wantArgs: []string{
				`{"path": "a.txt"}`,
				`{"pattern": "foo"}`,
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			calls, _, hasCalls := parseToolCalls(tt.content)
			if !hasCalls && tt.wantCalls > 0 {
				t.Fatalf("expected tool calls, got none")
			}
			if len(calls) != tt.wantCalls {
				t.Fatalf("expected %d tool calls, got %d", tt.wantCalls, len(calls))
			}
			for i, name := range tt.wantNames {
				if calls[i].Function.Name != name {
					t.Errorf("call %d: expected name %s, got %s", i, name, calls[i].Function.Name)
				}
				var gotArgs, wantArgs map[string]interface{}
				_ = json.Unmarshal([]byte(calls[i].Function.Arguments), &gotArgs)
				_ = json.Unmarshal([]byte(tt.wantArgs[i]), &wantArgs)

				gb, _ := json.Marshal(gotArgs)
				wb, _ := json.Marshal(wantArgs)
				if string(gb) != string(wb) {
					t.Errorf("call %d: expected args %s, got %s", i, string(wb), string(gb))
				}
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
			name:     "array tool call wrapper",
			jsonStr:  `[{"name": "test_wrapper_array", "arguments": {"b": 2}}]`,
			wantNil:  false,
			wantName: "test_wrapper_array",
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
			wantName: "test_wrapper",
			wantArgs: `{"b": 2}`,
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
			name:    "malformed json - missing quotes on arguments inner keys",
			jsonStr: `{"name": "test", "arguments": {path: "main.go"}}`,
			wantNil: true,
		},
		{
			name:    "malformed json - improperly escaped character",
			jsonStr: `{"name": "test_escape", "arguments": {"path": "C:\Program Files"}}`,
			wantNil: true,
		},
		{
			name:    "malformed json - incomplete unicode escape",
			jsonStr: `{"name": "test_unicode", "arguments": {"text": "\u12"}}`,
			wantNil: true,
		},
		{
			name:    "malformed json - unescaped tab character",
			jsonStr: "{\"name\": \"test_tab\", \"arguments\": {\"text\": \"hello\tworld\"}}",
			wantNil: true,
		},
		{
			name:    "malformed json - wrapper missing quotes on keys",
			jsonStr: `{tool_call: {"name": "test", "arguments": {}}}`,
			wantNil: true,
		},
		{
			name:    "malformed json - wrapper missing quotes on inner keys",
			jsonStr: `{"tool_call": {name: "test", arguments: {}}}`,
			wantNil: true,
		},
		{
			name:    "malformed json - wrapper missing quotes on arguments inner keys",
			jsonStr: `{"tool_call": {"name": "test", "arguments": {path: "main.go"}}}`,
			wantNil: true,
		},
		{
			name:    "malformed json - wrapper improperly escaped character",
			jsonStr: `{"tool_call": {"name": "test", "arguments": {"text": "hello \x00 world"}}}`,
			wantNil: true,
		},
		{
			name:    "malformed json - wrapper incomplete unicode escape",
			jsonStr: `{"tool_call": {"name": "test", "arguments": {"text": "\u12"}}}`,
			wantNil: true,
		},
		{
			name:    "malformed json - missing colons",
			jsonStr: `{"name" "test", "arguments" {}}`,
			wantNil: true,
		},
		{
			name:    "malformed json - wrapper missing colons",
			jsonStr: `{"tool_call": {"name" "test", "arguments" {}}}`,
			wantNil: true,
		},
		{
			name:     "flat format with empty string arguments",
			jsonStr:  `{"name": "test_empty_str_args", "arguments": ""}`,
			wantNil:  false,
			wantName: "test_empty_str_args",
			wantArgs: `{}`,
		},
		{
			name:     "flat format with invalid json string arguments",
			jsonStr:  `{"name": "test_invalid_json_args", "arguments": "{bad json}"}`,
			wantNil:  false,
			wantName: "test_invalid_json_args",
			wantArgs: `{}`,
		},
		{
			name:     "wrapper format with empty string arguments",
			jsonStr:  `{"tool_call": {"name": "test_wrapper_empty_str_args", "arguments": ""}}`,
			wantNil:  false,
			wantName: "test_wrapper_empty_str_args",
			wantArgs: `{}`,
		},
		{
			name:     "wrapper format with invalid json string arguments",
			jsonStr:  `{"tool_call": {"name": "test_wrapper_invalid_json_args", "arguments": "{bad json}"}}`,
			wantNil:  false,
			wantName: "test_wrapper_invalid_json_args",
			wantArgs: `{}`,
		},
		{
			name:    "malformed json - completely garbage string",
			jsonStr: `not even close to json`,
			wantNil: true,
		},
		{
			name:    "malformed json - missing opening brace",
			jsonStr: `"name": "test", "arguments": {}}`,
			wantNil: true,
		},
		{
			name:    "malformed json - missing closing brace",
			jsonStr: `{"name": "test", "arguments": {}`,
			wantNil: true,
		},
		{
			name:    "malformed json - missing colon",
			jsonStr: `{"name" "test", "arguments": {}}`,
			wantNil: true,
		},
		{
			name:    "malformed json - trailing comma",
			jsonStr: `{"name": "test", "arguments": {},}`,
			wantNil: true,
		},
		{
			name:    "malformed json - trailing comma in arguments object",
			jsonStr: `{"name": "test", "arguments": {"a": 1,}}`,
			wantNil: true,
		},
		{
			name:    "malformed json - trailing comma in wrapper",
			jsonStr: `{"tool_call": {"name": "test", "arguments": {}},}`,
			wantNil: true,
		},
		{
			name:    "malformed json - trailing comma in array",
			jsonStr: `{"name": "test", "arguments": {"a": [1, 2,]}}`,
			wantNil: true,
		},
		{
			name:    "malformed json - invalid type for name (number)",
			jsonStr: `{"name": 123, "arguments": {}}`,
			wantNil: true,
		},
		{
			name:     "malformed json - invalid type for arguments (number instead of string/object)",
			jsonStr:  `{"name": "test", "arguments": 123}`,
			wantNil:  false,
			wantName: "test",
			wantArgs: "{}", // Will fallback because 123 is not valid map[string]interface{}
		},
		{
			name:    "malformed json - wrapper missing closing brace",
			jsonStr: `{"tool_call": {"name": "test", "arguments": {}}`,
			wantNil: true,
		},
		{
			name:    "malformed json - garbage after valid JSON",
			jsonStr: `{"name": "test", "arguments": {}} garbage data`,
			wantNil: true,
		},
		{
			name:    "malformed json - unescaped literal newline in arguments",
			jsonStr: "{\"name\": \"test\", \"arguments\": {\"text\": \"line1\nline2\"}}", // literal newline in string
			wantNil: true,
		},
		{
			name:    "malformed json - single quotes instead of double quotes",
			jsonStr: `{'name': 'test', 'arguments': {}}`,
			wantNil: true,
		},
		{
			name:    "malformed json - wrapper single quotes instead of double quotes",
			jsonStr: `{'tool_call': {'name': 'test', 'arguments': {}}}`,
			wantNil: true,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			res := parseToolCallJSONList(tt.jsonStr, 0)
			if tt.wantNil {
				if res != nil {
					t.Errorf("expected nil result, got %+v", res)
				}
				return
			}
			if len(res) == 0 {
				t.Fatalf("expected non-nil result")
			}
			if res[0].Function.Name != tt.wantName {
				t.Errorf("expected name %q, got %q", tt.wantName, res[0].Function.Name)
			}

			// To compare args ignoring space
			var gotArgs, wantArgs interface{}
			_ = json.Unmarshal([]byte(res[0].Function.Arguments), &gotArgs)
			_ = json.Unmarshal([]byte(tt.wantArgs), &wantArgs)

			gb, _ := json.Marshal(gotArgs)
			wb, _ := json.Marshal(wantArgs)
			if string(gb) != string(wb) {
				t.Errorf("expected args %q, got %q (unmarshalled %s != %s)", tt.wantArgs, res[0].Function.Arguments, string(gb), string(wb))
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

func TestSimplifyToolSchema(t *testing.T) {
	// A bloated schema resembling what Claude Code might generate
	inputJSON := `{
		"type": "object",
		"title": "BloatedSchema",
		"$schema": "http://json-schema.org/draft-07/schema#",
		"properties": {
			"command": {
				"type": "string",
				"title": "CommandToRun",
				"examples": ["ls -la", "echo hello"],
				"default": "ls",
				"description": "This is a very long description that goes on and on and on and on and on and on and on and on and on and on and on and on and on and on and on and on and on and on and on and on and on and on and on. And it just keeps going to exceed the two hundred character limit set by the simplification algorithm to ensure truncation works exactly as intended."
			},
			"options": {
				"type": "object",
				"properties": {
					"timeout": {
						"type": "integer",
						"description": "Timeout in seconds 🚀"
					}
				}
			}
		},
		"required": ["command"]
	}`

	var rawSchema interface{}
	if err := json.Unmarshal([]byte(inputJSON), &rawSchema); err != nil {
		t.Fatalf("failed to unmarshal input json: %v", err)
	}

	simplified := simplifyToolSchema(rawSchema)

	// Convert back to map to verify structure
	simplifiedMap, ok := simplified.(map[string]interface{})
	if !ok {
		t.Fatalf("simplified schema is not a map, got %T", simplified)
	}

	// Verify bloat is removed
	if _, exists := simplifiedMap["title"]; exists {
		t.Errorf("expected 'title' to be stripped")
	}
	if _, exists := simplifiedMap["$schema"]; exists {
		t.Errorf("expected '$schema' to be stripped")
	}

	// Verify required structures are kept
	props, ok := simplifiedMap["properties"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected 'properties' to remain a map")
	}

	commandProp, ok := props["command"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected 'command' property to remain a map")
	}

	// Verify nested bloat is removed
	if _, exists := commandProp["title"]; exists {
		t.Errorf("expected nested 'title' to be stripped")
	}
	if _, exists := commandProp["examples"]; exists {
		t.Errorf("expected nested 'examples' to be stripped")
	}
	if _, exists := commandProp["default"]; exists {
		t.Errorf("expected nested 'default' to be stripped")
	}

	// Verify description truncation
	desc, ok := commandProp["description"].(string)
	if !ok {
		t.Fatalf("expected 'description' to be a string")
	}
	if len([]rune(desc)) > 200 {
		t.Errorf("expected description to be truncated to <= 200 runes, got %d runes", len([]rune(desc)))
	}
	if !strings.HasSuffix(desc, "...") {
		t.Errorf("expected description to have suffix '...'")
	}

	// Verify shorter descriptions are untouched and emojis survive
	optionsProp, ok := props["options"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected 'options' property to remain a map")
	}
	optionsProps, ok := optionsProp["properties"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected 'options.properties' to remain a map")
	}
	timeoutProp, ok := optionsProps["timeout"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected 'timeout' property to remain a map")
	}
	timeoutDesc, _ := timeoutProp["description"].(string)
	if timeoutDesc != "Timeout in seconds 🚀" {
		t.Errorf("expected short description with emoji to be intact, got: %q", timeoutDesc)
	}
}

func TestSimplifyToolSchemaObservability(t *testing.T) {
	contextLossMetricsMu.Lock()
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()
	// Add test coverage for diagnostic log when truncating long descriptions
	inputJSON := `{
		"type": "object",
		"properties": {
			"command": {
				"type": "string",
				"description": "This is a very long description that goes on and on and on and on and on and on and on and on and on and on and on and on and on and on and on and on and on and on and on and on and on and on and on. And it just keeps going to exceed the two hundred character limit set by the simplification algorithm to ensure truncation works exactly as intended."
			}
		}
	}`

	var rawSchema interface{}
	if err := json.Unmarshal([]byte(inputJSON), &rawSchema); err != nil {
		t.Fatalf("failed to unmarshal input json: %v", err)
	}

	var buf strings.Builder

	// Because log output can be overwritten by other tests in parallel,
	// we explicitly override standard log output for this test temporarily.
	// But in proxy tests, memory says:
	// "intercept the internal globalLogWriter.out instead of using log.SetOutput"
	// Let's actually do both because another test might have reset log.SetOutput.
	originalLogOutput := log.Writer()
	log.SetOutput(&buf)

	originalWriter := globalLogWriter.out
	globalLogWriter.out = &buf
	defer func() {
		globalLogWriter.out = originalWriter
		log.SetOutput(originalLogOutput)
	}()

	simplifyToolSchema(rawSchema)

	logOutput := buf.String()
	if !strings.Contains(logOutput, "[bridge] diagnostics: simplifyToolSchema truncated large description to prevent token bloat") {
		t.Errorf("expected diagnostic log for schema truncation, got: %q", logOutput)
	}

	contextLossMetricsMu.Lock()
	count, exists := contextLossMetrics["tool_schema_truncated"]
	contextLossMetricsMu.Unlock()

	if !exists || count != 1 {
		t.Errorf("expected metric 'tool_schema_truncated' to be 1 and exist, got %d (exists: %v)", count, exists)
	}
}

func TestSimplifyToolSchemaArrayNested(t *testing.T) {
	// A bloated array schema resembling what Claude Code might generate
	inputJSON := `{
		"type": "object",
		"properties": {
			"commands": {
				"type": "array",
				"items": {
					"anyOf": [
						{"type": "string", "title": "A String Command"},
						{
							"$ref": "#/definitions/ComplexCommand",
							"description": "Some deeply nested legacy ref"
						}
					],
					"allOf": [
						{"type": "object"}
					]
				}
			},
			"normal_field": {
				"anyOf": [
					{"type": "string"}
				]
			}
		}
	}`

	var rawSchema interface{}
	if err := json.Unmarshal([]byte(inputJSON), &rawSchema); err != nil {
		t.Fatalf("failed to unmarshal input json: %v", err)
	}

	simplified := simplifyToolSchema(rawSchema)

	simplifiedMap, ok := simplified.(map[string]interface{})
	if !ok {
		t.Fatalf("simplified schema is not a map")
	}

	props, ok := simplifiedMap["properties"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected 'properties' to remain a map")
	}

	commands, ok := props["commands"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected 'commands' to remain a map")
	}

	items, ok := commands["items"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected 'items' to remain a map")
	}

	if _, exists := items["anyOf"]; exists {
		t.Errorf("expected 'anyOf' to be stripped from array items")
	}
	if _, exists := items["allOf"]; exists {
		t.Errorf("expected 'allOf' to be stripped from array items")
	}

	normalField, ok := props["normal_field"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected 'normal_field' to remain a map")
	}
	if _, exists := normalField["anyOf"]; !exists {
		t.Errorf("expected 'anyOf' to be preserved outside of array items")
	}
}

func TestCoerceToolArgumentsArray(t *testing.T) {
	tests := []struct {
		name     string
		input    string
		expected string
	}{
		{
			name:     "boolean array elements",
			input:    `{"flags": ["true", "false", "true"]}`,
			expected: `{"flags":[true,false,true]}`,
		},
		{
			name:     "nested boolean array elements",
			input:    `{"nested": [["true"], ["false"]]}`,
			expected: `{"nested":[[true],[false]]}`,
		},
		{
			name:     "no coercion for numbers in array",
			input:    `{"numbers": ["123", "456"]}`,
			expected: `{"numbers":["123","456"]}`,
		},
		{
			name:     "mixed types in array",
			input:    `{"mixed": ["true", "123", "false", {"nested": "true"}]}`,
			expected: `{"mixed":[true,"123",false,{"nested":true}]}`,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := coerceToolArguments(json.RawMessage(tt.input))

			// For identical structures but different string serialization (spaces), unmarshal and compare
			var gotMap, expMap map[string]interface{}
			if string(got) != tt.expected && tt.expected != "" {
				errGot := json.Unmarshal(got, &gotMap)
				errExp := json.Unmarshal([]byte(tt.expected), &expMap)
				if errGot != nil || errExp != nil {
					t.Fatalf("failed to unmarshal for deep equal: %v, %v", errGot, errExp)
				}

				// Using json.Marshal again to ensure deep equality check works across types
				gotBytes, _ := json.Marshal(gotMap)
				expBytes, _ := json.Marshal(expMap)

				if string(gotBytes) != string(expBytes) {
					t.Errorf("expected %s, got %s", string(expBytes), string(gotBytes))
				}
			}
		})
	}
}

func TestParseToolCalls_CanonicalizationInFallbacks(t *testing.T) {
	// A JSON payload with extra spaces, out of order keys, and string-booleans
	// that coerceToolArguments will canonicalize.
	rawArgs := ` {
		"b": "true",
		"a" : 123
	} `
	expectedArgsCanonical := `{"a":123,"b":true}`

	tests := []struct {
		name    string
		content string
	}{
		{
			name:    "Markdown fence object",
			content: "Some text\n```json\n{\"name\": \"test_tool\", \"arguments\": " + rawArgs + "}\n```\nmore text",
		},
		{
			name:    "Markdown fence array",
			content: "Some text\n```json\n[{\"name\": \"test_tool\", \"arguments\": " + rawArgs + "}]\n```\nmore text",
		},
		{
			name:    "Unfenced object",
			content: "Some text {\"name\": \"test_tool\", \"arguments\": " + rawArgs + "} more text",
		},
		{
			name:    "Unfenced array",
			content: "Some text [{\"name\": \"test_tool\", \"arguments\": " + rawArgs + "}] more text",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			calls, _, hasCalls := parseToolCalls(tt.content)
			if !hasCalls || len(calls) != 1 {
				t.Fatalf("expected 1 call, got %d", len(calls))
			}
			call := calls[0]
			if call.Function.Name != "test_tool" {
				t.Errorf("expected name 'test_tool', got %q", call.Function.Name)
			}

			var gotArgs map[string]interface{}
			_ = json.Unmarshal([]byte(call.Function.Arguments), &gotArgs)

			var wantArgs map[string]interface{}
			_ = json.Unmarshal([]byte(expectedArgsCanonical), &wantArgs)

			gb, _ := json.Marshal(gotArgs)
			wb, _ := json.Marshal(wantArgs)

			if string(gb) != string(wb) {
				t.Errorf("expected arguments %q, got %q", expectedArgsCanonical, call.Function.Arguments)
			}
		})
	}
}

func TestSessionFallbackMetrics(t *testing.T) {
	sessionFallbackMetricsMu.Lock()
	sessionFallbackMetrics = make(map[string]int)
	sessionFallbackMetricsMu.Unlock()

	// Capture log output
	var buf bytes.Buffer
	originalLogOutput := log.Writer()
	defer log.SetOutput(originalLogOutput)
	log.SetOutput(&buf)

	// Call it once
	recordSessionFallbackMetric("session is nil")

	// Call it again
	recordSessionFallbackMetric("session is nil")

	// Call it with a different reason
	recordSessionFallbackMetric("TurnCount is 0")

	// Check metrics directly
	sessionFallbackMetricsMu.Lock()
	countNil := sessionFallbackMetrics["session is nil"]
	countZero := sessionFallbackMetrics["TurnCount is 0"]
	sessionFallbackMetricsMu.Unlock()

	if countNil != 2 {
		t.Errorf("Expected 'session is nil' count to be 2, got %d", countNil)
	}

	if countZero != 1 {
		t.Errorf("Expected 'TurnCount is 0' count to be 1, got %d", countZero)
	}

	// Check log output
	out := buf.String()
	if !strings.Contains(out, "[metrics] session_fallback: session is nil (total: 1)") {
		t.Errorf("Log output missing initial 'session is nil' line. Got: %s", out)
	}
	if !strings.Contains(out, "[metrics] session_fallback: session is nil (total: 2)") {
		t.Errorf("Log output missing incremented 'session is nil' line. Got: %s", out)
	}
	if !strings.Contains(out, "[metrics] session_fallback: TurnCount is 0 (total: 1)") {
		t.Errorf("Log output missing 'TurnCount is 0' line. Got: %s", out)
	}
}

func TestParseToolCalls_RobustBracketCountingNestedBraces(t *testing.T) {
	// A JSON payload where a string literal contains nested and unbalanced braces.
	// We want to ensure it extracts successfully without corrupting extraction.
	rawArgs := `{"text": "here is an { unbalanced brace inside a string"}`

	// A valid tool call followed by another valid tool call
	content := "Preceding text {\"name\": \"tool1\", \"arguments\": " + rawArgs + "} {\"name\": \"tool2\", \"arguments\": {}} trailing text"

	calls, _, hasCalls := parseToolCalls(content)
	if !hasCalls || len(calls) != 2 {
		t.Fatalf("expected 2 calls, got %d", len(calls))
	}
	if calls[0].Function.Name != "tool1" {
		t.Errorf("expected first call to be tool1, got %s", calls[0].Function.Name)
	}
	if calls[1].Function.Name != "tool2" {
		t.Errorf("expected second call to be tool2, got %s", calls[1].Function.Name)
	}
}

func TestParseToolCalls_RobustBracketCountingNegativeDepth(t *testing.T) {
	// A payload where a malformed block creates negative bracket depth,
	// which shouldn't prevent extraction of a sibling tool call.
	content := "malformed { \"invalid\": true }} {\"name\": \"tool2\", \"arguments\": {}}"

	calls, _, hasCalls := parseToolCalls(content)
	if !hasCalls || len(calls) != 1 {
		t.Fatalf("expected 1 call, got %d", len(calls))
	}
	if calls[0].Function.Name != "tool2" {
		t.Errorf("expected call to be tool2, got %s", calls[0].Function.Name)
	}
}

func TestParseToolCalls_RobustBracketCountingUnclosedString(t *testing.T) {
	// A payload where a string literal is unclosed and spans a newline,
	// which shouldn't prevent extraction of a subsequent valid sibling tool call.
	content := "malformed {\"name\": \"tool1\", \"arguments\": \"unclosed string } \n {\"name\": \"tool2\", \"arguments\": {}}"

	calls, _, hasCalls := parseToolCalls(content)
	if !hasCalls || len(calls) != 1 {
		t.Fatalf("expected 1 call, got %d", len(calls))
	}
	if calls[0].Function.Name != "tool2" {
		t.Errorf("expected call to be tool2, got %s", calls[0].Function.Name)
	}
}

func TestBuildTranscript_LegacyCollapseSearchContextDrop(t *testing.T) {
	contextLossMetricsMu.Lock()
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()

	messages := []ChatMessage{
		{Role: "user", Content: "Query 1"},
		{Role: "assistant", Content: "Here is what I found.\n\n---\nSources:\n[1] example.com"},
		{Role: "user", Content: "Query 2"},
		{Role: "assistant", Content: "Calling a tool", ToolCalls: []ToolCall{{ID: "1", Function: ToolCallFunction{Name: "Glob"}}}},
		{Role: "tool", Content: "results", ToolCallID: "1", Name: "Glob"},
	}

	// Create a tool to trigger the fallback logic properly
	tools := []Tool{
		{
			Type: "function",
			Function: ToolFunction{
				Name: "Glob",
			},
		},
	}
	for i := 0; i < 6; i++ {
		tools = append(tools, Tool{Type: "function", Function: ToolFunction{Name: "ToolX"}})
	}

	injectToolsIntoMessages(messages, tools, "claude-4", nil)

	contextLossMetricsMu.Lock()
	count, exists := contextLossMetrics["legacy_collapse_dropped_search_context"]
	contextLossMetricsMu.Unlock()

	if !exists || count != 1 {
		t.Errorf("Expected legacy_collapse_dropped_search_context to be 1, got %d (exists: %v)", count, exists)
	}
}

func TestInjectToolsIntoMessages_LegacyFallbackDiagnostics(t *testing.T) {
	messages := []ChatMessage{
		{Role: "user", Content: "Query 1"},
		{Role: "assistant", Content: "Calling a tool", ToolCalls: []ToolCall{{ID: "1", Function: ToolCallFunction{Name: "Glob"}}}},
		{Role: "tool", Content: "results", ToolCallID: "1", Name: "Glob"},
	}

	tools := []Tool{
		{
			Type: "function",
			Function: ToolFunction{
				Name: "Glob",
			},
		},
	}
	for i := 0; i < 6; i++ {
		tools = append(tools, Tool{Type: "function", Function: ToolFunction{Name: "ToolX"}})
	}

	var buf bytes.Buffer
	originalLogOutput := log.Writer()
	log.SetOutput(&buf)
	globalLogWriter.out = &buf
	defer func() {
		log.SetOutput(originalLogOutput)
		globalLogWriter.out = originalLogOutput
	}()

	injectToolsIntoMessages(messages, tools, "claude-4", nil)

	output := buf.String()
	if !strings.Contains(output, "[bridge] diagnostics: falling back from session to legacy collapse") {
		t.Errorf("Expected diagnostic log, got:\n%s", output)
	}
	if !strings.Contains(output, "reason: session is nil") {
		t.Errorf("Expected reason in log, got:\n%s", output)
	}
	if !strings.Contains(output, "messages: 3") {
		t.Errorf("Expected message count in log, got:\n%s", output)
	}
	if !strings.Contains(output, "user(len=7)") {
		t.Errorf("Expected user role and length in log, got:\n%s", output)
	}
}

func TestParseToolCallsUnparseableMetric(t *testing.T) {
	toolModeLossMetricsMu.Lock()
	toolModeLossMetrics = make(map[string]int)
	toolModeLossMetricsMu.Unlock()

	// Parse unparseable block
	content := `{
		"unrelated": true,
		"broken": [
			1, 2, {}
		]
	}`

	_, _, ok := parseToolCalls(content)
	if ok {
		t.Fatalf("Expected parseToolCalls to fail on unparseable block")
	}

	toolModeLossMetricsMu.Lock()
	count := toolModeLossMetrics["unparseable_json_candidate_blocks"]
	toolModeLossMetricsMu.Unlock()

	if count != 1 {
		t.Fatalf("Expected 1 unparseable_json_candidate_blocks, got %d", count)
	}
}

func TestSimplifyToolSchema_ComplexArrayFallback(t *testing.T) {
	contextLossMetricsMu.Lock()
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()
	rawJSON := `{"type": "array", "items": {"$ref": "#/defs/complex"}}`
	var rawSchema interface{}
	if err := json.Unmarshal([]byte(rawJSON), &rawSchema); err != nil {
		t.Fatalf("failed to parse test schema: %v", err)
	}

	var buf bytes.Buffer
	originalLogOutput := log.Writer()
	log.SetOutput(&buf)
	defer log.SetOutput(originalLogOutput)

	simplified := simplifyToolSchema(rawSchema)

	// Verify log
	logOutput := buf.String()
	if !strings.Contains(logOutput, "[bridge] diagnostics: simplifySchemaNode dropped complex array item \"$ref\" to prevent token bloat, returning empty schema") {
		t.Errorf("expected diagnostic log for schema fallback, got: %q", logOutput)
	}

	// Verify metric
	contextLossMetricsMu.Lock()
	metricCount, exists := contextLossMetrics["tool_schema_simplification_fallback"]
	contextLossMetricsMu.Unlock()

	if !exists || metricCount != 1 {
		t.Errorf("expected metric 'tool_schema_simplification_fallback' to be 1 and exist, got %d (exists: %v)", metricCount, exists)
	}

	// Verify output is {"items": {}} map essentially
	simplifiedMap, ok := simplified.(map[string]interface{})
	if !ok {
		t.Fatalf("expected map[string]interface{}, got %T", simplified)
	}

	itemsObj, ok := simplifiedMap["items"]
	if !ok {
		t.Fatalf("expected 'items' key in output")
	}

	itemsMap, ok := itemsObj.(map[string]interface{})
	if !ok {
		t.Fatalf("expected 'items' to be map[string]interface{}, got %T", itemsObj)
	}

	if len(itemsMap) != 0 {
		t.Errorf("expected empty schema fallback, got %v", itemsMap)
	}
}

func TestParseToolCalls_RobustBracketCountingIsolatedBrackets(t *testing.T) {
	content := `Some previous text that has an isolated bracket:
}

Another isolated bracket:
]

And then the real tool call:
{"name": "the_tool", "arguments": {"valid": true}}
`

	calls, _, isFallback := parseToolCalls(content)
	if !isFallback {
		t.Error("Expected fallback mode to be triggered")
	}

	if len(calls) != 1 {
		t.Fatalf("Expected 1 call, got %d", len(calls))
	}

	if calls[0].Function.Name != "the_tool" {
		t.Errorf("Expected name 'the_tool', got '%s'", calls[0].Function.Name)
	}

}

func TestParseToolCalls_RobustBracketCountingIsolatedBracketBeforeValid(t *testing.T) {
	content := `
{
  "some_output": "this is perfectly balanced but not a tool call"
}

Then an isolated bracket
}

And the valid tool call:
{"name": "real_tool", "arguments": {}}
`

	calls, _, isFallback := parseToolCalls(content)
	if !isFallback {
		t.Error("Expected fallback mode to be triggered")
	}

	if len(calls) != 1 {
		t.Fatalf("Expected 1 call, got %d", len(calls))
	}

	if calls[0].Function.Name != "real_tool" {
		t.Errorf("Expected name 'real_tool', got '%s'", calls[0].Function.Name)
	}

}

func TestSimplifyToolSchema_PropertiesArrayFallback(t *testing.T) {
	contextLossMetricsMu.Lock()
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()
	rawJSON := `{"type": "array", "items": {"properties": {"field": {"type": "string"}}}}`
	var rawSchema interface{}
	if err := json.Unmarshal([]byte(rawJSON), &rawSchema); err != nil {
		t.Fatalf("failed to parse test schema: %v", err)
	}

	var buf bytes.Buffer
	originalLogOutput := log.Writer()
	log.SetOutput(&buf)
	defer log.SetOutput(originalLogOutput)

	simplified := simplifyToolSchema(rawSchema)
	logOutput := buf.String()

	if !strings.Contains(logOutput, "[bridge] diagnostics: simplifySchemaNode dropped complex array item \"properties\" to prevent token bloat, returning empty schema") {
		t.Errorf("expected diagnostic log for properties fallback, got: %q", logOutput)
	}

	// Verify metric
	contextLossMetricsMu.Lock()
	metricCount, exists := contextLossMetrics["tool_schema_simplification_fallback"]
	contextLossMetricsMu.Unlock()

	if !exists || metricCount != 1 {
		t.Errorf("expected metric 'tool_schema_simplification_fallback' to be 1 and exist, got %d (exists: %v)", metricCount, exists)
	}

	// Verify output is {"type": "array", "items": {}} map essentially
	simplifiedMap, ok := simplified.(map[string]interface{})
	if !ok {
		t.Fatalf("expected map[string]interface{}, got %T", simplified)
	}

	itemsObj, ok := simplifiedMap["items"]
	if !ok {
		t.Fatalf("expected 'items' property in simplified schema")
	}

	itemsMap, ok := itemsObj.(map[string]interface{})
	if !ok {
		t.Fatalf("expected 'items' to be map[string]interface{}, got %T", itemsObj)
	}

	if len(itemsMap) != 0 {
		t.Errorf("expected 'items' to be empty map, got %v", itemsMap)
	}
}

func TestSimplifyToolSchema_UnboundedRecursion(t *testing.T) {
	// Create a deeply nested structure exceeding maxSchemaDepth (100)
	var root map[string]interface{} = map[string]interface{}{}
	current := root
	for i := 0; i < 105; i++ {
		next := map[string]interface{}{}
		current["nested"] = next
		current = next
	}

	result := simplifyToolSchema(root)

	// If it doesn't panic, the test basically passes on safety,
	// but we should also check that at depth 100 it returns an empty map
	// instead of continuing.

	depth := 0
	currResult, ok := result.(map[string]interface{})
	for ok {
		if next, exists := currResult["nested"]; exists {
			currResult, ok = next.(map[string]interface{})
			depth++
		} else {
			break
		}
	}

	// It should reach exactly maxSchemaDepth (100).
	if depth != 100 {
		t.Errorf("Expected truncated recursion depth 100, got %d", depth)
	}

	contextLossMetricsMu.Lock()
	countMapLimit, existsMapLimit := contextLossMetrics["tool_schema_simplification_recursion_limit"]
	countArrLimit, existsArrLimit := contextLossMetrics["tool_schema_simplification_recursion_limit_array"]
	contextLossMetricsMu.Unlock()

	if !existsMapLimit || countMapLimit == 0 {
		t.Errorf("expected tool_schema_simplification_recursion_limit metric to be recorded, but it was not")
	}

	if existsArrLimit && countArrLimit != 0 {
		t.Errorf("expected tool_schema_simplification_recursion_limit_array metric to NOT be recorded, got %d", countArrLimit)
	}
}

func TestSessionChainContinuation_SearchContext(t *testing.T) {
	contextLossMetricsMu.Lock()
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()

	longSearchContext := "---\nSources:\n[1] Something very very long. " + strings.Repeat("A", 700)

	messages := []ChatMessage{
		{Role: "user", Content: "Hello"},
		{Role: "assistant", Content: longSearchContext, ToolCalls: []ToolCall{
			{ID: "call_1", Function: ToolCallFunction{Name: "Search", Arguments: `{"query": "test"}`}},
		}},
		{Role: "tool", Content: "result", ToolCallID: "call_1", Name: "Search"},
	}

	continuation := buildSessionChainContinuation(messages, "- Search(query)", "")

	if len(continuation) == 0 {
		t.Fatalf("expected continuation message")
	}

	content := continuation[0].Content
	if !strings.Contains(content, "---\nSources:\n[1] Something very very long.") {
		t.Errorf("expected continuation to include search context")
	}

	if !strings.Contains(content, "...") {
		t.Errorf("expected search context to be truncated")
	}

	contextLossMetricsMu.Lock()
	_, exists := contextLossMetrics["search_context_truncated"]
	contextLossMetricsMu.Unlock()

	if !exists {
		t.Errorf("Expected search_context_truncated metric to be present")
	}
}

func TestInjectToolsIntoMessages_DropsEmptyWrapperUserMessage(t *testing.T) {
	contextLossMetricsMu.Lock()
	original := contextLossMetrics
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()

	t.Cleanup(func() {
		contextLossMetricsMu.Lock()
		contextLossMetrics = original
		contextLossMetricsMu.Unlock()
	})

	messages := []ChatMessage{
		{Role: "user", Content: "<system-reminder>\nReminder to be a good agent\n</system-reminder>"},
		{Role: "assistant", Content: "Hello"},
	}

	tools := make([]Tool, 6)
	for i := 0; i < 6; i++ {
		tools[i] = Tool{
			Type: "function",
			Function: ToolFunction{
				Name:        "test_tool",
				Description: "A test tool",
				Parameters:  map[string]interface{}{},
			},
		}
	}

	filtered := injectToolsIntoMessages(messages, tools, "claude-3-5-sonnet", nil)

	foundAssistant := false
	for _, m := range filtered {
		if m.Role == "assistant" {
			foundAssistant = true
		}
		if m.Role == "user" && strings.Contains(m.Content, "<system-reminder>") {
			t.Errorf("Expected wrapper message to be dropped but found: %s", m.Content)
		}
	}

	if !foundAssistant {
		t.Errorf("Expected assistant message to be kept")
	}

	contextLossMetricsMu.Lock()
	count, exists := contextLossMetrics["empty_wrapper_user_message_dropped"]
	contextLossMetricsMu.Unlock()

	if !exists {
		t.Errorf("Expected empty_wrapper_user_message_dropped metric to be present")
	} else if count != 1 {
		t.Errorf("Expected empty_wrapper_user_message_dropped to be recorded once, got %d", count)
	}
}

func TestInjectToolsIntoMessages_SuggestionModeMetric(t *testing.T) {
	toolModeLossMetricsMu.Lock()
	toolModeLossMetrics = make(map[string]int)
	toolModeLossMetricsMu.Unlock()

	messages := []ChatMessage{
		{Role: "user", Content: "[SUGGESTION MODE: Predict what the user will do next]"},
	}

	// Make len(tools) > 5 to trigger useLargeToolSet and thus reach SUGGESTION MODE branch
	tools := []Tool{
		{Type: "function", Function: ToolFunction{Name: "Tool1"}},
		{Type: "function", Function: ToolFunction{Name: "Tool2"}},
		{Type: "function", Function: ToolFunction{Name: "Tool3"}},
		{Type: "function", Function: ToolFunction{Name: "Tool4"}},
		{Type: "function", Function: ToolFunction{Name: "Tool5"}},
		{Type: "function", Function: ToolFunction{Name: "Tool6"}},
	}

	injectToolsIntoMessages(messages, tools, "claude-3-opus-20240229", nil)

	toolModeLossMetricsMu.Lock()
	count := toolModeLossMetrics["suggestion_mode_trigger"]
	toolModeLossMetricsMu.Unlock()

	if count != 1 {
		t.Fatalf("Expected 1 suggestion_mode_trigger metric, got %d", count)
	}
}

func TestBuildSessionChainContinuation_EmptyToolList(t *testing.T) {
	contextLossMetricsMu.Lock()
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()

	messages := []ChatMessage{
		{Role: "user", Content: "do it"},
		{Role: "assistant", Content: "working on it", ToolCalls: []ToolCall{{ID: "tc1", Function: ToolCallFunction{Name: "Bash"}}}},
		{Role: "tool", Content: "done", ToolCallID: "tc1"},
	}

	// This should trigger the warning and log "empty_tools_fallback"
	result := buildSessionChainContinuation(messages, "", "/cwd")

	// Ensure result is returned and not empty
	if len(result) == 0 {
		t.Fatalf("Expected non-empty result")
	}

	contextLossMetricsMu.Lock()
	count, exists := contextLossMetrics["empty_tools_fallback"]
	contextLossMetricsMu.Unlock()

	if !exists || count != 1 {
		t.Errorf("Expected empty_tools_fallback metric count 1, got %d", count)
	}
}

func TestInjectToolsIntoMessages_LargeSearchContext(t *testing.T) {
	contextLossMetricsMu.Lock()
	original := contextLossMetrics
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()

	t.Cleanup(func() {
		contextLossMetricsMu.Lock()
		contextLossMetrics = original
		contextLossMetricsMu.Unlock()
	})

	longSearchCtx := "---\nSources:\n[1] "
	for i := 0; i < 700; i++ {
		longSearchCtx += "a"
	}

	messages := []ChatMessage{
		{Role: "user", Content: "Test user query"},
		{Role: "assistant", Content: longSearchCtx},
		{Role: "user", Content: "What is the answer?"},
	}

	// We need > 5 tools to trigger `useLargeToolSet`
	tools := []Tool{
		{Function: ToolFunction{Name: "T1"}},
		{Function: ToolFunction{Name: "T2"}},
		{Function: ToolFunction{Name: "T3"}},
		{Function: ToolFunction{Name: "T4"}},
		{Function: ToolFunction{Name: "T5"}},
		{Function: ToolFunction{Name: "T6"}},
	}

	res := injectToolsIntoMessages(messages, tools, "claude-3-5-sonnet-20241022", nil)

	contextLossMetricsMu.Lock()
	_, exists := contextLossMetrics["search_context_truncated"]
	contextLossMetricsMu.Unlock()

	if !exists {
		t.Errorf("Expected search_context_truncated metric to be present")
	}

	if len(res) == 0 || !strings.Contains(res[len(res)-1].Content, "...") {
		t.Errorf("Expected truncated result to have '...'")
	}
}

func TestSearchContextTruncation_MultiByteRunes(t *testing.T) {
	contextLossMetricsMu.Lock()
	original := contextLossMetrics
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()

	t.Cleanup(func() {
		contextLossMetricsMu.Lock()
		contextLossMetrics = original
		contextLossMetricsMu.Unlock()
	})

	longSearchCtx := "---\nSources:\n[1] "
	for i := 0; i < 700; i++ {
		longSearchCtx += "абв"
	}

	messages := []ChatMessage{
		{Role: "user", Content: "Test user query"},
		{Role: "assistant", Content: longSearchCtx},
		{Role: "user", Content: "What is the answer?"},
	}

	tools := []Tool{
		{Function: ToolFunction{Name: "T1"}},
		{Function: ToolFunction{Name: "T2"}},
		{Function: ToolFunction{Name: "T3"}},
		{Function: ToolFunction{Name: "T4"}},
		{Function: ToolFunction{Name: "T5"}},
		{Function: ToolFunction{Name: "T6"}},
	}

	res := injectToolsIntoMessages(messages, tools, "claude-3-5-sonnet-20241022", nil)

	contextLossMetricsMu.Lock()
	_, exists := contextLossMetrics["search_context_truncated"]
	contextLossMetricsMu.Unlock()

	if !exists {
		t.Errorf("Expected search_context_truncated metric to be present")
	}

	if len(res) == 0 || !strings.Contains(res[len(res)-1].Content, "...") {
		t.Errorf("Expected truncated result to have '...'")
	}

	// Ensure valid UTF-8 (no rune splitting)
	if strings.ToValidUTF8(res[len(res)-1].Content, "") != res[len(res)-1].Content {
		t.Errorf("Truncated result contains invalid UTF-8 (likely a split rune)")
	}
}

func TestBuildSessionChainContinuation_LargeSearchContext(t *testing.T) {
	contextLossMetricsMu.Lock()
	original := contextLossMetrics
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()

	t.Cleanup(func() {
		contextLossMetricsMu.Lock()
		contextLossMetrics = original
		contextLossMetricsMu.Unlock()
	})

	longSearchCtx := "---\nSources:\n[1] "
	for i := 0; i < 700; i++ {
		longSearchCtx += "a"
	}

	messages := []ChatMessage{
		{Role: "user", Content: "Test user query"},
		{Role: "assistant", Content: longSearchCtx, ToolCalls: []ToolCall{{ID: "tc1", Function: ToolCallFunction{Name: "Bash"}}}},
		{Role: "tool", Content: "done", ToolCallID: "tc1"},
	}

	res := buildSessionChainContinuation(messages, "T1, T2", "/cwd")

	contextLossMetricsMu.Lock()
	_, exists := contextLossMetrics["search_context_truncated"]
	contextLossMetricsMu.Unlock()

	if !exists {
		t.Errorf("Expected search_context_truncated metric to be present")
	}

	if len(res) == 0 || !strings.Contains(res[len(res)-1].Content, "...") {
		t.Errorf("Expected truncated result to have '...'")
	}
}

func TestBuildSessionChain_LargeSearchContext_MultiByteRunes(t *testing.T) {
	contextLossMetricsMu.Lock()
	original := contextLossMetrics
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()

	t.Cleanup(func() {
		contextLossMetricsMu.Lock()
		contextLossMetrics = original
		contextLossMetricsMu.Unlock()
	})

	longSearchCtx := "---\nSources:\n[1] "
	for i := 0; i < 700; i++ {
		longSearchCtx += "абв"
	}

	messages := []ChatMessage{
		{Role: "user", Content: "Test user query"},
		{Role: "assistant", Content: longSearchCtx, ToolCalls: []ToolCall{{ID: "tc1", Function: ToolCallFunction{Name: "Bash"}}}},
		{Role: "tool", Content: "done", ToolCallID: "tc1"},
	}

	res := buildSessionChainContinuation(messages, "T1, T2", "/cwd")

	contextLossMetricsMu.Lock()
	_, exists := contextLossMetrics["search_context_truncated"]
	contextLossMetricsMu.Unlock()

	if !exists {
		t.Errorf("Expected search_context_truncated metric to be present")
	}

	if len(res) == 0 || !strings.Contains(res[len(res)-1].Content, "...") {
		t.Errorf("Expected truncated result to have '...'")
	}

	// Ensure valid UTF-8 (no rune splitting)
	if strings.ToValidUTF8(res[len(res)-1].Content, "") != res[len(res)-1].Content {
		t.Errorf("Truncated result contains invalid UTF-8 (likely a split rune)")
	}
}

func TestParseToolCalls_XMLArrayFallbackMetrics(t *testing.T) {
	toolModeLossMetricsMu.Lock()
	toolModeLossMetrics = make(map[string]int)
	toolModeLossMetricsMu.Unlock()
	xmlArrayMetricsMu.Lock()
	xmlArrayMetrics = make(map[string]int)
	xmlArrayMetricsMu.Unlock()

	// Direct array in XML wrapper
	contentDirect := `<tool_call>
[
  {"name": "tool_a", "arguments": {"a": 1}},
  {"name": "tool_b", "arguments": {"b": 2}}
]
</tool_call>`

	calls, _, _ := parseToolCalls(contentDirect, "auto")
	if len(calls) != 2 {
		t.Fatalf("expected 2 calls, got %d", len(calls))
	}

	xmlArrayMetricsMu.Lock()
	directCount, exists := xmlArrayMetrics["direct_array_mode_auto"]
	xmlArrayMetricsMu.Unlock()

	if !exists || directCount != 1 {
		t.Errorf("Expected direct_array_mode_auto metric to be 1, got %d", directCount)
	}

	toolModeLossMetricsMu.Lock()
	lossDirectCount, lossDirectExists := toolModeLossMetrics["xml_wrapper_fallback_mode_auto"]
	toolModeLossMetricsMu.Unlock()

	if !lossDirectExists || lossDirectCount != 1 {
		t.Errorf("Expected xml_wrapper_fallback_mode_auto metric to be 1, got %d", lossDirectCount)
	}

	// Wrapper array in XML wrapper
	contentWrapper := `<tool_call>
{"tool_call": [
  {"name": "tool_c", "arguments": {"c": 3}},
  {"name": "tool_d", "arguments": {"d": 4}}
]}
</tool_call>`

	callsWrapper, _, _ := parseToolCalls(contentWrapper)
	if len(callsWrapper) != 2 {
		t.Fatalf("expected 2 calls, got %d", len(callsWrapper))
	}

	xmlArrayMetricsMu.Lock()
	wrapperCount, wrapperExists := xmlArrayMetrics["wrapper_array"]
	xmlArrayMetricsMu.Unlock()

	if !wrapperExists || wrapperCount != 1 {
		t.Errorf("Expected wrapper_array metric to be 1, got %d", wrapperCount)
	}

	toolModeLossMetricsMu.Lock()
	lossWrapperCount, lossWrapperExists := toolModeLossMetrics["xml_wrapper_fallback"]
	toolModeLossMetricsMu.Unlock()

	if !lossWrapperExists || lossWrapperCount != 1 {
		t.Errorf("Expected xml_wrapper_fallback metric to be 1, got %d", lossWrapperCount)
	}
}

func TestParseToolCalls_XMLWrapperFallbackTracking(t *testing.T) {
	toolModeLossMetricsMu.Lock()
	toolModeLossMetrics = make(map[string]int)
	toolModeLossMetricsMu.Unlock()

	content := "<tool_call>{\"name\": \"test_tool\", \"arguments\": {\"param\": 1}}</tool_call>"

	calls, _, hasCalls := parseToolCalls(content, "auto")

	if !hasCalls || len(calls) != 1 {
		t.Fatalf("expected 1 call, got %d", len(calls))
	}
	if calls[0].Function.Name != "test_tool" {
		t.Errorf("expected test_tool, got %s", calls[0].Function.Name)
	}

	toolModeLossMetricsMu.Lock()
	count := toolModeLossMetrics["xml_wrapper_fallback_mode_auto"]
	toolModeLossMetricsMu.Unlock()

	if count != 1 {
		t.Errorf("expected xml_wrapper_fallback_mode_auto count to be 1, got %d", count)
	}
}

func TestParseToolCalls_MarkdownFenceFallbackTracking(t *testing.T) {
	toolModeLossMetricsMu.Lock()
	toolModeLossMetrics = make(map[string]int)
	toolModeLossMetricsMu.Unlock()

	content := "```json\n{\"name\": \"test_tool\", \"arguments\": {\"param\": 1}}\n```"

	calls, _, hasCalls := parseToolCalls(content, "any")

	if !hasCalls || len(calls) != 1 {
		t.Fatalf("expected 1 call, got %d", len(calls))
	}
	if calls[0].Function.Name != "test_tool" {
		t.Errorf("expected test_tool, got %s", calls[0].Function.Name)
	}

	toolModeLossMetricsMu.Lock()
	count := toolModeLossMetrics["markdown_fence_fallback_mode_any"]
	toolModeLossMetricsMu.Unlock()

	if count != 1 {
		t.Errorf("expected markdown_fence_fallback_mode_any count to be 1, got %d", count)
	}
}
func TestFallbackMissingAnchorMetric(t *testing.T) {
	contextLossMetricsMu.Lock()
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()

	messages := []ChatMessage{
		{Role: "user", Content: "do something"},
		{Role: "tool", Content: "result"}, // tool result without previous assistant message
	}

	buildSessionChainContinuation(messages, "", "")

	contextLossMetricsMu.Lock()
	val, exists := contextLossMetrics["fallback_missing_anchor"]
	contextLossMetricsMu.Unlock()

	if !exists || val == 0 {
		t.Errorf("Expected fallback_missing_anchor metric to be present and incremented, got exists=%v val=%d", exists, val)
	}
}

func TestLegacyCollapse_SearchContextTruncatedMetrics(t *testing.T) {
	contextLossMetricsMu.Lock()
	original := contextLossMetrics
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()

	t.Cleanup(func() {
		contextLossMetricsMu.Lock()
		contextLossMetrics = original
		contextLossMetricsMu.Unlock()
	})

	longSearchCtx := "---\nSources:\n[1] "
	for i := 0; i < 700; i++ {
		longSearchCtx += "a"
	}

	messages := []ChatMessage{
		{Role: "user", Content: "First query"},
		{Role: "assistant", Content: longSearchCtx},
		{Role: "user", Content: "What is the answer?"},
	}

	tools := []Tool{
		{Function: ToolFunction{Name: "T1"}},
		{Function: ToolFunction{Name: "T2"}},
		{Function: ToolFunction{Name: "T3"}},
		{Function: ToolFunction{Name: "T4"}},
		{Function: ToolFunction{Name: "T5"}},
		{Function: ToolFunction{Name: "T6"}},
	}

	injectToolsIntoMessages(messages, tools, "claude-3-5-sonnet-20241022", nil)

	contextLossMetricsMu.Lock()
	_, exists := contextLossMetrics["search_context_truncated"]
	contextLossMetricsMu.Unlock()

	if !exists {
		t.Errorf("Expected search_context_truncated metric to be present")
	}
}

func TestSimplifyToolSchema_UnboundedRecursionArray(t *testing.T) {
	// Create a deeply nested array structure exceeding maxSchemaDepth (100)
	var current interface{} = []interface{}{}

	for i := 0; i < 105; i++ {
		current = []interface{}{current}
	}

	result := simplifyToolSchema(current)

	// We only care that it doesn't panic.
	if result == nil {
		t.Errorf("Expected non-nil result")
	}

	contextLossMetricsMu.Lock()
	countMapLimit, existsMapLimit := contextLossMetrics["tool_schema_simplification_recursion_limit"]
	countArrLimit, existsArrLimit := contextLossMetrics["tool_schema_simplification_recursion_limit_array"]
	contextLossMetricsMu.Unlock()

	if !existsArrLimit || countArrLimit == 0 {
		t.Errorf("expected tool_schema_simplification_recursion_limit_array metric to be recorded, but it was not")
	}

	if existsMapLimit && countMapLimit != 0 {
		t.Errorf("expected tool_schema_simplification_recursion_limit metric to NOT be recorded, got %d", countMapLimit)
	}
}
func TestBuildSessionChainContinuation_NonEmptyToolList(t *testing.T) {
	contextLossMetricsMu.Lock()
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()

	messages := []ChatMessage{
		{Role: "user", Content: "do it"},
		{Role: "assistant", Content: "working on it", ToolCalls: []ToolCall{{ID: "tc1", Function: ToolCallFunction{Name: "Bash"}}}},
		{Role: "tool", Content: "done", ToolCallID: "tc1"},
	}

	result := buildSessionChainContinuation(messages, "T1", "/cwd")

	if len(result) == 0 {
		t.Fatalf("Expected non-empty result")
	}

	contextLossMetricsMu.Lock()
	count, exists := contextLossMetrics["empty_tools_fallback"]
	contextLossMetricsMu.Unlock()

	if exists && count != 0 {
		t.Errorf("Expected empty_tools_fallback metric count 0, got %d", count)
	}
}
func TestBuildSessionChainContinuation_RetryLoopMetric(t *testing.T) {
	contextLossMetricsMu.Lock()
	original := contextLossMetrics
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()

	t.Cleanup(func() {
		contextLossMetricsMu.Lock()
		contextLossMetrics = original
		contextLossMetricsMu.Unlock()
	})

	messages := []ChatMessage{
		{Role: "user", Content: "Task execution"},
		{Role: "assistant", Content: "try search", ToolCalls: []ToolCall{{ID: "c1", Function: ToolCallFunction{Name: "Search"}}}},
		{Role: "tool", Content: "error: rate limit", ToolCallID: "c1", Name: "Search"},
		{Role: "assistant", Content: "try search again", ToolCalls: []ToolCall{{ID: "c2", Function: ToolCallFunction{Name: "Search"}}}},
		{Role: "tool", Content: "error: rate limit", ToolCallID: "c2", Name: "Search"},
	}

	buildSessionChainContinuation(messages, "Search", "/tmp")

	contextLossMetricsMu.Lock()
	count, exists := contextLossMetrics["retry_loop_detected"]
	contextLossMetricsMu.Unlock()

	if !exists {
		t.Errorf("Expected retry_loop_detected metric to exist")
	} else if count != 1 {
		t.Errorf("Expected retry_loop_detected metric count 1, got %d", count)
	}
}

func TestBuildToolList_SchemaTruncation(t *testing.T) {
	contextLossMetricsMu.Lock()
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()

	longPropName := strings.Repeat("a", 4500)
	tools := []Tool{
		{
			Function: ToolFunction{
				Name:        "TestFunc",
				Description: "A test function",
				Parameters: map[string]interface{}{
					"type": "object",
					"properties": map[string]interface{}{
						longPropName: map[string]interface{}{
							"type": "string",
						},
					},
				},
			},
		},
	}

	result := buildToolList(tools)

	if !strings.Contains(result, "Function: TestFunc - A test function") {
		t.Errorf("expected function definition, got %s", result)
	}

	if !strings.Contains(result, "...") {
		t.Errorf("expected schema to be truncated")
	}

	if !utf8.ValidString(result) {
		t.Errorf("Truncated string is not valid UTF-8")
	}

	contextLossMetricsMu.Lock()
	count, exists := contextLossMetrics["tool_schema_json_truncated"]
	contextLossMetricsMu.Unlock()

	if !exists || count != 1 {
		t.Errorf("expected metric tool_schema_json_truncated to be 1 and exist, got %d (exists: %v)", count, exists)
	}
}

func TestParseToolCallsUnparseableMetricTruncated(t *testing.T) {
	toolModeLossMetricsMu.Lock()
	toolModeLossMetrics = make(map[string]int)
	toolModeLossMetricsMu.Unlock()

	// Parse unparseable block that is longer than 800 chars
	// The outer array should have over 800 characters to trigger truncation.
	longStr := strings.Repeat("a", 900)
	content := `{
		"unrelated": true,
		"broken": [
			"` + longStr + `"
		]
	}`

	_, _, ok := parseToolCalls(content)
	if ok {
		t.Fatalf("Expected parseToolCalls to fail on unparseable block")
	}

	toolModeLossMetricsMu.Lock()
	countUnparseable := toolModeLossMetrics["unparseable_json_candidate_blocks"]
	countTruncated := toolModeLossMetrics["unparseable_json_candidate_truncated"]
	toolModeLossMetricsMu.Unlock()

	if countUnparseable != 1 {
		t.Fatalf("Expected 1 unparseable_json_candidate_blocks, got %d", countUnparseable)
	}
	if countTruncated != 1 {
		t.Fatalf("Expected 1 unparseable_json_candidate_truncated, got %d", countTruncated)
	}
}

func TestBuildSessionChainContinuation_NoToolsNonEmptySequence(t *testing.T) {
	contextLossMetricsMu.Lock()
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()

	messages := []ChatMessage{
		{Role: "user", Content: "hello"},
		{Role: "assistant", Content: "hi there"},
		{Role: "user", Content: "how are you?"},
	}

	result := buildSessionChainContinuation(messages, "", "/cwd")

	if len(result) == 0 {
		t.Fatalf("Expected non-empty result")
	}

	contextLossMetricsMu.Lock()
	count, exists := contextLossMetrics["empty_tools_fallback"]
	contextLossMetricsMu.Unlock()

	if !exists || count != 1 {
		t.Errorf("Expected empty_tools_fallback metric count 1, got %d (exists: %t)", count, exists)
	}
}
func TestParseToolCalls_ValidCandidateNotLoggedAsUnparseable(t *testing.T) {
	toolModeLossMetricsMu.Lock()
	toolModeLossMetrics = make(map[string]int)
	toolModeLossMetricsMu.Unlock()

	// A valid JSON tool call that will be extracted via the bracket-counting fallback (Method 2)
	content := `Here is the requested tool call:
{
	"name": "ViewFile",
	"arguments": {
		"filename": "main.go"
	}
}`

	calls, _, ok := parseToolCalls(content)
	if !ok || len(calls) != 1 {
		t.Fatalf("Expected successfully parsed tool call, got %d calls, ok=%v", len(calls), ok)
	}

	toolModeLossMetricsMu.Lock()
	_, unparseableExists := toolModeLossMetrics["unparseable_json_candidate_blocks"]
	_, truncatedExists := toolModeLossMetrics["unparseable_json_candidate_truncated"]
	toolModeLossMetricsMu.Unlock()

	if unparseableExists {
		t.Errorf("Expected unparseable_json_candidate_blocks to not be incremented for a valid tool call")
	}
	if truncatedExists {
		t.Errorf("Expected unparseable_json_candidate_truncated to not be incremented for a valid tool call")
	}
}

func TestSimplifyToolSchemaJSONTruncation_CommaOkAssertion(t *testing.T) {
	contextLossMetricsMu.Lock()
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()

	longPropName := strings.Repeat("b", 5000)
	tools := []Tool{
		{
			Function: ToolFunction{
				Name:        "TestFunc2",
				Description: "A second test function",
				Parameters: map[string]interface{}{
					"type": "object",
					"properties": map[string]interface{}{
						longPropName: map[string]interface{}{
							"type": "string",
						},
					},
				},
			},
		},
	}

	out := buildToolList(tools)

	if !utf8.ValidString(out) {
		t.Errorf("Truncated string is not valid UTF-8: %s", out)
	}

	contextLossMetricsMu.Lock()
	count, exists := contextLossMetrics["tool_schema_json_truncated"]
	contextLossMetricsMu.Unlock()

	if !exists || count != 1 {
		t.Errorf("expected metric tool_schema_json_truncated to be 1 and exist using comma-ok, got %d (exists: %v)", count, exists)
	}
}
func TestLegacyCollapse_ToolResultTruncationBoundaries(t *testing.T) {
	// Subtest 1: exactly 800 characters (should not truncate)
	t.Run("Exactly800Chars", func(t *testing.T) {
		contextLossMetricsMu.Lock()
		contextLossMetrics = make(map[string]int)
		contextLossMetricsMu.Unlock()

		messages := []ChatMessage{
			{Role: "user", Content: "Query"},
			{Role: "assistant", Content: "Calling a tool", ToolCalls: []ToolCall{{ID: "1", Function: ToolCallFunction{Name: "Glob"}}}},
			{Role: "tool", Content: strings.Repeat("A", 800), ToolCallID: "1", Name: "Glob"},
		}

		tools := []Tool{
			{Type: "function", Function: ToolFunction{Name: "Glob"}},
		}
		for i := 0; i < 6; i++ {
			tools = append(tools, Tool{Type: "function", Function: ToolFunction{Name: "ToolX"}})
		}

		var buf bytes.Buffer
		originalLogOutput := log.Writer()
		log.SetOutput(&buf)
		globalLogWriter.out = &buf
		defer func() {
			log.SetOutput(originalLogOutput)
			globalLogWriter.out = originalLogOutput
		}()

		injectToolsIntoMessages(messages, tools, "claude-4", nil)

		contextLossMetricsMu.Lock()
		val, exists := contextLossMetrics["legacy_collapse_truncated"]
		contextLossMetricsMu.Unlock()

		if exists && val != 0 {
			t.Errorf("Expected legacy_collapse_truncated to not exist or be 0 for 800 chars, got %d", val)
		}

		// Add direct log-capture assertion
		if strings.Contains(buf.String(), "legacy collapse truncated") {
			t.Errorf("Expected no truncation log for 800 chars, but found one: %q", buf.String())
		}
	})

	// Subtest 2: exactly 801 characters (should truncate)
	t.Run("Exactly801Chars", func(t *testing.T) {
		contextLossMetricsMu.Lock()
		contextLossMetrics = make(map[string]int)
		contextLossMetricsMu.Unlock()

		messages := []ChatMessage{
			{Role: "user", Content: "Query"},
			{Role: "assistant", Content: "Calling a tool", ToolCalls: []ToolCall{{ID: "1", Function: ToolCallFunction{Name: "Glob"}}}},
			{Role: "tool", Content: strings.Repeat("A", 801), ToolCallID: "1", Name: "Glob"},
		}

		tools := []Tool{
			{Type: "function", Function: ToolFunction{Name: "Glob"}},
		}
		for i := 0; i < 6; i++ {
			tools = append(tools, Tool{Type: "function", Function: ToolFunction{Name: "ToolX"}})
		}

		var buf bytes.Buffer
		originalLogOutput := log.Writer()
		log.SetOutput(&buf)
		globalLogWriter.out = &buf
		defer func() {
			log.SetOutput(originalLogOutput)
			globalLogWriter.out = originalLogOutput
		}()

		injectToolsIntoMessages(messages, tools, "claude-4", nil)

		contextLossMetricsMu.Lock()
		val, exists := contextLossMetrics["legacy_collapse_truncated"]
		contextLossMetricsMu.Unlock()

		if !exists || val != 1 {
			t.Errorf("Expected legacy_collapse_truncated to be exactly 1 for 801 chars, got %d", val)
		}

		// Add direct log-capture assertion
		if !strings.Contains(buf.String(), "original: 801 chars, limit: 800 chars") {
			t.Errorf("Expected truncation log to indicate 'original: 801 chars, limit: 800 chars', but got: %q", buf.String())
		}
	})

	// Subtest 3: exactly 801 runes with multi-byte characters
	t.Run("Exactly801RunesUnicode", func(t *testing.T) {
		contextLossMetricsMu.Lock()
		contextLossMetrics = make(map[string]int)
		contextLossMetricsMu.Unlock()

		// "абвгд" is 5 runes. Repeat it 160 times to get 800 runes, then add one more rune ("а") for 801 total runes.
		multiByteStr := strings.Repeat("абвгд", 160) + "а"
		if len([]rune(multiByteStr)) != 801 {
			t.Fatalf("Expected exactly 801 runes, got %d", len([]rune(multiByteStr)))
		}

		messages := []ChatMessage{
			{Role: "user", Content: "Query"},
			{Role: "assistant", Content: "Calling a tool", ToolCalls: []ToolCall{{ID: "1", Function: ToolCallFunction{Name: "Glob"}}}},
			{Role: "tool", Content: multiByteStr, ToolCallID: "1", Name: "Glob"},
		}

		tools := []Tool{
			{Type: "function", Function: ToolFunction{Name: "Glob"}},
		}
		for i := 0; i < 6; i++ {
			tools = append(tools, Tool{Type: "function", Function: ToolFunction{Name: "ToolX"}})
		}

		var buf bytes.Buffer
		originalLogOutput := log.Writer()
		log.SetOutput(&buf)
		globalLogWriter.out = &buf
		defer func() {
			log.SetOutput(originalLogOutput)
			globalLogWriter.out = originalLogOutput
		}()

		injectToolsIntoMessages(messages, tools, "claude-4", nil)

		contextLossMetricsMu.Lock()
		val, exists := contextLossMetrics["legacy_collapse_truncated"]
		contextLossMetricsMu.Unlock()

		if !exists || val != 1 {
			t.Errorf("Expected legacy_collapse_truncated to be exactly 1 for 801 runes, got %d", val)
		}

		// Add direct log-capture assertion
		if !strings.Contains(buf.String(), "original: 801 chars, limit: 800 chars") {
			t.Errorf("Expected truncation log to indicate 'original: 801 chars, limit: 800 chars', but got: %q", buf.String())
		}
	})
}

func TestExactly801RunesUnicode(t *testing.T) {
	// The task requires this exact test name!
	// Reset the metric exactly once at the beginning of the test
	contextLossMetricsMu.Lock()
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()

	// To trigger tool_schema_json_truncated, the JSON string must be > 4000 runes.
	// We'll create a schema with exactly 4001 runes to trigger it.
	base := `{"properties":{"prop":"`
	end := `"},"type":"object"}`
	target := 4001
	strLen := target - len([]rune(base)) - len([]rune(end))

	s := strings.Repeat("а", strLen)

	schema := map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"prop": s,
		},
	}

	tools := []Tool{
		{
			Type: "function",
			Function: ToolFunction{
				Name:       "test_tool",
				Parameters: schema,
			},
		},
	}

	out := buildToolList(tools)

	if !strings.Contains(out, "...") {
		t.Errorf("Expected truncated string with '...', got: %s", out)
	}

	if !utf8.ValidString(out) {
		t.Errorf("Truncated string is not valid UTF-8")
	}

	contextLossMetricsMu.Lock()
	count, exists := contextLossMetrics["tool_schema_json_truncated"]
	contextLossMetricsMu.Unlock()

	if !exists || count != 1 {
		t.Errorf("Expected tool_schema_json_truncated metric to be 1, got %d (exists: %v)", count, exists)
	}
}

func TestExactly4001ByteMultibyteBoundary(t *testing.T) {
	// Reset the metric exactly once at the beginning of the test
	contextLossMetricsMu.Lock()
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()

	// The truncation happens at 4000 RUNES. The task asks to test "exact 4001 byte multibyte character truncation"
	// Let's create a 4001 RUNE string to force truncation, and verify it truncates exactly to 4000 RUNES without breaking valid UTF-8 strings.
	base := `{"properties":{"prop":"`
	end := `"},"type":"object"}`
	target := 4001
	strLen := target - len([]rune(base)) - len([]rune(end))

	s := strings.Repeat("а", strLen) // 'а' is 2 bytes

	schema := map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"prop": s,
		},
	}

	tools := []Tool{
		{
			Type: "function",
			Function: ToolFunction{
				Name:       "test_tool",
				Parameters: schema,
			},
		},
	}

	out := buildToolList(tools)

	if !strings.Contains(out, "...") {
		t.Errorf("Expected string to be truncated, but got: %s", out)
	}

	if !utf8.ValidString(out) {
		t.Errorf("String is not valid UTF-8")
	}

	contextLossMetricsMu.Lock()
	count, exists := contextLossMetrics["tool_schema_json_truncated"]
	contextLossMetricsMu.Unlock()

	if exists && count != 1 {
		t.Errorf("Expected tool_schema_json_truncated metric to be 1, got %d (exists: %v)", count, exists)
	} else if !exists {
		t.Errorf("Expected tool_schema_json_truncated metric to exist")
	}
}

func TestToolSchemaJSONTruncatedMultiByteCombos(t *testing.T) {
	contextLossMetricsMu.Lock()
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()

	// Mix of ASCII and multi-byte UTF-8 to test boundaries
	multiByteStr := "aабвгдe" // 1+5+1 = 7 runes

	base := `{"properties":{"prop":"`
	end := `"},"type":"object"}`
	target := 4010

	baseRunes := len([]rune(base))
	endRunes := len([]rune(end))
	remainingRunes := target - baseRunes - endRunes

	// Repeat multiByteStr to fill the remaining runes
	repeats := remainingRunes / len([]rune(multiByteStr))
	s := strings.Repeat(multiByteStr, repeats)
	// Add remaining single chars
	leftover := remainingRunes % len([]rune(multiByteStr))
	s += string([]rune(multiByteStr)[:leftover])

	schema := map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"prop": s,
		},
	}

	tools := []Tool{
		{
			Type: "function",
			Function: ToolFunction{
				Name:       "test_tool_multibyte",
				Parameters: schema,
			},
		},
	}

	out := buildToolList(tools)

	if !strings.Contains(out, "...") {
		t.Errorf("Expected truncated string with '...', got: %s", out)
	}

	if !utf8.ValidString(out) {
		t.Errorf("Truncated string is not valid UTF-8: %s", out)
	}

	contextLossMetricsMu.Lock()
	count, exists := contextLossMetrics["tool_schema_json_truncated"]
	contextLossMetricsMu.Unlock()

	if !exists || count != 1 {
		t.Errorf("Expected tool_schema_json_truncated metric to be 1, got %d (exists: %v)", count, exists)
	}
}

func TestToolSchemaJSONTruncatedEmojiBoundary(t *testing.T) {
	contextLossMetricsMu.Lock()
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()

	emojiStr := "🚀🔥😃👨‍👩‍👧‍👦" // Emojis with zero-width joiners
	base := `{"properties":{"prop":"`
	end := `"},"type":"object"}`
	target := 4010

	baseRunes := len([]rune(base))
	endRunes := len([]rune(end))
	remainingRunes := target - baseRunes - endRunes

	repeats := remainingRunes / len([]rune(emojiStr))
	s := strings.Repeat(emojiStr, repeats)
	leftover := remainingRunes % len([]rune(emojiStr))
	s += string([]rune(emojiStr)[:leftover])

	schema := map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"prop": s,
		},
	}

	tools := []Tool{
		{
			Type: "function",
			Function: ToolFunction{
				Name:       "test_tool_emoji",
				Parameters: schema,
			},
		},
	}

	out := buildToolList(tools)

	if !strings.Contains(out, "...") {
		t.Errorf("Expected truncated string with '...', got: %s", out)
	}

	if !utf8.ValidString(out) {
		t.Errorf("Truncated string is not valid UTF-8: %s", out)
	}

	contextLossMetricsMu.Lock()
	count, exists := contextLossMetrics["tool_schema_json_truncated"]
	contextLossMetricsMu.Unlock()

	if !exists || count != 1 {
		t.Errorf("Expected tool_schema_json_truncated metric to be 1, got %d (exists: %v)", count, exists)
	}
}

func TestIsSuggestionMode(t *testing.T) {
	tests := []struct {
		name     string
		content  string
		expected bool
	}{
		{
			name:     "suggestion mode string",
			content:  "[SUGGESTION MODE: I found a bug...]",
			expected: true,
		},
		{
			name:     "suggestion mode with leading whitespace",
			content:  "   \n\t [SUGGESTION MODE: testing whitespace",
			expected: true,
		},
		{
			name:     "suggestion mode with trailing whitespace only",
			content:  "[SUGGESTION MODE: x \n",
			expected: true,
		},
		{
			name:     "regular string without prefix",
			content:  "I found a bug...",
			expected: false,
		},
		{
			name:     "prefix somewhere inside string",
			content:  "Here is [SUGGESTION MODE: something",
			expected: false,
		},
		{
			name:     "empty string",
			content:  "",
			expected: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := isSuggestionMode(tt.content)
			if result != tt.expected {
				t.Errorf("isSuggestionMode(%q) = %v; want %v", tt.content, result, tt.expected)
			}
		})
	}
}

func TestToolSchemaJSONTruncatedExtremelySmallLimit(t *testing.T) {
	// Reset the metric exactly once at the beginning of the test
	contextLossMetricsMu.Lock()
	contextLossMetrics = make(map[string]int)
	contextLossMetricsMu.Unlock()

	// Override limit for test and restore after
	originalLimit := toolSchemaTruncationLimit
	toolSchemaTruncationLimit = 0
	defer func() {
		toolSchemaTruncationLimit = originalLimit
	}()

	schema := map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"prop": "value",
		},
	}

	tools := []Tool{
		{
			Type: "function",
			Function: ToolFunction{
				Name:       "test_tool_small_limit",
				Parameters: schema,
			},
		},
	}

	out := buildToolList(tools)

	if !strings.Contains(out, "...") {
		t.Errorf("Expected truncated string with '...', got: %s", out)
	}

	if !utf8.ValidString(out) {
		t.Errorf("Truncated string is not valid UTF-8: %s", out)
	}

	contextLossMetricsMu.Lock()
	count, exists := contextLossMetrics["tool_schema_json_truncated"]
	contextLossMetricsMu.Unlock()

	if !exists || count != 1 {
		t.Errorf("Expected tool_schema_json_truncated metric to be 1, got %d (exists: %v)", count, exists)
	}
}
