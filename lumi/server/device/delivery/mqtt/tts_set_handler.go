package mqtthandler

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"go-lamp.autonomous.ai/domain"
	"go-lamp.autonomous.ai/internal/device"
)

const (
	ttsSetVoiceActiveTimeout = 120 * time.Second
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

		// Wait for voice to be active by listening for the first chat_response
		// state:"final" event on the monitor bus — this fires when the agent
		// completes a full turn, proving STT+TTS are both live.
		if err := h.waitForVoiceActive(ttsSetVoiceActiveTimeout); err != nil {
			slog.Error("tts.set: voice did not become active", "component", "mqtt", "error", err)
			h.publishTTSSetAck("failure", err.Error(), &req)
			return
		}

		slog.Info("tts.set: applied", "component", "mqtt", "provider", req.Provider, "voice", req.Voice, "language", req.Language)
		h.publishTTSSetAck("success", "", &req)
	}()

	return nil
}

// waitForVoiceActive subscribes to the monitor bus and returns as soon as a
// chat_response state:"final" event arrives — that event proves the full
// STT→LLM→TTS pipeline completed at least one turn with the new config.
func (h *DeviceMQTTHandler) waitForVoiceActive(timeout time.Duration) error {
	sub, unsub := h.monitorBus.Subscribe()
	defer unsub()
	deadline := time.NewTimer(timeout)
	defer deadline.Stop()
	for {
		select {
		case evt := <-sub:
			if evt.Type == "chat_response" && evt.State == "final" {
				return nil
			}
		case <-deadline.C:
			return fmt.Errorf("voice pipeline did not produce a response within %s", timeout)
		}
	}
}
