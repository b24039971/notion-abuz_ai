package msalogin

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"strings"
	"time"

	"notion-manager/internal/netutil"
)

const (
	notionBase            = "https://www.notion.so"
	notionLoginURL        = notionBase + "/login"
	notionAPIBase         = notionBase + "/api/v3"
	notionCallbackURIPath = "/microsoftpopupcallback"

	msaBase             = "https://login.live.com"
	msaCheckPasswordURL = msaBase + "/checkpassword.srf"

	defaultUA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
		"(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

// Client drives a single Notion SSO login + onboarding for one MSA account.
// Reuse across multiple accounts is not supported (state is mutated).
type Client struct {
	main   Token
	backup *Token

	http   *http.Client
	jar    *cookiejar.Jar
	logger *log.Logger

	// proxyURL preserves the upstream proxy so consent.go can pass
	// it through to the headless-Chrome it spawns for the SPA
	// consent step on account.live.com/Consent/Update.
	proxyURL string

	tokenV2            string
	csrf               string
	clientVersion      string
	callbackState      string
	callbackClientInfo string
	msConfig           *msOAuthConfig

	// Cached space data populated by createSpace; falls back here when
	// loadUserContent has not yet propagated the new workspace.
	createdSpace       map[string]interface{}
	createdSpaceViewID string
}

// Options tweak the HTTP behavior of a Client.
type Options struct {
	// Backup is the Token used as MS proofs fallback (typically the next
	// account in a bulk file). Required for fresh accounts that trigger
	// MS email verification.
	Backup *Token
	// Timeout for individual HTTP requests. Defaults to 30s.
	Timeout time.Duration
	// Logger receives structured progress messages. nil → stdlib log.
	Logger *log.Logger
	// ProxyURL routes every outbound HTTPS request through the given
	// upstream proxy. Supported schemes: http, https, socks5, socks5h.
	// Empty means dial directly.
	ProxyURL string
}

// New constructs a Client. The provided main Token is the account being
// registered. Use opts.Backup to provide an N+1 backup account for proofs.
func New(main Token, opts Options) (*Client, error) {
	if main.Email == "" || main.Password == "" {
		return nil, fmt.Errorf("msalogin: email and password are required")
	}
	if err := netutil.ValidateProxyURL(opts.ProxyURL); err != nil {
		return nil, fmt.Errorf("msalogin: %w", err)
	}
	jar, err := cookiejar.New(nil)
	if err != nil {
		return nil, err
	}
	to := opts.Timeout
	if to <= 0 {
		to = 30 * time.Second
	}
	logger := opts.Logger
	if logger == nil {
		logger = log.Default()
	}
	// Pin the per-Client transport: the no-proxy case shares a process
	// singleton so the ALPN cache spans calls; proxied flows get a fresh
	// transport each so the cache doesn't bleed across upstream proxies.
	var tr http.RoundTripper
	if opts.ProxyURL == "" {
		tr = getChromeTransport()
	} else {
		tr = newChromeTransport(opts.ProxyURL)
	}
	c := &Client{
		main:     main,
		backup:   opts.Backup,
		jar:      jar,
		logger:   logger,
		proxyURL: opts.ProxyURL,
		http: &http.Client{
			Timeout:   to,
			Jar:       jar,
			Transport: tr,
			CheckRedirect: func(req *http.Request, via []*http.Request) error {
				return http.ErrUseLastResponse
			},
		},
	}
	return c, nil
}

// ── HTTP helpers ─────────────────────────────────────────────────────────

func (c *Client) logf(format string, args ...interface{}) {
	c.logger.Printf("[msalogin %s] "+format, append([]interface{}{c.main.Email}, args...)...)
}

func (c *Client) do(req *http.Request) (*http.Response, []byte, error) {
	if req.Header.Get("User-Agent") == "" {
		req.Header.Set("User-Agent", defaultUA)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	return resp, body, err
}

func (c *Client) get(rawurl string, headers map[string]string) (*http.Response, []byte, error) {
	req, err := http.NewRequest("GET", rawurl, nil)
	if err != nil {
		return nil, nil, err
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	return c.do(req)
}

func (c *Client) postForm(rawurl string, form url.Values, headers map[string]string) (*http.Response, []byte, error) {
	req, err := http.NewRequest("POST", rawurl, strings.NewReader(form.Encode()))
	if err != nil {
		return nil, nil, err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	return c.do(req)
}

func (c *Client) postJSON(rawurl string, body io.Reader, headers map[string]string) (*http.Response, []byte, error) {
	req, err := http.NewRequest("POST", rawurl, body)
	if err != nil {
		return nil, nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	return c.do(req)
}

// followRedirects walks 301/302/303/307/308 chains until the response
// terminates or we hit the Notion popup callback. When the callback fires
// it captures (code, state, client_info) and returns code != "".
func (c *Client) followRedirects(resp *http.Response, body []byte) (*http.Response, []byte, string, error) {
	const maxHops = 15
	for i := 0; i < maxHops; i++ {
		switch resp.StatusCode {
		case 301, 302, 303, 307, 308:
		default:
			return resp, body, "", nil
		}
		loc := resp.Header.Get("Location")
		if loc == "" {
			return resp, body, "", nil
		}

		// Resolve relative redirects against the current URL.
		if !strings.HasPrefix(loc, "http") {
			base := resp.Request.URL
			ref, err := base.Parse(loc)
			if err == nil {
				loc = ref.String()
			}
		}

		// Stop at the Notion callback so we can capture the code without
		// waiting for the SPA HTML.
		parsed, err := url.Parse(loc)
		if err == nil && strings.HasSuffix(strings.TrimRight(parsed.Path, "/"), notionCallbackURIPath) {
			code, state, ci := extractCodeFromURL(loc)
			if code != "" {
				c.callbackState = state
				c.callbackClientInfo = ci
				c.logf("captured auth code (%d chars) state=%s", len(code), truncate(state, 60))
				// Actually GET the callback so notion sets cookies (token_v2 etc.)
				resp, body, err = c.get(loc, nil)
				if err != nil {
					return resp, body, code, err
				}
				return resp, body, code, nil
			}
		}

		method := "GET"
		if resp.StatusCode == 307 || resp.StatusCode == 308 {
			method = resp.Request.Method
		}
		req, err := http.NewRequest(method, loc, nil)
		if err != nil {
			return resp, body, "", err
		}
		resp, body, err = c.do(req)
		if err != nil {
			return resp, body, "", err
		}
	}
	// If the chain ended with a code in the URL (rare), recover it.
	if resp != nil && resp.Request != nil {
		if code, state, ci := extractCodeFromURL(resp.Request.URL.String()); code != "" {
			c.callbackState = state
			c.callbackClientInfo = ci
			return resp, body, code, nil
		}
	}
	return resp, body, "", nil
}

// LoginUntilTokenV2 runs the Microsoft SSO state machine and Notion code
// exchange but stops *before* handleOnboarding. After it returns nil, the
// jar holds Notion's authenticated cookies (token_v2, csrf, notion_user_id,
// notion_browser_id, …) but the user has no workspace yet — useful for
// piping cookies into a real browser to capture the onboarding API
// sequence with proper request/response logging.
func (c *Client) LoginUntilTokenV2() error {
	if err := c.initNotionSession(); err != nil {
		return err
	}
	code, err := c.msLoginGetCode()
	if err != nil {
		return err
	}
	if code == "" {
		return newErr("ms_state", "MS state machine returned empty code")
	}
	return c.exchangeCode(code, c.callbackState, c.callbackClientInfo)
}

// ExportNotionCookies returns every cookie currently associated with the
// notion.so domain in the jar, suitable for serialisation into Playwright
// (or any other browser harness). Names match the field shape Playwright's
// browser_context.add_cookies expects.
func (c *Client) ExportNotionCookies() []NotionCookie {
	u := mustParse(notionBase)
	out := []NotionCookie{}
	for _, ck := range c.jar.Cookies(u) {
		out = append(out, NotionCookie{
			Name:   ck.Name,
			Value:  ck.Value,
			Domain: ".notion.so",
			Path:   "/",
		})
	}
	return out
}

// NotionCookie is a flat representation of a single jar cookie that can be
// shipped to Playwright via add_cookies.
type NotionCookie struct {
	Name   string `json:"name"`
	Value  string `json:"value"`
	Domain string `json:"domain"`
	Path   string `json:"path"`
}

// ── Cookie helpers ───────────────────────────────────────────────────────

func (c *Client) cookieValue(host, name string) string {
	u, err := url.Parse(host)
	if err != nil {
		return ""
	}
	for _, ck := range c.jar.Cookies(u) {
		if ck.Name == name {
			return ck.Value
		}
	}
	return ""
}

// syncCSRF deduplicates csrf cookies and returns the authoritative value.
// Notion sets csrf via Set-Cookie on /login + /microsoftpopupredirect; we
// must echo it back via x-csrf-token on subsequent API calls.
func (c *Client) syncCSRF() string {
	if v := c.cookieValue(notionBase, "csrf"); v != "" {
		c.csrf = v
		return v
	}
	if c.csrf == "" {
		buf := make([]byte, 16)
		_, _ = rand.Read(buf)
		c.csrf = hex.EncodeToString(buf)
	}
	return c.csrf
}

// ── Phase 1: Notion init ─────────────────────────────────────────────────

func (c *Client) initNotionSession() error {
	c.logf("fetching Notion login page")
	req, err := http.NewRequest("GET", notionLoginURL, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
	req.Header.Set("Accept-Language", "en-US,en;q=0.9")
	resp, body, err := c.do(req)
	if err != nil {
		return newErr("notion_init", "GET /login: %v", err)
	}
	// Manually follow Notion's HTML redirect to /onboarding/login or /home.
	if resp.StatusCode >= 300 && resp.StatusCode < 400 {
		resp, body, _, err = c.followRedirects(resp, body)
		if err != nil {
			return newErr("notion_init", "follow redirects: %v", err)
		}
	}
	if resp.StatusCode != 200 {
		return newErr("notion_init", "GET /login HTTP %d", resp.StatusCode)
	}
	c.clientVersion = extractClientVersion(string(body))
	_ = c.syncCSRF()
	c.logf("notion login page OK; version=%s", c.clientVersion)
	return nil
}

func (c *Client) discoverMSConfig() error {
	if c.msConfig != nil {
		return nil
	}
	c.logf("getting MS authorize URL")
	resp, body, err := c.get(notionBase+"/microsoftpopupredirect?callbackType=popup", nil)
	if err != nil {
		return newErr("ms_oauth_discovery", "GET /microsoftpopupredirect: %v", err)
	}
	if resp.StatusCode != 200 {
		return newErr("ms_oauth_discovery", "HTTP %d body=%s", resp.StatusCode, truncate(string(body), 200))
	}
	// JSON: { "url": "https://login.microsoftonline.com/..." }
	authURL := extractJSString(string(body), "url")
	if authURL == "" {
		// Try a JSON-style key without escapes
		s := string(body)
		i := strings.Index(s, `"url":"`)
		if i >= 0 {
			rest := s[i+7:]
			j := strings.Index(rest, `"`)
			if j >= 0 {
				authURL = strings.ReplaceAll(rest[:j], `\u0026`, "&")
				authURL = strings.ReplaceAll(authURL, `\/`, `/`)
			}
		}
	}
	if authURL == "" {
		return newErr("ms_oauth_discovery", "no URL in body=%s", truncate(string(body), 200))
	}
	parsed, _ := url.Parse(authURL)
	q := parsed.Query()
	c.msConfig = &msOAuthConfig{
		authorizeURL: authURL,
		clientID:     q.Get("client_id"),
		redirectURI:  q.Get("redirect_uri"),
	}
	_ = c.syncCSRF()
	c.logf("MS authorize URL OK; client_id=%s", c.msConfig.clientID)
	return nil
}

// ── Phase 2: MS login state machine ──────────────────────────────────────

func (c *Client) msLoginGetCode() (string, error) {
	if err := c.discoverMSConfig(); err != nil {
		return "", err
	}
	authURL := c.msConfig.authorizeURL
	sep := "?"
	if strings.Contains(authURL, "?") {
		sep = "&"
	}
	authURL += sep + "login_hint=" + url.QueryEscape(c.main.Email) + "&domain_hint=consumers"

	c.logf("navigating to MS authorize URL")
	resp, body, err := c.get(authURL, nil)
	if err != nil {
		return "", newErr("ms_authorize", "%v", err)
	}
	resp, body, code, err := c.followRedirects(resp, body)
	if err != nil {
		return "", newErr("ms_authorize", "%v", err)
	}
	if code != "" {
		return code, nil
	}
	if resp.StatusCode != 200 {
		return "", newErr("ms_authorize", "HTTP %d at %s", resp.StatusCode, resp.Request.URL.String())
	}

	return c.driveStateMachine(string(body), resp.Request.URL.String())
}

const maxStateIterations = 20

func (c *Client) driveStateMachine(html, currentURL string) (string, error) {
	for i := 0; i < maxStateIterations; i++ {
		state := detectMSState(html, currentURL)
		c.logf("[ms iter %d] state=%s url=%s", i, state, truncate(currentURL, 80))

		switch state {
		case "code_found":
			if code, _, _ := extractCodeFromURL(currentURL); code != "" {
				return code, nil
			}
			return "", newErr("ms_state", "code_found but no code in url")
		case "msa_login":
			next, nextURL, code, err := c.handleMSALoginPage(html, currentURL)
			if err != nil {
				return "", err
			}
			if code != "" {
				return code, nil
			}
			html, currentURL = next, nextURL
		case "msa_kmsi":
			next, nextURL, code, err := c.handleMSAKmsi(html, currentURL)
			if err != nil {
				return "", err
			}
			if code != "" {
				return code, nil
			}
			html, currentURL = next, nextURL
		case "ests_login":
			next, nextURL, code, err := c.handleESTSLogin(html)
			if err != nil {
				return "", err
			}
			if code != "" {
				return code, nil
			}
			html, currentURL = next, nextURL
		case "redirect_form":
			next, nextURL, code, err := c.handleRedirectForm(html)
			if err != nil {
				return "", err
			}
			if code != "" {
				return code, nil
			}
			html, currentURL = next, nextURL
		case "bsso_interrupt":
			next, nextURL, code, err := c.handleBSSOInterrupt(html, currentURL)
			if err != nil {
				return "", err
			}
			if code != "" {
				return code, nil
			}
			html, currentURL = next, nextURL
		case "proofs":
			next, nextURL, code, err := c.handleMSProofs(html, currentURL)
			if err != nil {
				return "", err
			}
			if code != "" {
				return code, nil
			}
			html, currentURL = next, nextURL
		case "consent":
			next, nextURL, code, err := c.handleMSConsent(html, currentURL)
			if err != nil {
				return "", err
			}
			if code != "" {
				return code, nil
			}
			html, currentURL = next, nextURL
		case "error":
			return "", newErr("ms_state", "MS login error: %s", extractError(html))
		default:
			path := dumpProofsDebug(c, "unknown_state", currentURL, html)
			return "", newErr(
				"ms_state",
				"unknown MS login state at iteration %d url=%s html_sample=%s (HTML dumped to %s)",
				i, truncate(currentURL, 100), truncate(html, 300), path,
			)
		}
	}
	return "", newErr("ms_state", "state machine exceeded %d iterations", maxStateIterations)
}

// ── Errors ───────────────────────────────────────────────────────────────

// ErrCodeFound is a sentinel returned by inner handlers when they discovered
// the auth code via redirect. Use errors.Is.
var ErrCodeFound = errors.New("auth code found")
