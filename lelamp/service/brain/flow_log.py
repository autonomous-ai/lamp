"""Brain → Flow Monitor bridge.

POSTs brain decision + latency events to the Go lamp server's
``/api/sensing/brain/event`` endpoint so they land in the same
``local/flow_events_*.jsonl`` the Monitor Flow UI reads.

Design constraints:
  - Fire-and-forget on a daemon thread per call. The brain's hot path
    (call-mode ``handle_stt_final``, live-mode ``_on_delegate``) must
    never block on HTTP.
  - Silent on errors. Observability must not break routing — a
    Lamp-side outage just means the event is lost, not that the brain
    stalls.
  - Disable via ``LELAMP_BRAIN_FLOW_LOG=0`` (default on).

Each brain turn mints its own ``brain-<id>`` run-id via
:func:`mint_run_id`. Go-side ``NextChatRunID`` runs only when the brain
delegates and ``_send_to_lamp`` POSTs to ``/api/sensing/event``, so the
brain-side and Lamp-side runIDs don't share. The Monitor UI groups by
runID, so brain decisions appear as their own short "turns" alongside
the eventual STT/chat events; cross-correlate by timestamp + the
``user_text`` field embedded in each brain event.
"""

import json
import logging
import os
import queue
import threading
import time
import uuid
from typing import Optional
from urllib import request as _urlrequest
from urllib.error import URLError

logger = logging.getLogger("lelamp.brain.flow")

_DEFAULT_URL = "http://127.0.0.1:5000/api/sensing/brain/event"
_DEFAULT_ALLOC_URL = "http://127.0.0.1:5000/api/sensing/alloc-runid"
_DEFAULT_TIMEOUT = 0.5
_ALLOC_TIMEOUT = 0.4  # sync call on the brain hot path; fail-fast back to local mint
_QUEUE_MAX = 1024  # drop new events if backlog exceeds — observability must not OOM


def _env_disabled() -> bool:
    return os.environ.get("LELAMP_BRAIN_FLOW_LOG", "1") == "0"


def mint_run_id(prefix: str = "brain") -> str:
    """One unique run-id per brain turn. Used so Monitor UI groups all
    events belonging to one decision together."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def alloc_chat_run_id(timeout: float = _ALLOC_TIMEOUT) -> tuple[str, str]:
    """Pre-allocate a (req_id, run_id) pair from the Go lamp server so a
    brain delegate turn shares ONE Monitor row with the eventual
    OpenClaw turn. Returns ``("", lamp-chat-N-<ts>)`` from Go on
    success; falls back to ``("", "brain-<uuid>")`` from
    :func:`mint_run_id` when the alloc endpoint is unreachable (older
    Go build, alloc 404, network blip). Brain code MUST work with the
    fallback id — the only consequence is the delegate turn splits
    into two adjacent rows (same as the pre-alloc behavior).

    Synchronous on purpose: the brain hot path needs the id BEFORE
    emitting voice_pipeline_start. Timeout is tight (~400ms) so a
    Lamp-side hang degrades gracefully instead of stalling the turn.
    """
    if _env_disabled():
        return "", mint_run_id()
    try:
        body = json.dumps({}).encode("utf-8")
        req = _urlrequest.Request(
            _DEFAULT_ALLOC_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _urlrequest.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        data = payload.get("data") or {}
        run_id = data.get("runId") or ""
        req_id = data.get("reqId") or ""
        if run_id.startswith("lamp-chat-"):
            return req_id, run_id
        logger.debug("brain alloc-runid returned unexpected payload: %r", payload)
    except (URLError, OSError) as e:
        logger.debug("brain alloc-runid failed (%s) — falling back to local mint", e)
    except Exception as e:
        logger.debug("brain alloc-runid unexpected error (%s) — falling back to local mint", e)
    return "", mint_run_id()


class BrainFlowLog:
    """Single-worker POST client to ``/api/sensing/brain/event``.

    Why a worker thread instead of fire-and-forget per call: the brain
    emits ``voice_pipeline_start`` → ``brain_input`` → ``brain_decision``
    in a strict order, but per-call daemon threads race on POST and the
    Monitor UI relies on event order to open a turn before attaching
    decoration events. A single worker preserves emission order while
    still keeping the brain hot path non-blocking.
    """

    def __init__(self, url: str = _DEFAULT_URL, timeout: float = _DEFAULT_TIMEOUT):
        self._url = url
        self._timeout = timeout
        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        self._worker = threading.Thread(
            target=self._loop, name="brain-flow-log", daemon=True,
        )
        self._worker.start()

    def log(
        self,
        node: str,
        data: Optional[dict] = None,
        run_id: str = "",
    ) -> None:
        if _env_disabled():
            return
        payload = {"node": node, "data": data or {}}
        if run_id:
            payload["runId"] = run_id
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            logger.debug("brain flow log queue full — dropping %s", node)

    def _loop(self) -> None:
        while True:
            payload = self._queue.get()
            self._post(payload)
            self._queue.task_done()

    def _post(self, payload: dict) -> None:
        try:
            body = json.dumps(payload, default=str).encode("utf-8")
            req = _urlrequest.Request(
                self._url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _urlrequest.urlopen(req, timeout=self._timeout) as resp:
                resp.read()
        except (URLError, OSError) as e:
            logger.debug("brain flow log POST failed (%s): %s", payload.get("node"), e)
        except Exception as e:
            logger.debug("brain flow log POST unexpected error (%s): %s", payload.get("node"), e)


_singleton: Optional[BrainFlowLog] = None
_singleton_lock = threading.Lock()


def brain_flow() -> BrainFlowLog:
    """Process-wide singleton — first caller initialises."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = BrainFlowLog()
    return _singleton


def now_ms() -> int:
    return int(time.time() * 1000)
