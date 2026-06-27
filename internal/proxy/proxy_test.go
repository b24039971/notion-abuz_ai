package proxy

import (
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
