package proxy

import (
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestConvertOpenAIChatCompletionRequest_WithFilesToolsAndJSONSchema(t *testing.T) {
	pdfData := base64.StdEncoding.EncodeToString([]byte("%PDF-1.4 mock"))
	imageData := base64.StdEncoding.EncodeToString([]byte("png-bytes"))
	req := &OpenAIChatCompletionRequest{
		Model: "gpt-5.4",
		Messages: []OpenAIChatMessage{
			{Role: "developer", Content: "Always answer in Chinese."},
			{Role: "user", Content: []interface{}{
				map[string]interface{}{"type": "text", "text": "分析这个文件"},
				map[string]interface{}{"type": "image_url", "image_url": map[string]interface{}{"url": "data:image/png;base64," + imageData}},
				map[string]interface{}{"type": "file", "file": map[string]interface{}{"filename": "spec.pdf", "file_data": pdfData}},
			}},
		},
		Tools: []OpenAITool{{
			Type: "function",
			Function: &OpenAIFunctionDefinition{
				Name:        "Read",
				Description: "Read a file",
				Parameters: map[string]interface{}{
					"type": "object",
					"properties": map[string]interface{}{
						"path": map[string]interface{}{"type": "string"},
					},
				},
			},
		}},
		ToolChoice: map[string]interface{}{
			"type":     "function",
			"function": map[string]interface{}{"name": "Read"},
		},
		ResponseFormat: &OpenAIChatResponseFormat{
			Type:       "json_schema",
			JSONSchema: &OpenAIJSONSchemaConfig{Schema: map[string]interface{}{"type": "object"}},
		},
	}

	anthReq, err := convertOpenAIChatCompletionRequest(req)
	if err != nil {
		t.Fatalf("convertOpenAIChatCompletionRequest() error = %v", err)
	}
	if anthReq.Model != "gpt-5.4" {
		t.Fatalf("model = %q, want gpt-5.4", anthReq.Model)
	}
	if anthReq.System != "Always answer in Chinese." {
		t.Fatalf("system = %#v", anthReq.System)
	}
	if len(anthReq.Tools) != 1 || anthReq.Tools[0].Name != "Read" {
		t.Fatalf("tools = %#v", anthReq.Tools)
	}
	if anthReq.OutputConfig == nil || anthReq.OutputConfig.Format == nil || anthReq.OutputConfig.Format.Type != "json_schema" {
		t.Fatalf("output_config = %#v", anthReq.OutputConfig)
	}
	if len(anthReq.Messages) != 1 {
		t.Fatalf("messages len = %d, want 1", len(anthReq.Messages))
	}
	blocks, ok := anthReq.Messages[0].Content.([]interface{})
	if !ok || len(blocks) != 3 {
		t.Fatalf("content blocks = %#v", anthReq.Messages[0].Content)
	}
	first := blocks[0].(map[string]interface{})
	if first["type"] != "text" {
		t.Fatalf("first block = %#v", first)
	}
	second := blocks[1].(map[string]interface{})
	if second["type"] != "image" {
		t.Fatalf("second block = %#v", second)
	}
	third := blocks[2].(map[string]interface{})
	if third["type"] != "document" {
		t.Fatalf("third block = %#v", third)
	}
}

func TestConvertOpenAIChatCompletionRequest_ReasoningEffortRouting(t *testing.T) {
	original := SnapshotModelMap()
	ReplaceModelMap(map[string]string{
		"opus-4.8-high": "notion-internal-high-id",
	})
	defer ReplaceModelMap(original)

	t.Run("reasoning_effort alias exists", func(t *testing.T) {
		req := &OpenAIChatCompletionRequest{
			Model:           "opus-4.8",
			ReasoningEffort: "high",
			Messages:        []OpenAIChatMessage{{Role: "user", Content: "test"}},
		}
		anthReq, err := convertOpenAIChatCompletionRequest(req)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if anthReq.Model != "opus-4.8-high" {
			t.Errorf("expected model 'opus-4.8-high', got '%s'", anthReq.Model)
		}
	})

	t.Run("reasoningEffort camel case alias exists", func(t *testing.T) {
		req := &OpenAIChatCompletionRequest{
			Model:                "opus-4.8",
			ReasoningEffortCamel: "high",
			Messages:             []OpenAIChatMessage{{Role: "user", Content: "test"}},
		}
		anthReq, err := convertOpenAIChatCompletionRequest(req)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if anthReq.Model != "opus-4.8-high" {
			t.Errorf("expected model 'opus-4.8-high', got '%s'", anthReq.Model)
		}
	})

	t.Run("alias absent fallback", func(t *testing.T) {
		req := &OpenAIChatCompletionRequest{
			Model:           "opus-4.8",
			ReasoningEffort: "low",
			Messages:        []OpenAIChatMessage{{Role: "user", Content: "test"}},
		}
		anthReq, err := convertOpenAIChatCompletionRequest(req)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if anthReq.Model != "opus-4.8" {
			t.Errorf("expected model 'opus-4.8', got '%s'", anthReq.Model)
		}
	})
}

func TestConvertOpenAIResponsesRequest_WithFunctionCallOutput(t *testing.T) {
	req := &OpenAIResponsesRequest{
		Model:        "gpt-5.4",
		Instructions: "Return JSON only.",
		Input: []interface{}{
			map[string]interface{}{"type": "input_text", "text": "hello"},
			map[string]interface{}{"type": "function_call_output", "call_id": "call_123", "output": "done"},
		},
		Text: &OpenAIResponsesTextConfig{Format: &OpenAIChatResponseFormat{Type: "json_object"}},
	}

	anthReq, err := convertOpenAIResponsesRequest(req)
	if err != nil {
		t.Fatalf("convertOpenAIResponsesRequest() error = %v", err)
	}
	if anthReq.System != "Return JSON only." {
		t.Fatalf("system = %#v", anthReq.System)
	}
	if anthReq.OutputConfig == nil || anthReq.OutputConfig.Format == nil || anthReq.OutputConfig.Format.Type != "json_schema" {
		t.Fatalf("output_config = %#v", anthReq.OutputConfig)
	}
	if len(anthReq.Messages) != 2 {
		t.Fatalf("messages len = %d, want 2", len(anthReq.Messages))
	}
	firstBlocks := anthReq.Messages[0].Content.([]interface{})
	if firstBlocks[0].(map[string]interface{})["type"] != "text" {
		t.Fatalf("first message blocks = %#v", firstBlocks)
	}
	secondBlocks := anthReq.Messages[1].Content.([]interface{})
	toolResult := secondBlocks[0].(map[string]interface{})
	if toolResult["type"] != "tool_result" || toolResult["tool_use_id"] != "call_123" {
		t.Fatalf("tool result = %#v", toolResult)
	}
}

func TestConvertOpenAIResponsesRequest_ReasoningEffortRouting(t *testing.T) {
	original := SnapshotModelMap()
	ReplaceModelMap(map[string]string{
		"opus-4.8-high": "notion-internal-high-id",
	})
	defer ReplaceModelMap(original)

	t.Run("reasoning_effort alias exists", func(t *testing.T) {
		req := &OpenAIResponsesRequest{
			Model:           "opus-4.8",
			ReasoningEffort: "high",
			Input:           "test input",
		}
		anthReq, err := convertOpenAIResponsesRequest(req)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if anthReq.Model != "opus-4.8-high" {
			t.Errorf("expected model 'opus-4.8-high', got '%s'", anthReq.Model)
		}
	})

	t.Run("reasoningEffort camel case alias exists", func(t *testing.T) {
		req := &OpenAIResponsesRequest{
			Model:                "opus-4.8",
			ReasoningEffortCamel: "high",
			Input:                "test input",
		}
		anthReq, err := convertOpenAIResponsesRequest(req)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if anthReq.Model != "opus-4.8-high" {
			t.Errorf("expected model 'opus-4.8-high', got '%s'", anthReq.Model)
		}
	})

	t.Run("alias absent fallback", func(t *testing.T) {
		req := &OpenAIResponsesRequest{
			Model:           "opus-4.8",
			ReasoningEffort: "low",
			Input:           "test input",
		}
		anthReq, err := convertOpenAIResponsesRequest(req)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if anthReq.Model != "opus-4.8" {
			t.Errorf("expected model 'opus-4.8', got '%s'", anthReq.Model)
		}
	})
}

func TestBuildOpenAIChatCompletionResponse_FromAnthropicBlocks(t *testing.T) {
	stopReason := "tool_use"
	resp := buildOpenAIChatCompletionResponse("chatcmpl_test", 123, "gpt-5.4", &AnthropicResponse{
		Content: []AnthropicContentBlock{
			{Type: "text", Text: "先读文件"},
			{Type: "tool_use", ID: "call_1", Name: "Read", Input: json.RawMessage(`{"path":"README.md"}`)},
		},
		StopReason: &stopReason,
		Usage:      &AnthropicUsage{InputTokens: 10, OutputTokens: 5},
	})

	if got := resp.Choices[0].Message["content"]; got != "先读文件" {
		t.Fatalf("content = %#v", got)
	}
	toolCalls, ok := resp.Choices[0].Message["tool_calls"].([]OpenAIChatToolCall)
	if !ok || len(toolCalls) != 1 {
		t.Fatalf("tool_calls = %#v", resp.Choices[0].Message["tool_calls"])
	}
	if resp.Choices[0].FinishReason == nil || *resp.Choices[0].FinishReason != "tool_calls" {
		t.Fatalf("finish_reason = %#v", resp.Choices[0].FinishReason)
	}
	if resp.Usage["total_tokens"] != 15 {
		t.Fatalf("usage = %#v", resp.Usage)
	}
}

func TestOpenAIChatStreamTranscoder_EmitsToolCallsAndDone(t *testing.T) {
	rr := httptest.NewRecorder()
	transcoder := newOpenAIChatStreamTranscoder(rr, rr, "chatcmpl_test", "gpt-5.4", 123, true)
	frames := []anthropicSSEFrame{
		{Event: "message_start", Data: json.RawMessage(`{"message":{"usage":{"input_tokens":11}}}`)},
		{Event: "content_block_start", Data: json.RawMessage(`{"index":0,"content_block":{"type":"tool_use","id":"call_1","name":"Read","input":{}}}`)},
		{Event: "content_block_delta", Data: json.RawMessage(`{"index":0,"delta":{"type":"input_json_delta","partial_json":"{\"path\""}}`)},
		{Event: "content_block_delta", Data: json.RawMessage(`{"index":0,"delta":{"type":"input_json_delta","partial_json":":\"README.md\"}"}}`)},
		{Event: "message_delta", Data: json.RawMessage(`{"delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":7}}`)},
		{Event: "message_stop", Data: json.RawMessage(`{"type":"message_stop"}`)},
	}
	for _, frame := range frames {
		if err := transcoder.HandleFrame(frame); err != nil {
			t.Fatalf("HandleFrame(%s) error = %v", frame.Event, err)
		}
	}
	body := rr.Body.String()
	if !strings.Contains(body, "chat.completion.chunk") {
		t.Fatalf("body missing chat.completion.chunk: %s", body)
	}
	if !strings.Contains(body, `"tool_calls"`) || !strings.Contains(body, `README.md`) {
		t.Fatalf("body missing tool call data: %s", body)
	}
	if !strings.Contains(body, `"usage":{`) || !strings.Contains(body, `"prompt_tokens":11`) || !strings.Contains(body, `"completion_tokens":7`) || !strings.Contains(body, `"total_tokens":18`) {
		t.Fatalf("body missing usage chunk: %s", body)
	}
	if !strings.Contains(body, "data: [DONE]") {
		t.Fatalf("body missing DONE: %s", body)
	}
}

func TestOpenAIResponsesStreamTranscoder_LargeChunks(t *testing.T) {
	rr := httptest.NewRecorder()
	transcoder := newOpenAIResponsesStreamTranscoder(rr, rr, "resp_large", "gpt-5.4", 456)
	largeStr := strings.Repeat("a", 100000)
	deltaPayload, _ := json.Marshal(map[string]interface{}{
		"index": 0,
		"delta": map[string]interface{}{
			"type": "text_delta",
			"text": largeStr,
		},
	})
	frames := []anthropicSSEFrame{
		{Event: "message_start", Data: json.RawMessage(`{"message":{"usage":{"input_tokens":9}}}`)},
		{Event: "content_block_delta", Data: deltaPayload},
		{Event: "message_delta", Data: json.RawMessage(`{"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":6}}`)},
	}
	for _, frame := range frames {
		if err := transcoder.HandleFrame(frame); err != nil {
			t.Fatalf("HandleFrame(%s) error = %v", frame.Event, err)
		}
	}
	body := rr.Body.String()
	if !strings.Contains(body, "event: response.output_text.delta") {
		t.Fatalf("missing response.output_text.delta")
	}
	if !strings.Contains(body, largeStr) {
		t.Fatalf("missing large string data")
	}
	if !strings.Contains(body, "event: response.completed") {
		t.Fatalf("missing response.completed")
	}
}

func TestOpenAIResponsesStreamTranscoder_EmitsCompletedResponse(t *testing.T) {
	rr := httptest.NewRecorder()
	transcoder := newOpenAIResponsesStreamTranscoder(rr, rr, "resp_test", "gpt-5.4", 456)
	frames := []anthropicSSEFrame{
		{Event: "message_start", Data: json.RawMessage(`{"message":{"usage":{"input_tokens":9}}}`)},
		{Event: "content_block_delta", Data: json.RawMessage(`{"index":0,"delta":{"type":"text_delta","text":"你好"}}`)},
		{Event: "content_block_start", Data: json.RawMessage(`{"index":1,"content_block":{"type":"tool_use","id":"call_2","name":"Read","input":{}}}`)},
		{Event: "content_block_delta", Data: json.RawMessage(`{"index":1,"delta":{"type":"input_json_delta","partial_json":"{\"pat"}}`)},
		{Event: "content_block_delta", Data: json.RawMessage(`{"index":1,"delta":{"type":"input_json_delta","partial_json":"h\":\"a.txt\"}"}}`)},
		{Event: "content_block_stop", Data: json.RawMessage(`{"index":1}`)},
		{Event: "message_delta", Data: json.RawMessage(`{"delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":6}}`)},
	}
	for _, frame := range frames {
		if err := transcoder.HandleFrame(frame); err != nil {
			t.Fatalf("HandleFrame(%s) error = %v", frame.Event, err)
		}
	}
	body := rr.Body.String()
	for _, required := range []string{
		"event: response.created",
		"event: response.in_progress",
		"event: response.output_item.added",
		"event: response.content_part.added",
		"event: response.output_text.delta",
		"event: response.output_text.done",
		"event: response.content_part.done",
		"event: response.output_item.done",
		"event: response.function_call_arguments.delta",
		"event: response.completed",
	} {
		if !strings.Contains(body, required) {
			t.Fatalf("missing %s in body:\n%s", required, body)
		}
	}
	if !strings.Contains(body, "你好") {
		t.Fatalf("missing text content: %s", body)
	}
	if !strings.Contains(body, `a.txt`) {
		t.Fatalf("missing function call arguments: %s", body)
	}
}

func TestOpenAIResponsesStreamTranscoder_EmptyContent(t *testing.T) {
	rr := httptest.NewRecorder()
	transcoder := newOpenAIResponsesStreamTranscoder(rr, rr, "resp_empty", "claude-opus-4.6", 789)
	frames := []anthropicSSEFrame{
		{Event: "message_start", Data: json.RawMessage(`{"message":{"usage":{"input_tokens":5}}}`)},
		{Event: "content_block_start", Data: json.RawMessage(`{"index":0,"content_block":{"type":"thinking","thinking":""}}`)},
		{Event: "content_block_delta", Data: json.RawMessage(`{"index":0,"delta":{"type":"thinking_delta","thinking":""}}`)},
		{Event: "content_block_stop", Data: json.RawMessage(`{"index":0}`)},
		{Event: "content_block_start", Data: json.RawMessage(`{"index":1,"content_block":{"type":"text","text":""}}`)},
		{Event: "content_block_delta", Data: json.RawMessage(`{"index":1,"delta":{"type":"text_delta","text":""}}`)},
		{Event: "content_block_stop", Data: json.RawMessage(`{"index":1}`)},
		{Event: "message_delta", Data: json.RawMessage(`{"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":20}}`)},
	}
	for _, frame := range frames {
		if err := transcoder.HandleFrame(frame); err != nil {
			t.Fatalf("HandleFrame(%s) error = %v", frame.Event, err)
		}
	}
	body := rr.Body.String()
	if strings.Contains(body, "response.reasoning_summary_text.delta") {
		t.Fatalf("body should not contain response.reasoning_summary_text.delta: %s", body)
	}
	if strings.Contains(body, "response.output_text.delta") {
		t.Fatalf("body should not contain response.output_text.delta: %s", body)
	}
}

func TestOpenAIResponsesStreamTranscoder_ThinkingBlocks(t *testing.T) {
	rr := httptest.NewRecorder()
	transcoder := newOpenAIResponsesStreamTranscoder(rr, rr, "resp_think", "claude-opus-4.6", 789)
	frames := []anthropicSSEFrame{
		{Event: "message_start", Data: json.RawMessage(`{"message":{"usage":{"input_tokens":5}}}`)},
		{Event: "content_block_start", Data: json.RawMessage(`{"index":0,"content_block":{"type":"thinking","thinking":""}}`)},
		{Event: "content_block_delta", Data: json.RawMessage(`{"index":0,"delta":{"type":"thinking_delta","thinking":"Let me think..."}}`)},
		{Event: "content_block_delta", Data: json.RawMessage(`{"index":0,"delta":{"type":"signature_delta","signature":"sig123"}}`)},
		{Event: "content_block_stop", Data: json.RawMessage(`{"index":0}`)},
		{Event: "content_block_start", Data: json.RawMessage(`{"index":1,"content_block":{"type":"text","text":""}}`)},
		{Event: "content_block_delta", Data: json.RawMessage(`{"index":1,"delta":{"type":"text_delta","text":"Hello!"}}`)},
		{Event: "content_block_stop", Data: json.RawMessage(`{"index":1}`)},
		{Event: "message_delta", Data: json.RawMessage(`{"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":20}}`)},
	}
	for _, frame := range frames {
		if err := transcoder.HandleFrame(frame); err != nil {
			t.Fatalf("HandleFrame(%s) error = %v", frame.Event, err)
		}
	}
	body := rr.Body.String()
	for _, required := range []string{
		"event: response.created",
		"event: response.in_progress",
		"event: response.output_item.added",
		"event: response.reasoning_summary_part.added",
		"event: response.reasoning_summary_text.delta",
		"event: response.reasoning_summary_text.done",
		"event: response.reasoning_summary_part.done",
		"event: response.content_part.added",
		"event: response.output_text.delta",
		"event: response.output_text.done",
		"event: response.content_part.done",
		"event: response.output_item.done",
		"event: response.completed",
	} {
		if !strings.Contains(body, required) {
			t.Fatalf("missing %s in body:\n%s", required, body)
		}
	}
	if !strings.Contains(body, "Let me think...") {
		t.Fatalf("missing thinking text in body:\n%s", body)
	}
	if !strings.Contains(body, "Hello!") {
		t.Fatalf("missing text content in body:\n%s", body)
	}
}

func TestOpenAIChatStreamTranscoder_EmptyContent(t *testing.T) {
	rr := httptest.NewRecorder()
	transcoder := newOpenAIChatStreamTranscoder(rr, rr, "chatcmpl_empty", "gpt-5.4", 123, true)
	frames := []anthropicSSEFrame{
		{Event: "message_start", Data: json.RawMessage(`{"message":{"usage":{"input_tokens":11}}}`)},
		{Event: "content_block_start", Data: json.RawMessage(`{"index":0,"content_block":{"type":"text","text":""}}`)},
		{Event: "content_block_delta", Data: json.RawMessage(`{"index":0,"delta":{"type":"text_delta","text":""}}`)},
		{Event: "message_delta", Data: json.RawMessage(`{"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":7}}`)},
		{Event: "message_stop", Data: json.RawMessage(`{"type":"message_stop"}`)},
	}
	for _, frame := range frames {
		if err := transcoder.HandleFrame(frame); err != nil {
			t.Fatalf("HandleFrame(%s) error = %v", frame.Event, err)
		}
	}
	body := rr.Body.String()
	if strings.Contains(body, `"content":""`) || strings.Contains(body, `"content": ""`) {
		t.Fatalf("body should not contain empty content chunks: %s", body)
	}
}

func TestOpenAIChatStreamTranscoder_LargeChunks(t *testing.T) {
	rr := httptest.NewRecorder()
	transcoder := newOpenAIChatStreamTranscoder(rr, rr, "chatcmpl_large", "gpt-5.4", 123, true)
	largeStr := strings.Repeat("a", 100000)
	deltaPayload, _ := json.Marshal(map[string]interface{}{
		"index": 0,
		"delta": map[string]interface{}{
			"type": "text_delta",
			"text": largeStr,
		},
	})
	frames := []anthropicSSEFrame{
		{Event: "message_start", Data: json.RawMessage(`{"message":{"usage":{"input_tokens":11}}}`)},
		{Event: "content_block_start", Data: json.RawMessage(`{"index":0,"content_block":{"type":"text","text":""}}`)},
		{Event: "content_block_delta", Data: deltaPayload},
		{Event: "message_delta", Data: json.RawMessage(`{"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":7}}`)},
		{Event: "message_stop", Data: json.RawMessage(`{"type":"message_stop"}`)},
	}
	for _, frame := range frames {
		if err := transcoder.HandleFrame(frame); err != nil {
			t.Fatalf("HandleFrame(%s) error = %v", frame.Event, err)
		}
	}
	body := rr.Body.String()
	if !strings.Contains(body, "chat.completion.chunk") {
		t.Fatalf("body missing chat.completion.chunk")
	}
	if !strings.Contains(body, largeStr) {
		t.Fatalf("body missing large string data")
	}
	if !strings.Contains(body, "data: [DONE]") {
		t.Fatalf("body missing DONE")
	}
}

func TestOpenAIChatStreamTranscoder_EmitsTextDelta(t *testing.T) {
	rr := httptest.NewRecorder()
	transcoder := newOpenAIChatStreamTranscoder(rr, rr, "chatcmpl_test", "gpt-5.4", 123, true)
	frames := []anthropicSSEFrame{
		{Event: "message_start", Data: json.RawMessage(`{"message":{"usage":{"input_tokens":11}}}`)},
		{Event: "content_block_start", Data: json.RawMessage(`{"index":0,"content_block":{"type":"text","text":""}}`)},
		{Event: "content_block_delta", Data: json.RawMessage(`{"index":0,"delta":{"type":"text_delta","text":"Hello World!"}}`)},
		{Event: "message_delta", Data: json.RawMessage(`{"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":7}}`)},
		{Event: "message_stop", Data: json.RawMessage(`{"type":"message_stop"}`)},
	}
	for _, frame := range frames {
		if err := transcoder.HandleFrame(frame); err != nil {
			t.Fatalf("HandleFrame(%s) error = %v", frame.Event, err)
		}
	}
	body := rr.Body.String()
	if !strings.Contains(body, "chat.completion.chunk") {
		t.Fatalf("body missing chat.completion.chunk: %s", body)
	}
	if !strings.Contains(body, `"content":"Hello World!"`) {
		t.Fatalf("body missing text delta data: %s", body)
	}
	if !strings.Contains(body, `"usage":{`) || !strings.Contains(body, `"prompt_tokens":11`) || !strings.Contains(body, `"completion_tokens":7`) || !strings.Contains(body, `"total_tokens":18`) {
		t.Fatalf("body missing usage chunk: %s", body)
	}
	if !strings.Contains(body, "data: [DONE]") {
		t.Fatalf("body missing DONE: %s", body)
	}
}

func TestOpenAIChatStreamTranscoder_ErrorHandling(t *testing.T) {
	rr := httptest.NewRecorder()
	transcoder := newOpenAIChatStreamTranscoder(rr, rr, "chatcmpl_test", "gpt-5.4", 123, true)

	invalidJSONFrame := anthropicSSEFrame{
		Event: "message_start",
		Data:  json.RawMessage(`{"message":{"usage":{"input_tokens":11}}`), // Missing closing brace
	}
	if err := transcoder.HandleFrame(invalidJSONFrame); err == nil {
		t.Fatal("Expected error for invalid JSON frame in message_start, got nil")
	}

	invalidContentBlockDeltaFrame := anthropicSSEFrame{
		Event: "content_block_delta",
		Data:  json.RawMessage(`{"delta":{"type":"text_delta","text":"hello"`), // Missing closing brace
	}
	if err := transcoder.HandleFrame(invalidContentBlockDeltaFrame); err == nil {
		t.Fatal("Expected error for invalid JSON frame in content_block_delta, got nil")
	}

	invalidContentBlockStartFrame := anthropicSSEFrame{
		Event: "content_block_start",
		Data:  json.RawMessage(`{"index":0,"content_block":{"type":"text"`), // Missing closing brace
	}
	if err := transcoder.HandleFrame(invalidContentBlockStartFrame); err == nil {
		t.Fatal("Expected error for invalid JSON frame in content_block_start, got nil")
	}

	invalidContentBlockStopFrame := anthropicSSEFrame{
		Event: "content_block_stop",
		Data:  json.RawMessage(`{"index":0`), // Missing closing brace
	}
	if err := transcoder.HandleFrame(invalidContentBlockStopFrame); err == nil {
		t.Fatal("Expected error for invalid JSON frame in content_block_stop, got nil")
	}

	invalidContentBlockStopTypeMismatchFrame := anthropicSSEFrame{
		Event: "content_block_stop",
		Data:  json.RawMessage(`{"index":"not_an_int"}`),
	}
	_ = transcoder.HandleFrame(invalidContentBlockStopTypeMismatchFrame)

	invalidMessageDeltaFrame := anthropicSSEFrame{
		Event: "message_delta",
		Data:  json.RawMessage(`{"delta":{"stop_reason":"end_turn"`), // Missing closing brace
	}
	if err := transcoder.HandleFrame(invalidMessageDeltaFrame); err == nil {
		t.Fatal("Expected error for invalid JSON frame in message_delta, got nil")
	}

	invalidMessageStopFrame := anthropicSSEFrame{
		Event: "message_stop",
		Data:  json.RawMessage(`{"type":"message_stop"`), // Missing closing brace
	}
	if err := transcoder.HandleFrame(invalidMessageStopFrame); err == nil {
		t.Fatal("Expected error for invalid JSON frame in message_stop, got nil")
	}

	invalidPingFrame := anthropicSSEFrame{
		Event: "ping",
		Data:  json.RawMessage(`{"type":"ping"`), // Missing closing brace
	}
	if err := transcoder.HandleFrame(invalidPingFrame); err == nil {
		t.Fatal("Expected error for invalid JSON frame in ping, got nil")
	}

	invalidErrorFrame := anthropicSSEFrame{
		Event: "error",
		Data:  json.RawMessage(`{"error":{"type":"overloaded_error"`), // Missing closing brace
	}
	if err := transcoder.HandleFrame(invalidErrorFrame); err == nil {
		t.Fatal("Expected error for invalid JSON frame in error, got nil")
	}

	invalidUnknownFrame := anthropicSSEFrame{
		Event: "unknown",
		Data:  json.RawMessage(`{"type":"unknown"`), // Missing closing brace
	}
	if err := transcoder.HandleFrame(invalidUnknownFrame); err == nil {
		t.Fatal("Expected error for invalid JSON frame in unknown event, got nil")
	}

	invalidTypeContentBlockDeltaFrame := anthropicSSEFrame{
		Event: "content_block_delta",
		Data:  json.RawMessage(`{"index":"not-an-int","delta":{"type":123,"text":456}}`),
	}
	_ = transcoder.HandleFrame(invalidTypeContentBlockDeltaFrame)

	invalidTypeContentBlockStartFrame := anthropicSSEFrame{
		Event: "content_block_start",
		Data:  json.RawMessage(`{"index":"not-an-int","content_block":{"type":123,"text":456}}`),
	}
	_ = transcoder.HandleFrame(invalidTypeContentBlockStartFrame)

	invalidTypeMessageStartFrame := anthropicSSEFrame{
		Event: "message_start",
		Data:  json.RawMessage(`{"message":{"usage":{"input_tokens":"not-an-int"}}}`),
	}
	_ = transcoder.HandleFrame(invalidTypeMessageStartFrame)

	invalidTypeMessageDeltaFrame := anthropicSSEFrame{
		Event: "message_delta",
		Data:  json.RawMessage(`{"delta":{"stop_reason":123},"usage":{"output_tokens":"not-an-int"}}`),
	}
	_ = transcoder.HandleFrame(invalidTypeMessageDeltaFrame)
}

func TestOpenAIResponsesStreamTranscoder_ErrorHandling(t *testing.T) {
	rr := httptest.NewRecorder()
	transcoder := newOpenAIResponsesStreamTranscoder(rr, rr, "resp_test", "gpt-5.4", 456)

	invalidJSONFrame := anthropicSSEFrame{
		Event: "message_start",
		Data:  json.RawMessage(`{"message":{"usage":{"input_tokens":11}}`), // Missing closing brace
	}
	if err := transcoder.HandleFrame(invalidJSONFrame); err == nil {
		t.Fatal("Expected error for invalid JSON frame in message_start, got nil")
	}

	invalidContentBlockDeltaFrame := anthropicSSEFrame{
		Event: "content_block_delta",
		Data:  json.RawMessage(`{"delta":{"type":"text_delta","text":"hello"`), // Missing closing brace
	}
	if err := transcoder.HandleFrame(invalidContentBlockDeltaFrame); err == nil {
		t.Fatal("Expected error for invalid JSON frame in content_block_delta, got nil")
	}

	invalidContentBlockStartFrame := anthropicSSEFrame{
		Event: "content_block_start",
		Data:  json.RawMessage(`{"index":0,"content_block":{"type":"text"`), // Missing closing brace
	}
	if err := transcoder.HandleFrame(invalidContentBlockStartFrame); err == nil {
		t.Fatal("Expected error for invalid JSON frame in content_block_start, got nil")
	}

	invalidContentBlockStopFrame := anthropicSSEFrame{
		Event: "content_block_stop",
		Data:  json.RawMessage(`{"index":0`), // Missing closing brace
	}
	if err := transcoder.HandleFrame(invalidContentBlockStopFrame); err == nil {
		t.Fatal("Expected error for invalid JSON frame in content_block_stop, got nil")
	}

	invalidContentBlockStopTypeMismatchFrame := anthropicSSEFrame{
		Event: "content_block_stop",
		Data:  json.RawMessage(`{"index":"not_an_int"}`),
	}
	_ = transcoder.HandleFrame(invalidContentBlockStopTypeMismatchFrame)

	invalidMessageDeltaFrame := anthropicSSEFrame{
		Event: "message_delta",
		Data:  json.RawMessage(`{"delta":{"stop_reason":"end_turn"`), // Missing closing brace
	}
	if err := transcoder.HandleFrame(invalidMessageDeltaFrame); err == nil {
		t.Fatal("Expected error for invalid JSON frame in message_delta, got nil")
	}

	invalidMessageStopFrame := anthropicSSEFrame{
		Event: "message_stop",
		Data:  json.RawMessage(`{"type":"message_stop"`), // Missing closing brace
	}
	if err := transcoder.HandleFrame(invalidMessageStopFrame); err == nil {
		t.Fatal("Expected error for invalid JSON frame in message_stop, got nil")
	}

	invalidPingFrame := anthropicSSEFrame{
		Event: "ping",
		Data:  json.RawMessage(`{"type":"ping"`), // Missing closing brace
	}
	if err := transcoder.HandleFrame(invalidPingFrame); err == nil {
		t.Fatal("Expected error for invalid JSON frame in ping, got nil")
	}

	invalidErrorFrame := anthropicSSEFrame{
		Event: "error",
		Data:  json.RawMessage(`{"error":{"type":"overloaded_error"`), // Missing closing brace
	}
	if err := transcoder.HandleFrame(invalidErrorFrame); err == nil {
		t.Fatal("Expected error for invalid JSON frame in error, got nil")
	}

	invalidUnknownFrame := anthropicSSEFrame{
		Event: "unknown",
		Data:  json.RawMessage(`{"type":"unknown"`), // Missing closing brace
	}
	if err := transcoder.HandleFrame(invalidUnknownFrame); err == nil {
		t.Fatal("Expected error for invalid JSON frame in unknown event, got nil")
	}

	invalidTypeContentBlockDeltaFrame := anthropicSSEFrame{
		Event: "content_block_delta",
		Data:  json.RawMessage(`{"index":"not-an-int","delta":{"type":123,"text":456}}`),
	}
	_ = transcoder.HandleFrame(invalidTypeContentBlockDeltaFrame)

	invalidTypeContentBlockStartFrame := anthropicSSEFrame{
		Event: "content_block_start",
		Data:  json.RawMessage(`{"index":"not-an-int","content_block":{"type":123,"text":456}}`),
	}
	_ = transcoder.HandleFrame(invalidTypeContentBlockStartFrame)

	invalidTypeMessageStartFrame := anthropicSSEFrame{
		Event: "message_start",
		Data:  json.RawMessage(`{"message":{"usage":{"input_tokens":"not-an-int"}}}`),
	}
	_ = transcoder.HandleFrame(invalidTypeMessageStartFrame)

	invalidTypeMessageDeltaFrame := anthropicSSEFrame{
		Event: "message_delta",
		Data:  json.RawMessage(`{"delta":{"stop_reason":123},"usage":{"output_tokens":"not-an-int"}}`),
	}
	_ = transcoder.HandleFrame(invalidTypeMessageDeltaFrame)
}

func TestOpenAIErrorNormalization_UnsupportedToolType(t *testing.T) {
	req := &OpenAIChatCompletionRequest{
		Model: "gpt-4",
		Messages: []OpenAIChatMessage{
			{Role: "user", Content: "Hello"},
		},
		Tools: []OpenAITool{{
			Type: "unsupported_type",
		}},
	}

	_, err := convertOpenAIChatCompletionRequest(req)
	if err == nil {
		t.Fatal("Expected error for unsupported tool type")
	}
	if !strings.Contains(err.Error(), "unsupported tool type") {
		t.Errorf("Expected unsupported tool type error, got: %v", err)
	}
}

func TestOpenAIErrorNormalization_UnsupportedMessageRole(t *testing.T) {
	req := &OpenAIChatCompletionRequest{
		Model: "gpt-4",
		Messages: []OpenAIChatMessage{
			{Role: "unsupported_role", Content: "Hello"},
		},
	}

	_, err := convertOpenAIChatCompletionRequest(req)
	if err == nil {
		t.Fatal("Expected error for unsupported message role")
	}
	if !strings.Contains(err.Error(), "unsupported message role") {
		t.Errorf("Expected unsupported message role error, got: %v", err)
	}
}

func TestOpenAIChatStreamTranscoder_ToolCallChunks(t *testing.T) {
	rr := httptest.NewRecorder()
	transcoder := newOpenAIChatStreamTranscoder(rr, rr, "chatcmpl_test", "gpt-5.4", 123, true)
	frames := []anthropicSSEFrame{
		{Event: "message_start", Data: json.RawMessage(`{"message":{"usage":{"input_tokens":11}}}`)},
		{Event: "content_block_start", Data: json.RawMessage(`{"index":0,"content_block":{"type":"tool_use","id":"call_1","name":"Read","input":{}}}`)},
		{Event: "content_block_delta", Data: json.RawMessage(`{"index":0,"delta":{"type":"input_json_delta","partial_json":"{\"f"}}`)},
		{Event: "content_block_delta", Data: json.RawMessage(`{"index":0,"delta":{"type":"input_json_delta","partial_json":"ile\":\"test.go\"}"}}`)},
		{Event: "message_delta", Data: json.RawMessage(`{"delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":7}}`)},
		{Event: "message_stop", Data: json.RawMessage(`{"type":"message_stop"}`)},
	}
	for _, frame := range frames {
		if err := transcoder.HandleFrame(frame); err != nil {
			t.Fatalf("HandleFrame(%s) error = %v", frame.Event, err)
		}
	}
	body := rr.Body.String()
	if !strings.Contains(body, "{\\\"f") || !strings.Contains(body, "ile\\\":\\\"test.go\\\"}") {
		t.Fatalf("body missing split JSON chunks: %s", body)
	}
}

func TestOpenAIResponsesStreamTranscoder_ToolCallChunks(t *testing.T) {
	rr := httptest.NewRecorder()
	transcoder := newOpenAIResponsesStreamTranscoder(rr, rr, "resp_test", "gpt-5.4", 456)
	frames := []anthropicSSEFrame{
		{Event: "message_start", Data: json.RawMessage(`{"message":{"usage":{"input_tokens":9}}}`)},
		{Event: "content_block_start", Data: json.RawMessage(`{"index":0,"content_block":{"type":"tool_use","id":"call_1","name":"Read","input":{}}}`)},
		{Event: "content_block_delta", Data: json.RawMessage(`{"index":0,"delta":{"type":"input_json_delta","partial_json":"{\"f"}}`)},
		{Event: "content_block_delta", Data: json.RawMessage(`{"index":0,"delta":{"type":"input_json_delta","partial_json":"ile\":\"test.go\"}"}}`)},
		{Event: "content_block_stop", Data: json.RawMessage(`{"index":0}`)},
		{Event: "message_delta", Data: json.RawMessage(`{"delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":6}}`)},
	}
	for _, frame := range frames {
		if err := transcoder.HandleFrame(frame); err != nil {
			t.Fatalf("HandleFrame(%s) error = %v", frame.Event, err)
		}
	}
	body := rr.Body.String()
	if !strings.Contains(body, "{\\\"f") || !strings.Contains(body, "ile\\\":\\\"test.go\\\"}") {
		t.Fatalf("body missing split JSON chunks: %s", body)
	}
}

func TestHandleOpenAIChatCompletions_UnsupportedToolType(t *testing.T) {
	reqBody := `{"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}], "tools": [{"type": "unsupported_type"}]}`
	req := httptest.NewRequest("POST", "/v1/chat/completions", strings.NewReader(reqBody))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()

	// Create a dummy pool since HandleOpenAIChatCompletions needs it, though it might not use it if validation fails first.
	// Actually we can just pass nil and it will fail before trying to use it.
	HandleOpenAIChatCompletions(nil).ServeHTTP(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("Expected 400 Bad Request, got %d", w.Code)
	}

	var resp OpenAIErrorResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("Failed to parse response body: %v", err)
	}

	if resp.Error.Type != "invalid_request_error" {
		t.Errorf("Expected invalid_request_error, got %s", resp.Error.Type)
	}
	if !strings.Contains(resp.Error.Message, "unsupported tool type") {
		t.Errorf("Expected error message to contain 'unsupported tool type', got %s", resp.Error.Message)
	}
}

func TestParseAnthropicSSEFrame_Heartbeat(t *testing.T) {
	inputs := []struct {
		name     string
		raw      string
		expected string
		isErr    bool
	}{
		{
			name:     "comment only",
			raw:      ": keep-alive",
			expected: "ping",
			isErr:    false,
		},
		{
			name:     "empty lines",
			raw:      "   \n  \n",
			expected: "ping",
			isErr:    false,
		},
		{
			name:     "empty string",
			raw:      "",
			expected: "ping",
			isErr:    false,
		},
		{
			name:     "actual event",
			raw:      "event: message_start\ndata: {}",
			expected: "message_start",
			isErr:    false,
		},
		{
			name:     "invalid format without event",
			raw:      "data: {}",
			expected: "",
			isErr:    true,
		},
	}

	for _, tt := range inputs {
		t.Run(tt.name, func(t *testing.T) {
			frame, err := parseAnthropicSSEFrame(tt.raw)
			if tt.isErr {
				if err == nil {
					t.Errorf("expected error for %q, got nil", tt.raw)
				}
			} else {
				if err != nil {
					t.Errorf("unexpected error for %q: %v", tt.raw, err)
				}
				if frame.Event != tt.expected {
					t.Errorf("expected event %q, got %q", tt.expected, frame.Event)
				}
			}
		})
	}
}

func TestOpenAIChatStreamTranscoder_Heartbeat(t *testing.T) {
	rr := httptest.NewRecorder()
	transcoder := newOpenAIChatStreamTranscoder(rr, rr, "chat_heartbeat", "gpt-5", 123, true)

	// Ensure that ping frames don't crash the transcoder and don't emit garbage chunks
	pingFrame := anthropicSSEFrame{Event: "ping", Data: nil}
	if err := transcoder.HandleFrame(pingFrame); err != nil {
		t.Fatalf("unexpected error on ping frame: %v", err)
	}

	body := rr.Body.String()
	if body != "" {
		t.Fatalf("expected empty body for ping frame, got: %s", body)
	}
}

func TestOpenAIResponsesStreamTranscoder_Heartbeat(t *testing.T) {
	rr := httptest.NewRecorder()
	transcoder := newOpenAIResponsesStreamTranscoder(rr, rr, "resp_heartbeat", "gpt-5", 123)

	// Ensure that ping frames don't crash the transcoder and don't emit garbage chunks
	pingFrame := anthropicSSEFrame{Event: "ping", Data: nil}
	if err := transcoder.HandleFrame(pingFrame); err != nil {
		t.Fatalf("unexpected error on ping frame: %v", err)
	}

	body := rr.Body.String()
	if body != "" {
		t.Fatalf("expected empty body for ping frame, got: %s", body)
	}
}
