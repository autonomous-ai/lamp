"""Audio processing utilities for speaker recognition endpoints."""

from __future__ import annotations

import base64
import io
import urllib.request
from typing import Any
from urllib.parse import urlparse

import numpy as np
from fastapi import UploadFile
from starlette.datastructures import UploadFile as StarletteUploadFile


def is_multipart_file(value: object) -> bool:
    """Detect uploaded file parts (Starlette may not match fastapi.UploadFile by isinstance)."""
    if isinstance(value, (str, bytes, bytearray)):
        return False
    if isinstance(value, (UploadFile, StarletteUploadFile)):
        return True
    read = getattr(value, "read", None)
    filename = getattr(value, "filename", None)
    return callable(read) and filename is not None


def collect_upload_files(form: Any) -> list[Any]:
    """Collect all file parts from multipart form."""
    uploads: list[Any] = []
    for _, value in form.multi_items():
        if is_multipart_file(value):
            uploads.append(value)
    return uploads


def is_http_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def validate_wav_url(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not is_http_url(cleaned):
        raise ValueError(f"{field_name} must be an http/https URL (local paths are not allowed).")
    return cleaned


def split_waveform_to_chunks(
    waveform: np.ndarray, sample_rate: int, chunk_seconds: float = 0.5
) -> list[list[float]]:
    chunk_size = max(1, int(sample_rate * chunk_seconds))
    chunks: list[list[float]] = []
    for start in range(0, len(waveform), chunk_size):
        part = waveform[start : start + chunk_size]
        if part.size > 0:
            chunks.append(part.astype(np.float32).tolist())
    return chunks


def decode_pcm16_b64_to_chunks(
    pcm16_b64: str, sample_rate: int, chunk_seconds: float = 0.5
) -> list[list[float]]:
    try:
        raw = base64.b64decode(pcm16_b64)
    except Exception as exc:
        raise ValueError(f"Invalid pcm16_b64 payload: {exc}") from exc
    if len(raw) % 2 != 0:
        raise ValueError("pcm16_b64 byte length must be divisible by 2 (int16).")
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return split_waveform_to_chunks(pcm, sample_rate=sample_rate, chunk_seconds=chunk_seconds)


def decode_b64_wav(b64: str) -> "Audio":
    """Decode a base64-encoded WAV into an Audio dataclass."""
    from core.models.media import Audio

    raw = base64.b64decode(b64)
    if not raw:
        raise ValueError("empty audio payload")
    waveform, sample_rate = _read_wav_bytes(raw)
    return Audio(waveform=waveform, sample_rate=int(sample_rate))


def _read_wav_bytes(raw: bytes) -> tuple[np.ndarray, int]:
    """Read raw WAV bytes into (mono float32 waveform, sample_rate)."""
    import soundfile as sf

    waveform, sample_rate = sf.read(io.BytesIO(raw), dtype="float32")
    arr = np.asarray(waveform, dtype=np.float32)
    if arr.ndim == 2:
        arr = arr.mean(axis=1)
    elif arr.ndim != 1:
        raise ValueError("Uploaded wav must be mono/stereo waveform.")
    return arr, int(sample_rate)


def wav_bytes_to_chunks(raw: bytes, chunk_seconds: float = 0.5) -> tuple[list[list[float]], int]:
    arr, sample_rate = _read_wav_bytes(raw)
    return split_waveform_to_chunks(
        arr, sample_rate=sample_rate, chunk_seconds=chunk_seconds
    ), sample_rate


async def wav_upload_to_chunks(
    wav_file: UploadFile, chunk_seconds: float = 0.5
) -> tuple[list[list[float]], int]:
    raw = await wav_file.read()
    if not raw:
        raise ValueError("Uploaded wav file is empty.")
    return wav_bytes_to_chunks(raw, chunk_seconds=chunk_seconds)


def wav_url_to_chunks(url: str, chunk_seconds: float = 0.5) -> tuple[list[list[float]], int]:
    try:
        with urllib.request.urlopen(url) as response:
            raw = response.read()
    except Exception as exc:
        raise ValueError(f"Failed to download wav from URL '{url}': {exc}") from exc
    if not raw:
        raise ValueError(f"Downloaded wav is empty from URL '{url}'.")
    return wav_bytes_to_chunks(raw, chunk_seconds=chunk_seconds)
