"""Konfiguration (YAML -> pydantic)."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

# COCO-Klassen -> Gruppe
GROUPS: dict[str, str] = {
    "person": "person",
    "bicycle": "vehicle", "car": "vehicle", "motorcycle": "vehicle", "bus": "vehicle",
    "truck": "vehicle", "boat": "vehicle", "train": "vehicle", "airplane": "vehicle",
    "bird": "animal", "cat": "animal", "dog": "animal", "horse": "animal", "sheep": "animal",
    "cow": "animal", "elephant": "animal", "bear": "animal", "zebra": "animal", "giraffe": "animal",
}
GROUP_NAMES = ["person", "vehicle", "animal"]

COCO = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog",
    "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle",
    "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
    "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors",
    "teddy bear", "hair drier", "toothbrush",
]

Point = tuple[float, float]


class AuthCfg(BaseModel):
    username: str = ""
    password: str = ""


class MqttCfg(BaseModel):
    enabled: bool = False
    host: str = "localhost"
    port: int = 1883
    username: str = ""
    password: str = ""
    topic_prefix: str = "argus"
    discovery_prefix: str = "homeassistant"


class DetectorCfg(BaseModel):
    device: Literal["auto", "cuda", "tensorrt", "cpu"] = "auto"
    gpu_id: int = 0
    model: str = ""
    min_score: float = 0.45
    nms_iou: float = 0.45


class ObjectFilter(BaseModel):
    min_score: float = 0.5
    min_area: int = 0          # Pixel im Erkennungsbild
    max_area: int = 10**9


class ObjectsCfg(BaseModel):
    track: list[str] = Field(default_factory=lambda: ["person", "car", "truck", "bus", "motorcycle",
                                                      "bicycle", "dog", "cat", "bird"])
    filters: dict[str, ObjectFilter] = Field(default_factory=dict)
    min_hits: int = 3             # so viele Treffer, bevor ein Ereignis entsteht
    stationary_seconds: int = 30  # unbewegt länger als das -> Ereignis endet (geparktes Auto)

    def filter_for(self, label: str) -> ObjectFilter:
        return self.filters.get(label, ObjectFilter())


class FacesCfg(BaseModel):
    enabled: bool = True
    recognition_threshold: float = 0.42
    auto_learn: bool = True
    auto_learn_threshold: float = 0.55
    max_samples_per_person: int = 40
    unknown_cluster_threshold: float = 0.45
    min_face_size: int = 36
    min_quality: float = 0.30


class RetentionCfg(BaseModel):
    events_days: int = 14
    continuous_days: int = 3
    max_storage_gb: float = 100


class RecordCfg(BaseModel):
    mode: Literal["events", "continuous", "off"] = "events"
    source: Literal["main", "sub"] = "main"
    audio: bool = False
    pre_roll: int = 5
    post_roll: int = 8
    max_clip_length: int = 300
    retention: RetentionCfg = Field(default_factory=RetentionCfg)


class SnapshotCfg(BaseModel):
    quality: int = 75
    max_width: int = 1280


class WebRtcCfg(BaseModel):
    candidates: list[str] = Field(default_factory=list)


class ZoneCfg(BaseModel):
    points: list[Point]
    objects: list[str] = Field(default_factory=list)   # leer = alle


class MotionCfg(BaseModel):
    threshold: int = 25
    min_area: float = 0.002


class DetectCfg(BaseModel):
    stream: Literal["main", "sub"] = "sub"
    width: int | None = None
    height: int | None = None
    fps: int = 5
    enabled: bool = True


class CameraCfg(BaseModel):
    id: str = ""
    name: str = ""
    main: str
    sub: str | None = None
    detect: DetectCfg = Field(default_factory=DetectCfg)
    hwaccel: Literal["cuda", "none"] = "cuda"
    faces: bool = True
    record: bool = True
    objects: list[str] | None = None
    zones: dict[str, ZoneCfg] = Field(default_factory=dict)
    require_zone: bool = False      # True = Ereignisse nur innerhalb der Zonen
    masks: list[list[Point]] = Field(default_factory=list)
    motion: MotionCfg = Field(default_factory=MotionCfg)

    @property
    def detect_stream(self) -> str:
        return f"{self.id}_sub" if (self.detect.stream == "sub" and self.sub) else self.id

    def record_stream(self, source: str) -> str:
        return f"{self.id}_sub" if (source == "sub" and self.sub) else self.id


class Config(BaseModel):
    auth: AuthCfg = Field(default_factory=AuthCfg)
    mqtt: MqttCfg = Field(default_factory=MqttCfg)
    detector: DetectorCfg = Field(default_factory=DetectorCfg)
    objects: ObjectsCfg = Field(default_factory=ObjectsCfg)
    faces: FacesCfg = Field(default_factory=FacesCfg)
    record: RecordCfg = Field(default_factory=RecordCfg)
    snapshots: SnapshotCfg = Field(default_factory=SnapshotCfg)
    webrtc: WebRtcCfg = Field(default_factory=WebRtcCfg)
    cameras: dict[str, CameraCfg] = Field(default_factory=dict)
    go2rtc: dict[str, Any] = Field(default_factory=dict)

    @field_validator("cameras")
    @classmethod
    def _ids(cls, v: dict[str, CameraCfg]) -> dict[str, CameraCfg]:
        for key, cam in v.items():
            if not re.fullmatch(r"[a-z0-9_]+", key):
                raise ValueError(f"Kamera-ID '{key}' darf nur a-z, 0-9 und _ enthalten")
            cam.id = key
            cam.name = cam.name or key.replace("_", " ").title()
        return v

    def camera_objects(self, cam: CameraCfg) -> list[str]:
        return cam.objects if cam.objects else self.objects.track


class Paths:
    def __init__(self) -> None:
        self.config = Path(os.environ.get("ARGUS_CONFIG", "/config/config.yml"))
        self.media = Path(os.environ.get("ARGUS_MEDIA", "/media/argus"))
        self.cache = Path(os.environ.get("ARGUS_CACHE", "/tmp/cache"))
        self.models = Path(os.environ.get("ARGUS_MODELS", "/models"))
        self.clips = self.media / "clips"
        self.recordings = self.media / "recordings"
        self.snapshots = self.media / "snapshots"
        self.faces = self.media / "faces"
        self.db = self.media / "argus.db"

    def ensure(self) -> None:
        for p in (self.media, self.cache, self.clips, self.recordings, self.snapshots, self.faces):
            p.mkdir(parents=True, exist_ok=True)


def load_config(path: Path) -> Config:
    if not path.exists():
        example = Path(__file__).resolve().parent.parent / "config" / "config.example.yml"
        raise SystemExit(f"Keine Konfiguration unter {path} gefunden. Vorlage: {example}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config.model_validate(raw)
