"""
Abstract Brain interface — voice-in router placed in front of OpenClaw.

A Brain receives the same 16 kHz PCM stream that an STT provider would, but
instead of producing a transcript it decides:

  - "chit-chat" → emit PCM audio chunks that play directly out the speaker,
                  nothing is sent to OpenClaw.
  - "task"      → emit the user's transcript via on_delegate, the caller is
                  expected to forward it to OpenClaw the same way an STT
                  transcript would be forwarded.

The Brain is pluggable so we can swap providers (Gemini Live first, others
later) without touching VoiceService.
"""

import logging
from abc import ABC, abstractmethod
from typing import Callable, Optional

logger = logging.getLogger("lelamp.brain")


class BrainSession(ABC):
    """A single streaming brain session (connect → send audio → receive
    audio/tool calls → close)."""

    @abstractmethod
    def start(
        self,
        on_delegate: Callable[[str], None],
        on_audio_chunk: Callable[[bytes], None],
        on_text: Optional[Callable[[str, bool], None]] = None,
        on_user_input: Optional[Callable[[str, bool], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
        on_usage: Optional[Callable[[int, int, int], None]] = None,
    ) -> bool:
        """Open the upstream connection and start streaming.

        Args:
            on_delegate:    Called once with the user's transcript when the
                            brain decides the input is a task that should go
                            through OpenClaw. After this fires the session
                            closes itself.
            on_audio_chunk: Called with raw PCM bytes (24 kHz int16 LE for
                            Gemini Live) for the chit-chat reply. May be
                            called many times per session.
            on_text:        Optional. Called with (text, is_final) for the
                            brain's *reply* — the transcript of what Lumi
                            just said. Useful for monitor logging.
            on_user_input:  Optional. Called with (text, is_final) for the
                            brain's transcription of the *user's* speech.
                            Used by callers that want to log user turns
                            and run per-turn speaker recognition.
            on_error:       Optional. Called once when the session encounters
                            a fatal error.
            on_usage:       Optional. Called with (prompt_tokens,
                            response_tokens, total_tokens) per turn when
                            the provider surfaces usage metadata. Used by
                            BrainBenchmark to track cost; pure additive,
                            providers that don't report usage simply never
                            invoke this.

        Returns:
            True if the session started, False otherwise.
        """

    @abstractmethod
    def send_audio(self, pcm16k_bytes: bytes) -> None:
        """Push a chunk of mic audio (PCM int16 LE 16 kHz mono) to the brain.
        Safe to call from the audio thread; implementations marshal across
        threads if their upstream client requires it."""

    def notify_activity_start(self) -> None:
        """Hint to the provider that the user just started speaking.

        Providers that support *manual activity detection* (Gemini Live
        with ``automatic_activity_detection.disabled=True``) need an
        explicit start signal — they don't run server VAD in that mode.
        Providers with their own server VAD (OpenAI Realtime) can no-op
        this (default) and let the audio stream drive turn detection.
        Safe to call multiple times — implementations should de-dup."""

    def notify_activity_end(self) -> None:
        """Hint to the provider that the user just stopped speaking.

        Symmetric to :meth:`notify_activity_start`. Default no-op so
        providers without manual mode aren't forced to implement it."""

    @abstractmethod
    def close(self) -> None:
        """Close the upstream connection and release resources. Idempotent."""

    @abstractmethod
    def is_closed(self) -> bool:
        """Whether the session is no longer accepting audio."""


class Brain(ABC):
    """Factory that creates BrainSessions. One Brain instance per VoiceService."""

    @abstractmethod
    def create_session(self) -> BrainSession:
        """Create a new streaming session."""

    @property
    @abstractmethod
    def available(self) -> bool:
        """Whether this brain is properly configured and ready to use."""

    @property
    def name(self) -> str:
        return self.__class__.__name__
