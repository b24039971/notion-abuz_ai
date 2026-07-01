package proxy

import (
	"bytes"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// Tests that a Notion persona leakage payload triggers the exact bridge decision log natively.
func TestEnsureNotionPersonaLeakageLoggedAsDecision(t *testing.T) {
	// A mock server that responds with NDJSON format representing identity drift text.
	// For CallInference stream parsing, we need a complete NDJSON `agent-inference` message
	// with a `step.FinishedAt` to trigger the final handling of the text.
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/x-ndjson")
		w.WriteHeader(http.StatusOK)

		// 1) Submit text via agent-inference (Notion stream protocol)
		w.Write([]byte(`{"type": "agent-inference", "id":"test", "value": [{"type":"text","content":"I am Notion AI, I cannot access your local file system. I don't have the ability to run Bash or Edit tools."}]}` + "\n"))

		// 2) Finish inference turn (trigger cb with true)
		w.Write([]byte(`{"type": "agent-inference", "id":"test", "value": [], "finishedAt":"2023-01-01T00:00:00Z"}` + "\n"))
	}))
	defer ts.Close()

	// Override NotionAPIBase and transport temporarily
	origBase := NotionAPIBase
	NotionAPIBase = ts.URL
	defer func() { NotionAPIBase = origBase }()

	origClient := getChromeHTTPClient
	getChromeHTTPClient = func(timeout time.Duration) *http.Client {
		return ts.Client()
	}
	defer func() { getChromeHTTPClient = origClient }()

	var buf bytes.Buffer
	originalOutput := log.Writer()
	log.SetOutput(&buf)
	defer log.SetOutput(originalOutput)

	// Call a handler to trigger CallInference
	acc := &Account{UserEmail: "test@test.com"}
	messages := []ChatMessage{{Role: "user", Content: "test"}}

	// Ensure that it runs inference via NonStream (or Stream) and logs the decision
	_ = handleAnthropicNonStream(
		httptest.NewRecorder(),
		acc,
		messages,
		"claude-3-opus",
		"req_test",
		true, // hasTools
		false,
		false,
		nil,
		false,
		nil,
		nil,
		nil,
	)

	output := buf.String()
	expectedLogFragment := "[bridge] req_test decision: Notion persona leakage explicitly detected"
	if !strings.Contains(output, expectedLogFragment) {
		t.Fatalf("expected observability log to contain %q, but got:\n%s", expectedLogFragment, output)
	}
	expectedMetricFragment := "[metrics] identity_drift: Notion persona leakage"
	if !strings.Contains(output, expectedMetricFragment) {
		t.Fatalf("expected observability metric log to contain %q, but got:\n%s", expectedMetricFragment, output)
	}
}

// Tests that a tool-call refusal payload triggers the exact bridge decision log natively.
func TestEnsureToolCallRefusalLoggedAsDecision(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/x-ndjson")
		w.WriteHeader(http.StatusOK)

		w.Write([]byte(`{"type": "agent-inference", "id":"test", "value": [{"type":"text","content":"I do not have access to run terminal commands such as bash or read or edit local files. You will need to copy and paste this into your coding assistant."}]}` + "\n"))
		w.Write([]byte(`{"type": "agent-inference", "id":"test", "value": [], "finishedAt":"2023-01-01T00:00:00Z"}` + "\n"))
	}))
	defer ts.Close()

	origBase := NotionAPIBase
	NotionAPIBase = ts.URL
	defer func() { NotionAPIBase = origBase }()

	origClient := getChromeHTTPClient
	getChromeHTTPClient = func(timeout time.Duration) *http.Client {
		return ts.Client()
	}
	defer func() { getChromeHTTPClient = origClient }()

	var buf bytes.Buffer
	originalOutput := log.Writer()
	log.SetOutput(&buf)
	defer log.SetOutput(originalOutput)

	acc := &Account{UserEmail: "test2@test.com"}
	messages := []ChatMessage{{Role: "user", Content: "test2"}}

	_ = handleAnthropicNonStream(
		httptest.NewRecorder(), acc, messages, "claude-3-opus", "req_test_2",
		true, false, false, nil, false, nil, nil, nil,
	)

	output := buf.String()
	expectedLogFragment := "[bridge] req_test_2 decision: tool-call refusal explicitly detected"
	if !strings.Contains(output, expectedLogFragment) {
		t.Fatalf("expected observability log to contain %q, but got:\n%s", expectedLogFragment, output)
	}
}

// Tests that a JSON mode loss (tool-call refusal) triggers the session recovery retry loop natively and logs it.
func TestEnsureSessionRecoveryLoggedForToolCallLoss(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/x-ndjson")
		w.WriteHeader(http.StatusOK)

		w.Write([]byte(`{"type": "agent-inference", "id":"test", "value": [{"type":"text","content":"I do not have access to run terminal commands such as bash or read or edit local files. You will need to copy and paste this into your coding assistant."}]}` + "\n"))
		w.Write([]byte(`{"type": "agent-inference", "id":"test", "value": [], "finishedAt":"2023-01-01T00:00:00Z"}` + "\n"))
	}))
	defer ts.Close()

	origBase := NotionAPIBase
	NotionAPIBase = ts.URL
	defer func() { NotionAPIBase = origBase }()

	origClient := getChromeHTTPClient
	getChromeHTTPClient = func(timeout time.Duration) *http.Client {
		return ts.Client()
	}
	defer func() { getChromeHTTPClient = origClient }()

	var buf bytes.Buffer
	originalOutput := log.Writer()
	log.SetOutput(&buf)
	defer log.SetOutput(originalOutput)

	pool := NewAccountPool()
	pool.AddAccount(&Account{UserEmail: "test-recovery@test.com", FullCookie: "mock_cookie"})

	handler := HandleAnthropicMessages(pool)

	reqBody := `{"model":"claude-3-5-sonnet-20241022","messages":[{"role":"user","content":"test recovery"}],"tools":[{"name":"bash","description":"run bash","input_schema":{"type":"object","properties":{"command":{"type":"string"}}}}]}`
	req := httptest.NewRequest("POST", "/v1/messages", strings.NewReader(reqBody))
	req.Header.Set("Content-Type", "application/json")
	rr := httptest.NewRecorder()

	handler.ServeHTTP(rr, req)

	output := buf.String()
	expectedLogFragment1 := "[bridge] decision: retry triggered by ErrToolBridgeNoTool (test-recovery@test.com), clearing session and retrying once with sanitized recovery prompt"
	expectedLogFragment2 := "requesting clean retry"

	if !strings.Contains(output, expectedLogFragment1) {
		t.Fatalf("expected observability log to contain %q, but got:\n%s", expectedLogFragment1, output)
	}
	if !strings.Contains(output, expectedLogFragment2) {
		t.Fatalf("expected observability log to contain %q, but got:\n%s", expectedLogFragment2, output)
	}
}
