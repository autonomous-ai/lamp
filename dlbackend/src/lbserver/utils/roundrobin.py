"""Thread-safe round-robin backend selector."""

import itertools
import threading

from fastapi import HTTPException


class RoundRobin:
    """Thread-safe round-robin over a list of backend URLs."""

    def __init__(self, backends: list[str]) -> None:
        self._cycle = itertools.cycle(backends) if backends else itertools.cycle([""])
        self._lock = threading.Lock()

    def next(self) -> str:
        with self._lock:
            backend: str = next(self._cycle)
        if not backend:
            raise HTTPException(status_code=503, detail="No backends configured")
        return backend
