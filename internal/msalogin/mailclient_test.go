package msalogin

import (
	"strings"
	"testing"
	"time"
)

// TestSelectMailClientUsesIMAPForConsumerDomains pins the routing
// table that sets us free from the AADSTS70000 / Graph-401 dead end:
// hotmail.com / live.com / live.cn / live.jp / msn.com tokens are
// FOCI consumer tokens whose granted scopes do NOT include Mail.Read,
// so they must go through IMAP XOAUTH2.
//
// outlook.com (and other modern domains) DO have Mail.Read on their
// tokens, so we keep the Graph fast-path for them.
//
// Mirrors fuckteam/outlook_mail/src/outlook_mail/__init__.py:_IMAP_DOMAINS.
func TestSelectMailClientUsesIMAPForConsumerDomains(t *testing.T) {
	cases := []struct {
		email    string
		wantIMAP bool
		whyDescr string
	}{
		{"alice@hotmail.com", true, "hotmail FOCI tokens lack Mail.Read"},
		{"bob@HOTMAIL.com", true, "domain check must be case-insensitive"},
		{"carol@live.com", true, "live.com FOCI tokens lack Mail.Read"},
		{"dan@live.cn", true, "live.cn region variant"},
		{"emi@live.jp", true, "live.jp region variant"},
		{"fred@msn.com", true, "msn.com legacy Hotmail variant"},
		{"gail@outlook.com", false, "outlook.com tokens have Mail.Read on Graph"},
		{"hank@example.com", false, "unknown domain falls through to Graph"},
	}
	for _, tc := range cases {
		got := SelectMailClient(tc.email, 5*time.Second)
		if got == nil {
			t.Fatalf("nil client for %s", tc.email)
		}
		_, isIMAP := got.(*IMAPMailClient)
		if isIMAP != tc.wantIMAP {
			t.Errorf("%s -> imap=%v want %v (%s)",
				tc.email, isIMAP, tc.wantIMAP, tc.whyDescr)
		}
	}
}

// TestSelectMailClientHandlesEmptyEmail makes sure we don't crash on
// the empty / malformed email case (defensive — caller should never
// pass empty, but we promise not to nil-deref).
func TestSelectMailClientHandlesEmptyEmail(t *testing.T) {
	got := SelectMailClient("", 5*time.Second)
	if got == nil {
		t.Fatalf("nil client even for empty email")
	}
	if _, isIMAP := got.(*IMAPMailClient); isIMAP {
		t.Fatalf("empty email must default to Graph, not IMAP (no user_email to log into IMAP)")
	}
}

// TestMailClientInterface ensures both implementations satisfy the
// shared interface — this is mostly a compile-time guarantee, but
// failing here gives a much clearer error than a downstream caller.
func TestMailClientInterface(t *testing.T) {
	var _ MailClient = (*OutlookClient)(nil)
	var _ MailClient = (*IMAPMailClient)(nil)
}

// TestIMAPClientRefreshUsesOutlookOfficeScope mirrors Python's
// imap_client.IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All".
// Critical: graph.microsoft.com/.default tokens DO NOT work for IMAP
// XOAUTH2 — Microsoft enforces audience separation. Use the
// outlook.office.com audience explicitly.
func TestIMAPClientRefreshUsesOutlookOfficeScope(t *testing.T) {
	srv := newRecordingTokenServer(t, `{"access_token":"AT","expires_in":3600,"scope":"https://outlook.office.com/IMAP.AccessAsUser.All"}`)
	defer srv.Close()

	c := newIMAPMailClientForTest("alice@hotmail.com", 2*time.Second, srv.URL)
	tok, err := c.refreshAccessToken("rt", "cid")
	if err != nil {
		t.Fatalf("refresh: %v", err)
	}
	if tok != "AT" {
		t.Fatalf("token: %s", tok)
	}
	if got := srv.lastForm.Get("scope"); got != "https://outlook.office.com/IMAP.AccessAsUser.All" {
		t.Fatalf("IMAP must request outlook.office.com IMAP scope, got %q", got)
	}
	if !strings.Contains(srv.lastForm.Get("grant_type"), "refresh") {
		t.Fatalf("grant_type: %s", srv.lastForm.Get("grant_type"))
	}
}

// TestIMAPClientRefreshSurfacesPermanentError ensures that the IMAP
// path inherits the same fail-fast classification: invalid_grant on
// these tokens means the user truly has to be re-onboarded; we don't
// want the proofs poller to keep banging.
func TestIMAPClientRefreshSurfacesPermanentError(t *testing.T) {
	srv := newRecordingTokenServer(t, "")
	srv.setStatus(400)
	srv.setBody(`{"error":"invalid_grant","error_description":"AADSTS70000 ..."}`)
	defer srv.Close()

	c := newIMAPMailClientForTest("alice@hotmail.com", 2*time.Second, srv.URL)
	_, err := c.refreshAccessToken("rt", "cid")
	if err == nil {
		t.Fatalf("expected error")
	}
	if !IsPermanentRefreshError(err) {
		t.Fatalf("invalid_grant must be permanent: %v", err)
	}
}

// TestBuildXOAuth2String pins the bizarre but standard XOAUTH2 wire
// format documented at
// https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth.
// Format: "user=<email>\x01auth=Bearer <token>\x01\x01"
// Getting the control bytes wrong means IMAP rejects auth with no
// useful diagnostic — pin it in a test forever.
func TestBuildXOAuth2String(t *testing.T) {
	got := buildXOAuth2String("alice@hotmail.com", "AT123")
	want := "user=alice@hotmail.com\x01auth=Bearer AT123\x01\x01"
	if got != want {
		t.Fatalf("XOAUTH2 wire format mismatch:\n got=%q\nwant=%q", got, want)
	}
}
