package msalogin

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
)

const (
	defaultOutlookTokenEndpoint = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
	defaultGraphBase            = "https://graph.microsoft.com"
)

// OutlookClient wraps the minimal Microsoft Graph calls we need to read the
// MS proofs verification email. It reuses an OAuth refresh_token to mint a
// fresh access_token, then queries /me/messages for a 6-digit code.
type OutlookClient struct {
	http      *http.Client
	tokenURL  string
	graphBase string // e.g. "https://graph.microsoft.com"; tests override
}

// NewOutlookClient returns a client with a sane HTTP timeout. The same
// instance can be reused across multiple Refresh / List calls.
func NewOutlookClient(timeout time.Duration) *OutlookClient {
	return NewOutlookClientWithEndpoint(timeout, defaultOutlookTokenEndpoint)
}

// NewOutlookClientWithEndpoint is like NewOutlookClient but lets callers
// (only tests, in practice) point Refresh at a stub server. Production
// code should use NewOutlookClient and let the default endpoint hit the
// real Microsoft consumers token URL.
func NewOutlookClientWithEndpoint(timeout time.Duration, tokenURL string) *OutlookClient {
	if timeout <= 0 {
		timeout = 30 * time.Second
	}
	if tokenURL == "" {
		tokenURL = defaultOutlookTokenEndpoint
	}
	return &OutlookClient{
		http:      &http.Client{Timeout: timeout},
		tokenURL:  tokenURL,
		graphBase: defaultGraphBase,
	}
}

// PermanentRefreshError marks an OAuth refresh failure that cannot
// possibly succeed on retry — typically `invalid_grant` (revoked /
// expired / wrong scope) or `invalid_client` (app deleted, wrong
// client_id). WaitVerificationCode short-circuits when it sees one of
// these so we don't burn the full timeout on a doomed mailbox.
type PermanentRefreshError struct {
	OAuthError string // e.g. "invalid_grant"
	Inner      error
}

func (e *PermanentRefreshError) Error() string {
	if e.Inner == nil {
		return fmt.Sprintf("permanent refresh error: %s", e.OAuthError)
	}
	return e.Inner.Error()
}

func (e *PermanentRefreshError) Unwrap() error { return e.Inner }

// IsPermanentRefreshError reports whether err (or anything it wraps)
// is a PermanentRefreshError. Callers use this to decide whether to
// keep polling or to give up the whole proofs flow immediately.
func IsPermanentRefreshError(err error) bool {
	var p *PermanentRefreshError
	return errors.As(err, &p)
}

// newPermanentRefreshError is the sole constructor; we keep it private
// so the classification stays funneled through Refresh's response
// parser (and stays mockable from tests via the exported sentinel).
func newPermanentRefreshError(inner error) *PermanentRefreshError {
	return &PermanentRefreshError{OAuthError: oauthErrorCode(inner), Inner: inner}
}

// oauthErrorCode digs the `error` field out of an inner error's message
// when present (it is the canonical OAuth2 error code from RFC 6749).
// We only need this for the human-friendly Error() string.
func oauthErrorCode(err error) string {
	if err == nil {
		return ""
	}
	msg := err.Error()
	for _, code := range permanentOAuthErrors {
		if strings.Contains(msg, code) {
			return code
		}
	}
	return "unknown"
}

// permanentOAuthErrors is the closed set of OAuth `error` field values
// that the IETF + Microsoft consider terminal for a refresh_token: the
// caller's grant is gone and no amount of retrying brings it back.
//
// We intentionally exclude `interaction_required` (might be solvable by
// a re-consent) and any 5xx/network failure (transient). New entries
// must be permanent in the literal "will never succeed" sense.
var permanentOAuthErrors = []string{
	"invalid_grant",
	"invalid_client",
	"unauthorized_client",
}

// outlookRefreshResp is the relevant subset of the OAuth2 token endpoint
// response.
type outlookRefreshResp struct {
	AccessToken string `json:"access_token"`
	ExpiresIn   int    `json:"expires_in"`
	Scope       string `json:"scope"`
}

// Refresh exchanges a long-lived refresh_token for a short-lived access_token
// targeting Microsoft Graph. We request `https://graph.microsoft.com/.default`
// rather than concrete sub-scopes because the FOCI consumer client ids
// shipped with these refresh_tokens (e.g. 9e5f94bc-...) reject
// incremental consent at the consumers/oauth2 endpoint with AADSTS70000.
// `/.default` reuses every scope consented at app-registration time —
// which for these clients includes Mail.Read and IMAP.AccessAsUser.All —
// so the resulting access_token can read the mailbox.
//
// Errors classify into two buckets:
//   - PermanentRefreshError: OAuth `error` field is one of
//     permanentOAuthErrors (e.g. invalid_grant on AADSTS70000); the
//     refresh_token is dead and no retry will help.
//   - Generic error: HTTP 5xx, network failures, malformed JSON; safe
//     to retry within the proofs polling window.
func (c *OutlookClient) Refresh(refreshToken, clientID string) (string, error) {
	if refreshToken == "" || clientID == "" {
		return "", fmt.Errorf("refresh: missing refresh_token or client_id")
	}
	endpoint := c.tokenURL
	if endpoint == "" {
		endpoint = defaultOutlookTokenEndpoint
	}
	form := url.Values{
		"client_id":     {clientID},
		"refresh_token": {refreshToken},
		"grant_type":    {"refresh_token"},
		// FOCI MSA consumer refresh_tokens (e.g. client_id
		// 9e5f94bc-e8a4-4e73-b8be-63364c29d753 — Microsoft's Outlook
		// mobile family) reject incremental scopes with
		// "AADSTS70000: scopes requested are unauthorized or expired".
		// `/.default` tells AAD to issue an access_token covering
		// every scope already consented at app-registration time.
		"scope": {"https://graph.microsoft.com/.default"},
	}
	req, err := http.NewRequest(
		"POST",
		endpoint,
		strings.NewReader(form.Encode()),
	)
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := c.http.Do(req)
	if err != nil {
		return "", fmt.Errorf("refresh: %w", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 {
		base := fmt.Errorf("refresh: HTTP %d: %s", resp.StatusCode, truncate(string(body), 240))
		// Only 4xx + a recognised OAuth `error` code is permanent; 5xx
		// is treated as transient even when MS labels it `server_error`.
		if resp.StatusCode >= 400 && resp.StatusCode < 500 {
			if isPermanentOAuthError(body) {
				return "", newPermanentRefreshError(base)
			}
		}
		return "", base
	}
	var data outlookRefreshResp
	if err := json.Unmarshal(body, &data); err != nil {
		return "", fmt.Errorf("refresh: bad JSON: %w", err)
	}
	if data.AccessToken == "" {
		return "", fmt.Errorf("refresh: empty access_token (body=%s)", truncate(string(body), 240))
	}
	// Some FOCI consumer client_ids (notably the Outlook-mobile family
	// `9e5f94bc-...` shipping with hotmail.com / live.com refresh_tokens)
	// hand back a perfectly valid access_token whose granted scopes are
	// IMAP/POP/SMTP only — no Mail.Read. Graph then 401s on /me/messages
	// with no useful body, and the proofs poller silently burns 90s.
	// Detect this early and surface it as a permanent error so the
	// registration job can move on. Until we ship IMAP XOAUTH2 there is
	// nothing useful we can do with these tokens.
	if !scopeContainsMailRead(data.Scope) {
		return "", newPermanentRefreshError(fmt.Errorf(
			"refresh: token granted but missing Mail.Read scope "+
				"(got %q) — Graph mailbox APIs will reject; this "+
				"client_id likely needs IMAP XOAUTH2 fallback",
			data.Scope,
		))
	}
	return data.AccessToken, nil
}

// scopeContainsMailRead reports whether the OAuth `scope` field contains
// any of the Microsoft mail-read scopes that authorize Graph /me/messages.
// We accept Graph and outlook.office.com variants because Microsoft's
// own apps mix them.
func scopeContainsMailRead(scope string) bool {
	if scope == "" {
		return false
	}
	for _, s := range strings.Fields(scope) {
		if strings.HasSuffix(s, "/Mail.Read") || strings.HasSuffix(s, "/Mail.ReadWrite") {
			return true
		}
	}
	return false
}

// isPermanentOAuthError parses an OAuth error response body and reports
// whether the `error` field is one of permanentOAuthErrors. We accept
// any body shape that JSON-decodes; non-JSON bodies are treated as
// transient (Microsoft sometimes returns HTML for 502/503).
func isPermanentOAuthError(body []byte) bool {
	var data struct {
		Error string `json:"error"`
	}
	if err := json.Unmarshal(body, &data); err != nil {
		return false
	}
	for _, code := range permanentOAuthErrors {
		if data.Error == code {
			return true
		}
	}
	return false
}

// graphMessagesResp models the relevant /me/messages payload.
type graphMessagesResp struct {
	Value []struct {
		ID          string `json:"id"`
		Subject     string `json:"subject"`
		BodyPreview string `json:"bodyPreview"`
		ReceivedAt  string `json:"receivedDateTime"`
		Body        struct {
			Content     string `json:"content"`
			ContentType string `json:"contentType"`
		} `json:"body"`
	} `json:"value"`
}

// listMessages returns the most recent N messages across the entire mailbox.
func (c *OutlookClient) listMessages(accessToken string, top int) (*graphMessagesResp, error) {
	if top <= 0 {
		top = 10
	}
	base := c.graphBase
	if base == "" {
		base = defaultGraphBase
	}
	endpoint := fmt.Sprintf(
		"%s/v1.0/me/messages"+
			"?$top=%d&$orderby=receivedDateTime%%20desc"+
			"&$select=id,subject,bodyPreview,receivedDateTime,body",
		base, top,
	)
	req, err := http.NewRequest("GET", endpoint, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+accessToken)
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("list messages: HTTP %d: %s", resp.StatusCode, truncate(string(body), 240))
	}
	var out graphMessagesResp
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, fmt.Errorf("list messages: bad JSON: %w", err)
	}
	return &out, nil
}

// CollectExistingIDs returns a snapshot of message IDs currently in the
// mailbox AND a watermark (`ts=<max receivedDateTime>;seq=0`) so a later
// WaitVerificationCode can require "strictly newer than pre-scan".
//
// ID dedup alone is not enough on Graph: the message `id` field is
// MUTABLE and changes when the server auto-categorizes / moves a message
// (which happens within seconds of arrival for Microsoft account
// verification mails). Without a date watermark, a stale code email left
// from a previous round of the proofs flow gets served back as if it
// were freshly arrived — driving the proofs handler into an infinite
// re-submit loop on the same code.
func (c *OutlookClient) CollectExistingIDs(refreshToken, clientID string) map[string]struct{} {
	out := map[string]struct{}{}
	access, err := c.Refresh(refreshToken, clientID)
	if err != nil {
		// Even on failure, leave a wall-clock watermark so the
		// poller doesn't accept arbitrarily-old mail.
		out[imapWatermarkKey] = struct{}{}
		out[fmt.Sprintf("ts=%s;seq=0", time.Now().UTC().Format(time.RFC3339))] = struct{}{}
		return out
	}
	maxTS := time.Time{}
	if msgs, err := c.listMessages(access, 10); err == nil {
		for _, m := range msgs.Value {
			out[m.ID] = struct{}{}
			if t, err := time.Parse(time.RFC3339, m.ReceivedAt); err == nil {
				if t.After(maxTS) {
					maxTS = t
				}
			}
		}
	}
	if maxTS.IsZero() {
		maxTS = time.Now().UTC()
	}
	out[imapWatermarkKey] = struct{}{}
	out[fmt.Sprintf("ts=%s;seq=0", maxTS.UTC().Format(time.RFC3339))] = struct{}{}
	return out
}

var sixDigitCodeRE = regexp.MustCompile(`(?:^|[^\d])(\d{6})(?:[^\d]|$)`)

// extractVerificationCode looks for a 6-digit code in either the body or the
// subject of a Microsoft account verification email. We accept any 6-digit
// number; MS templates always carry exactly one.
func extractVerificationCode(subject, preview, body string) string {
	for _, src := range []string{preview, subject, body} {
		if src == "" {
			continue
		}
		if m := sixDigitCodeRE.FindStringSubmatch(src); m != nil {
			return m[1]
		}
	}
	return ""
}

// WaitVerificationCode polls the mailbox until a new message arrives whose
// contents include a 6-digit code, or the timeout expires.
func (c *OutlookClient) WaitVerificationCode(
	refreshToken, clientID string,
	skipIDs map[string]struct{},
	timeout, pollInterval time.Duration,
) (string, error) {
	if timeout <= 0 {
		timeout = 90 * time.Second
	}
	if pollInterval <= 0 {
		pollInterval = 5 * time.Second
	}

	deadline := time.Now().Add(timeout)
	waterTS, _ := extractWatermark(skipIDs)
	log.Printf("[outlook] watermark: ts=%s", waterTS.Format(time.RFC3339))

	pollIdx := 0
	for time.Now().Before(deadline) {
		pollIdx++
		access, err := c.Refresh(refreshToken, clientID)
		if err != nil {
			// AADSTS70000 / invalid_grant / invalid_client are
			// terminal — the backup refresh_token is dead. Don't
			// burn the rest of the timeout window pretending it
			// might recover; bail so the caller can map this to
			// errKindProofsRequired and move on to the next account.
			if IsPermanentRefreshError(err) {
				log.Printf("[outlook] refresh permanently failed (giving up): %v", err)
				return "", err
			}
			log.Printf("[outlook] refresh while polling: %v", err)
			time.Sleep(pollInterval)
			continue
		}
		msgs, err := c.listMessages(access, 10)
		if err != nil {
			log.Printf("[outlook] list while polling: %v", err)
			time.Sleep(pollInterval)
			continue
		}
		for _, m := range msgs.Value {
			if _, skip := skipIDs[m.ID]; skip {
				continue
			}
			// Watermark guard — required because Graph `id` is
			// mutable (it changes when MS auto-categorizes or
			// moves a message), so ID dedup alone leaks stale
			// code mail across rounds of the proofs flow.
			if t, err := time.Parse(time.RFC3339, m.ReceivedAt); err == nil {
				if !t.After(waterTS) {
					continue
				}
			} else {
				// Couldn't parse — be conservative and
				// reject; better to time out than to hand
				// back a stale code that loops MS forever.
				log.Printf("[outlook] poll#%d: skip msg id=%s, unparseable receivedDateTime=%q",
					pollIdx, truncate(m.ID, 40), m.ReceivedAt)
				continue
			}
			subj := strings.ToLower(m.Subject)
			if !strings.Contains(subj, "microsoft") &&
				!strings.Contains(subj, "verify") &&
				!strings.Contains(subj, "code") &&
				!strings.Contains(subj, "verification") &&
				!strings.Contains(subj, "security") &&
				!strings.Contains(subj, "登录") &&
				!strings.Contains(subj, "代码") &&
				!strings.Contains(subj, "验证") {
				continue
			}
			if code := extractVerificationCode(m.Subject, m.BodyPreview, m.Body.Content); code != "" {
				log.Printf("[outlook] poll#%d: extracted code=%s from id=%s receivedAt=%s",
					pollIdx, code, truncate(m.ID, 40), m.ReceivedAt)
				return code, nil
			}
		}
		time.Sleep(pollInterval)
	}
	return "", fmt.Errorf("timed out waiting for verification code (%s)", timeout)
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}
