package mqtthandler

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"go-lamp.autonomous.ai/domain"
	"go-lamp.autonomous.ai/internal/device"
	"go-lamp.autonomous.ai/lib/lelamp"
)

const (
	ttsSetHealthTimeout  = 30 * time.Second
	ttsSetHealthWarmup   = 3 * time.Second
	ttsSetHealthInterval = 2 * time.Second
	ttsSetMaxRetries     = 4
)

func (h *DeviceMQTTHandler) publishTTSSetAck(status, errMsg string, data *domain.MQTTTTSSetData) {
	ack := domain.MQTTTTSSetAck{
		MQTTInfoResponse: domain.NewMQTTInfoResponse(h.config, "data", device.GetDeviceMac()),
		Kind:             domain.KindTTSSet,
		Status:           status,
		Error:            errMsg,
		Data:             data,
	}
	if err := h.publish(ack); err != nil {
		slog.Warn("tts.set: publish ack failed", "component", "mqtt", "status", status, "error", err)
	}
}

func (h *DeviceMQTTHandler) handleTTSSet(cmd domain.MQTTMessage) error {
	var envelope domain.MQTTTTSSetCommand
	if err := json.Unmarshal(cmd.Raw(), &envelope); err != nil {
		slog.Error("tts.set: invalid payload", "component", "mqtt", "error", err)
		h.publishTTSSetAck("failure", "invalid JSON payload", nil)
		return err
	}
	req := envelope.Data

	slog.Info("tts.set: received", "component", "mqtt", "provider", req.Provider, "voice", req.Voice, "language", req.Language)

	// Ack immediately so BFF knows the device received the command.
	h.publishTTSSetAck("starting", "", nil)

	go func() {
		if err := h.deviceService.UpdateVoiceConfig(req.Provider, req.Voice, req.Language); err != nil {
			slog.Error("tts.set: UpdateVoiceConfig failed", "component", "mqtt", "error", err)
			h.publishTTSSetAck("failure", err.Error(), &req)
			return
		}

		// Wait for lumi-lelamp to come back up with the new TTS config applied.
		// Retry up to ttsSetMaxRetries times before giving up — lelamp may need
		// extra time for certain languages/providers (e.g. zh-CN cold start).
		var lastErr error
		for attempt := 1; attempt <= ttsSetMaxRetries; attempt++ {
			warmup := ttsSetHealthWarmup
			if attempt > 1 {
				warmup = 0 // already past startup on retries
			}
			if lastErr = waitForVoice(ttsSetHealthTimeout, warmup, ttsSetHealthInterval); lastErr == nil {
				break
			}
			slog.Warn("tts.set: health check timeout, retrying", "component", "mqtt", "attempt", attempt, "max", ttsSetMaxRetries)
		}
		if lastErr != nil {
			slog.Error("tts.set: lumi-lelamp did not recover after retries", "component", "mqtt", "error", lastErr)
			h.publishTTSSetAck("failure", lastErr.Error(), &req)
			return
		}

		slog.Info("tts.set: applied", "component", "mqtt", "provider", req.Provider, "voice", req.Voice, "language", req.Language)
		h.publishTTSSetAck("success", "", &req)
	}()

	return nil
}

// waitForVoice polls lelamp /health until voice=true or timeout.
// warmup gives systemd time to stop the old process before polling starts.
func waitForVoice(timeout, warmup, interval time.Duration) error {
	time.Sleep(warmup)
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		h, err := lelamp.GetHealth()
		if err == nil && h.Voice {
			return nil
		}
		time.Sleep(interval)
	}
	return fmt.Errorf("lumi-lelamp voice pipeline did not recover within %s", timeout)
}
