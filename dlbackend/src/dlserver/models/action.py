from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Tag

from core.models.action import HumanActionDetection
from core.types import Omit, omit


class ActionFrameRequest(BaseModel):
    type: Literal["frame"] = "frame"
    task: Literal["action"] = "action"
    frame_b64: str


class ActionConfigRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(arbitrary_types_allowed=True)
    type: Literal["config"] = "config"
    task: Literal["action"] = "action"
    whitelist: list[str] | None | Omit = omit
    threshold: float | Omit = omit
    person_detection_enabled: bool | Omit = omit  # toggle person detector on/off for this session
    person_min_area_ratio: float | Omit = (
        omit  # override person detector min area ratio for this session
    )


class ActionHeartBeatRequest(BaseModel):
    type: Literal["heartbeat"] = "heartbeat"
    task: Literal["action"] = "action"


ActionRequest = Annotated[
    Annotated[ActionFrameRequest, Tag("frame")]
    | Annotated[ActionConfigRequest, Tag("config")]
    | Annotated[ActionHeartBeatRequest, Tag("heartbeat")],
    Discriminator("type"),
]


class ActionDetection(BaseModel):
    class_name: str
    conf: float


class ActionResponse(BaseModel):
    """Single human action analysis result."""

    detected_classes: list[ActionDetection]

    @staticmethod
    def from_human_action_detection(human_action_detection: HumanActionDetection):
        detected_classes = [
            ActionDetection(class_name=a.class_name, conf=a.conf)
            for a in human_action_detection.actions
        ]
        return ActionResponse(detected_classes=detected_classes)
