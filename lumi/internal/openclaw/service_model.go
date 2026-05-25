package openclaw

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
)

// UpdatePrimaryModel patches agents.defaults.model.primary in openclaw.json
// to "autonomous/{modelKey}" and restarts the gateway so the change takes
// effect immediately. It touches the write flag before the file write so the
// primary-model watcher recognises this as a Lumi-initiated write and does not
// sync it back (which would be a no-op loop anyway, but the flag eliminates
// the redundant read + Save round-trip).
//
// No-op when modelKey is empty or when openclaw.json does not yet exist
// (device not set up).
func (s *Service) UpdatePrimaryModel(modelKey string) error {
	if modelKey == "" {
		return nil
	}

	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	raw, err := os.ReadFile(configPath)
	if err != nil {
		if os.IsNotExist(err) {
			return nil // device not set up yet; skip silently
		}
		return fmt.Errorf("read openclaw config: %w", err)
	}

	var configData map[string]any
	if err := json.Unmarshal(raw, &configData); err != nil {
		return fmt.Errorf("parse openclaw config: %w", err)
	}

	newPrimary := customProviderName + "/" + modelKey
	if current := extractPrimaryModel(configData); current == newPrimary {
		return nil // already set, nothing to do
	}

	// Drill to agents.defaults.model and update primary.
	agents := ensureMap(configData, "agents")
	defaults := ensureMap(agents, "defaults")
	modelMap := ensureMap(defaults, "model")
	modelMap["primary"] = newPrimary
	defaults["model"] = modelMap
	agents["defaults"] = defaults
	configData["agents"] = agents

	written, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal openclaw config: %w", err)
	}

	// Touch flag BEFORE the write so the watcher skips this change.
	touchLumiWriteFlag(s.config.OpenclawConfigDir)

	if err := atomicWriteFile(configPath, written, 0600); err != nil {
		return fmt.Errorf("write openclaw config: %w", err)
	}
	if err := chownRuntimeUserIfRoot(configPath, openclawRuntimeUser); err != nil {
		slog.Warn("[model] chown openclaw config after primary update", "err", err)
	}

	slog.Info("[model] updated primary model in openclaw.json", "new", newPrimary)

	if err := restartOpenclawGateway(); err != nil {
		slog.Warn("[model] restart gateway after primary model update", "err", err)
	}
	return nil
}
