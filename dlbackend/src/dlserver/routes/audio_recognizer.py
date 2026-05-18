"""HTTP endpoints for speaker audio recognition service."""

from __future__ import annotations

import base64
import io
import urllib.request
from typing import Any
from urllib.parse import urlparse

import numpy as np
from fastapi import APIRouter, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field, model_validator
from starlette.datastructures import UploadFile as StarletteUploadFile

from core.perception.audio_recognition.audio_recognizer import (
    BaseAudioRecognizer,
    create_audio_recognizer,
)


def _is_multipart_file(value: object) -> bool:
    """Detect uploaded file parts (Starlette may not match fastapi.UploadFile by isinstance)."""
    if isinstance(value, (str, bytes, bytearray)):
        return False
    if isinstance(value, (UploadFile, StarletteUploadFile)):
        return True
    # Duck-type Starlette UploadFile (read + filename)
    read = getattr(value, "read", None)
    filename = getattr(value, "filename", None)
    return callable(read) and filename is not None


def _collect_upload_files(form: Any) -> list[Any]:
    """Collect all file parts from multipart form (values() can miss files in some clients)."""
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


class RegisterSpeakerRequest(BaseModel):
    """Request to enroll or overwrite a speaker profile."""

    name: str = Field(min_length=1)
    wav_path: str | None = None
    wav_paths: list[str] | None = None
    chunks: list[list[float]] | None = None
    pcm16_b64: str | None = None
    chunk_sample_rate: int = 16000

    @model_validator(mode="after")
    def _validate_sources(self) -> "RegisterSpeakerRequest":
        if not self.wav_path and not self.wav_paths and not self.chunks and not self.pcm16_b64:
            raise ValueError(
                "Provide at least one source: wav_path, wav_paths, chunks, or pcm16_b64."
            )
        if self.wav_path:
            self.wav_path = _validate_wav_url(self.wav_path, "wav_path")
        if self.wav_paths:
            self.wav_paths = [_validate_wav_url(item, "wav_paths[]") for item in self.wav_paths]
        return self


class RecognizeSpeakerRequest(BaseModel):
    """Request to recognize one speaker from wav path or chunk list."""

    wav_path: str | None = None
    chunks: list[list[float]] | None = None
    pcm16_b64: str | None = None
    chunk_sample_rate: int = 16000

    @model_validator(mode="after")
    def _validate_sources(self) -> "RecognizeSpeakerRequest":
        if not self.wav_path and not self.chunks and not self.pcm16_b64:
            raise ValueError("Provide either wav_path, chunks, or pcm16_b64.")
        if self.wav_path:
            self.wav_path = _validate_wav_url(self.wav_path, "wav_path")
        return self


class RemoveSpeakerResponse(BaseModel):
    name: str
    removed: bool


class SpeakerSummary(BaseModel):
    name: str
    embedding_dim: int


class SpeakerListResponse(BaseModel):
    total: int
    speakers: list[SpeakerSummary]


class RecognizeSpeakerResponse(BaseModel):
    name: str
    confidence: float


class EmbedAudioRequest(BaseModel):
    """Request for stateless embedding computation.

    Contract used by LeLamp's SpeakerRecognizer: one or more WAV audios come
    in as base64 strings, one aggregated L2-normalized embedding comes out.
    No DB write.

    Set ``return_chunks=True`` when the caller needs per-chunk embeddings
    (e.g. to perform per-chunk voting at recognize time, mirroring this
    service's own ``/recognize`` matching logic).
    """

    audios_b64: list[str] = Field(min_length=1)
    chunk_seconds: float = 0.5
    return_chunks: bool = False


class EmbedAudioResponse(BaseModel):
    embedding: list[float]
    embedding_dim: int
    chunk_embeddings: list[list[float]] | None = None


router = APIRouter(tags=["audio-recognizer"])
_audio_recognizer: BaseAudioRecognizer | None = None


def _get_audio_recognizer() -> BaseAudioRecognizer:
    global _audio_recognizer
    if _audio_recognizer is not None:
        return _audio_recognizer
    try:
        _audio_recognizer = create_audio_recognizer()
        return _audio_recognizer
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"Audio recognizer is unavailable: {exc}"
        ) from exc


def _split_waveform_to_chunks(
    waveform: np.ndarray, sample_rate: int, chunk_seconds: float = 0.5
) -> list[list[float]]:
    chunk_size = max(1, int(sample_rate * chunk_seconds))
    chunks: list[list[float]] = []
    for start in range(0, len(waveform), chunk_size):
        part = waveform[start : start + chunk_size]
        if part.size > 0:
            chunks.append(part.astype(np.float32).tolist())
    return chunks


def _decode_pcm16_b64_to_chunks(
    pcm16_b64: str, sample_rate: int, chunk_seconds: float = 0.5
) -> list[list[float]]:
    try:
        raw = base64.b64decode(pcm16_b64)
    except Exception as exc:
        raise ValueError(f"Invalid pcm16_b64 payload: {exc}") from exc
    if len(raw) % 2 != 0:
        raise ValueError("pcm16_b64 byte length must be divisible by 2 (int16).")
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return _split_waveform_to_chunks(pcm, sample_rate=sample_rate, chunk_seconds=chunk_seconds)


async def _wav_upload_to_chunks(
    wav_file: UploadFile, chunk_seconds: float = 0.5
) -> tuple[list[list[float]], int]:
    try:
        import soundfile as sf
    except ImportError as exc:
        raise ValueError("soundfile is required to parse uploaded wav files.") from exc

    raw = await wav_file.read()
    if not raw:
        raise ValueError("Uploaded wav file is empty.")

    return _wav_bytes_to_chunks(raw, chunk_seconds=chunk_seconds)


def _wav_bytes_to_chunks(raw: bytes, chunk_seconds: float = 0.5) -> tuple[list[list[float]], int]:
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
    return _split_waveform_to_chunks(
        arr, sample_rate=sample_rate, chunk_seconds=chunk_seconds
    ), int(sample_rate)


def _wav_url_to_chunks(url: str, chunk_seconds: float = 0.5) -> tuple[list[list[float]], int]:
    try:
        with urllib.request.urlopen(url) as response:
            raw = response.read()
    except Exception as exc:
        raise ValueError(f"Failed to download wav from URL '{url}': {exc}") from exc
    if not raw:
        raise ValueError(f"Downloaded wav is empty from URL '{url}'.")
    return _wav_bytes_to_chunks(raw, chunk_seconds=chunk_seconds)


async def _extract_register_request_from_http(
    request: Request,
) -> tuple[RegisterSpeakerRequest, list[list[float]]]:
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        name = str(form.get("name", "")).strip()
        if not name:
            raise HTTPException(status_code=422, detail="Field 'name' is required in form-data.")

        chunk_sample_rate = int(form.get("chunk_sample_rate", 16000))
        chunk_seconds = float(form.get("chunk_seconds", 0.5))

        wav_uploads = _collect_upload_files(form)
        if not wav_uploads:
            raise HTTPException(
                status_code=422,
                detail="Provide at least one wav UploadFile in multipart/form-data.",
            )

        merged_chunks: list[list[float]] = []
        last_sr = chunk_sample_rate
        for wav_file in wav_uploads:
            file_chunks, file_sr = await _wav_upload_to_chunks(
                wav_file, chunk_seconds=chunk_seconds
            )
            merged_chunks.extend(file_chunks)
            last_sr = file_sr

        payload = RegisterSpeakerRequest(
            name=name,
            chunks=merged_chunks,
            chunk_sample_rate=last_sr,
        )
        return payload, merged_chunks

    raw = await request.json()
    payload = RegisterSpeakerRequest.model_validate(raw)
    chunks = payload.chunks or []
    if payload.pcm16_b64:
        chunks = _decode_pcm16_b64_to_chunks(payload.pcm16_b64, payload.chunk_sample_rate)
    elif payload.wav_path or payload.wav_paths:
        chunk_seconds = float(raw.get("chunk_seconds", 0.5))
        merged_chunks: list[list[float]] = []
        wav_urls: list[str] = []
        if payload.wav_path:
            wav_urls.append(payload.wav_path)
        if payload.wav_paths:
            wav_urls.extend(payload.wav_paths)
        last_sr = payload.chunk_sample_rate
        for wav_url in wav_urls:
            file_chunks, file_sr = _wav_url_to_chunks(wav_url, chunk_seconds=chunk_seconds)
            merged_chunks.extend(file_chunks)
            last_sr = file_sr
        payload.chunk_sample_rate = last_sr
        chunks = merged_chunks
    return payload, chunks


async def _extract_recognize_request_from_http(
    request: Request,
) -> tuple[RecognizeSpeakerRequest, list[list[float]]]:
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        chunk_sample_rate = int(form.get("chunk_sample_rate", 16000))
        chunk_seconds = float(form.get("chunk_seconds", 0.5))
        uploads = _collect_upload_files(form)
        wav_upload = uploads[0] if uploads else None
        if wav_upload is None:
            raise HTTPException(
                status_code=422,
                detail="Provide one wav UploadFile in multipart/form-data for recognition.",
            )

        chunks, sr = await _wav_upload_to_chunks(wav_upload, chunk_seconds=chunk_seconds)
        payload = RecognizeSpeakerRequest(chunks=chunks, chunk_sample_rate=sr or chunk_sample_rate)
        return payload, chunks

    raw = await request.json()
    payload = RecognizeSpeakerRequest.model_validate(raw)
    chunks = payload.chunks or []
    if payload.pcm16_b64:
        chunks = _decode_pcm16_b64_to_chunks(payload.pcm16_b64, payload.chunk_sample_rate)
    elif payload.wav_path:
        chunk_seconds = float(raw.get("chunk_seconds", 0.5))
        chunks, sr = _wav_url_to_chunks(payload.wav_path, chunk_seconds=chunk_seconds)
        payload.chunk_sample_rate = sr
    return payload, chunks


@router.post("/audio-recognizer/register")
async def register_speaker(request: Request):
    """Register one speaker from wav path(s), chunks, pcm16_b64, or multipart wav upload."""
    recognizer = _get_audio_recognizer()
    try:
        payload, parsed_chunks = await _extract_register_request_from_http(request)

        if parsed_chunks:
            emb = recognizer._extract_embedding_from_chunks(
                parsed_chunks,
                chunk_sample_rate=payload.chunk_sample_rate,
            )
            recognizer.db.set(payload.name, emb)
            stored = recognizer.db.get(payload.name)
            if stored is None:
                raise RuntimeError("Failed to persist speaker embedding.")
            return {
                "name": payload.name,
                "num_samples": len(parsed_chunks),
                "embedding_dim": int(stored.shape[0]),
            }

        raise ValueError("No valid audio input provided for registration.")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/audio-recognizer/speakers/{speaker_name}", response_model=RemoveSpeakerResponse)
async def remove_speaker(speaker_name: str):
    """Remove one speaker from the recognition DB."""
    recognizer = _get_audio_recognizer()
    removed = recognizer.remove(speaker_name)
    return RemoveSpeakerResponse(name=speaker_name, removed=removed)


@router.post("/audio-recognizer/recognize", response_model=RecognizeSpeakerResponse)
async def recognize_speaker(request: Request):
    """Recognize speaker from wav path, chunks, pcm16_b64, or multipart wav upload."""
    recognizer = _get_audio_recognizer()
    try:
        payload, parsed_chunks = await _extract_recognize_request_from_http(request)
        result = recognizer.recognize(parsed_chunks, payload.chunk_sample_rate)
        return RecognizeSpeakerResponse(**result)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/audio-recognizer/embed", response_model=EmbedAudioResponse)
async def embed_audio(req: EmbedAudioRequest):
    """Return per-chunk and/or aggregated L2-normalized embeddings.

    Stateless — does NOT touch the speaker DB. ``embedding`` is always the
    weighted aggregate over all chunks (same vector as ``/recognize`` would
    compare against). When ``return_chunks=True`` the response also carries
    the per-chunk embeddings so callers can run their own per-chunk voting
    against a local store.
    """
    recognizer = _get_audio_recognizer()
    try:
        merged_chunks: list[list[float]] = []
        last_sr = 16000
        for item in req.audios_b64:
            try:
                raw = base64.b64decode(item)
            except Exception as exc:
                raise ValueError(f"invalid base64 payload: {exc}") from exc
            if not raw:
                raise ValueError("empty audio payload")
            chunks, sr = _wav_bytes_to_chunks(raw, chunk_seconds=req.chunk_seconds)
            if chunks:
                merged_chunks.extend(chunks)
                last_sr = sr
        if not merged_chunks:
            raise ValueError("no audio chunks extracted from inputs")

        # Inline _extract_embedding_from_chunks so we keep the per-chunk
        # embeddings around (the wrapper throws them away after aggregation).
        # Speech-gate failures raise PreprocessRejected below, which the
        # outer handler maps to a structured 400 body.
        waveforms = recognizer._prepare_waveforms_from_chunks(
            merged_chunks, chunk_sample_rate=last_sr
        )
        chunk_embs = recognizer._extract_embeddings_from_waveforms_batch(waveforms)
        if not chunk_embs:
            raise ValueError("no embeddings produced")
        agg = recognizer._aggregate_embeddings(chunk_embs)

        agg_vec = np.asarray(agg, dtype=np.float32).flatten()
        chunk_payload: list[list[float]] | None = None
        if req.return_chunks:
            chunk_payload = [np.asarray(e, dtype=np.float32).flatten().tolist() for e in chunk_embs]
        return EmbedAudioResponse(
            embedding=agg_vec.tolist(),
            embedding_dim=int(agg_vec.shape[0]),
            chunk_embeddings=chunk_payload,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/audio-recognizer/speakers", response_model=SpeakerListResponse)
async def list_speakers():
    """List active speakers in recognition DB."""
    recognizer = _get_audio_recognizer()
    db = recognizer.speaker_db
    speakers = [
        SpeakerSummary(name=name, embedding_dim=int(np.asarray(emb).shape[0]))
        for name, emb in sorted(db.items())
    ]
    return SpeakerListResponse(total=len(speakers), speakers=speakers)
