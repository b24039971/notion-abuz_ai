package proxy

import (
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
