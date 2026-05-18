"""Health check endpoint."""

from fastapi import APIRouter

from dlserver.utils.state import get_action_model, get_emotion_model, get_pose_model

router = APIRouter()


@router.get("/health")
async def health():
    """Health check endpoint."""
    action_model = get_action_model()
    emotion_model = get_emotion_model()
    pose_model = get_pose_model()
    return {
        "status": "ok",
        "action_model": action_model is not None and action_model.is_ready(),
        "emotion_model": emotion_model is not None and emotion_model.is_ready(),
        "pose_model": pose_model is not None and pose_model.is_ready(),
    }
