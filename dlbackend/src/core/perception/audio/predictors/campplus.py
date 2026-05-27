"""CAM++ speaker embedding model."""

from pathlib import Path

from core.enums.files import ModelEnum
from core.perception.audio.predictors.base import AudioEmbedder
from core.utils.files import get_default_cdn_url, get_default_model_path


class CamPPlusEmbedder(AudioEmbedder):
    """WeSpeaker CAM++ speaker embedder."""

    DEFAULT_MODEL_PATH: Path | None = get_default_model_path(ModelEnum.WESPEAKER_CAMPPLUS)
    DEFAULT_REMOTE_URL: str | None = get_default_cdn_url(ModelEnum.WESPEAKER_CAMPPLUS)
