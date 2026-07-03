package proxy

import (
	"bytes"
	"encoding/json"
	"log"
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
	// Reset the metric state
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
