package msalogin

import (
	"strings"
	"testing"
	"time"
)

// Real text/plain alternative we observed from MS for the
// account-security-noreply@accountprotection.microsoft.com sender:
//
//	Please use the following security code for your personal Microsoft account ay**5@hotmail.com.
//	Security code: 054325
//	Only enter this code on an official website or app. Don't share it with anyone.
//
// The bug we just fixed: the previous extractor would happily return
// any 6-digit number from any email subject-matching "code"/"verify",
// including ChatGPT/OpenAI OTP mails or stale earlier MS codes — so
// the proofs flow would submit the wrong number and silently loop.
//
// These tests pin down the corrected behaviour.
func TestExtractMSSecurityCodeFromCanonicalBody(t *testing.T) {
	body := strings.Join([]string{
		"From: Microsoft account team <account-security-noreply@accountprotection.microsoft.com>",
		"Subject: Personal Microsoft account security code",
		"",
		"Please use the following security code for your personal Microsoft account ay**5@hotmail.com.",
		"",
		"Security code: 054325",
		"",
		"Only enter this code on an official website or app.",
	}, "\r\n")
	got := extractMSSecurityCode(body)
	if got != "054325" {
		t.Fatalf("want 054325, got %q", got)
	}
}

func TestExtractMSSecurityCodeIgnoresOtherSixDigitNumbers(t *testing.T) {
	// Real MS bodies have lots of unrelated 6-digit IDs (trace
	// guids, Message-ID fragments, antispam IDs). The extractor
	// must NOT return any of them — only the labelled "Security
	// code: N" line.
	body := strings.Join([]string{
		"X-MS-Exchange-Organization-Network-Message-Id: ea00f2fb-4fa1-4476-9996-08dea3facfa0",
		"Subject: Personal Microsoft account security code",
		"X-Some-Trace: 989104", // looks like a code but isn't
		"",
		"<html>...random tracking pixel id 123456...",
		"",
		"Security code: 054325",
		"",
	}, "\r\n")
	got := extractMSSecurityCode(body)
	if got != "054325" {
		t.Fatalf("must prefer labelled code, got %q", got)
	}
}

func TestExtractMSSecurityCodeRefusesUnlabelledBody(t *testing.T) {
	// Body has 6-digit numbers but no "Security code:" line — we
	// refuse to guess (the alternative is mis-submitting a totally
	// unrelated OTP, which is what the previous greedy regex did).
	body := "Hello 123456 world, here is some other 989104 number 555555."
	if got := extractMSSecurityCode(body); got != "" {
		t.Fatalf("must return empty for unlabelled body, got %q", got)
	}
}

func TestIsMSSecurityCodeMailMatchesAccountProtectionSender(t *testing.T) {
	cases := []struct {
		from, subj string
		want       bool
	}{
		{"account-security-noreply@accountprotection.microsoft.com", "Personal Microsoft account security code", true},
		{"random@noreply.microsoft.com", "Microsoft account security code", true},      // by subject
		{"verify@chatgpt.openai.com", "你的 ChatGPT 代码为 055913", false},                  // wrong sender + wrong subject
		{"openai@mandrillapp.com", "你的 OpenAI 代码为 532420", false},                      // wrong sender + wrong subject
		{"account-security-noreply@accountprotection.microsoft.com", "anything", true}, // sender suffices
	}
	for _, tc := range cases {
		got := isMSSecurityCodeMail(fetchedMessage{From: tc.from, Subject: tc.subj})
		if got != tc.want {
			t.Errorf("isMSSecurityCodeMail(from=%q subj=%q): got %v want %v",
				tc.from, tc.subj, got, tc.want)
		}
	}
}

// TestExtractWatermarkRoundTrip pins the (timestamp, max-seq)
// watermark we stamp in CollectExistingIDs and read back in
// WaitVerificationCode.  Without the watermark, a stale
// "Security code: 446575" left in the inbox from a previous run gets
// picked up as the current code.
func TestExtractWatermarkRoundTrip(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Second)
	skipIDs := map[string]struct{}{
		imapWatermarkKey: {},
		"id-1":           {},
		"id-2":           {},
		"ts=" + now.Format(time.RFC3339) + ";seq=42": {},
	}
	gotTS, gotSeq := extractWatermark(skipIDs)
	if !gotTS.Equal(now) {
		t.Fatalf("ts mismatch: got %v want %v", gotTS, now)
	}
	if gotSeq != 42 {
		t.Fatalf("seq mismatch: got %d want 42", gotSeq)
	}
}

func TestExtractWatermarkFallsBackWhenMissing(t *testing.T) {
	// No "ts=...;seq=..." entry → fallback to (now-1m, 0). Verify
	// the seq is 0 (so any non-zero seq message will be considered
	// fresh) and the ts is in the past.
	skipIDs := map[string]struct{}{"id-1": {}, "id-2": {}}
	gotTS, gotSeq := extractWatermark(skipIDs)
	if gotSeq != 0 {
		t.Fatalf("expected seq=0 fallback, got %d", gotSeq)
	}
	if gotTS.After(time.Now().UTC()) {
		t.Fatalf("fallback ts should be in the past, got %v", gotTS)
	}
}
