package agent

import (
	"log/slog"

	"go-lamp.autonomous.ai/domain"
	"go-lamp.autonomous.ai/internal/hermes"
	"go-lamp.autonomous.ai/internal/monitor"
	"go-lamp.autonomous.ai/internal/openclaw"
	"go-lamp.autonomous.ai/internal/statusled"
	"go-lamp.autonomous.ai/server/config"
)

// ProvideGateway returns the AgentGateway implementation based on config.AgentRuntime.
//
// "openclaw" (default): persistent WebSocket to the OpenClaw daemon at
// 127.0.0.1:18789. See internal/openclaw and docs/lamp-server.md.
//
// "hermes": HTTP+SSE client against the Hermes API server (default
// 127.0.0.1:8642). See internal/hermes and hermes.md at the repo root.
func ProvideGateway(cfg *config.Config, bus *monitor.Bus, sled *statusled.Service) domain.AgentGateway {
	switch cfg.AgentRuntime {
	case "hermes":
		logBackendBanner("HERMES", map[string]string{
			"base_url":     cfg.GetHermesBaseURL(),
			"conversation": cfg.GetHermesConversation(),
			"model":        cfg.GetHermesModel(),
			"api_key_set":  boolStr(cfg.HermesAPIKey != ""),
		})
		return hermes.ProvideService(cfg, bus, sled)
	default:
		effective := cfg.AgentRuntime
		if effective == "" {
			effective = "openclaw (default — agent_runtime unset)"
		} else if effective != "openclaw" {
			effective = "openclaw (FALLBACK — unknown agent_runtime=" + cfg.AgentRuntime + ")"
		}
		logBackendBanner("OPENCLAW", map[string]string{
			"config_dir":      cfg.OpenclawConfigDir,
			"effective_value": effective,
		})
		return openclaw.ProvideService(cfg, bus, sled)
	}
}

func logBackendBanner(name string, fields map[string]string) {
	args := []any{"component", "agent", "backend", name}
	for k, v := range fields {
		args = append(args, k, v)
	}
	slog.Info("══════════════════════════════════════════════════════", "component", "agent")
	slog.Info("  AGENT BACKEND ACTIVE → "+name, args...)
	slog.Info("══════════════════════════════════════════════════════", "component", "agent")
}

func boolStr(b bool) string {
	if b {
		return "yes"
	}
	return "no"
}
