// Package proxy provides reverse proxying and session management for Notion accounts.
//
// Cookie Forwarding:
// The reverse proxy handles cookie forwarding by extracting the targeted Notion
// account's FullCookie from the active proxy session (np_session). It then
// injects this FullCookie into all upstream requests to Notion. This ensures
// that client requests are properly authenticated as the assigned pooled account
// without exposing the raw Notion tokens to the client.
package proxy

import (
	"bytes"
	"net/http"
	"strings"
)

// RequestLoggingMiddleware wraps an http.Handler and logs incoming HTTP requests.
// It ensures that sensitive headers (like Authorization, Cookie, and API keys) are redacted.
func RequestLoggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var buf bytes.Buffer
		buf.WriteString(r.Method + " " + r.URL.Path + "\n")

		for k, v := range r.Header {
			lowerK := strings.ToLower(k)
			if lowerK == "authorization" || lowerK == "cookie" || lowerK == "x-api-key" || lowerK == "token" {
				buf.WriteString(k + ": [REDACTED]\n")
			} else {
				buf.WriteString(k + ": " + strings.Join(v, ",") + "\n")
			}
		}

		LogAPIInputText("req_middleware", "incoming request", buf.String())

		next.ServeHTTP(w, r)
	})
}
