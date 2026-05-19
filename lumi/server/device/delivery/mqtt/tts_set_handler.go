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
	ttsSetStopWarmup = 2 * time.Second
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

		// Stop the running voice pipeline so lumi-lelamp releases ALSA,
		// then restart it — StartVoice reads stt_language/stt_model from
		// the config.json we just saved, so STT + TTS both pick up the new config.
		// Both calls are synchronous: nil return = pipeline is live.
		_ = lelamp.StopVoicePipeline()
		time.Sleep(ttsSetStopWarmup)
		if err := lelamp.StartVoice(lelamp.VoiceStartConfig{
			DeepgramKey:     h.config.DeepgramAPIKey,
			LLMKey:          h.config.LLMAPIKey,
			LLMBaseURL:      h.config.LLMBaseURL,
			STTKey:          h.config.GetSTTAPIKey(),
			STTBaseURL:      h.config.GetSTTBaseURL(),
			TTSKey:          h.config.GetTTSAPIKey(),
			TTSBaseURL:      h.config.GetTTSBaseURL(),
			TTSVoice:        h.config.TTSVoice,
			TTSInstructions: h.config.TTSInstructions,
			TTSProvider:     h.config.TTSProvider,
		}); err != nil {
			slog.Error("tts.set: StartVoice failed", "component", "mqtt", "error", err)
			h.publishTTSSetAck("failure", fmt.Sprintf("voice restart failed: %s", err), &req)
			return
		}

		slog.Info("tts.set: applied", "component", "mqtt", "provider", req.Provider, "voice", req.Voice, "language", req.Language)
		h.publishTTSSetAck("success", "", &req)
	}()

	return nil
}

