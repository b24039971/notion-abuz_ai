package proxy

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestDashboardAuth_LoginEndpoints(t *testing.T) {
	// 1. Create stored hash
	plaintext := "my-secret-password"
	storedHash := HashAdminPassword(plaintext)

	// Server extracts expected client hash
	clientHash := AdminPasswordHash(storedHash)

	auth := NewDashboardAuth(storedHash, "api-key")

	// Test successful login
	t.Run("correct password", func(t *testing.T) {
		body, _ := json.Marshal(map[string]string{"hash": clientHash})
		req := httptest.NewRequest("POST", "/auth/login", bytes.NewReader(body))
		w := httptest.NewRecorder()

		handler := auth.HandleAuthLogin()
		handler(w, req)

		if w.Code != http.StatusOK {
			t.Errorf("expected status OK, got %d", w.Code)
		}

		var resp map[string]string
		json.Unmarshal(w.Body.Bytes(), &resp)
		if resp["status"] != "ok" {
			t.Errorf("expected status ok, got %v", resp)
		}
	})

	// Test incorrect password
	t.Run("incorrect password", func(t *testing.T) {
		body, _ := json.Marshal(map[string]string{"hash": "wronghash"})
		req := httptest.NewRequest("POST", "/auth/login", bytes.NewReader(body))
		w := httptest.NewRecorder()

		handler := auth.HandleAuthLogin()
		handler(w, req)

		if w.Code != http.StatusUnauthorized {
			t.Errorf("expected status Unauthorized, got %d", w.Code)
		}
	})
}
