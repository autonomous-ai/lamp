"""
Standalone demo for GeminiLiveBrain — works on a Mac/Linux laptop without
needing the rest of lelamp (no VoiceService, no Lumi, no OpenClaw).

Usage:
    export GEMINI_API_KEY=...
    python -m lelamp.brain_demo

Optional env:
    LELAMP_GEMINI_LIVE_MODEL    default gemini-3.1-flash-live-preview
    LELAMP_GEMINI_LIVE_VOICE    default Aoede
    LELAMP_GEMINI_LIVE_LANGUAGE default vi-VN
    OPENCLAW_WORKSPACE          for SOUL.md (defaults to a built-in dev persona)
    LUMI_BASE_URL               if set + reachable, also pulls recent turns

Press Ctrl+C to quit. When the brain decides the input is a task it prints
the transcript instead of speaking — that's what would be forwarded to
OpenClaw in production.
"""

import logging
import os
import queue
import signal
import sys
import threading
from typing import Optional

from lelamp.service.brain.audio_sink import PCMAudioSink
from lelamp.service.brain.context_loader import BrainContext, Turn, load_context
from lelamp.service.brain.gemini_live import GeminiLiveBrain

MIC_RATE = 16000
MIC_CHANNELS = 1
MIC_BLOCKSIZE = 1024   # ~64 ms at 16 kHz

# Fallback persona used only when OPENCLAW_WORKSPACE has no SOUL.md.
# Keeps the demo runnable on a fresh Mac without Pi files.
DEV_FALLBACK_SOUL = """\
Bạn là Lumi — đèn thông minh dễ thương, vui vẻ, nói tiếng Việt giọng miền
Nam tự nhiên. Trả lời ngắn, ấm áp, có chút hài hước. Khi không biết câu trả
lời thì thừa nhận thẳng thắn.
"""


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _load_demo_context() -> BrainContext:
    ctx = load_context(include_history=bool(os.environ.get("LUMI_BASE_URL")))
    if not ctx.soul.strip():
        ctx.soul = DEV_FALLBACK_SOUL
    return ctx


def _run_mic_loop(send_audio, stop_event: threading.Event) -> None:
    try:
        import sounddevice as sd
    except ImportError:
        print("sounddevice missing — `pip install sounddevice` first", file=sys.stderr)
        stop_event.set()
        return

    def on_audio(indata, _frames, _time, _status):
        if stop_event.is_set():
            raise sd.CallbackStop()
        # sounddevice gives us int16 numpy array; brain wants raw bytes.
        send_audio(bytes(indata))

    print(">>> mic open — talk to Lumi (Ctrl+C to quit)")
    with sd.RawInputStream(
        samplerate=MIC_RATE,
        channels=MIC_CHANNELS,
        dtype="int16",
        blocksize=MIC_BLOCKSIZE,
        callback=on_audio,
    ):
        stop_event.wait()
    print(">>> mic closed")


def main() -> int:
    _setup_logging()

    brain = GeminiLiveBrain(context=_load_demo_context())
    if not brain.available:
        print(
            "GeminiLiveBrain not available. Set GEMINI_API_KEY and "
            "`pip install google-genai`.",
            file=sys.stderr,
        )
        return 1

    sink = PCMAudioSink()
    if not sink.start():
        print("Could not open output audio device — speaker missing?", file=sys.stderr)
        return 1

    stop_event = threading.Event()
    delegate_q: "queue.Queue[str]" = queue.Queue()

    def on_delegate(transcript: str) -> None:
        delegate_q.put(transcript)
        # Demo behaviour: print and end the session so user can ask another thing.
        print(f"\n>>> [TASK → would POST to Lumi] {transcript!r}\n")

    def on_audio_chunk(pcm: bytes) -> None:
        sink.push(pcm)

    def on_text(text: str, is_final: bool) -> None:
        if text:
            print(f"[lumi] {text}", end="" if not is_final else "\n", flush=True)

    def on_error(err: Exception) -> None:
        print(f"!!! brain error: {err}", file=sys.stderr)
        stop_event.set()

    session = brain.create_session()
    if not session.start(on_delegate, on_audio_chunk, on_text=on_text, on_error=on_error):
        print("Could not start Gemini Live session", file=sys.stderr)
        sink.stop()
        return 1

    def shutdown(*_args):
        stop_event.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        _run_mic_loop(session.send_audio, stop_event)
    finally:
        session.close()
        sink.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
