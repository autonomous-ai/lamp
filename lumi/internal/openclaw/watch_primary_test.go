package openclaw

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// ---- splitProviderModel ----

func TestSplitProviderModel(t *testing.T) {
	tests := []struct {
		input    string
		provider string
		key      string
		ok       bool
	}{
		{"autonomous/claude-opus-4-6", "autonomous", "claude-opus-4-6", true},
		{"openai-codex/gpt-5.5", "openai-codex", "gpt-5.5", true},
		// Model key may itself contain slashes (e.g. "org/model/variant")
		{"autonomous/meta/llama-3", "autonomous", "meta/llama-3", true},
		{"claude-opus-4-6", "", "claude-opus-4-6", false}, // no provider prefix
		{"", "", "", false},
	}
	for _, tc := range tests {
		prov, key, ok := splitProviderModel(tc.input)
		if ok != tc.ok || prov != tc.provider || key != tc.key {
			t.Errorf("splitProviderModel(%q) = (%q, %q, %v); want (%q, %q, %v)",
				tc.input, prov, key, ok, tc.provider, tc.key, tc.ok)
		}
	}
}

// ---- extractPrimaryModel ----

func TestExtractPrimaryModel(t *testing.T) {
	cfg := map[string]any{
		"agents": map[string]any{
			"defaults": map[string]any{
				"model": map[string]any{
					"primary": "autonomous/claude-opus-4-6",
				},
			},
		},
	}
	got := extractPrimaryModel(cfg)
	if got != "autonomous/claude-opus-4-6" {
		t.Errorf("extractPrimaryModel = %q; want %q", got, "autonomous/claude-opus-4-6")
	}
}

func TestExtractPrimaryModel_Missing(t *testing.T) {
	cases := []map[string]any{
		{},
		{"agents": map[string]any{}},
		{"agents": map[string]any{"defaults": map[string]any{}}},
		{"agents": map[string]any{"defaults": map[string]any{"model": map[string]any{}}}},
	}
	for _, cfg := range cases {
		if got := extractPrimaryModel(cfg); got != "" {
			t.Errorf("expected empty string for %v, got %q", cfg, got)
		}
	}
}

// ---- flag file helpers ----

func TestLumiWriteFlag(t *testing.T) {
	dir := t.TempDir()

	// Flag should not exist yet.
	if isRecentLumiWrite(dir) {
		t.Fatal("expected no flag before touch")
	}

	// After touch, it should be detected as a recent write.
	touchLumiWriteFlag(dir)
	if !isRecentLumiWrite(dir) {
		t.Fatal("expected flag to be detected after touch")
	}

	// After clear, it should be gone.
	clearLumiWriteFlag(dir)
	if isRecentLumiWrite(dir) {
		t.Fatal("expected no flag after clear")
	}
}

func TestLumiWriteFlag_Expired(t *testing.T) {
	dir := t.TempDir()
	flagPath := filepath.Join(dir, lumiWriteFlagName)

	touchLumiWriteFlag(dir)

	// Back-date the mtime by more than the window.
	past := time.Now().Add(-(lumiWriteFlagWindow + time.Second))
	if err := os.Chtimes(flagPath, past, past); err != nil {
		t.Fatalf("chtimes: %v", err)
	}

	if isRecentLumiWrite(dir) {
		t.Fatal("expected flag to be expired after back-dating mtime")
	}
}
