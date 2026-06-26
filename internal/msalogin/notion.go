package msalogin

import (
	"bytes"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/url"
	"regexp"
	"strings"
	"time"
)

// cryptoRandRead is a tiny indirection so we can stub out randomness in
// tests if needed.
var cryptoRandRead = rand.Read

// ── Phase 3: exchange code with Notion ───────────────────────────────────

// parseOAuthState decodes the state parameter Notion echoes back. It tries
// raw JSON, then urlsafe-base64 JSON, then standard base64 JSON.
func parseOAuthState(state string) map[string]interface{} {
	if state == "" {
		return nil
	}
	pad := func(s string) string {
		switch len(s) % 4 {
		case 2:
			return s + "=="
		case 3:
			return s + "="
		default:
			return s
		}
	}
	tries := []func(string) ([]byte, error){
		func(s string) ([]byte, error) { return []byte(s), nil },
		func(s string) ([]byte, error) { return base64.URLEncoding.DecodeString(pad(s)) },
		func(s string) ([]byte, error) { return base64.StdEncoding.DecodeString(pad(s)) },
	}
	for _, dec := range tries {
		raw, err := dec(state)
		if err != nil {
			continue
		}
		var obj map[string]interface{}
		if err := json.Unmarshal(raw, &obj); err == nil {
			return obj
		}
	}
	return nil
}

// notionAPIHeaders builds the header set Notion's createApiHeaders() emits.
func (c *Client) notionAPIHeaders() map[string]string {
	cookies := map[string]string{}
	for _, ck := range c.jar.Cookies(mustParse(notionBase)) {
		cookies[ck.Name] = ck.Value
	}
	version := cookies["notion_client_version"]
	if version == "" {
		version = c.clientVersion
	}
	headers := map[string]string{
		"Origin":                      notionBase,
		"Referer":                     notionBase + "/",
		"x-notion-active-user-header": cookies["notion_user_id"],
	}
	if version != "" {
		headers["notion-client-version"] = version
	}
	if csrf := c.syncCSRF(); csrf != "" {
		headers["x-csrf-token"] = csrf
	}
	return headers
}

func mustParse(u string) *url.URL {
	p, _ := url.Parse(u)
	return p
}

// buildAuthCallbackReferer recreates the Referer URL the browser would
// have when calling loginWithMicrosoftAuth (i.e. the popup's
// /microsoftauthcallback URL with code/state echoed back).
func buildAuthCallbackReferer(code, state, clientInfo string) string {
	if code == "" {
		return ""
	}
	parts := []string{"code=" + url.QueryEscape(code)}
	if clientInfo != "" {
		parts = append(parts, "client_info="+url.QueryEscape(clientInfo))
	}
	if state != "" {
		parts = append(parts, "state="+url.QueryEscape(state))
	}
	return notionBase + "/microsoftauthcallback?" + strings.Join(parts, "&")
}

func (c *Client) exchangeCode(code, state, clientInfo string) error {
	if c.tokenV2 != "" {
		c.logf("token_v2 already set; skipping exchange")
		return nil
	}
	stateObj := parseOAuthState(state)
	encryptedToken, _ := stateObj["encryptedToken"].(string)
	encryptedNonce, _ := stateObj["encryptedNonce"].(string)

	payload := map[string]interface{}{
		"code":                 code,
		"encryptedToken":       encryptedToken,
		"encryptedNonce":       encryptedNonce,
		"state":                state,
		"loginRouteOrigin":     "login",
		"requireWorkTypeEmail": false,
	}
	body, _ := json.Marshal(payload)
	headers := c.notionAPIHeaders()
	if ref := buildAuthCallbackReferer(code, state, clientInfo); ref != "" {
		headers["Referer"] = ref
	}
	c.logf("POST /loginWithMicrosoftAuth (state_obj_keys=%d)", len(stateObj))
	resp, raw, err := c.postJSON(notionAPIBase+"/loginWithMicrosoftAuth", bytes.NewReader(body), headers)
	if err != nil {
		return newErr("notion_exchange", "%v", err)
	}
	if resp.StatusCode != 200 {
		return newErr("notion_exchange", "HTTP %d body=%s", resp.StatusCode, truncate(string(raw), 300))
	}
	for _, ck := range c.jar.Cookies(mustParse(notionBase)) {
		if ck.Name == "token_v2" {
			c.tokenV2 = ck.Value
		}
	}
	if c.tokenV2 == "" {
		// As a fallback, parse Set-Cookie headers manually.
		for _, sc := range resp.Header.Values("Set-Cookie") {
			if strings.HasPrefix(sc, "token_v2=") {
				if m := regexp.MustCompile(`token_v2=([^;]+)`).FindStringSubmatch(sc); m != nil {
					c.tokenV2 = m[1]
					break
				}
			}
		}
	}
	if c.tokenV2 == "" {
		return newErr("notion_exchange", "no token_v2 in response (body=%s)", truncate(string(raw), 240))
	}
	c.logf("token_v2 obtained (%d chars)", len(c.tokenV2))
	return nil
}

// ── Phase 4: onboarding (createSpace) ────────────────────────────────────

// onboardingPollInterval / onboardingPollTimeout / onboardingMaxAttempts are
// package-level vars (not consts) so tests can shrink the wait without
// resorting to time-based mocking. Production values mirror what the
// Notion SPA waits for in the wild before declaring the workspace ready.
var (
	onboardingPollInterval = 800 * time.Millisecond
	onboardingPollTimeout  = 8 * time.Second
	onboardingMaxAttempts  = 3
)

// handleOnboarding ensures the freshly authenticated MSA user has at least
// one accessible workspace before extractSession runs. It returns an error
// (rather than silently logging) when the new account ends up without a
// space_view bound to user_root — which is the exact failure mode that
// turned 18/19 batch registrations into the "no_workspace" zombies that
// plagued /ai. Callers MUST treat the error as a fatal registration
// outcome and not persist the half-baked session to disk.
func (c *Client) handleOnboarding() error {
	headers := c.notionAPIHeaders()

	// Step 1: ask getLifecycleUserProfile whether onboarding is done.
	{
		body := bytes.NewReader([]byte(`{}`))
		resp, raw, err := c.postJSON(notionAPIBase+"/getLifecycleUserProfile", body, headers)
		if err == nil && resp.StatusCode == 200 {
			var data struct {
				LifecycleUserProfile struct {
					OnboardingCompleted bool `json:"onboarding_completed"`
				} `json:"lifecycleUserProfile"`
			}
			if json.Unmarshal(raw, &data) == nil && data.LifecycleUserProfile.OnboardingCompleted {
				c.logf("onboarding already completed")
				return nil
			}
		}
	}

	// Step 2: getSpacesInitial to discover user_id + user_name.
	cookies := map[string]string{}
	for _, ck := range c.jar.Cookies(mustParse(notionBase)) {
		cookies[ck.Name] = ck.Value
	}
	userID := cookies["notion_user_id"]
	userName := ""
	{
		body := bytes.NewReader([]byte(`{}`))
		resp, raw, err := c.postJSON(notionAPIBase+"/getSpacesInitial", body, headers)
		if err == nil && resp.StatusCode == 200 {
			var data struct {
				Users map[string]struct {
					NotionUser map[string]struct {
						Value map[string]interface{} `json:"value"`
					} `json:"notion_user"`
				} `json:"users"`
			}
			if json.Unmarshal(raw, &data) == nil {
				for uid, urec := range data.Users {
					if userID == "" {
						userID = uid
					}
					if nu, ok := urec.NotionUser[uid]; ok {
						val := nu.Value
						if inner, ok := val["value"].(map[string]interface{}); ok {
							val = inner
						}
						if n, ok := val["name"].(string); ok && n != "" {
							userName = n
							break
						}
					}
				}
			}
		}
	}
	if userID == "" {
		return newErr("notion_onboarding", "no notion_user_id available")
	}

	spaceName := "My Workspace"
	if userName != "" {
		spaceName = userName + "的工作空间"
	}
	deviceID := cookies["device_id"]
	if deviceID == "" {
		deviceID = cookies["notion_browser_id"]
	}
	if deviceID == "" {
		deviceID = newUUID()
	}

	// Step 3: createSpace, with retry. Notion's batch SSO onboarding
	// occasionally returns 200 OK with an empty space_view map — the
	// space exists in spaces table but is never linked to user_root, so
	// /ai later spins on an empty skeleton. Treat such "ghost spaces" as
	// retryable failures: a fresh deviceId per attempt seems to dodge
	// the spam heuristic that suppresses the space_view link.
	var lastErr error
	for attempt := 1; attempt <= onboardingMaxAttempts; attempt++ {
		if attempt > 1 {
			deviceID = newUUID()
			c.logf("createSpace retry %d/%d (new deviceId=%s)", attempt, onboardingMaxAttempts, deviceID)
		}
		err := c.createSpaceOnce(spaceName, deviceID, userID, headers)
		if err == nil {
			break
		}
		lastErr = err
		c.logf("createSpace attempt %d/%d failed: %v", attempt, onboardingMaxAttempts, err)
	}
	if c.createdSpace == nil {
		return newErr("notion_onboarding", "createSpace failed after %d attempts: %v", onboardingMaxAttempts, lastErr)
	}

	// Step 4: poll until user_root.space_view_pointers actually
	// references the freshly created space. Without this verification
	// the call to /ai keeps the SPA in a perpetual skeleton state — the
	// reverse-proxy "stuck" symptom that surfaced this whole bug.
	if err := c.waitForWorkspaceReady(userID, headers); err != nil {
		return err
	}
	return nil
}

// createSpaceOnce performs a single /createSpace round followed by the
// saveTransactionsMain call that the SPA fires immediately after — the
// pair of requests is what actually links the new workspace into
// user_root. Splitting them and only running /createSpace was the bug
// that made every Go-side registration end up as a "ghost space" with
// an unbound user_root: Notion's /createSpace endpoint never returns a
// space_view record on its own.
//
// On success it caches the space + the (client-generated) space_view
// id on the Client.
func (c *Client) createSpaceOnce(spaceName, deviceID, userID string, headers map[string]string) error {
	cs := map[string]interface{}{
		"name":           spaceName,
		"icon":           "🏠",
		"planType":       "personal",
		"planSelection":  "personal",
		"initialPersona": "unfilled",
		"deviceId":       deviceID,
		"deviceType":     "web-desktop",
		"source":         "handle_root_redirect",
	}
	csHeaders := map[string]string{}
	for k, v := range headers {
		csHeaders[k] = v
	}
	csHeaders["Referer"] = notionBase + "/onboarding"
	if userID != "" {
		csHeaders["x-notion-active-user-header"] = userID
	}
	body, _ := json.Marshal(cs)
	resp, raw, err := c.postJSON(notionAPIBase+"/createSpace", bytes.NewReader(body), csHeaders)
	if err != nil {
		return fmt.Errorf("post: %w", err)
	}
	if resp.StatusCode != 200 {
		return fmt.Errorf("HTTP %d body=%s", resp.StatusCode, truncate(string(raw), 240))
	}
	space, err := parseCreateSpaceResponse(raw, spaceName)
	if err != nil {
		dumpProofsDebug(c, "createSpace_response", notionAPIBase+"/createSpace", string(raw))
		return err
	}
	spaceID := stringOf(space["id"])
	if spaceID == "" {
		return fmt.Errorf("createSpace returned no spaceId")
	}

	// Phase 4b: the SPA invents a fresh space_view UUID client-side and
	// posts it via /api/v3/saveTransactionsMain. Without this transaction
	// the space exists but user_root.space_view_pointers stays empty
	// (which is the exact ghost-space symptom). Mirror the browser body
	// byte-for-byte so anti-spam heuristics don't reject us.
	viewID := newUUID()
	if err := c.attachSpaceView(spaceID, viewID, userID, headers); err != nil {
		return fmt.Errorf("attach space_view: %w", err)
	}

	c.createdSpace = space
	c.createdSpaceViewID = viewID
	c.logf("createSpace ok → space_id=%s space_view_id=%s name=%s",
		spaceID, viewID, spaceName)
	return nil
}

// attachSpaceView posts the saveTransactionsMain call that links a freshly
// created space into user_root. The shape of this body is captured in
// internal/msalogin/testdata/space_view_attach_capture.json — keep the
// operation order identical (set space_view → listAfter space_views →
// keyedObjectListAfter space_view_pointers) since Notion processes them
// in order and the latter two depend on the first.
func (c *Client) attachSpaceView(spaceID, viewID, userID string, headers map[string]string) error {
	if userID == "" {
		return fmt.Errorf("no userID")
	}
	requestID := newUUID()
	txID := newUUID()
	now := time.Now().UnixMilli()

	tx := map[string]interface{}{
		"requestId": requestID,
		"transactions": []map[string]interface{}{
			{
				"id":      txID,
				"spaceId": spaceID,
				"debug":   map[string]interface{}{"userAction": "spaceActions.createSpace"},
				"operations": []map[string]interface{}{
					{
						"pointer": map[string]interface{}{
							"table":   "space_view",
							"id":      viewID,
							"spaceId": spaceID,
						},
						"path":    []interface{}{},
						"command": "set",
						"args": map[string]interface{}{
							"id":                      viewID,
							"version":                 1,
							"space_id":                spaceID,
							"notify_mobile":           true,
							"notify_desktop":          true,
							"notify_email":            true,
							"parent_id":               userID,
							"parent_table":            "user_root",
							"alive":                   true,
							"first_joined_space_time": now,
							"joined":                  true,
							"settings": map[string]interface{}{
								"notify_email_digest":      true,
								"notify_home_digest_email": true,
							},
						},
					},
					{
						"pointer": map[string]interface{}{"table": "user_root", "id": userID},
						"path":    []interface{}{"space_views"},
						"command": "listAfter",
						"args":    map[string]interface{}{"id": viewID},
					},
					{
						"pointer": map[string]interface{}{"table": "user_root", "id": userID},
						"path":    []interface{}{"space_view_pointers"},
						"command": "keyedObjectListAfter",
						"args": map[string]interface{}{
							"value": map[string]interface{}{
								"table":   "space_view",
								"id":      viewID,
								"spaceId": spaceID,
							},
						},
					},
				},
			},
		},
	}

	body, _ := json.Marshal(tx)
	stHeaders := map[string]string{}
	for k, v := range headers {
		stHeaders[k] = v
	}
	stHeaders["Referer"] = notionBase + "/onboarding"
	stHeaders["x-notion-active-user-header"] = userID

	resp, raw, err := c.postJSON(notionAPIBase+"/saveTransactionsMain", bytes.NewReader(body), stHeaders)
	if err != nil {
		return fmt.Errorf("post: %w", err)
	}
	if resp.StatusCode != 200 {
		return fmt.Errorf("HTTP %d body=%s", resp.StatusCode, truncate(string(raw), 240))
	}
	c.logf("attachSpaceView ok → space_id=%s space_view_id=%s tx=%s", spaceID, viewID, txID)
	return nil
}

// parseCreateSpaceResponse extracts the canonical space record from a
// /createSpace response body. The endpoint never returns a space_view
// record — that comes from a follow-up saveTransactionsMain call (see
// attachSpaceView). It only returns an error when the response is
// missing the spaceId entirely.
func parseCreateSpaceResponse(raw []byte, fallbackName string) (map[string]interface{}, error) {
	var csResp struct {
		SpaceID   string `json:"spaceId"`
		RecordMap struct {
			Space map[string]json.RawMessage `json:"space"`
		} `json:"recordMap"`
	}
	if err := json.Unmarshal(raw, &csResp); err != nil {
		return nil, fmt.Errorf("parse: %w", err)
	}
	if csResp.SpaceID == "" {
		return nil, fmt.Errorf("response carries no spaceId")
	}
	space := map[string]interface{}{
		"id":        csResp.SpaceID,
		"name":      fallbackName,
		"plan_type": "personal",
	}
	if rawSpace, ok := csResp.RecordMap.Space[csResp.SpaceID]; ok {
		var rec struct {
			Value struct {
				Value map[string]interface{} `json:"value"`
			} `json:"value"`
		}
		if json.Unmarshal(rawSpace, &rec) == nil && rec.Value.Value != nil {
			space = rec.Value.Value
		}
	}
	return space, nil
}

// waitForWorkspaceReady polls syncRecordValuesMain until user_root actually
// references at least one space_view (or the timeout elapses). This catches
// the case where createSpace returned a space_view in its response, but the
// user_root mutation hasn't propagated yet — extractSession would otherwise
// race ahead and read an empty user_root from /loadUserContent.
func (c *Client) waitForWorkspaceReady(userID string, headers map[string]string) error {
	deadline := time.Now().Add(onboardingPollTimeout)
	body, _ := json.Marshal(map[string]interface{}{
		"requests": []map[string]interface{}{
			{
				"pointer": map[string]string{"table": "user_root", "id": userID},
				"version": -1,
			},
		},
	})
	for {
		resp, raw, err := c.postJSON(notionAPIBase+"/syncRecordValuesMain", bytes.NewReader(body), headers)
		if err == nil && resp.StatusCode == 200 {
			if userRootHasSpaceView(raw, userID) {
				c.logf("user_root.space_view_pointers populated for %s", userID)
				return nil
			}
		}
		if time.Now().After(deadline) {
			return newErr("notion_onboarding", "user_root.space_view_pointers never appeared within %s — workspace not linked to user", onboardingPollTimeout)
		}
		time.Sleep(onboardingPollInterval)
	}
}

// userRootHasSpaceView returns true when the syncRecordValuesMain response
// body shows that user_root[userID].value.value.space_view_pointers (or
// space_views) is non-empty — i.e. Notion has actually linked at least one
// workspace to this user.
func userRootHasSpaceView(raw []byte, userID string) bool {
	var rm struct {
		RecordMap struct {
			UserRoot map[string]json.RawMessage `json:"user_root"`
		} `json:"recordMap"`
	}
	if err := json.Unmarshal(raw, &rm); err != nil {
		return false
	}
	rawUR, ok := rm.RecordMap.UserRoot[userID]
	if !ok {
		return false
	}
	var ur struct {
		Value struct {
			Value struct {
				SpaceViewPointers []json.RawMessage `json:"space_view_pointers"`
				SpaceViews        []json.RawMessage `json:"space_views"`
			} `json:"value"`
		} `json:"value"`
	}
	if err := json.Unmarshal(rawUR, &ur); err != nil {
		return false
	}
	return len(ur.Value.Value.SpaceViewPointers) > 0 || len(ur.Value.Value.SpaceViews) > 0
}

// ── Phase 5: extract session ────────────────────────────────────────────

func (c *Client) extractSession() (*NotionSession, error) {
	headers := c.notionAPIHeaders()
	cookies := map[string]string{}
	for _, ck := range c.jar.Cookies(mustParse(notionBase)) {
		cookies[ck.Name] = ck.Value
	}

	c.logf("calling loadUserContent")
	resp, raw, err := c.postJSON(notionAPIBase+"/loadUserContent", bytes.NewReader([]byte(`{}`)), headers)
	if err != nil {
		return nil, newErr("notion_load_user", "%v", err)
	}
	if resp.StatusCode != 200 {
		return nil, newErr("notion_load_user", "HTTP %d", resp.StatusCode)
	}
	var ud struct {
		RecordMap struct {
			NotionUser   map[string]json.RawMessage `json:"notion_user"`
			UserRoot     map[string]json.RawMessage `json:"user_root"`
			Space        map[string]json.RawMessage `json:"space"`
			UserSettings map[string]json.RawMessage `json:"user_settings"`
		} `json:"recordMap"`
	}
	if err := json.Unmarshal(raw, &ud); err != nil {
		return nil, newErr("notion_load_user", "JSON: %v", err)
	}

	userID, userName, userEmail := pickUserInfo(ud.RecordMap.NotionUser)

	bestSpace, bestSVID := pickBestSpace(ud.RecordMap.Space, ud.RecordMap.UserRoot, userID)
	if bestSpace == nil {
		// Pre-fix behavior fell back to c.createdSpace here, which let
		// "ghost spaces" (created on Notion's side but never bound to
		// user_root) sneak into accounts/ as zombie no-workspace JSONs.
		// handleOnboarding() now guarantees user_root has at least one
		// space_view_pointer before we reach this point, so an empty
		// best-space at this stage is a hard failure — surface it
		// instead of writing a session that /ai will hang on.
		return nil, newErr("notion_load_user", "loadUserContent returned no usable space (user_root.space_views empty; createSpace likely never linked to user_root)")
	}

	spaceID := stringOf(bestSpace["id"])
	spaceName := stringOf(bestSpace["name"])
	planType := stringOf(bestSpace["plan_type"])
	if spaceID == "" {
		return nil, newErr("notion_load_user", "best space record has no id")
	}

	timezone := "UTC"
	if usRaw, ok := ud.RecordMap.UserSettings[userID]; ok {
		var us struct {
			Value struct {
				Value struct {
					Settings struct {
						TimeZone string `json:"time_zone"`
					} `json:"settings"`
				} `json:"value"`
			} `json:"value"`
		}
		if json.Unmarshal(usRaw, &us) == nil && us.Value.Value.Settings.TimeZone != "" {
			timezone = us.Value.Value.Settings.TimeZone
		}
	}

	clientVersion := cookies["notion_client_version"]
	if clientVersion == "" {
		clientVersion = c.clientVersion
	}
	if clientVersion == "" {
		clientVersion = "unknown"
	}

	models := c.fetchModels(spaceID, headers)

	allCookies := []string{}
	for _, ck := range c.jar.Cookies(mustParse(notionBase)) {
		allCookies = append(allCookies, fmt.Sprintf("%s=%s", ck.Name, ck.Value))
	}

	return &NotionSession{
		TokenV2:         c.tokenV2,
		UserID:          userID,
		UserName:        userName,
		UserEmail:       userEmail,
		SpaceID:         spaceID,
		SpaceName:       spaceName,
		SpaceViewID:     bestSVID,
		PlanType:        planType,
		Timezone:        timezone,
		ClientVersion:   clientVersion,
		BrowserID:       cookies["notion_browser_id"],
		DeviceID:        cookies["device_id"],
		FullCookie:      strings.Join(allCookies, "; "),
		AvailableModels: models,
		ExtractedAt:     time.Now().UTC().Format(time.RFC3339),
	}, nil
}

// pickUserInfo extracts the first notion_user record's id/name/email.
func pickUserInfo(users map[string]json.RawMessage) (id, name, email string) {
	for uid, raw := range users {
		var rec struct {
			Value struct {
				Value map[string]interface{} `json:"value"`
			} `json:"value"`
		}
		if json.Unmarshal(raw, &rec) == nil {
			val := rec.Value.Value
			if val == nil {
				var fallback struct {
					Value map[string]interface{} `json:"value"`
				}
				if json.Unmarshal(raw, &fallback) == nil {
					val = fallback.Value
				}
			}
			if val != nil {
				name = stringOf(val["name"])
				email = stringOf(val["email"])
			}
		}
		id = uid
		return
	}
	return "", "", ""
}

// pickBestSpace prefers a non-free space with AI enabled; falls back to the
// first space with a non-empty id.
func pickBestSpace(
	spaces, userRoots map[string]json.RawMessage, userID string,
) (map[string]interface{}, string) {
	type ptr struct {
		ID      string `json:"id"`
		SpaceID string `json:"spaceId"`
	}
	var pointers []ptr
	if rawUR, ok := userRoots[userID]; ok {
		var ur struct {
			Value struct {
				Value struct {
					SpaceViewPointers []ptr `json:"space_view_pointers"`
				} `json:"value"`
			} `json:"value"`
		}
		if json.Unmarshal(rawUR, &ur) == nil {
			pointers = ur.Value.Value.SpaceViewPointers
		}
	}
	loadSpace := func(sid string) map[string]interface{} {
		raw, ok := spaces[sid]
		if !ok {
			return nil
		}
		var rec struct {
			Value struct {
				Value map[string]interface{} `json:"value"`
			} `json:"value"`
		}
		if json.Unmarshal(raw, &rec) != nil || rec.Value.Value == nil {
			return nil
		}
		return rec.Value.Value
	}

	var best map[string]interface{}
	bestSVID := ""
	for _, p := range pointers {
		val := loadSpace(p.SpaceID)
		if val == nil || stringOf(val["id"]) == "" {
			continue
		}
		settings, _ := val["settings"].(map[string]interface{})
		aiOK := true
		if settings != nil {
			if v, ok := settings["enable_ai_feature"].(bool); ok && !v {
				aiOK = false
			}
			if v, ok := settings["disable_ai_feature"].(bool); ok && v {
				aiOK = false
			}
		}
		if best == nil || (aiOK && stringOf(val["plan_type"]) != "free") {
			best = val
			bestSVID = p.ID
		}
	}
	if best != nil {
		return best, bestSVID
	}
	for sid := range spaces {
		if val := loadSpace(sid); val != nil && stringOf(val["id"]) != "" {
			return val, ""
		}
	}
	return nil, ""
}

// fetchModels calls getAvailableModels for the given space.
func (c *Client) fetchModels(spaceID string, headers map[string]string) []map[string]interface{} {
	if spaceID == "" {
		return nil
	}
	body, _ := json.Marshal(map[string]string{"spaceId": spaceID})
	resp, raw, err := c.postJSON(notionAPIBase+"/getAvailableModels", bytes.NewReader(body), headers)
	if err != nil || resp.StatusCode != 200 {
		return nil
	}
	var data struct {
		Models []struct {
			Model        string `json:"model"`
			ModelMessage string `json:"modelMessage"`
			IsDisabled   bool   `json:"isDisabled"`
		} `json:"models"`
	}
	if err := json.Unmarshal(raw, &data); err != nil {
		return nil
	}
	out := make([]map[string]interface{}, 0, len(data.Models))
	for _, m := range data.Models {
		if m.IsDisabled {
			continue
		}
		name := m.ModelMessage
		if name == "" {
			name = m.Model
		}
		out = append(out, map[string]interface{}{
			"name": name,
			"id":   m.Model,
		})
	}
	return out
}

// newUUID returns a random UUID v4 string.
func newUUID() string {
	b := make([]byte, 16)
	_, _ = cryptoRandRead(b)
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:])
}
