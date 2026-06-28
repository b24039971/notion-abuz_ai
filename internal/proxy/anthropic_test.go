package proxy

import (
	"bytes"
	"net/http/httptest"
	"strings"
	"testing"
)

// mockFlusher wraps httptest.ResponseRecorder to implement http.Flusher
type mockFlusher struct {
	*httptest.ResponseRecorder
}

func (m *mockFlusher) Flush() {}

func TestAnthropicStreaming_ToolCallChunks(t *testing.T) {
	rr := httptest.NewRecorder()
	mf := &mockFlusher{rr}

	sendAnthropicSSE(mf, mf, "content_block_delta", map[string]interface{}{
		"type":  "content_block_delta",
		"index": 0,
		"delta": map[string]interface{}{
			"type":         "input_json_delta",
			"partial_json": "{\"f",
		},
	})

	sendAnthropicSSE(mf, mf, "content_block_delta", map[string]interface{}{
		"type":  "content_block_delta",
		"index": 0,
		"delta": map[string]interface{}{
			"type":         "input_json_delta",
			"partial_json": "ile\":\"test.go\"}",
		},
	})

	body := mf.Body.String()
	if !strings.Contains(body, `{"delta":{"partial_json":"{\"f","type":"input_json_delta"}`) || !strings.Contains(body, `{"delta":{"partial_json":"ile\":\"test.go\"}","type":"input_json_delta"}`) {
		t.Fatalf("body missing split JSON chunks: %s", body)
	}
}

func TestAnthropicHandleFrameRobustness(t *testing.T) {
	// Test that parseNDJSONStream handles malformed/unknown NDJSON events gracefully without panicking

	defer func() {
		if r := recover(); r != nil {
			t.Errorf("parseNDJSONStream panicked on unknown event type: %v", r)
		}
	}()

	malformedStream := bytes.NewBufferString(`{"type": "some_random_unsupported_type", "data": "garbage"}
{"type": "agent-inference", "data": {"unexpected": []}}
{"completely_invalid_json"
`)

	var cb StreamCallback = func(delta string, done bool, usage *UsageInfo) {}

	err := parseNDJSONStream(malformedStream, "test-req", cb, nil, nil, nil, nil, nil, nil)
	if err != nil {
		// an error is fine, as long as it doesn't panic and handles it gracefully
		t.Logf("Returned error as expected or handled gracefully: %v", err)
	}
}

func TestAnthropicHandleFrameRobustness_UnexpectedTypes(t *testing.T) {
	defer func() {
		if r := recover(); r != nil {
			t.Errorf("parseNDJSONStream panicked on unexpected JSON types: %v", r)
		}
	}()

	unexpectedStream := bytes.NewBufferString(`123
"string payload"
null
[]
[1, 2, 3]
{"type": "agent-inference", "value": "not_an_array"}
{"type": "agent-tool-result", "toolCallId": 12345}
{"type": "error", "message": {"nested": "object_instead_of_string"}}
`)

	var cb StreamCallback = func(delta string, done bool, usage *UsageInfo) {}

	err := parseNDJSONStream(unexpectedStream, "test-req", cb, nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Logf("Returned error as expected or handled gracefully: %v", err)
	}
}

func TestAnthropicHandleFrameRobustness_MissingFields(t *testing.T) {
	defer func() {
		if r := recover(); r != nil {
			t.Errorf("parseNDJSONStream panicked on valid JSON missing fields: %v", r)
		}
	}()

	missingFieldsStream := bytes.NewBufferString(`{"type": "agent-inference"}
{"type": "agent-inference", "value": []}
{"type": "patch"}
{"type": "patch", "v": []}
{"type": "patch", "v": [{"o": "a"}]}
{"type": "patch", "v": [{"o": "a", "p": "/value/-"}]}
{"type": "search-status"}
{"type": "error"}
{"type": "agent-tool-result"}
{"type": "call-function"}
`)

	var cb StreamCallback = func(delta string, done bool, usage *UsageInfo) {}

	err := parseNDJSONStream(missingFieldsStream, "test-req", cb, nil, nil, nil, nil, nil, nil)
	if err != nil {
		// an error is fine, as long as it doesn't panic and handles it gracefully
		t.Logf("Returned error as expected or handled gracefully: %v", err)
	}
}

func TestAnthropicHandleFrameRobustness_MissingDeltaFields(t *testing.T) {
	defer func() {
		if r := recover(); r != nil {
			t.Errorf("parseNDJSONStream panicked on valid JSON missing delta fields: %v", r)
		}
	}()

	missingFieldsStream := bytes.NewBufferString(`{"type": "agent-inference", "value": [{"type": "text"}]}
{"type": "agent-inference", "value": [{"type": "thinking"}]}
{"type": "agent-inference", "value": [{"type": "tool_use"}]}
{"type": "agent-inference", "value": [{"type": "tool_use", "name": "search"}]}
{"type": "agent-inference", "value": [{"type": "tool_use", "id": "t1"}]}
{"type": "agent-tool-result", "toolCallId": "t1", "result": {}}
{"type": "agent-search-extracted-results", "toolCallId": "t1", "results": [{}]}
{"type": "patch", "v": [{"o": "a", "p": "/value/0/content"}]}
`)

	var cb StreamCallback = func(delta string, done bool, usage *UsageInfo) {}

	err := parseNDJSONStream(missingFieldsStream, "test-req", cb, nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Logf("Returned error as expected or handled gracefully: %v", err)
	}
}

func TestAnthropicHandleFrameRobustness_UnknownEvent(t *testing.T) {
	defer func() {
		if r := recover(); r != nil {
			t.Errorf("parseNDJSONStream panicked on unknown event type string: %v", r)
		}
	}()

	// Testing specifically an unknown future event type like "new_feature_start"
	unknownEventStream := bytes.NewBufferString(`{"type": "new_feature_start", "data": {"something": "here"}}
{"type": "agent-inference"}
`)

	var cb StreamCallback = func(delta string, done bool, usage *UsageInfo) {}

	err := parseNDJSONStream(unknownEventStream, "test-req", cb, nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Logf("Returned error as expected or handled gracefully: %v", err)
	}
}

func TestAnthropicHandleFrameRobustness_InvalidEventNameTypes(t *testing.T) {
	defer func() {
		if r := recover(); r != nil {
			t.Errorf("parseNDJSONStream panicked on invalid nested payload types: %v", r)
		}
	}()

	invalidNestedStream := bytes.NewBufferString(`{"type": "agent-inference", "value": [{"type": "tool_use", "id": 123, "name": 456}]}
{"type": "agent-tool-result", "toolCallId": ["array", "instead", "of", "string"]}
{"type": "researcher-next-steps", "value": {"nextSteps": [{"key": 123, "displayName": 456}]}}
`)

	var cb StreamCallback = func(delta string, done bool, usage *UsageInfo) {}

	err := parseNDJSONStream(invalidNestedStream, "test-req", cb, nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Logf("Returned error as expected or handled gracefully: %v", err)
	}
}

func TestAnthropicHandleFrameRobustness_InvalidEventNameFormat(t *testing.T) {
	defer func() {
		if r := recover(); r != nil {
			t.Errorf("parseNDJSONStream panicked on invalid event name format: %v", r)
		}
	}()

	invalidEventStream := bytes.NewBufferString(`{"type": 123, "data": {}}
{"type": ["not-a-string"], "data": {}}
{"type": {"obj": true}, "data": {}}
{"type": null, "data": {}}
{"type": "agent-inference"}
`)

	var cb StreamCallback = func(delta string, done bool, usage *UsageInfo) {}

	err := parseNDJSONStream(invalidEventStream, "test-req", cb, nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Logf("Returned error as expected or handled gracefully: %v", err)
	}
}

func TestAnthropicTrimCitationContext_Robustness(t *testing.T) {
	// Test that trimCitationContext handles various edge-case string lengths gracefully without panicking or out-of-bounds slicing

	defer func() {
		if r := recover(); r != nil {
			t.Errorf("trimCitationContext panicked: %v", r)
		}
	}()

	testCases := []string{
		"",                           // empty string
		"a",                          // extremely short string
		strings.Repeat("a", 50),      // shorter than maxRunes (320)
		strings.Repeat("a", 320),     // exactly maxRunes
		strings.Repeat("a", 500),     // longer than maxRunes
		strings.Repeat("こんにちは", 100), // multi-byte characters
		"word",                       // single word
		strings.Repeat(" ", 400),     // only spaces
	}

	for _, tc := range testCases {
		res := trimCitationContext(tc)
		runes := []rune(res)
		if len(runes) > 320 {
			t.Errorf("trimCitationContext returned string longer than maxRunes (320): len = %d", len(runes))
		}
	}
}

func TestAnthropicHandleFrameRobustness_UnknownPrimitivePayloads(t *testing.T) {
	defer func() {
		if r := recover(); r != nil {
			t.Errorf("parseNDJSONStream panicked on unknown primitive payloads: %v", r)
		}
	}()

	primitiveStream := bytes.NewBufferString(`null
false
true
""
" "
-1
0.5
`)

	var cb StreamCallback = func(delta string, done bool, usage *UsageInfo) {}

	err := parseNDJSONStream(primitiveStream, "test-req", cb, nil, nil, nil, nil, nil, nil)
	if err != nil {
		t.Logf("Returned error as expected or handled gracefully: %v", err)
	}
}

func TestAnthropicTrimCitationContext_Empty(t *testing.T) {
	defer func() {
		if r := recover(); r != nil {
			t.Errorf("trimCitationContext panicked on empty string: %v", r)
		}
	}()

	res := trimCitationContext("")
	if res != "" {
		t.Errorf("Expected empty string, got %q", res)
	}
}

func TestAnthropicTrimCitationContext_ShortContexts(t *testing.T) {
	defer func() {
		if r := recover(); r != nil {
			t.Errorf("trimCitationContext panicked on short context: %v", r)
		}
	}()

	testCases := []string{
		"a",
		"hello",
		"short string test",
		strings.Repeat("a", 10),
		strings.Repeat("a", 20),
		strings.Repeat("a", 30),
		strings.Repeat("a", 40),
		strings.Repeat("a", 49),
	}

	for _, tc := range testCases {
		res := trimCitationContext(tc)
		if res != tc {
			t.Errorf("Expected string %q to be unchanged, got %q", tc, res)
		}
	}
}

func TestAnthropicTrimCitationContext_Malformed(t *testing.T) {
	defer func() {
		if r := recover(); r != nil {
			t.Errorf("trimCitationContext panicked on malformed context: %v", r)
		}
	}()

	testCases := []string{
		strings.Repeat("word", 100), // single very long word (no spaces)
		strings.Repeat(" ", 400),    // just spaces
		"\n\t\r",                    // just control characters/whitespace
		strings.Repeat("\n", 400),   // very long newline sequence
	}

	for _, tc := range testCases {
		res := trimCitationContext(tc)
		runes := []rune(res)
		if len(runes) > 320 {
			t.Errorf("trimCitationContext returned string longer than maxRunes (320): len = %d", len(runes))
		}

		// If the input is <= 320 runes, the output should match the input
		origRunes := []rune(tc)
		if len(origRunes) <= 320 && res != tc {
			t.Errorf("Expected string %q to be unchanged, got %q", tc, res)
		}
	}
}
