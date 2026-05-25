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
		slog.Info("agent runtime: Hermes", "component", "agent", "base_url", cfg.GetHermesBaseURL())
		return hermes.ProvideService(cfg, bus, sled)
	default:
		return openclaw.ProvideService(cfg, bus, sled)
	}
}
