from enum import StrEnum


class PerceptionEnum(StrEnum):
    ACTION = "action"
    FER = "fer"
    SER = "ser"
    POSE_2D = "pose_2d"
    POSE_3D = "pose_3d"
    AUDIO = "audio"
    FACE = "face"
    PERSON = "person"
    OBJECT = "object"
