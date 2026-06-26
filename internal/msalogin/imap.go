package msalogin

import (
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	imap "github.com/emersion/go-imap/v2"
	"github.com/emersion/go-imap/v2/imapclient"
)

// imapTokenScope is the scope to request from the consumers OAuth2
// token endpoint when we plan to authenticate against IMAP. It MUST
// be the outlook.office.com audience — graph.microsoft.com tokens are
// rejected by Outlook IMAP with no useful diagnostic.
//
// FOCI consumer clients (e.g. 9e5f94bc-…) have this scope pre-consented,
// so the request succeeds without user interaction.
const imapTokenScope = "https://outlook.office.com/IMAP.AccessAsUser.All"

// imapHost is the global Microsoft consumer / hotmail IMAP endpoint.
const imapHost = "outlook.office365.com:993"

// IMAPMailClient reads MS verification codes via IMAP XOAUTH2, the
// only path that works for FOCI consumer tokens whose granted scopes
// don't include Mail.Read. Behaviourally interchangeable with
// OutlookClient (Graph) — both implement MailClient.
type IMAPMailClient struct {
	userEmail string
	timeout   time.Duration

	// tokenURL is overridable for tests (defaults to MS consumers /token).
	tokenURL string

	// http is used only for the OAuth refresh; IMAP is dialed
	// directly with crypto/tls.
	http *http.Client

	// dialIMAP is overridable in tests so we can stub the IMAP
	// server (real Outlook IMAP can't be hit from CI).
	dialIMAP func(host string, timeout time.Duration) (*imapclient.Client, error)

	// tokens caches the most recent access_token per (refresh, client)
	// pair, with an expiry. Production callers re-use one client across
	// many polls to avoid hammering the token endpoint every 5 s.
	tokensMu sync.Mutex
	tokens   map[string]cachedAccessToken
}

type cachedAccessToken struct {
	token   string
	expires time.Time
}

// NewIMAPMailClient returns a client bound to the given user email
// (required for XOAUTH2). Reuse one instance across repeated polls
// to amortize the access_token cache.
func NewIMAPMailClient(userEmail string, timeout time.Duration) *IMAPMailClient {
	if timeout <= 0 {
		timeout = 30 * time.Second
	}
	return &IMAPMailClient{
		userEmail: userEmail,
		timeout:   timeout,
		tokenURL:  defaultOutlookTokenEndpoint,
		http:      &http.Client{Timeout: timeout},
		dialIMAP:  dialIMAPDefault,
		tokens:    map[string]cachedAccessToken{},
	}
}

// newIMAPMailClientForTest returns an IMAPMailClient pointed at a
// stub token endpoint. IMAP itself isn't dialled by the caller in the
// tests that use this helper; tests that exercise IMAP interactions
// override dialIMAP directly.
func newIMAPMailClientForTest(userEmail string, timeout time.Duration, tokenURL string) *IMAPMailClient {
	c := NewIMAPMailClient(userEmail, timeout)
	c.tokenURL = tokenURL
	return c
}

// refreshAccessToken exchanges the long-lived refresh_token for a
// short-lived access_token scoped to outlook.office.com IMAP. Errors
// are classified the same way as OutlookClient.Refresh: invalid_grant
// / invalid_client / unauthorized_client become PermanentRefreshError
// so the proofs poller can fail fast.
//
// Caches access tokens until 5 minutes before their declared expiry.
func (c *IMAPMailClient) refreshAccessToken(refreshToken, clientID string) (string, error) {
	if refreshToken == "" || clientID == "" {
		return "", fmt.Errorf("imap refresh: missing refresh_token or client_id")
	}

	cacheKey := refreshToken + "|" + clientID
	c.tokensMu.Lock()
	if cached, ok := c.tokens[cacheKey]; ok && time.Now().Before(cached.expires) {
		c.tokensMu.Unlock()
		return cached.token, nil
	}
	c.tokensMu.Unlock()

	form := url.Values{
		"client_id":     {clientID},
		"refresh_token": {refreshToken},
		"grant_type":    {"refresh_token"},
		"scope":         {imapTokenScope},
	}
	req, err := http.NewRequest("POST", c.tokenURL, strings.NewReader(form.Encode()))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := c.http.Do(req)
	if err != nil {
		return "", fmt.Errorf("imap refresh: %w", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)

	if resp.StatusCode != 200 {
		base := fmt.Errorf("imap refresh: HTTP %d: %s", resp.StatusCode, truncate(string(body), 240))
		if resp.StatusCode >= 400 && resp.StatusCode < 500 && isPermanentOAuthError(body) {
			return "", newPermanentRefreshError(base)
		}
		return "", base
	}

	var data outlookRefreshResp
	if err := json.Unmarshal(body, &data); err != nil {
		return "", fmt.Errorf("imap refresh: bad JSON: %w", err)
	}
	if data.AccessToken == "" {
		return "", fmt.Errorf("imap refresh: empty access_token (body=%s)", truncate(string(body), 240))
	}

	expIn := data.ExpiresIn
	if expIn <= 0 {
		expIn = 3600
	}
	c.tokensMu.Lock()
	c.tokens[cacheKey] = cachedAccessToken{
		token:   data.AccessToken,
		expires: time.Now().Add(time.Duration(expIn-300) * time.Second),
	}
	c.tokensMu.Unlock()

	return data.AccessToken, nil
}

// dialIMAPDefault opens a TLS connection to outlook.office365.com:993
// and returns a ready imapclient.Client.
func dialIMAPDefault(host string, timeout time.Duration) (*imapclient.Client, error) {
	d := &net.Dialer{Timeout: timeout}
	hostname := strings.SplitN(host, ":", 2)[0]
	conn, err := tls.DialWithDialer(d, "tcp", host, &tls.Config{ServerName: hostname})
	if err != nil {
		return nil, fmt.Errorf("imap dial: %w", err)
	}
	return imapclient.New(conn, &imapclient.Options{}), nil
}

// xoauth2SASL is a minimal SASL Client implementing Microsoft's
// XOAUTH2 mechanism. The wire format is one round trip: send the
// initial response (user=…\x01auth=Bearer …\x01\x01), expect the
// server to either accept (no challenge) or send an error JSON
// challenge (which we simply respond to with empty).
type xoauth2SASL struct {
	user        string
	accessToken string
	sentInitial bool
}

func (s *xoauth2SASL) Start() (mech string, ir []byte, err error) {
	s.sentInitial = true
	return "XOAUTH2", []byte(buildXOAuth2String(s.user, s.accessToken)), nil
}

func (s *xoauth2SASL) Next(challenge []byte) ([]byte, error) {
	// On error, MS sends a JSON blob; the spec says we must answer
	// with an empty client response so the server can issue the
	// final tagged NO. Returning the challenge would be a protocol
	// violation. (We log it for diagnostics only.)
	if len(challenge) > 0 {
		log.Printf("[imap-xoauth2] server challenge: %s", truncate(string(challenge), 200))
	}
	return []byte{}, nil
}

// fetchRecentMessageIDs returns the UIDs of the N most recent INBOX
// messages, newest last (IMAP convention). Used by both
// CollectExistingIDs and WaitVerificationCode.
func (c *IMAPMailClient) fetchRecentMessages(refreshToken, clientID string, top uint32) ([]fetchedMessage, error) {
	access, err := c.refreshAccessToken(refreshToken, clientID)
	if err != nil {
		return nil, err
	}
	conn, err := c.dialIMAP(imapHost, c.timeout)
	if err != nil {
		return nil, err
	}
	defer conn.Close()

	if err := conn.Authenticate(&xoauth2SASL{user: c.userEmail, accessToken: access}); err != nil {
		return nil, fmt.Errorf("imap auth: %w", err)
	}
	if _, err := conn.Select("INBOX", nil).Wait(); err != nil {
		return nil, fmt.Errorf("imap select INBOX: %w", err)
	}

	// Search ALL → get sequence numbers, take last `top` (newest).
	searchData, err := conn.Search(&imap.SearchCriteria{}, nil).Wait()
	if err != nil {
		return nil, fmt.Errorf("imap search: %w", err)
	}
	allNums := searchData.AllSeqNums()
	if len(allNums) == 0 {
		return nil, nil
	}
	from := 0
	if uint32(len(allNums)) > top {
		from = len(allNums) - int(top)
	}
	recent := allNums[from:]

	seqSet := imap.SeqSet{}
	for _, n := range recent {
		seqSet.AddNum(n)
	}

	fetchOpts := &imap.FetchOptions{
		Envelope:    true,
		BodySection: []*imap.FetchItemBodySection{{}}, // RFC822 full body
	}
	fetchCmd := conn.Fetch(seqSet, fetchOpts)
	defer fetchCmd.Close()

	var out []fetchedMessage
	for {
		msg := fetchCmd.Next()
		if msg == nil {
			break
		}
		buf, err := msg.Collect()
		if err != nil {
			log.Printf("[imap] fetch collect error: %v", err)
			continue
		}
		fm := fetchedMessage{
			SeqNum: buf.SeqNum,
		}
		if buf.Envelope != nil {
			fm.Subject = buf.Envelope.Subject
			fm.MessageID = buf.Envelope.MessageID
			fm.Date = buf.Envelope.Date
			if len(buf.Envelope.From) > 0 {
				fm.From = buf.Envelope.From[0].Addr()
			}
		}
		// First (and only) BodySection captures full RFC822.
		for _, body := range buf.BodySection {
			fm.Body = string(body.Bytes)
			break
		}
		// Use Message-ID as stable id when available, else seqnum.
		fm.ID = fm.MessageID
		if fm.ID == "" {
			fm.ID = fmt.Sprintf("seq-%d", buf.SeqNum)
		}
		out = append(out, fm)
	}
	if err := fetchCmd.Close(); err != nil {
		return nil, fmt.Errorf("imap fetch close: %w", err)
	}
	return out, nil
}

type fetchedMessage struct {
	ID        string
	SeqNum    uint32
	MessageID string
	Subject   string
	From      string
	Date      time.Time
	Body      string
}

// IMAPDiagMessage is an exported view of fetchedMessage used by the
// internal IMAP diagnostic command. Not part of the stable API.
type IMAPDiagMessage struct {
	ID        string
	SeqNum    uint32
	MessageID string
	Subject   string
	Body      string
}

// IMAPRefreshForTest exchanges a refresh_token for an access_token
// using the same path the IMAP client uses internally. Exposed only
// for cmd/_imapdiag.
func IMAPRefreshForTest(c *IMAPMailClient, refreshToken, clientID string) (string, error) {
	return c.refreshAccessToken(refreshToken, clientID)
}

// IMAPFetchForTest pulls the most-recent N messages via the same path
// CollectExistingIDs / WaitVerificationCode use. Exposed only for
// cmd/_imapdiag.
func IMAPFetchForTest(c *IMAPMailClient, refreshToken, clientID string, top uint32) ([]IMAPDiagMessage, error) {
	msgs, err := c.fetchRecentMessages(refreshToken, clientID, top)
	if err != nil {
		return nil, err
	}
	out := make([]IMAPDiagMessage, 0, len(msgs))
	for _, m := range msgs {
		out = append(out, IMAPDiagMessage{
			ID: m.ID, SeqNum: m.SeqNum, MessageID: m.MessageID,
			Subject: m.Subject, Body: m.Body,
		})
	}
	return out, nil
}

// imapWatermarkKey is the special skipIDs entry where pre-scan
// records the wall-clock and the highest-seen UID at the moment we
// "started waiting". WaitVerificationCode then refuses any message
// whose Date is older than that wall-clock — preventing the case
// where a stale "Microsoft account security code" email from a
// previous run gets returned as if it were the current one.
//
// The value format is "ts=<RFC3339>;seq=<uint32>".
const imapWatermarkKey = "__watermark__"

// CollectExistingIDs implements MailClient.
//
// We collect IDs of every recent message (so we can dedupe against
// them) AND record a watermark (timestamp + max sequence number) so
// WaitVerificationCode can later require "strictly newer than
// pre-scan" — pure ID dedup is not enough when MS reuses IDs or when
// a stale code mail from an earlier run is still in the inbox.
func (c *IMAPMailClient) CollectExistingIDs(refreshToken, clientID string) map[string]struct{} {
	out := map[string]struct{}{}
	msgs, err := c.fetchRecentMessages(refreshToken, clientID, 30)
	if err != nil {
		log.Printf("[imap] pre-scan failed: %v", err)
		// Even on failure, leave a wall-clock watermark so the
		// poller doesn't hand back stale mail when it eventually
		// connects.
		out[imapWatermarkKey] = struct{}{}
		out["__watermark_value__"] = struct{}{}
		return out
	}
	var maxSeq uint32
	for _, m := range msgs {
		out[m.ID] = struct{}{}
		if m.SeqNum > maxSeq {
			maxSeq = m.SeqNum
		}
		log.Printf("[imap] pre-scan id=%s seq=%d from=%s date=%s subj=%q",
			truncate(m.ID, 60), m.SeqNum, m.From, m.Date.Format(time.RFC3339), m.Subject)
	}
	out[imapWatermarkKey] = struct{}{}
	out[fmt.Sprintf("ts=%s;seq=%d", time.Now().UTC().Format(time.RFC3339), maxSeq)] = struct{}{}
	return out
}

// extractWatermark pulls the (timestamp, maxSeq) tuple out of the
// pre-scan map.  Falls back to (now, 0) if missing — i.e. pre-scan
// was skipped or failed.
func extractWatermark(skipIDs map[string]struct{}) (time.Time, uint32) {
	for k := range skipIDs {
		if !strings.HasPrefix(k, "ts=") {
			continue
		}
		parts := strings.Split(k, ";")
		if len(parts) != 2 {
			continue
		}
		tsStr := strings.TrimPrefix(parts[0], "ts=")
		seqStr := strings.TrimPrefix(parts[1], "seq=")
		ts, err := time.Parse(time.RFC3339, tsStr)
		if err != nil {
			continue
		}
		var seq uint32
		fmt.Sscanf(seqStr, "%d", &seq)
		return ts, seq
	}
	return time.Now().UTC().Add(-1 * time.Minute), 0
}

// WaitVerificationCode implements MailClient.
//
// Mirrors OutlookClient.WaitVerificationCode: poll until a fresh
// message appears containing a 6-digit code, propagate
// PermanentRefreshError unchanged, time-bound by `timeout`.
func (c *IMAPMailClient) WaitVerificationCode(
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
	waterTS, waterSeq := extractWatermark(skipIDs)
	log.Printf("[imap] watermark: ts=%s seq=%d", waterTS.Format(time.RFC3339), waterSeq)

	pollIdx := 0
	for time.Now().Before(deadline) {
		pollIdx++
		msgs, err := c.fetchRecentMessages(refreshToken, clientID, 30)
		if err != nil {
			if IsPermanentRefreshError(err) {
				log.Printf("[imap] refresh permanently failed (giving up): %v", err)
				return "", err
			}
			log.Printf("[imap] poll error: %v", err)
			time.Sleep(pollInterval)
			continue
		}
		// Sort msgs newest-first by Date so we always evaluate the
		// freshest matching code first.
		sort.SliceStable(msgs, func(i, j int) bool {
			return msgs[i].Date.After(msgs[j].Date)
		})

		for _, m := range msgs {
			_, skipByID := skipIDs[m.ID]
			if skipByID {
				continue
			}
			if !isMSSecurityCodeMail(m) {
				continue
			}
			// Fresh = strictly newer than the watermark we set
			// in CollectExistingIDs. Without this guard, a
			// stale "Security code: 446575" sitting in the
			// inbox from yesterday would get returned as the
			// code for today's request.
			fresh := m.SeqNum > waterSeq &&
				(m.Date.IsZero() || m.Date.After(waterTS.Add(-30*time.Second)))
			if !fresh {
				continue
			}
			code := extractMSSecurityCode(m.Body)
			if code == "" {
				log.Printf("[imap] poll#%d: MS mail seq=%d date=%s but no Security-code line in body",
					pollIdx, m.SeqNum, m.Date.Format(time.RFC3339))
				continue
			}
			log.Printf("[imap] poll#%d: extracted code=%s from seq=%d date=%s",
				pollIdx, code, m.SeqNum, m.Date.Format(time.RFC3339))
			return code, nil
		}
		log.Printf("[imap] poll#%d: %d msgs, none fresh+MS+coded yet", pollIdx, len(msgs))
		time.Sleep(pollInterval)
	}
	return "", fmt.Errorf("timed out waiting for verification code (%s)", timeout)
}

// isMSSecurityCodeMail returns true when m looks like a real
// Microsoft "personal account security code" email — sent from the
// well-known account-security-noreply address.  We deliberately do
// NOT match on subject keywords like "code"/"verify" because those
// match every random OTP mail in the mailbox (ChatGPT, OpenAI, etc.).
func isMSSecurityCodeMail(m fetchedMessage) bool {
	from := strings.ToLower(m.From)
	if strings.Contains(from, "account-security-noreply@accountprotection.microsoft.com") ||
		strings.Contains(from, "@accountprotection.microsoft.com") {
		return true
	}
	subj := strings.ToLower(m.Subject)
	// Fallback: subject is the canonical MS phrasing, in any locale.
	if strings.Contains(subj, "microsoft account security code") ||
		strings.Contains(subj, "microsoft 帐户安全代码") ||
		strings.Contains(subj, "microsoft 帐户的安全代码") {
		return true
	}
	return false
}

// msSecurityCodeRE matches the canonical "Security code: 123456" line
// MS includes verbatim in the text/plain alternative of every
// account-security email (all locales).  We also fall through to
// "code: 123456" for translated variants.
var msSecurityCodeREs = []*regexp.Regexp{
	regexp.MustCompile(`(?i)security code[:\s]*([0-9]{6})\b`),
	regexp.MustCompile(`(?i)安全代码[:\s：]*([0-9]{6})\b`),
	regexp.MustCompile(`(?i)安全码[:\s：]*([0-9]{6})\b`),
	regexp.MustCompile(`(?i)code[:\s]+([0-9]{6})\b`),
}

// extractMSSecurityCode pulls the 6-digit code from a Microsoft
// security-code email body (RFC822 raw bytes).  Returns "" when the
// canonical "Security code: N" pattern is missing — we deliberately
// don't fall back to "any 6 digits in the body" because MS bodies
// contain unrelated 6-digit numbers (Message-IDs, tracking pixels,
// trace IDs).
func extractMSSecurityCode(body string) string {
	body = decodeIMAPBody(body)
	for _, re := range msSecurityCodeREs {
		if m := re.FindStringSubmatch(body); m != nil {
			return m[1]
		}
	}
	return ""
}

// decodeIMAPBody normalises an RFC822 body so the 6-digit code regex
// has a fighting chance. Microsoft's verification mails are usually
// HTML with quoted-printable encoding — the 6-digit code typically
// shows up in plain text inside <body> as well as in any text/plain
// alternative, so we just lowercase + strip the most common QP
// artefacts and call it a day. We deliberately do NOT pull in a full
// MIME parser: extractVerificationCode is robust to surrounding
// markup, and over-engineering here is what made the Python flow
// fragile.
func decodeIMAPBody(raw string) string {
	if raw == "" {
		return ""
	}
	// Strip the "=\r\n" soft-line-break used by quoted-printable.
	raw = strings.ReplaceAll(raw, "=\r\n", "")
	raw = strings.ReplaceAll(raw, "=\n", "")
	return raw
}
