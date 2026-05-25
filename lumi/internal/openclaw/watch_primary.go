package openclaw

import (
	"context"
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/fsnotify/fsnotify"
)

const lumiWriteFlagName = ".lumi-model-write-flag"
const primarySyncDebounce = 300 * time.Millisecond
const lumiWriteFlagWindow = 3 * time.Second
const primaryWatchRetryInterval = 5 * time.Second

// touchLumiWriteFlag creates or updates the flag file that signals the next
// openclaw.json write was initiated by Lumi. Call this BEFORE writing
// openclaw.json so the primary-model watcher can distinguish Lumi writes
// from external (dashboard / CLI) writes and avoid a sync loop.
func touchLumiWriteFlag(configDir string) {
	flagPath := filepath.Join(configDir, lumiWriteFlagName)
	f, err := os.Create(flagPath)
	if err != nil {
		slog.Warn("[primarysync] touch flag failed", "path", flagPath, "err", err)
		return
	}
	_ = f.Close()
}

// isRecentLumiWrite returns true when the flag file exists and its mtime is
// within lumiWriteFlagWindow. Used by the watcher to skip writes made by Lumi.
func isRecentLumiWrite(configDir string) bool {
	info, err := os.Stat(filepath.Join(configDir, lumiWriteFlagName))
	if err != nil {
		return false
	}
	return time.Since(info.ModTime()) < lumiWriteFlagWindow
}

// clearLumiWriteFlag removes the flag file after consuming it.
func clearLumiWriteFlag(configDir string) {
	_ = os.Remove(filepath.Join(configDir, lumiWriteFlagName))
}

// StartPrimaryModelWatch watches the openclaw config directory for changes to
// openclaw.json. When a change originates externally (no Lumi write flag), it
// reads agents.defaults.model.primary and syncs it back to config.LLMModel
// — but only when the provider is "autonomous". Non-autonomous providers are
// silently ignored (they have their own credentials and are out of Lumi scope).
//
// Uses directory-level watching instead of file-level because atomicWriteFile
// (and OpenClaw itself) writes via a tmp+rename sequence: fsnotify loses the
// inode after the rename and emits no further events on the original path.
//
// If the config directory does not exist yet (device not set up), the function
// retries every primaryWatchRetryInterval until the directory appears or the
// context is cancelled.
func (s *Service) StartPrimaryModelWatch(ctx context.Context) {
	dir := s.config.OpenclawConfigDir

	// Wait for the config dir to exist before starting the watcher.
	for {
		if _, err := os.Stat(dir); err == nil {
			break
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(primaryWatchRetryInterval):
		}
	}

	watcher, err := fsnotify.NewWatcher()
	if err != nil {
		slog.Error("[primarysync] create watcher failed", "err", err)
		return
	}
	defer watcher.Close()

	if err := watcher.Add(dir); err != nil {
		slog.Error("[primarysync] watch dir failed", "dir", dir, "err", err)
		return
	}
	slog.Info("[primarysync] watching openclaw config dir for primary model changes", "dir", dir)

	var debounceTimer *time.Timer
	resetDebounce := func() {
		if debounceTimer != nil {
			debounceTimer.Stop()
		}
		debounceTimer = time.AfterFunc(primarySyncDebounce, func() {
			s.syncPrimaryFromFile()
		})
	}

	for {
		select {
		case <-ctx.Done():
			if debounceTimer != nil {
				debounceTimer.Stop()
			}
			return
		case event, ok := <-watcher.Events:
			if !ok {
				return
			}
			if filepath.Base(event.Name) != "openclaw.json" {
				continue
			}
			if event.Has(fsnotify.Write) || event.Has(fsnotify.Create) || event.Has(fsnotify.Rename) {
				resetDebounce()
			}
		case err, ok := <-watcher.Errors:
			if !ok {
				return
			}
			slog.Warn("[primarysync] watcher error", "err", err)
		}
	}
}

// syncPrimaryFromFile is the debounced handler that fires after openclaw.json
// changes. It skips Lumi-initiated writes (flag file present) and syncs the
// new autonomous primary model back into config.LLMModel.
func (s *Service) syncPrimaryFromFile() {
	configDir := s.config.OpenclawConfigDir

	// Lumi-initiated write — not an external change, skip to avoid a loop.
	if isRecentLumiWrite(configDir) {
		clearLumiWriteFlag(configDir)
		slog.Debug("[primarysync] skipping Lumi-initiated write")
		return
	}

	configPath := filepath.Join(configDir, "openclaw.json")
	raw, err := os.ReadFile(configPath)
	if err != nil {
		slog.Warn("[primarysync] read openclaw.json failed", "err", err)
		return
	}
	var cfg map[string]any
	if err := json.Unmarshal(raw, &cfg); err != nil {
		slog.Warn("[primarysync] parse openclaw.json failed", "err", err)
		return
	}

	primary := extractPrimaryModel(cfg)
	if primary == "" {
		return
	}

	provider, modelKey, ok := splitProviderModel(primary)
	if !ok || provider != customProviderName {
		// External change switched to a non-autonomous provider.
		// Lumi does not manage credentials for other providers — skip.
		slog.Info("[primarysync] external primary uses non-autonomous provider, ignoring",
			"primary", primary)
		return
	}

	if s.config.LLMModel == modelKey {
		return // already in sync, nothing to do
	}

	slog.Info("[primarysync] external model change detected, syncing to Lumi config",
		"old", s.config.LLMModel, "new", modelKey)
	s.config.LLMModel = modelKey
	if err := s.config.Save(); err != nil {
		slog.Error("[primarysync] save Lumi config failed", "err", err)
	}
}

// extractPrimaryModel drills into agents.defaults.model.primary in a parsed
// openclaw.json map and returns the value, or "" when any level is absent.
func extractPrimaryModel(cfg map[string]any) string {
	agents, _ := cfg["agents"].(map[string]any)
	if agents == nil {
		return ""
	}
	defaults, _ := agents["defaults"].(map[string]any)
	if defaults == nil {
		return ""
	}
	model, _ := defaults["model"].(map[string]any)
	if model == nil {
		return ""
	}
	primary, _ := model["primary"].(string)
	return primary
}

// splitProviderModel splits a "provider/model-key" string into its two parts.
// Returns ok=false when the string contains no "/" separator.
func splitProviderModel(fullKey string) (provider, key string, ok bool) {
	idx := strings.IndexByte(fullKey, '/')
	if idx < 0 {
		return "", fullKey, false
	}
	return fullKey[:idx], fullKey[idx+1:], true
}
