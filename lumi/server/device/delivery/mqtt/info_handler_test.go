package mqtthandler_test

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	lumimqtt "go-lamp.autonomous.ai/lib/mqtt"
)

const (
	testDeviceID = "69dcb449080f8511d27c1d7f"
	testFATopic  = "Lumi/f_a/" + testDeviceID
	testFDTopic  = "Lumi/f_d/" + testDeviceID
)

func TestInfoMQTTPayload(t *testing.T) {
	received := make(chan []byte, 1)

	client := lumimqtt.ProvideClient(lumimqtt.Options{
		Endpoint: "sds-mqtt.autonomous.ai",
		Port:     1883,
		Username: "mosquitto",
		Password: "828f7bd4",
		ClientID: "test-info-listener",
	})

	client.Subscribe(testFDTopic, 0, func(_ string, payload []byte) {
		select {
		case received <- payload:
		default:
		}
	})

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	if err := client.Connect(ctx); err != nil {
		t.Fatalf("connect failed: %v", err)
	}
	defer client.Close()

	time.Sleep(500 * time.Millisecond)

	if err := client.Publish(ctx, testFATopic, 0, []byte(`{"cmd":"info"}`)); err != nil {
		t.Fatalf("publish failed: %v", err)
	}

	select {
	case payload := <-received:
		t.Logf("raw payload: %s", payload)
		var result map[string]any
		if err := json.Unmarshal(payload, &result); err != nil {
			t.Fatalf("unmarshal: %v", err)
		}
		for _, key := range []string{"version", "lelamp_version", "openclaw_version", "local_ip", "device", "id"} {
			val, ok := result[key]
			if !ok || val == "" {
				t.Errorf("missing or empty: %s", key)
			} else {
				t.Logf("  %-20s = %v", key, val)
			}
		}
	case <-time.After(10 * time.Second):
		t.Fatal("timeout waiting for Pi info response")
	}
}
