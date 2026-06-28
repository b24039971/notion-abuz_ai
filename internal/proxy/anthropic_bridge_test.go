package proxy

import (
	"strings"
	"testing"
)

func TestExtractAnthropicSessionSalt(t *testing.T) {
	metadata := map[string]interface{}{
		"user_id": `{"device_id":"dev-1","session_id":"sess-123","account_uuid":""}`,
	}

	if got := extractAnthropicSessionSalt(metadata); got != "sess-123" {
		t.Fatalf("extractAnthropicSessionSalt() = %q, want %q", got, "sess-123")
	}
}

func TestComputeSessionFingerprintWithSalt_IgnoresBillingHeaderDrift(t *testing.T) {
	turn1 := []ChatMessage{
		{Role: "system", Content: "x-anthropic-billing-header: cc_version=2.1.81.a; cch=aaaa;\nYou are Claude Code, Anthropic's official CLI for Claude.\nSystem body"},
		{Role: "user", Content: "<available-deferred-tools>\nGrep\nRead\n</available-deferred-tools>"},
	}
	turn2 := []ChatMessage{
		{Role: "system", Content: "x-anthropic-billing-header: cc_version=2.1.81.b; cch=bbbb;\nYou are Claude Code, Anthropic's official CLI for Claude.\nSystem body"},
		{Role: "user", Content: "<available-deferred-tools>\nGrep\nRead\n</available-deferred-tools>"},
		{Role: "assistant", Content: "", ToolCalls: []ToolCall{
			{ID: "call_1", Type: "function", Function: ToolCallFunction{Name: "Grep", Arguments: `{"pattern":"copy"}`}},
		}},
		{Role: "tool", ToolCallID: "call_1", Name: "Grep", Content: "Found 1 file\nsrc/content.js"},
	}

	fp1 := computeSessionFingerprintWithSalt(turn1, "sess-123")
	fp2 := computeSessionFingerprintWithSalt(turn2, "sess-123")
	if fp1 != fp2 {
		t.Fatalf("fingerprint drifted across billing-header changes: %s vs %s", fp1, fp2)
	}
}

func TestApplyStructuredOutputBridge_JSONSchema(t *testing.T) {
	messages := []ChatMessage{
		{Role: "system", Content: "x-anthropic-billing-header: cc_version=2.1.81; cch=aaaa;"},
		{Role: "system", Content: "You are Claude Code, Anthropic's official CLI for Claude."},
		{Role: "system", Content: "Generate a concise title.\nReturn JSON with a single \"title\" field."},
		{Role: "user", Content: "检查为什么右侧预览栏的md copy按钮出不来"},
	}
	cfg := &AnthropicOutputConfig{
		Format: &AnthropicOutputFormat{
			Type: "json_schema",
			Schema: map[string]interface{}{
				"type": "object",
				"properties": map[string]interface{}{
					"title": map[string]interface{}{"type": "string"},
				},
				"required":             []string{"title"},
				"additionalProperties": false,
			},
		},
	}

	bridged := applyStructuredOutputBridge(messages, cfg)
	if len(bridged) != 1 {
		t.Fatalf("expected 1 bridged message, got %d", len(bridged))
	}
	if bridged[0].Role != "user" {
		t.Fatalf("expected bridged role=user, got %s", bridged[0].Role)
	}

	content := bridged[0].Content
	if strings.Contains(content, "x-anthropic-billing-header") {
		t.Fatalf("structured output bridge leaked billing header: %s", content)
	}
	if strings.Contains(content, "You are Claude Code") {
		t.Fatalf("structured output bridge leaked Claude identity line: %s", content)
	}
	if !strings.Contains(content, `Return JSON with a single "title" field.`) {
		t.Fatalf("structured output bridge dropped system instruction: %s", content)
	}
	if !strings.Contains(content, "检查为什么右侧预览栏的md copy按钮出不来") {
		t.Fatalf("structured output bridge dropped user content: %s", content)
	}
	if !strings.Contains(content, `"title": {`) || !strings.Contains(content, `"required": [`) {
		t.Fatalf("structured output bridge did not embed schema JSON: %s", content)
	}
}

func TestInjectToolsIntoMessages_DropsWrapperOnlyUserMessage(t *testing.T) {
	tools := []Tool{
		{Type: "function", Function: ToolFunction{Name: "Bash", Description: "Execute shell command", Parameters: map[string]interface{}{"type": "object"}}},
		{Type: "function", Function: ToolFunction{Name: "Read", Description: "Read a file", Parameters: map[string]interface{}{"type": "object"}}},
		{Type: "function", Function: ToolFunction{Name: "Write", Description: "Write a file", Parameters: map[string]interface{}{"type": "object"}}},
		{Type: "function", Function: ToolFunction{Name: "Edit", Description: "Edit a file", Parameters: map[string]interface{}{"type": "object"}}},
		{Type: "function", Function: ToolFunction{Name: "Glob", Description: "Find files", Parameters: map[string]interface{}{"type": "object"}}},
		{Type: "function", Function: ToolFunction{Name: "Grep", Description: "Search files", Parameters: map[string]interface{}{"type": "object"}}},
	}
	messages := []ChatMessage{
		{Role: "system", Content: "You are Claude Code."},
		{Role: "user", Content: "<available-deferred-tools>\nRead\nEdit\n</available-deferred-tools>"},
		{Role: "user", Content: "修复登录校验"},
	}

	got := injectToolsIntoMessages(messages, tools, "claude-opus-4-6", nil)
	if len(got) != 1 {
		t.Fatalf("expected 1 bridged message, got %d", len(got))
	}

	content := got[0].Content
	if strings.Contains(content, "User: Hello") || strings.Contains(content, "\nHello\n") {
		t.Fatalf("wrapper-only message should not turn into synthetic Hello: %q", content)
	}
	if strings.Contains(content, "<available-deferred-tools>") {
		t.Fatalf("wrapper-only message leaked into bridged content: %q", content)
	}
	if !strings.Contains(content, `Input: "修复登录校验"`) {
		t.Fatalf("expected actual user query in bridged content, got %q", content)
	}
}

func TestNormalizeStructuredOutputText_StripsLangTagAndMarkdownFence(t *testing.T) {
	raw := "<lang primary=\"zh-CN\"/>\n\n```json\n{\"title\":\"Fix digest error\"}\n```"
	got := normalizeStructuredOutputText(raw)
	want := "{\"title\":\"Fix digest error\"}"
	if got != want {
		t.Fatalf("normalizeStructuredOutputText() = %q, want %q", got, want)
	}
}

func TestNormalizeStructuredOutputText_ExtractsJSONObjectFromPrefixedText(t *testing.T) {
	raw := "Here is the JSON output you requested:\n{\"title\":\"Fix invalid password\"}"
	got := normalizeStructuredOutputText(raw)
	want := "{\"title\":\"Fix invalid password\"}"
	if got != want {
		t.Fatalf("normalizeStructuredOutputText() = %q, want %q", got, want)
	}
}

func TestDetectToolBridgeNoToolResponse_MatchesIdentityDriftHandOff(t *testing.T) {
	raw := `<lang primary="zh-CN"/>

抱歉，我理解你希望我直接帮你修改文件，但**我是 Notion AI，无法访问你的本地文件系统**。我没有 Read、Edit、Bash 这些工具的能力。

把下面这段话直接发给你的编码助手（Cursor / Claude Code），它就能帮你操作。`

	isNoTool, reason := detectToolBridgeNoToolResponse(raw)
	if !isNoTool {
		t.Fatalf("expected no-tool identity drift text to be detected")
	}
	if reason != "Notion persona leakage" {
		t.Fatalf("expected reason 'Notion persona leakage', got %q", reason)
	}
}

func TestDetectToolBridgeNoToolResponse_DoesNotMatchNormalAnswer(t *testing.T) {
	raw := "我已经根据上面的 grep 结果定位到文件，下一步建议缩小 Read 范围后继续编辑。"

	isNoTool, reason := detectToolBridgeNoToolResponse(raw)
	if isNoTool {
		t.Fatalf("normal answer should not be classified as no-tool identity drift")
	}
	if reason != "" {
		t.Fatalf("expected empty reason for normal answer, got %q", reason)
	}
}

func TestDetectToolBridgeNoToolResponse_MatchesIdentityDriftHandOff_English(t *testing.T) {
	raw := `<lang primary="en-US"/>

I am Notion AI, I cannot access your local file system. I don't have the ability to run Bash or Edit tools.
Please copy and paste this text manually into your coding assistant like Claude Code.`

	isNoTool, reason := detectToolBridgeNoToolResponse(raw)
	if !isNoTool {
		t.Fatalf("expected English no-tool identity drift text to be detected")
	}
	if reason != "Notion persona leakage" {
		t.Fatalf("expected reason 'Notion persona leakage', got %q", reason)
	}
}

func TestDetectToolBridgeNoToolResponse_MatchesToolCallRefusal_English(t *testing.T) {
	raw := `I do not have access to run terminal commands such as bash or read or edit local files. You will need to copy and paste this into your coding assistant.`

	isNoTool, reason := detectToolBridgeNoToolResponse(raw)
	if !isNoTool {
		t.Fatalf("expected English tool-call refusal to be detected")
	}
	if reason != "tool-call refusal" {
		t.Fatalf("expected reason 'tool-call refusal', got %q", reason)
	}
}

func TestDetectToolBridgeNoToolResponse_MatchesFinalAnswerDrift(t *testing.T) {
	raw := `I am Notion AI, and I don't have access to your coding assistant. Therefore, I cannot run edit or bash to modify those files.`

	isNoTool, reason := detectToolBridgeNoToolResponse(raw)
	if !isNoTool {
		t.Fatalf("expected English final-answer identity drift to be detected")
	}
	if reason != "Notion persona leakage" {
		t.Fatalf("expected reason 'Notion persona leakage', got %q", reason)
	}
}

func TestDetectToolBridgeNoToolResponse_MatchesWorkspaceReframing(t *testing.T) {
	cases := []string{
		`I cannot run bash commands to modify files. However, I can help you create a Notion page or search the Notion workspace.`,
		`I am unable to use the terminal to run commands. Would you like me to create a Notion database instead?`,
		`作为AI，我无法直接运行本地命令。但我可以帮你将这些内容保存到 Notion 文档中。`,
		`I am Notion AI. I don't have access to edit local files, but I can save to notion for you.`,
		`<boltArtifact id="test" title="Failed Tool">I cannot run bash commands to modify files. However, I can help you search the Notion workspace.</boltArtifact>`,
		"```json\n{\n  \"error\": \"I cannot run bash commands to modify files. However, I can help you create a Notion page.\"\n}\n```",
	}

	for i, raw := range cases {
		isNoTool, reason := detectToolBridgeNoToolResponse(raw)
		if !isNoTool {
			t.Fatalf("expected Workspace Reframing to be detected for case %d: %s", i, raw)
		}
		if reason != "workspace reframing" {
			t.Fatalf("expected reason 'workspace reframing', got %q for case %d", reason, i)
		}
	}
}

func TestWorkspaceReframingDetection_BubblesUpReason(t *testing.T) {
	raw := `I cannot run bash commands to modify files. However, I can help you create a Notion page or search the Notion workspace.`

	isNoTool, driftReason := detectToolBridgeNoToolResponse(raw)
	if !isNoTool {
		t.Fatalf("expected Workspace Reframing to be detected")
	}
	if driftReason != "workspace reframing" {
		t.Fatalf("expected driftReason to bubble up as 'workspace reframing', got %q", driftReason)
	}
}

func TestClaudeCodeAgentLoop_PreservesCodingIntent(t *testing.T) {
	// A simulated Claude Code transcript with CLAUDE.md instructions,
	// inline command-name tags, MCP server tags, and system-reminder blocks.
	messages := []ChatMessage{
		{Role: "system", Content: "You are Claude Code.\nRead CLAUDE.md for rules."},
		{Role: "user", Content: "I need you to build a new agentic loop matrix. <system-reminder>DO NOT USE NOTION AI</system-reminder>\nRun <command-name>npm test</command-name> and verify <file>test.js</file>.\nContext from <mcp-server name=\"github\">Provides github tools</mcp-server>.\n<project-instructions>Use spaces</project-instructions>"},
	}

	isAssistant := isCodingAssistantRequest(messages)
	if !isAssistant {
		t.Fatalf("expected isCodingAssistantRequest to be true for this transcript")
	}

	sanitized := sanitizeForBridge(messages)

	// Should have replaced the first message with the bridge system prompt
	if len(sanitized) != 2 || sanitized[0].Role != "system" {
		t.Fatalf("expected 2 messages starting with system bridge, got %v", sanitized)
	}

	if strings.Contains(sanitized[0].Content, "You are Claude Code") {
		t.Fatalf("expected Claude Code system prompt to be dropped, got: %s", sanitized[0].Content)
	}
	if !strings.Contains(sanitized[0].Content, "output the function call as JSON") {
		t.Fatalf("expected bridge system prompt, got: %s", sanitized[0].Content)
	}

	userMsg := sanitized[1].Content
	// <system-reminder> should be fully stripped
	if strings.Contains(userMsg, "DO NOT USE NOTION AI") {
		t.Fatalf("expected system-reminder block to be stripped, got: %s", userMsg)
	}
	// Inline tags <command-name> and <file> should have their tags stripped but content kept
	if !strings.Contains(userMsg, "Run npm test") || !strings.Contains(userMsg, "verify test.js") {
		t.Fatalf("expected coding intent to be preserved, got: %s", userMsg)
	}

	// MCP server tags and project instructions should have tags stripped but content kept
	if !strings.Contains(userMsg, "Provides github tools") || !strings.Contains(userMsg, "Use spaces") {
		t.Fatalf("expected MCP and project instructions intent to be preserved, got: %s", userMsg)
	}
}
