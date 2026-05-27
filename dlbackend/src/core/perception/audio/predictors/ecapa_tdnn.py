"""ECAPA-TDNN 1024 speaker embedding model."""

from pathlib import Path

from core.enums.files import ModelEnum
from core.perception.audio.predictors.base import AudioEmbedder
from core.utils.files import get_default_cdn_url, get_default_model_path


class EcapaTdnn1024Embedder(AudioEmbedder):
    """WeSpeaker ECAPA-TDNN-1024-LM speaker embedder."""

    DEFAULT_MODEL_PATH: Path | None = get_default_model_path(ModelEnum.WESPEAKER_ECAPA_TDNN_1024)
    DEFAULT_REMOTE_URL: str | None = get_default_cdn_url(ModelEnum.WESPEAKER_ECAPA_TDNN_1024)
