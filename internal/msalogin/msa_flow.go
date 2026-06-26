package msalogin

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"net/url"
	"strings"
	"time"
)

// ── MSA login.live.com flow ──────────────────────────────────────────────

// msaCheckPassword posts JSON to /checkpassword.srf and returns the
// vanguardflowtoken that we forward to the type=11 ppsecure submit.
func (c *Client) msaCheckPassword(password, referer, correlationID string) (string, error) {
	cid := strings.ReplaceAll(correlationID, "-", "")
	if cid == "" {
		buf := make([]byte, 16)
		_, _ = rand.Read(buf)
		cid = hex.EncodeToString(buf)
	}
	body, _ := json.Marshal(map[string]string{
		"username":               c.main.Email,
		"password":               password,
		"checkpasswordflowtoken": "",
	})
	headers := map[string]string{
		"Content-Type":      "application/json; charset=utf-8",
		"Accept":            "application/json",
		"Origin":            msaBase,
		"Referer":           referer,
		"client-request-id": cid,
		"correlationid":     cid,
		"hpgid":             "33",
		"hpgact":            "0",
		"hpgrequestid":      cid,
	}

	c.logf("POST checkpassword.srf")
	resp, raw, err := c.postJSON(msaCheckPasswordURL, strings.NewReader(string(body)), headers)
	if err != nil {
		return "", newErr("msa_checkpassword", "%v", err)
	}
	if resp.StatusCode != 200 {
		return "", newErr("msa_checkpassword", "HTTP %d body=%s", resp.StatusCode, truncate(string(raw), 240))
	}
	var data map[string]interface{}
	if err := json.Unmarshal(raw, &data); err != nil {
		return "", newErr("msa_checkpassword", "non-JSON body=%s", truncate(string(raw), 240))
	}
	if ec, _ := data["errorCode"]; ec != nil && ec != "" {
		return "", &LoginError{
			Stage:   "msa_checkpassword",
			Message: stringOf(data["errorMessage"]),
			Kind:    classifyMsaErrorCode(stringOf(ec)),
		}
	}
	for _, k := range []string{"vanguardflowtoken", "VanguardFlowToken", "flowToken"} {
		if v := stringOf(data[k]); v != "" {
			return v, nil
		}
	}
	c.logf("checkpassword 200 but no vanguardflowtoken; keys=%v", keysOf(data))
	return "", nil
}

func classifyMsaErrorCode(code string) loginErrorKind {
	switch code {
	case "1041", "0x80048821", "AADSTS50034":
		return errKindAccountNotFound
	case "0x80048823", "0x80048822":
		return errKindBadPassword
	case "0x80048820":
		return errKindAccountLocked
	default:
		return errKindGeneric
	}
}

// handleMSALoginPage processes the login.live.com OAuth2 authorize page.
// On success it returns the resulting HTML/URL of the next step (typically
// the KMSI page).
func (c *Client) handleMSALoginPage(html, currentURL string) (string, string, string, error) {
	cfg, err := parseMSALoginPage(html)
	if err != nil {
		return "", "", "", err
	}
	c.logf("MSA login page parsed; ppft_len=%d", len(cfg.ppft))

	vft, err := c.msaCheckPassword(c.main.Password, currentURL, cfg.correlationID)
	if err != nil {
		return "", "", "", err
	}

	form := url.Values{
		"ps":                    {"2"},
		"PPFT":                  {cfg.ppft},
		"PPSX":                  {"Passpo"},
		"NewUser":               {"1"},
		"FoundMSAs":             {""},
		"fspost":                {"0"},
		"i21":                   {"0"},
		"CookieDisclosure":      {"0"},
		"IsFidoSupported":       {"1"},
		"isSignupPost":          {"0"},
		"isRecoveryAttemptPost": {"0"},
		"i13":                   {"0"},
		"login":                 {c.main.Email},
		"loginfmt":              {c.main.Email},
		"type":                  {"11"},
		"LoginOptions":          {"3"},
		"cpr":                   {"0"},
		"passwd":                {c.main.Password},
		"vanguardflowtoken":     {vft},
	}
	headers := map[string]string{
		"Origin":  msaBase,
		"Referer": currentURL,
	}
	c.logf("MSA password POST → %s", truncate(cfg.urlPost, 100))
	resp, body, err := c.postForm(cfg.urlPost, form, headers)
	if err != nil {
		return "", "", "", newErr("msa_password", "%v", err)
	}
	resp, body, code, err := c.followRedirects(resp, body)
	if err != nil {
		return "", "", code, newErr("msa_password", "%v", err)
	}
	if code != "" {
		return string(body), resp.Request.URL.String(), code, nil
	}
	if resp.StatusCode == 200 {
		if e := extractError(string(body)); e != "" && !strings.Contains(strings.ToLower(e), "proof") {
			return "", "", "", &LoginError{Stage: "msa_password", Message: e, Kind: errKindBadPassword}
		}
	}
	return string(body), resp.Request.URL.String(), "", nil
}

// handleMSAKmsi processes the "Stay signed in?" page with a Yes (type=28).
func (c *Client) handleMSAKmsi(html, currentURL string) (string, string, string, error) {
	cfg, err := parseMSAKmsiPage(html)
	if err != nil {
		return "", "", "", err
	}
	form := url.Values{
		"PPFT":         {cfg.ppft},
		"LoginOptions": {"1"},
		"type":         {"28"},
	}
	headers := map[string]string{
		"Origin":  msaBase,
		"Referer": currentURL,
	}
	c.logf("MSA KMSI POST → %s", truncate(cfg.urlPost, 100))
	resp, body, err := c.postForm(cfg.urlPost, form, headers)
	if err != nil {
		return "", "", "", newErr("msa_kmsi", "%v", err)
	}
	resp, body, code, err := c.followRedirects(resp, body)
	if err != nil {
		return "", "", code, newErr("msa_kmsi", "%v", err)
	}
	if code != "" {
		return string(body), resp.Request.URL.String(), code, nil
	}
	return string(body), resp.Request.URL.String(), "", nil
}

// handleESTSLogin handles the AAD ESTS login page. For consumer accounts
// this only fires to call GetCredentialType, which returns a
// FederationRedirectUrl back to login.live.com — we follow that and
// return the resulting MSA HTML.
func (c *Client) handleESTSLogin(html string) (string, string, string, error) {
	cfg, err := parseESTSLoginPage(html)
	if err != nil {
		return "", "", "", err
	}
	if cfg.urlGetCredentialType == "" {
		return "", "", "", newErr("ests_login", "no urlGetCredentialType in $Config")
	}
	payload, _ := json.Marshal(map[string]interface{}{
		"checkPhones":                    true,
		"country":                        "",
		"federationFlags":                3,
		"flowToken":                      cfg.ppft,
		"forceotclogin":                  false,
		"isCookieBannerShown":            false,
		"isExternalFederationDisallowed": false,
		"isFederationDisabled":           false,
		"isFidoSupported":                true,
		"isOtherIdpSupported":            true,
		"isRemoteNGCSupported":           true,
		"isSignup":                       false,
		"otclogindisallowed":             false,
		"username":                       c.main.Email,
	})
	c.logf("GetCredentialType for %s", c.main.Email)
	resp, body, err := c.postJSON(cfg.urlGetCredentialType, strings.NewReader(string(payload)), nil)
	if err != nil {
		return "", "", "", newErr("ests_get_cred", "%v", err)
	}
	if resp.StatusCode != 200 {
		return "", "", "", newErr("ests_get_cred", "HTTP %d", resp.StatusCode)
	}
	var data map[string]interface{}
	if err := json.Unmarshal(body, &data); err != nil {
		return "", "", "", newErr("ests_get_cred", "non-JSON")
	}
	creds, _ := data["Credentials"].(map[string]interface{})
	fedURL := stringOf(creds["FederationRedirectUrl"])
	if fedURL != "" && strings.Contains(fedURL, "login.live.com") {
		c.logf("MSA federation; following %s", truncate(fedURL, 120))
		resp, body, err := c.get(fedURL, nil)
		if err != nil {
			return "", "", "", newErr("ests_fed", "%v", err)
		}
		resp, body, code, err := c.followRedirects(resp, body)
		if err != nil {
			return "", "", code, newErr("ests_fed", "%v", err)
		}
		return string(body), resp.Request.URL.String(), code, nil
	}
	// Otherwise we'd need to do a full ESTS password POST, which our
	// consumer-only flow doesn't support.
	return "", "", "", newErr("ests_login", "non-MSA ESTS account is not supported (no FederationRedirectUrl)")
}

// handleBSSOInterrupt processes AAD's Chrome-SSO probe page. The page tries
// to detect Edge/Chrome browser SSO via cookies (AADSSO/ESTSSSO); when no
// browser SSO is available the JS times out and reloads urlPost. Since we
// aren't a real browser we just take the urlPost shortcut directly.
func (c *Client) handleBSSOInterrupt(html, currentURL string) (string, string, string, error) {
	urlPost := extractJSString(html, "urlPost")
	if urlPost == "" {
		return "", "", "", newErr("bsso_interrupt", "no urlPost in $Config")
	}
	urlPost = strings.ReplaceAll(urlPost, `\u0026`, `&`)
	urlPost = strings.ReplaceAll(urlPost, `\/`, `/`)
	if !strings.HasPrefix(urlPost, "http") {
		// Resolve relative URL against current page (login.microsoftonline.com).
		base, err := url.Parse(currentURL)
		if err == nil {
			ref, err := base.Parse(urlPost)
			if err == nil {
				urlPost = ref.String()
			}
		}
	}
	c.logf("BSSO interrupt — bypassing via %s", truncate(urlPost, 100))
	resp, body, err := c.get(urlPost, map[string]string{
		"Referer": currentURL,
	})
	if err != nil {
		return "", "", "", newErr("bsso_interrupt", "%v", err)
	}
	resp, body, code, err := c.followRedirects(resp, body)
	if err != nil {
		return "", "", code, newErr("bsso_interrupt", "%v", err)
	}
	return string(body), resp.Request.URL.String(), code, nil
}

// handleRedirectForm submits any auto-submit hidden form encountered between
// MS handoffs. This covers the AAD→MSA bridge and the like.
func (c *Client) handleRedirectForm(html string) (string, string, string, error) {
	action, fields := parseRedirectForm(html)
	if action == "" {
		return "", "", "", newErr("redirect_form", "no form action found")
	}
	form := url.Values{}
	for k, v := range fields {
		form.Set(k, v)
	}
	c.logf("submitting redirect form to %s", truncate(action, 80))
	resp, body, err := c.postForm(action, form, nil)
	if err != nil {
		return "", "", "", newErr("redirect_form", "%v", err)
	}
	resp, body, code, err := c.followRedirects(resp, body)
	if err != nil {
		return "", "", code, newErr("redirect_form", "%v", err)
	}
	return string(body), resp.Request.URL.String(), code, nil
}

// handleMSProofs handles MS email-verification ("proofs") prompts.
//
// First, we look for a "Use your password instead" (urlUsePassword) bypass
// link — when present, MS lets us skip proofs entirely. Otherwise we need a
// backup mailbox to receive the verification code, which only works when
// the caller passed an Outlook OAuth token via Options.Backup.
func (c *Client) handleMSProofs(html, currentURL string) (string, string, string, error) {
	if useURL := extractJSString(html, "urlUsePassword"); useURL != "" {
		c.logf("proofs — using 'use password' bypass: %s", truncate(useURL, 80))
		resp, body, err := c.get(useURL, nil)
		if err != nil {
			return "", "", "", newErr("proofs", "%v", err)
		}
		resp, body, code, err := c.followRedirects(resp, body)
		if err != nil {
			return "", "", code, newErr("proofs", "%v", err)
		}
		return string(body), resp.Request.URL.String(), code, nil
	}

	if c.backup == nil || c.backup.RefreshToken == "" || c.backup.ClientID == "" {
		return "", "", "", &LoginError{
			Stage:   "proofs",
			Message: "MS requires email verification but no backup Outlook OAuth token provided",
			Kind:    errKindProofsRequired,
		}
	}
	if c.backup.Email == "" {
		return "", "", "", &LoginError{
			Stage:   "proofs",
			Message: "MS requires email verification but backup token has no email",
			Kind:    errKindProofsRequired,
		}
	}
	c.logf("proofs — email flow via backup %s", c.backup.Email)
	return c.handleProofsEmailFlow(html, currentURL)
}

// handleProofsEmailFlow drives Microsoft's email-verification ("proofs")
// challenge using the configured backup mailbox. Microsoft can present
// this challenge in two distinct shapes:
//
//   - First-encounter pages have an `frmAddProof` form: we POST AddProof
//     with the backup email, MS sends a code to that mailbox, then we
//     submit it via the follow-up `frmVerifyProof` form.
//
//   - Compliance ("CatB") re-verification pages — which MS sometimes
//     interjects right after the first SLT submit, with a fresh `epid` —
//     skip AddProof entirely and land us straight on `frmVerifyProof`
//     with a code already auto-sent. Earlier versions of this flow
//     errored out here with `no proofs/Add form action`.
//
// Detect the page shape and route through a shared submitVerifyProof
// helper so the verify-and-SLT tail works the same in both cases.
func (c *Client) handleProofsEmailFlow(html, currentURL string) (string, string, string, error) {
	addAction := extractFormAttr(html, `id="frmAddProof"`, "action")
	if addAction == "" {
		addAction = extractFormAttr(html, `action="[^"]*proofs/Add[^"]*"`, "action")
	}
	hasVerify := strings.Contains(html, `id="frmVerifyProof"`) ||
		strings.Contains(currentURL, "proofs/Verify")

	if addAction == "" && hasVerify {
		c.logf("proofs — second-round verify-only (CatB re-verification of %s)", c.backup.Email)
		return c.runProofsVerifyOnly(html, currentURL)
	}
	if addAction == "" {
		path := dumpProofsDebug(c, "no_form_action", currentURL, html)
		return "", "", "", newErr("proofs",
			"no proofs/Add form action (URL=%s, HTML dumped to %s)",
			truncate(currentURL, 120), path)
	}

	canary := extractInputValue(html, "canary")

	// Pick mailbox backend per backup email domain: hotmail.com /
	// live.com / msn.com FOCI tokens have no Mail.Read on Graph
	// (AADSTS70000 / "scopes unauthorized") and must use IMAP
	// XOAUTH2 against outlook.office365.com:993 instead. Other
	// domains (outlook.com etc.) keep the Graph fast path.
	mailClient := SelectMailClient(c.backup.Email, 30*time.Second)
	skipIDs := mailClient.CollectExistingIDs(c.backup.RefreshToken, c.backup.ClientID)
	c.logf("proofs — pre-scan mailbox: %d ids (via %s)", len(skipIDs), mailBackendName(mailClient))

	addForm := url.Values{
		"iProofOptions":   {"Email"},
		"EmailAddress":    {c.backup.Email},
		"PhoneNumber":     {""},
		"PhoneCountryISO": {""},
		"canary":          {canary},
		"action":          {"AddProof"},
	}
	c.logf("proofs/Add → %s", c.backup.Email)
	resp, body, err := c.postForm(addAction, addForm, map[string]string{"Referer": addAction})
	if err != nil {
		return "", "", "", newErr("proofs_add", "%v", err)
	}
	resp, body, code, err := c.followRedirects(resp, body)
	if err != nil {
		return "", "", code, newErr("proofs_add", "%v", err)
	}
	if code != "" {
		return string(body), resp.Request.URL.String(), code, nil
	}

	verifyHTML := string(body)
	verifyURL := resp.Request.URL.String()
	dumpProofsDebug(c, "after_addproof", verifyURL, verifyHTML)
	return c.submitVerifyProof(verifyHTML, verifyURL, mailClient, skipIDs, canary)
}

// runProofsVerifyOnly handles a bare proofs/Verify page that MS sometimes
// emits after a successful first-round verify+SLT (the "CatB compliance"
// re-prompt for the same backup email with a fresh epid).
//
// Because this page lands fully-rendered with a code already auto-sent,
// we have to pre-scan the mailbox *here* — that gives WaitVerificationCode
// a fresh watermark and lets it ignore the first-round code that's
// definitely still in the inbox.
func (c *Client) runProofsVerifyOnly(html, currentURL string) (string, string, string, error) {
	mailClient := SelectMailClient(c.backup.Email, 30*time.Second)
	skipIDs := mailClient.CollectExistingIDs(c.backup.RefreshToken, c.backup.ClientID)
	c.logf("proofs (verify-only) — pre-scan mailbox: %d ids (via %s)",
		len(skipIDs), mailBackendName(mailClient))
	return c.submitVerifyProof(html, currentURL, mailClient, skipIDs, "")
}

// submitVerifyProof parses the frmVerifyProof form on the supplied page,
// waits for a fresh OTT code in the backup mailbox, POSTs the code, then
// optionally drives the follow-up frmSubmitSLT hop. Shared by both the
// AddProof path (first encounter) and the verify-only path (CatB
// re-verification).
//
// fallbackCanary is consulted when the page itself has no canary input
// (very rare — only happens on AddProof's redirect). Pass "" when the
// caller did not encounter such a page.
func (c *Client) submitVerifyProof(
	verifyHTML, verifyURL string,
	mailClient MailClient,
	skipIDs map[string]struct{},
	fallbackCanary string,
) (string, string, string, error) {
	verifyAction := extractFormAttr(verifyHTML, `id="frmVerifyProof"`, "action")
	verifyAction = strings.ReplaceAll(verifyAction, "&amp;", "&")
	if verifyAction == "" {
		verifyAction = verifyURL
	}
	verifyCanary := extractInputValue(verifyHTML, "canary")
	if verifyCanary == "" {
		verifyCanary = fallbackCanary
	}
	proofValue := extractInputValueWherePrefix(verifyHTML, "proof", "OTT")
	c.logf("verify-form parsed: action=%s canary_len=%d proof_present=%t",
		truncate(verifyAction, 100), len(verifyCanary), proofValue != "")

	c.logf("waiting for verification code (%s)…", mailBackendName(mailClient))
	codeStr, err := mailClient.WaitVerificationCode(
		c.backup.RefreshToken, c.backup.ClientID, skipIDs,
		90*time.Second, 5*time.Second,
	)
	if err != nil {
		return "", "", "", &LoginError{Stage: "proofs", Message: err.Error(), Kind: errKindProofsRequired}
	}
	c.logf("got verification code %s — submitting", codeStr)

	verifyForm := url.Values{
		"iOttText":      {codeStr},
		"canary":        {verifyCanary},
		"action":        {"VerifyProof"},
		"iProofOptions": {""},
	}
	if proofValue != "" {
		verifyForm.Set("proof", proofValue)
	}
	c.logf("verify POST: code=%s, action=%s, fields=%v",
		codeStr, truncate(verifyAction, 80), formKeys(verifyForm))
	resp, body, err := c.postForm(verifyAction, verifyForm, map[string]string{"Referer": verifyAction})
	if err != nil {
		return "", "", "", newErr("proofs_verify", "%v", err)
	}
	resp, body, code, err := c.followRedirects(resp, body)
	if err != nil {
		return "", "", code, newErr("proofs_verify", "%v", err)
	}
	dumpProofsDebug(c, "after_verifyproof", resp.Request.URL.String(), string(body))
	c.logf("verify POST done: url=%s code=%s body_len=%d",
		truncate(resp.Request.URL.String(), 100), code, len(body))
	if code != "" {
		return string(body), resp.Request.URL.String(), code, nil
	}

	// Optional: SLT submit if Microsoft demands one more hop.
	sltHTML := string(body)
	if strings.Contains(sltHTML, `id="frmSubmitSLT"`) {
		sltAction := extractFormAttr(sltHTML, `id="frmSubmitSLT"`, "action")
		sltAction = strings.ReplaceAll(sltAction, "&amp;", "&")
		sltVal := extractInputValue(sltHTML, "slt")
		if sltAction != "" && sltVal != "" {
			c.logf("submitting SLT to %s", truncate(sltAction, 80))
			resp, body, err = c.postForm(sltAction, url.Values{"slt": {sltVal}}, nil)
			if err == nil {
				resp, body, code, err = c.followRedirects(resp, body)
				if err == nil && code != "" {
					return string(body), resp.Request.URL.String(), code, nil
				}
			}
		}
	}
	return string(body), resp.Request.URL.String(), "", nil
}

// ── helpers ──────────────────────────────────────────────────────────────

func formKeys(v url.Values) []string {
	keys := make([]string, 0, len(v))
	for k := range v {
		keys = append(keys, k)
	}
	return keys
}

func stringOf(v interface{}) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

func keysOf(m map[string]interface{}) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	return keys
}
