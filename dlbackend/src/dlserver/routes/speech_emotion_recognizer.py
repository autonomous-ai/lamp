"""HTTP endpoints for the Speech Emotion Recognition (SER) service."""

from __future__ import annotations

import base64
import io
import urllib.request
from typing import Any
from urllib.parse import urlparse

import numpy as np
from fastapi import APIRouter, HTTPException, Request, UploadFile
from pydantic import BaseModel, model_validator
from starlette.datastructures import UploadFile as StarletteUploadFile

from config import settings
from core.ser.speech_emotion_recognizer import (
    BaseSpeechEmotionRecognizer,
    create_speech_emotion_recognizer,
)


def _is_multipart_file(value: object) -> bool:
    if isinstance(value, (str, bytes, bytearray)):
        return False
    if isinstance(value, (UploadFile, StarletteUploadFile)):
        return True
    read = getattr(value, "read", None)
    filename = getattr(value, "filename", None)
    return callable(read) and filename is not None


def _collect_upload_files(form: Any) -> list[Any]:
    uploads: list[Any] = []
    for _, value in form.multi_items():
        if _is_multipart_file(value):
            uploads.append(value)
    return uploads


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _validate_wav_url(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not _is_http_url(cleaned):
        raise ValueError(f"{field_name} must be an http/https URL (local paths are not allowed).")
    return cleaned


def _wav_bytes_to_waveform(raw: bytes) -> tuple[np.ndarray, int]:
    """Decode WAV bytes into ``(mono float32 waveform, sample_rate)``."""
    try:
        import soundfile as sf
    except ImportError as exc:
        raise ValueError("soundfile is required to parse wav content.") from exc

    waveform, sample_rate = sf.read(io.BytesIO(raw), dtype="float32")
    arr = np.asarray(waveform, dtype=np.float32)
    if arr.ndim == 2:
        arr = arr.mean(axis=1)
    elif arr.ndim != 1:
        raise ValueError("Uploaded wav must be mono/stereo waveform.")
    return arr, int(sample_rate)


def _wav_url_to_waveform(url: str) -> tuple[np.ndarray, int]:
    try:
        with urllib.request.urlopen(url) as response:
            raw = response.read()
    except Exception as exc:
        raise ValueError(f"Failed to download wav from URL '{url}': {exc}") from exc
    if not raw:
        raise ValueError(f"Downloaded wav is empty from URL '{url}'.")
    return _wav_bytes_to_waveform(raw)


class RecognizeEmotionRequest(BaseModel):
    """JSON body for ``POST /api/dl/ser/recognize``.

    Provide exactly one source; multipart upload is handled separately by
    the route based on ``Content-Type``.
    """

    wav_path: str | None = None
    audio_b64: str | None = None
    return_scores: bool = True

    @model_validator(mode="after")
    def _validate_sources(self) -> "RecognizeEmotionRequest":
        if not self.wav_path and not self.audio_b64:
            raise ValueError(
                "Provide one of: wav_path (http/https URL), audio_b64 (WAV bytes b64), "
                "or send the wav as multipart/form-data."
            )
        if self.wav_path:
            self.wav_path = _validate_wav_url(self.wav_path, "wav_path")
        return self


class EmotionScore(BaseModel):
    label: str
    score: float


class RecognizeEmotionResponse(BaseModel):
    label: str
    confidence: float
    scores: dict[str, float] | None = None


class LabelsResponse(BaseModel):
    engine: str
    labels: list[str]


router = APIRouter(tags=["ser"])
_recognizer: BaseSpeechEmotionRecognizer | None = None


def _get_recognizer() -> BaseSpeechEmotionRecognizer:
    """Lazy singleton driven by central ``settings``.

    Engine selection and model-file path come from the dlbackend config
    (``ser_recognition_model`` / ``ser_recognition_ckpt_path``). Runtime
    tunables come from the ``ser`` nested settings block. The core
    package's env-var fallbacks (``SER_ENGINE`` / ``SER_MODEL_PATH``)
    still apply as a last resort when settings leave a field at default.
    """
    global _recognizer
    if _recognizer is not None:
        return _recognizer
    try:
        _recognizer = create_speech_emotion_recognizer(
            engine=str(settings.ser_recognition_model),
            model_path=settings.ser_recognition_ckpt_path,
            labels_path=settings.ser_recognition_labels_path,
            sample_rate=settings.ser.sample_rate,
            intra_op_threads=settings.ser.intra_op_threads,
            providers=settings.ser.providers or None,
        )
        return _recognizer
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Speech emotion recognizer is unavailable: {exc}",
        ) from exc


@router.post("/ser/recognize", response_model=RecognizeEmotionResponse)
async def recognize_emotion(request: Request):
    """Classify the emotion of a single utterance.

    Accepted inputs (mutually exclusive):

    * ``Content-Type: multipart/form-data`` with one ``wav`` file part.
    * JSON body ``{"wav_path": "https://..."}`` -- http/https URL only.
    * JSON body ``{"audio_b64": "<base64 of full WAV bytes>"}``.

    Returns the top label with its softmax confidence and (optionally) the
    full score distribution.
    """
    recognizer = _get_recognizer()
    try:
        content_type = request.headers.get("content-type", "").lower()

        waveform: np.ndarray
        sample_rate: int
        return_scores = True

        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            return_scores = _form_bool(form.get("return_scores"), default=True)
            uploads = _collect_upload_files(form)
            if not uploads:
                raise HTTPException(
                    status_code=422,
                    detail="Provide a wav UploadFile in multipart/form-data.",
                )
            raw = await uploads[0].read()
            if not raw:
                raise ValueError("Uploaded wav file is empty.")
            waveform, sample_rate = _wav_bytes_to_waveform(raw)
        else:
            raw_json = await request.json()
            payload = RecognizeEmotionRequest.model_validate(raw_json)
            return_scores = payload.return_scores
            if payload.audio_b64:
                try:
                    raw_bytes = base64.b64decode(payload.audio_b64)
                except Exception as exc:
                    raise ValueError(f"invalid base64 payload: {exc}") from exc
                if not raw_bytes:
                    raise ValueError("audio_b64 is empty.")
                waveform, sample_rate = _wav_bytes_to_waveform(raw_bytes)
            else:
                assert payload.wav_path is not None  # validated upstream
                waveform, sample_rate = _wav_url_to_waveform(payload.wav_path)

        result = recognizer.predict(waveform, sample_rate=sample_rate)

        return RecognizeEmotionResponse(
            label=result["label"],
            confidence=result["confidence"],
            scores=result["scores"] if return_scores else None,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/ser/labels", response_model=LabelsResponse)
async def list_emotion_labels():
    """Return the active engine name + ordered label list."""
    recognizer = _get_recognizer()
    return LabelsResponse(engine=recognizer.ENGINE_NAME, labels=list(recognizer.labels))


def _form_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
