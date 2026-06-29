package proxy

import (
	"bytes"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func init() {
	AppConfig = &Config{}
	AppConfig.Timeouts.TLSDialTimeout = 30
}

func TestProxyTimeoutHandling(t *testing.T) {
	// A raw TCP listener that accepts connections but never responds,
	// causing the TLS handshake to time out.
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()

	go func() {
		for {
			conn, err := l.Accept()
			if err != nil {
				return
			}
			// Just hold the connection to cause a timeout
			defer conn.Close()
		}
	}()

	client := getChromeHTTPClient(10 * time.Millisecond)

	req, err := http.NewRequest("GET", "https://"+l.Addr().String(), nil)
	if err != nil {
		t.Fatal(err)
	}

	_, err = client.Do(req)
	if err == nil {
		t.Fatal("expected timeout error")
	}
	if !strings.Contains(err.Error(), "context deadline exceeded") && !strings.Contains(err.Error(), "timeout") {
		t.Errorf("expected timeout error message, got: %v", err)
	}
}

func TestProxyNetworkFailureHandling(t *testing.T) {
	client := getChromeHTTPClient(100 * time.Millisecond)

	req, err := http.NewRequest("GET", "https://127.0.0.1:0", nil)
	if err != nil {
		t.Fatal(err)
	}

	_, err = client.Do(req)
	if err == nil {
		t.Fatal("expected connection refused error")
	}
	if !strings.Contains(err.Error(), "connection refused") && !strings.Contains(err.Error(), "dial tcp") && !strings.Contains(err.Error(), "network is unreachable") {
		t.Errorf("expected connection refused error message, got: %v", err)
	}
}

func TestReverseProxy_BackendFailure(t *testing.T) {
	pool := NewAccountPool()
	rp := NewReverseProxy(pool)

	acc := &Account{UserEmail: "test@example.com", FullCookie: "token_v2=abc;"}
	sess := &ProxySession{Account: acc}

	req := httptest.NewRequest("GET", "/_msgproxy/127.0.0.1:0/path", nil)
	w := httptest.NewRecorder()

	rp.proxyMsgstoreHTTP(w, req, sess, "127.0.0.1:0", "/path")

	if w.Code != http.StatusBadGateway && w.Code != http.StatusInternalServerError {
		t.Errorf("expected error status code for network failure, got %d", w.Code)
	}
}

func TestRequestLogging(t *testing.T) {
	var buf bytes.Buffer
	originalWriter := globalLogWriter.out
	globalLogWriter.out = &buf
	defer func() {
		globalLogWriter.out = originalWriter
	}()

	SetAPILogInputEnabled(true)
	defer SetAPILogInputEnabled(false)

	reqID := "req_123"
	label := "test_label"

	t.Run("LogAPIInputText", func(t *testing.T) {
		buf.Reset()
		text := "sensitive_data_here"
		LogAPIInputText(reqID, label, text)

		output := buf.String()
		if !strings.Contains(output, reqID) {
			t.Errorf("Expected requestID %s in log output", reqID)
		}
		if !strings.Contains(output, label) {
			t.Errorf("Expected label %s in log output", label)
		}
		if !strings.Contains(output, text) {
			t.Errorf("Expected text %s in log output", text)
		}
	})

	t.Run("LogAPIInputJSONBytes", func(t *testing.T) {
		buf.Reset()
		jsonBytes := []byte(`{"key": "value", "password": "secret_password"}`)
		LogAPIInputJSONBytes(reqID, label, jsonBytes)

		output := buf.String()
		if !strings.Contains(output, `"key": "value"`) {
			t.Errorf("Expected JSON keys in log output")
		}
	})

	t.Run("LogAPIInputJSON", func(t *testing.T) {
		buf.Reset()
		data := map[string]string{"foo": "bar"}
		LogAPIInputJSON(reqID, label, data)

		output := buf.String()
		if !strings.Contains(output, `"foo": "bar"`) {
			t.Errorf("Expected JSON payload in log output")
		}
	})

	t.Run("DisabledLogging", func(t *testing.T) {
		buf.Reset()
		SetAPILogInputEnabled(false)
		defer SetAPILogInputEnabled(true)
		LogAPIInputText(reqID, label, "should not be logged")
		if buf.Len() > 0 {
			t.Errorf("Expected empty buffer when logging is disabled, got: %s", buf.String())
		}
	})
}

func TestRequestLoggingMiddleware(t *testing.T) {
	var buf bytes.Buffer
	originalWriter := globalLogWriter.out
	globalLogWriter.out = &buf
	defer func() {
		globalLogWriter.out = originalWriter
	}()

	SetAPILogInputEnabled(true)
	defer SetAPILogInputEnabled(false)

	req := httptest.NewRequest("GET", "/test-path", nil)
	req.Header.Set("Authorization", "Bearer sensitive_token")
	req.Header.Set("Cookie", "session=sensitive_cookie")
	req.Header.Set("X-API-Key", "sensitive_key")
	req.Header.Set("User-Agent", "test-agent")

	handler := RequestLoggingMiddleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("Expected status 200, got %d", rec.Code)
	}

	output := buf.String()

	// Check essential fields
	if !strings.Contains(output, "GET /test-path") {
		t.Errorf("Expected essential request info in log: %s", output)
	}
	if !strings.Contains(output, "User-Agent: test-agent") {
		t.Errorf("Expected User-Agent in log: %s", output)
	}

	// Check redacted fields
	if strings.Contains(output, "sensitive_token") {
		t.Errorf("Expected Authorization header to be redacted, log: %s", output)
	}
	if !strings.Contains(output, "Authorization: [REDACTED]") {
		t.Errorf("Expected Authorization header to show as [REDACTED], log: %s", output)
	}

	if strings.Contains(output, "sensitive_cookie") {
		t.Errorf("Expected Cookie header to be redacted, log: %s", output)
	}
	if !strings.Contains(output, "Cookie: [REDACTED]") {
		t.Errorf("Expected Cookie header to show as [REDACTED], log: %s", output)
	}

	if strings.Contains(output, "sensitive_key") {
		t.Errorf("Expected X-API-Key header to be redacted, log: %s", output)
	}
	if !strings.Contains(output, "X-Api-Key: [REDACTED]") && !strings.Contains(output, "X-API-Key: [REDACTED]") {
		t.Errorf("Expected X-API-Key header to show as [REDACTED], log: %s", output)
	}
}

// TestProxy_StreamTranscoder_MalformedPayloads ensures that the proxy handles corrupted
// or malformed stream payloads without panicking, specifically in the fallback parsing
// logic used by stream transcoders when native tool use is not available.
func TestProxy_StreamTranscoder_MalformedPayloads(t *testing.T) {
	payloads := []string{
		`{"tool_call": {"name": "test", "arguments": [1, 2, 3]}}`,
		`{"name": "test", "arguments": "string"}`,
		`{"tool_call": {"name": "test", "arguments": null}}`,
		`<tool_call>not json</tool_call>`,
		"```json\n{malformed\n```",
		`{"tool_call": `,
		`{"name": "test", "arguments": {"nested": }}`,
		`[1, 2, 3]`,
		`null`,
		`"just a string"`,
		`{"tool_call": null}`,
		`{"tool_call": {"name": "test"}}`, // Missing arguments
	}

	for _, p := range payloads {
		// Ensure this doesn't panic
		parseToolCalls(p)
		parseToolCallJSON(p, 0)
	}
}
