package proxy

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"log"
	"strings"
	"sync"
	"time"
)

var (
	contextLossMetricsMu sync.Mutex
	contextLossMetrics   = make(map[string]int)
)

func recordContextLossMetric(reason string) {
	contextLossMetricsMu.Lock()
	contextLossMetrics[reason]++
	count := contextLossMetrics[reason]
	contextLossMetricsMu.Unlock()
	log.Printf("[metrics] context_loss: %s (total: %d)", reason, count)
}

// Session represents an active multi-turn conversation mapped to a Notion thread.
// A thread is bound to the account that created it — subsequent turns must use the same account.
type Session struct {
	ThreadID     string // Notion threadId (generated on first turn, reused)
	TurnCount    int    // completed conversation turns (user+assistant pairs)
	AccountEmail string // bound account (thread is tied to the creating account)
	CreatedAt    time.Time
	LastUsedAt   time.Time

	// Reused transcript entry IDs (generated on first turn, reused on subsequent turns)
	ConfigID  string
	ContextID string

	// Each completed turn produces one updated-config placeholder ID
	UpdatedConfigIDs []string

	// First turn's context.currentDatetime (reused on subsequent turns — NOT updated!)
	OriginalDatetime string

	// Model resolved on first turn (added to config on subsequent turns)
	ModelUsed string

	// Total non-system messages in the Anthropic request at this turn.
	// Used to distinguish chain continuation (count increased) from retry (count unchanged).
	RawMessageCount int
}

// SessionManager manages the mapping from Anthropic API conversation fingerprints to Notion threads.
type SessionManager struct {
	mu       sync.RWMutex
	sessions map[string]*Session
	ttl      time.Duration
}

// globalSessionManager is the package-level session manager instance
var globalSessionManager *SessionManager

func init() {
	globalSessionManager = NewSessionManager(30 * time.Minute)
}

// NewSessionManager creates a new SessionManager with the given TTL and starts cleanup.
func NewSessionManager(ttl time.Duration) *SessionManager {
	sm := &SessionManager{
		sessions: make(map[string]*Session),
		ttl:      ttl,
	}
	go sm.cleanupLoop()
	return sm
}

// Get retrieves a session by fingerprint, optionally filtering by account email.
// Returns nil if no matching session exists or if the session has expired.
func (sm *SessionManager) Get(fingerprint string) *Session {
	sm.mu.RLock()
	defer sm.mu.RUnlock()

	s, ok := sm.sessions[fingerprint]
	if !ok {
		return nil
	}
	if time.Since(s.LastUsedAt) > sm.ttl {
		return nil
	}
	return s
}

// Set stores a session for the given fingerprint.
func (sm *SessionManager) Set(fingerprint string, session *Session) {
	sm.mu.Lock()
	defer sm.mu.Unlock()
	sm.sessions[fingerprint] = session
}

// Delete removes a session by fingerprint.
func (sm *SessionManager) Delete(fingerprint string) {
	sm.mu.Lock()
	defer sm.mu.Unlock()
	delete(sm.sessions, fingerprint)
}

// DeleteByAccount removes all sessions bound to a specific account email.
func (sm *SessionManager) DeleteByAccount(email string) {
	sm.mu.Lock()
	defer sm.mu.Unlock()
	for fp, s := range sm.sessions {
		if s.AccountEmail == email {
			delete(sm.sessions, fp)
		}
	}
}

// Count returns the number of active sessions.
func (sm *SessionManager) Count() int {
	sm.mu.RLock()
	defer sm.mu.RUnlock()
	return len(sm.sessions)
}

// cleanupLoop periodically removes expired sessions.
func (sm *SessionManager) cleanupLoop() {
	ticker := time.NewTicker(5 * time.Minute)
	defer ticker.Stop()
	for range ticker.C {
		sm.mu.Lock()
		now := time.Now()
		removed := 0
		for fp, s := range sm.sessions {
			if now.Sub(s.LastUsedAt) > sm.ttl {
				delete(sm.sessions, fp)
				removed++
			}
		}
		sm.mu.Unlock()
		if removed > 0 {
			log.Printf("[session] cleaned up %d expired sessions, %d remaining", removed, sm.Count())
		}
	}
}

func normalizeSessionSystemContent(content string) string {
	if content == "" {
		return ""
	}
	lines := strings.Split(content, "\n")
	filtered := make([]string, 0, len(lines))
	for _, line := range lines {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "x-anthropic-billing-header:") {
			continue
		}
		filtered = append(filtered, line)
	}
	return strings.TrimSpace(strings.Join(filtered, "\n"))
}

func normalizeSessionUserContent(content string) string {
	if content == "" {
		return ""
	}
	return strings.TrimSpace(stripClaudeCodeInstructions(content))
}

func isMeaningfulUserMessage(msg ChatMessage) bool {
	if msg.Role != "user" || msg.ToolCallID != "" {
		return false
	}
	content := normalizeSessionUserContent(msg.Content)
	if content == "" {
		return false
	}

	// Skip multi-turn continuation wrappers so we anchor to the actual user query
	if strings.HasPrefix(content, "Here is the result of the tool run:") || strings.HasPrefix(content, "Results from executed function(s):") {
		return false
	}
	return true
}

func shouldCountNonSystemMessage(msg ChatMessage) bool {
	switch msg.Role {
	case "system":
		return false
	case "user":
		return isMeaningfulUserMessage(msg)
	case "assistant":
		return strings.TrimSpace(msg.Content) != "" || len(msg.ToolCalls) > 0
	case "tool":
		return strings.TrimSpace(msg.Content) != "" || msg.ToolCallID != "" || msg.Name != ""
	default:
		return strings.TrimSpace(msg.Content) != ""
	}
}

// cloneChatMessages returns a deep copy of the message slice so callers can
// mutate the copy (e.g. tool injection rewriting Content in place) without
// affecting the original. Tool call slices are also copied because the
// underlying ToolCall structs are read-only after construction.
func cloneChatMessages(src []ChatMessage) []ChatMessage {
	if src == nil {
		return nil
	}
	out := make([]ChatMessage, len(src))
	for i, m := range src {
		out[i] = m
		if len(m.ToolCalls) > 0 {
			out[i].ToolCalls = append([]ToolCall(nil), m.ToolCalls...)
		}
	}
	return out
}

// computeSessionFingerprintWithSalt generates a fingerprint from the message history
// to identify the same conversation across Anthropic API requests.
// Strategy: hash(optional stable salt + normalized system prompt prefix + first user message prefix).
func computeSessionFingerprintWithSalt(messages []ChatMessage, stableSalt string) string {
	h := sha256.New()
	if stableSalt != "" {
		h.Write([]byte("salt:"))
		h.Write([]byte(stableSalt))
		h.Write([]byte{'\n'})
	}
	// Include system prompt
	for _, m := range messages {
		if m.Role == "system" {
			content := normalizeSessionSystemContent(m.Content)
			if len([]rune(content)) > 200 {
				recordContextLossMetric("fingerprint_system_truncated")
				content = string([]rune(content)[:200])
			}
			h.Write([]byte(content))
			break
		}
	}
	// Include first user message
	for _, m := range messages {
		if isMeaningfulUserMessage(m) {
			content := normalizeSessionUserContent(m.Content)
			if len([]rune(content)) > 200 {
				recordContextLossMetric("fingerprint_user_truncated")
				content = string([]rune(content)[:200])
			}
			h.Write([]byte(content))
			break
		}
	}
	return hex.EncodeToString(h.Sum(nil))[:32]
}

// computeSessionFingerprint keeps the legacy signature for tests/callers that
// do not have an explicit stable salt available.
func computeSessionFingerprint(messages []ChatMessage) string {
	return computeSessionFingerprintWithSalt(messages, "")
}

// countUserMessages counts the number of user-role messages in the list.
func countUserMessages(messages []ChatMessage) int {
	count := 0
	for _, m := range messages {
		if isMeaningfulUserMessage(m) {
			count++
		}
	}
	return count
}

// countNonSystemMessages counts all messages except system-role messages.
// Used for session continuation detection: tool chains add assistant+tool messages
// each turn, while user message count stays constant.
func countNonSystemMessages(messages []ChatMessage) int {
	count := 0
	for _, m := range messages {
		if shouldCountNonSystemMessage(m) {
			count++
		}
	}
	return count
}

// extractLastUserMessage returns the content of the last user message.
func extractLastUserMessage(messages []ChatMessage) string {
	for i := len(messages) - 1; i >= 0; i-- {
		if isMeaningfulUserMessage(messages[i]) {
			return normalizeSessionUserContent(messages[i].Content)
		}
	}
	return ""
}

// needsFreshThreadRecovery returns true when the incoming message list carries
// prior conversation state that should be collapsed before starting a new
// Notion thread. Replaying assistant history as a fresh transcript is brittle
// and can lead to empty responses from Notion.
func needsFreshThreadRecovery(messages []ChatMessage) bool {
	lastUserIdx := -1
	for i := len(messages) - 1; i >= 0; i-- {
		if isMeaningfulUserMessage(messages[i]) {
			lastUserIdx = i
			break
		}
	}
	if lastUserIdx < 0 {
		return false
	}
	for i := 0; i < len(messages); i++ {
		if i != lastUserIdx && shouldCountNonSystemMessage(messages[i]) {
			return true
		}
	}
	return false
}

// buildFreshThreadRecoveryMessages collapses prior conversation state into a
// single self-contained user prompt for use when we must recover onto a brand
// new Notion thread (for example after session loss or account failover).
func buildRecoveryMessages(messages []ChatMessage, skipEntry func(ChatMessage, string) bool) []ChatMessage {
	if !needsFreshThreadRecovery(messages) {
		return messages
	}

	const (
		maxSystemChars  = 1200
		maxHistoryChars = 4000
		maxEntryChars   = 900
	)

	lastUserIdx := -1
	for i := len(messages) - 1; i >= 0; i-- {
		if isMeaningfulUserMessage(messages[i]) {
			lastUserIdx = i
			break
		}
	}
	if lastUserIdx < 0 {
		return messages
	}

	firstUserIdx := -1
	for i := 0; i < len(messages); i++ {
		if isMeaningfulUserMessage(messages[i]) {
			firstUserIdx = i
			break
		}
	}

	clip := func(s string, limit int, label string) string {
		runes := []rune(s)
		if limit <= 0 || len(runes) <= limit {
			return s
		}

		droppedLines := 0
		if limit < 50 {
			dropped := string(runes[limit:])
			droppedLines = strings.Count(dropped, "\n")
		} else {
			headLimit := limit / 2
			tailLimit := limit - headLimit - 25
			if len(runes) > headLimit+tailLimit {
				dropped := string(runes[headLimit : len(runes)-tailLimit])
				droppedLines = strings.Count(dropped, "\n")
			}
		}
		log.Printf("[bridge] diagnostic: session recovery truncated %s context (original: %d chars, limit: %d chars, dropped %d lines)", label, len(runes), limit, droppedLines)

		var reason string
		if label == "system" {
			reason = "system_instruction_truncated"
		} else if label == "User (latest)" {
			reason = "latest_user_message_truncated"
		} else if strings.HasPrefix(label, "Tool") {
			reason = "tool_result_truncated"
		} else {
			reason = "history_entry_truncated"
		}
		recordContextLossMetric(reason)

		if limit < 50 {
			return string(runes[:limit]) + "..."
		}

		headLimit := limit / 2
		tailLimit := limit - headLimit - 25

		head := string(runes[:headLimit])
		tail := string(runes[len(runes)-tailLimit:])

		return head + "\n\n...[truncated]...\n\n" + tail
	}

	var systemParts []string
	for _, m := range messages {
		if m.Role == "system" && strings.TrimSpace(m.Content) != "" {
			systemParts = append(systemParts, strings.TrimSpace(m.Content))
		} else if m.Role == "system" && strings.TrimSpace(m.Content) == "" {
			recordContextLossMetric("empty_system_prompt_dropped")
		}
	}

	type historyEntry struct {
		label   string
		content string
	}

	var reversed []historyEntry
	preservedFirstUser := false
	usedChars := 0
	for i := lastUserIdx - 1; i >= 0; i-- {
		m := messages[i]
		if m.Role == "system" {
			continue
		}

		content := strings.TrimSpace(m.Content)
		if m.Role == "user" {
			content = normalizeSessionUserContent(m.Content)
		}
		if content == "" {
			if m.Role == "tool" {
				content = "(empty output)"
			} else {
				recordContextLossMetric("recovery_empty_entry_dropped")
				continue
			}
		}
		if skipEntry != nil && skipEntry(m, content) {
			log.Printf("[bridge] diagnostic: skipped entry during recovery traversal (role: %s, name: %s)", m.Role, m.Name)
			recordContextLossMetric("recovery_skipped_entry")
			continue
		}

		label := ""
		switch m.Role {
		case "user":
			label = "User"
		case "assistant":
			label = "Assistant"
		case "tool":
			name := m.Name
			if name == "" {
				name = "tool"
			}
			label = fmt.Sprintf("Tool (%s)", name)
		default:
			continue
		}

		content = clip(content, maxEntryChars, label)
		entryCost := len(label) + len(content) + 4
		if usedChars > 0 && usedChars+entryCost > maxHistoryChars {
			log.Printf("[bridge] diagnostic: session recovery truncated conversation history (used %d chars, dropping oldest entries)", usedChars)
			recordContextLossMetric("conversation_history_dropped")
			break
		}
		usedChars += entryCost
		reversed = append(reversed, historyEntry{label: label, content: content})
		if i == firstUserIdx {
			preservedFirstUser = true
		}
	}

	if !preservedFirstUser {
		recordContextLossMetric("first_user_message_dropped")
	}
	log.Printf("[bridge] diagnostic: instruction preservation during handoff - first user message included: %v, used chars: %d", preservedFirstUser, usedChars)

	// Build tool call ID -> name map to properly resolve tool names in multi-turn
	tcMap := make(map[string]string)
	for _, m := range messages {
		for _, tc := range m.ToolCalls {
			tcMap[tc.ID] = tc.Function.Name
		}
	}

	var trailingReversed []historyEntry
	for i := len(messages) - 1; i > lastUserIdx; i-- {
		m := messages[i]
		if m.Role == "system" || m.Role == "user" {
			continue
		}

		content := strings.TrimSpace(m.Content)
		if m.Role == "assistant" && len(m.ToolCalls) > 0 {
			var calls []string
			for _, tc := range m.ToolCalls {
				calls = append(calls, fmt.Sprintf("Call %s(%s)", tc.Function.Name, tc.Function.Arguments))
			}
			if content != "" {
				content += "\n" + strings.Join(calls, "\n")
			} else {
				content = strings.Join(calls, "\n")
			}
		}

		// Ensure we strictly preserve tool results even if content is empty (e.g. successful bash execution with no output)
		if content == "" {
			if m.Role == "tool" {
				content = "(empty output)"
			} else {
				recordContextLossMetric("recovery_empty_entry_dropped")
				continue
			}
		}
		if skipEntry != nil && skipEntry(m, content) {
			log.Printf("[bridge] diagnostic: skipped entry during recovery traversal (role: %s, name: %s)", m.Role, m.Name)
			recordContextLossMetric("recovery_skipped_entry")
			continue
		}

		label := ""
		switch m.Role {
		case "assistant":
			label = "Assistant"
		case "tool":
			name := m.Name
			if name == "" && m.ToolCallID != "" {
				if n, ok := tcMap[m.ToolCallID]; ok {
					name = n
				}
			}
			if name == "" {
				name = "tool"
			}
			label = fmt.Sprintf("Tool (%s)", name)
		default:
			continue
		}

		content = clip(content, maxEntryChars, label)
		entryCost := len(label) + len(content) + 4
		if usedChars > 0 && usedChars+entryCost > maxHistoryChars {
			log.Printf("[bridge] diagnostic: session recovery truncated partial progress (used %d chars, dropping oldest entries)", usedChars)
			recordContextLossMetric("trailing_progress_dropped")
			break
		}
		usedChars += entryCost
		trailingReversed = append(trailingReversed, historyEntry{label: label, content: content})
	}

	var history strings.Builder
	for i := len(reversed) - 1; i >= 0; i-- {
		if history.Len() > 0 {
			history.WriteString("\n\n")
		}
		history.WriteString(reversed[i].label)
		history.WriteString(": ")
		history.WriteString(reversed[i].content)
	}

	latest := clip(normalizeSessionUserContent(messages[lastUserIdx].Content), 8000, "User (latest)")

	var prompt strings.Builder
	prompt.WriteString("Continue this conversation on a fresh thread.\n")
	prompt.WriteString("Use the context below and answer the latest user message directly.\n")
	prompt.WriteString("Do not mention missing context, prior thread state, or recovery.\n")

	if len(systemParts) > 0 {
		prompt.WriteString("\n\nSystem instructions:\n")
		prompt.WriteString(clip(strings.Join(systemParts, "\n\n"), maxSystemChars, "system"))
	}

	if history.Len() > 0 {
		prompt.WriteString("\n\nConversation context:\n")
		prompt.WriteString(history.String())
	}

	prompt.WriteString("\n\nLatest user message:\n")
	prompt.WriteString(latest)

	if len(trailingReversed) > 0 {
		prompt.WriteString("\n\nPartial progress since the latest user message:\n")
		for i := len(trailingReversed) - 1; i >= 0; i-- {
			prompt.WriteString(trailingReversed[i].label)
			prompt.WriteString(": ")
			prompt.WriteString(trailingReversed[i].content)
			if i > 0 {
				prompt.WriteString("\n\n")
			}
		}
		prompt.WriteString("\n\nContinue from the partial progress above and provide the next step or final answer.")
	}

	return []ChatMessage{{
		Role:    "user",
		Content: prompt.String(),
	}}
}

func buildFreshThreadRecoveryMessages(messages []ChatMessage) []ChatMessage {
	return buildRecoveryMessages(messages, nil)
}

func buildToolBridgeRecoveryMessages(messages []ChatMessage) []ChatMessage {
	return buildRecoveryMessages(messages, func(msg ChatMessage, content string) bool {
		if msg.Role != "assistant" {
			return false
		}
		isNoTool, reason := detectToolBridgeNoToolResponse(content)
		if isNoTool {
			if reason == "workspace reframing" {
				log.Printf("[bridge] diagnostic: workspace reframing explicitly tracked (dropped from context during session recovery)")
			} else if reason != "" {
				log.Printf("[bridge] diagnostic: %s explicitly tracked (dropped from context during session recovery)", reason)
			}
		}
		return isNoTool
	})
}
