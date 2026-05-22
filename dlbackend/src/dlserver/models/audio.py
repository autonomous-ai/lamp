"""HTTP/WS models for audio recognizer endpoints — Pydantic request/response types."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from dlserver.utils.audio import validate_wav_url


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
            self.wav_path = validate_wav_url(self.wav_path, "wav_path")
        if self.wav_paths:
            self.wav_paths = [validate_wav_url(item, "wav_paths[]") for item in self.wav_paths]
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
            self.wav_path = validate_wav_url(self.wav_path, "wav_path")
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
    """Request for stateless embedding computation."""

    audios_b64: list[str] = Field(min_length=1)
    return_chunks: bool = False


class EmbedAudioResponse(BaseModel):
    embedding: list[float]
    embedding_dim: int
    chunk_embeddings: list[list[float]] | None = None

    @staticmethod
    def from_raw_embedding(
        raw: "RawAudioEmbedding", return_chunks: bool = False
    ) -> "EmbedAudioResponse":
        from core.models.audio import RawAudioEmbedding

        chunk_payload: list[list[float]] | None = None
        if return_chunks:
            chunk_payload = [row.tolist() for row in raw.chunk_embeddings]
        return EmbedAudioResponse(
            embedding=raw.embedding.tolist(),
            embedding_dim=int(raw.embedding.shape[0]),
            chunk_embeddings=chunk_payload,
        )
