package http

import (
	"context"
	"encoding/json"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"go-lamp.autonomous.ai/internal/buddy"
	"go-lamp.autonomous.ai/server/serializers"
)

type commandRequest struct {
	ID        string         `json:"id"`
	Action    string         `json:"action" binding:"required"`
	Params    map[string]any `json:"params"`
	TimeoutMs int            `json:"timeout_ms"`
}

// Command dispatches one command to the connected buddy and returns the buddy's
// response. Localhost-only at the route layer (OpenClaw skill on the lamp is
// the intended caller).
func (h *BuddyHandler) Command(c *gin.Context) {
	var req commandRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if req.Params == nil {
		req.Params = map[string]any{}
	}
	cmd := buddy.Command{
		ID:        req.ID,
		Action:    req.Action,
		Params:    req.Params,
		TimeoutMs: req.TimeoutMs,
		IssuedAt:  time.Now().UTC().Format(time.RFC3339),
		IssuedBy:  "api:/api/buddy/command",
	}
	if cmd.ID == "" {
		cmd.ID = buddy.NewCommandID()
	}

	timeout := 30 * time.Second
	if req.TimeoutMs > 0 {
		timeout = time.Duration(req.TimeoutMs)*time.Millisecond + 5*time.Second
	}
	ctx, cancel := context.WithTimeout(c.Request.Context(), timeout)
	defer cancel()

	raw, err := h.service.Dispatch(ctx, cmd)
	if err != nil {
		c.JSON(http.StatusBadGateway, serializers.ResponseError(err.Error()))
		return
	}
	// Buddy's response is already shaped {id, ok, result, error, duration_ms}.
	// Pass it through inside the lumi envelope so callers get a consistent
	// {status: 1, data: <buddy-response>, message: null}.
	var inner map[string]any
	if err := json.Unmarshal(raw, &inner); err != nil {
		c.JSON(http.StatusOK, serializers.ResponseSuccess(json.RawMessage(raw)))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(inner))
}
