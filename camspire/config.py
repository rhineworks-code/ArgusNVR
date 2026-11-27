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
    topic_prefix: str = "camspire"
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


class StorageCfg(BaseModel):
    """Wohin Aufnahmen geschrieben werden. Datenbank, Snapshots und Gesichter bleiben lokal."""
    type: Literal["local", "smb", "nfs", "path"] = "local"
    server: str = ""            # IP/Name des NAS
    share: str = ""             # SMB: Freigabe (z. B. "surveillance" oder "video/camspire"), NFS: Export-Pfad
    username: str = ""
    password: str = ""
    domain: str = ""
    smb_version: str = "3.0"
    options: str = ""           # zusätzliche Mount-Optionen
    path: str = ""              # type=path: im Container eingebundener Ordner (z. B. /nas)
    subfolder: str = "camspire"    # Unterordner auf dem Ziel


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
    record_mode: Literal["default", "events", "continuous", "off"] = "default"   # default = globale Einstellung
    events_days: int | None = None        # None = global
    continuous_days: int | None = None    # None = global
    live_transcode: Literal["auto", "on", "off"] = "auto"   # H.265 -> H.264 fürs Live-Bild im Browser
    objects: list[str] | None = None
    zones: dict[str, ZoneCfg] = Field(default_factory=dict)
    require_zone: bool = False      # True = Ereignisse nur innerhalb der Zonen
    masks: list[list[Point]] = Field(default_factory=list)
    motion: MotionCfg = Field(default_factory=MotionCfg)

    @property
    def detect_stream(self) -> str:
        return f"{self.id}_sub" if (self.detect.stream == "sub" and self.sub) else self.id

    def record_mode_effective(self, glob: "RecordCfg") -> str:
        if not self.record:
            return "off"
        return glob.mode if self.record_mode == "default" else self.record_mode

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
    storage: StorageCfg = Field(default_factory=StorageCfg)
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


def _env(name: str, default: str) -> str:
    # ARGUS_* = alter Name (vor der Umbenennung in CamSpire), weiterhin akzeptiert
    return os.environ.get(f"CAMSPIRE_{name}") or os.environ.get(f"ARGUS_{name}") or default


class Paths:
    def __init__(self) -> None:
        self.config = Path(_env("CONFIG", "/config/config.yml"))
        self.media = Path(_env("MEDIA", "/media/camspire"))
        self.cache = Path(_env("CACHE", "/tmp/cache"))
        self.models = Path(_env("MODELS", "/models"))
        self.clips = self.media / "clips"
        self.recordings = self.media / "recordings"
        self.snapshots = self.media / "snapshots"
        self.faces = self.media / "faces"
        self.db = self.media / "camspire.db"

    def ensure(self) -> None:
        for p in (self.media, self.cache, self.clips, self.recordings, self.snapshots, self.faces):
            p.mkdir(parents=True, exist_ok=True)
        # Datenbank aus der Zeit vor der Umbenennung (argus.db) übernehmen
        old = self.media / "argus.db"
        if old.exists() and not self.db.exists():
            for suffix in ("", "-wal", "-shm"):
                src = Path(str(old) + suffix)
                if src.exists():
                    src.rename(Path(str(self.db) + suffix))


def load_config(path: Path) -> Config:
    """Fehlt die Datei, startet CamSpire leer – die Einrichtung erfolgt dann in der Weboberfläche."""
    if not path.exists():
        return Config()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = Config.model_validate(raw)
    # Umbenennung Argus -> CamSpire: altes MQTT-Präfix übernehmen
    if cfg.mqtt.topic_prefix == "argus":
        cfg.mqtt.topic_prefix = "camspire"
        save_config(path, cfg)
    return cfg


class _Dumper(yaml.SafeDumper):
    """Blöcke für Abschnitte, aber kurze Listen (Objekte, Punkte) einzeilig."""


def _repr_list(dumper, data):
    flat = all(not isinstance(x, (dict, list)) for x in data) or \
        all(isinstance(x, list) and all(not isinstance(y, (dict, list)) for y in x) for x in data)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=flat)


_Dumper.add_representer(list, _repr_list)

SECRET = "********"
HEADER = ("# CamSpire NVR – Konfiguration\n"
          "# Wird von der Weboberfläche (Einstellungen) geschrieben. Bearbeiten per Hand ist möglich,\n"
          "# Kommentare gehen beim nächsten Speichern aus der Oberfläche allerdings verloren.\n"
          "# Vorheriger Stand: config.yml.bak\n\n")


def dump_config(cfg: Config) -> dict[str, Any]:
    d = cfg.model_dump(mode="json")
    for cam in d.get("cameras", {}).values():
        cam.pop("id", None)
    if not d.get("go2rtc"):
        d.pop("go2rtc", None)
    return d


def save_config(path: Path, cfg: Config) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.with_suffix(".yml.bak").write_bytes(path.read_bytes())
    tmp = path.with_suffix(".yml.tmp")
    tmp.write_text(HEADER + yaml.dump(dump_config(cfg), Dumper=_Dumper, sort_keys=False, allow_unicode=True,
                                      default_flow_style=False, width=120), encoding="utf-8")
    tmp.replace(path)


def public_config(cfg: Config) -> dict[str, Any]:
    """Für die Oberfläche: Passwörter werden nicht zurückgegeben."""
    d = dump_config(cfg)
    for sec in ("auth", "mqtt", "storage"):
        if d[sec].get("password"):
            d[sec]["password"] = SECRET
    return d


def merge_secrets(raw: dict[str, Any], old: Config) -> dict[str, Any]:
    for sec in ("auth", "mqtt", "storage"):
        part = raw.get(sec) or {}
        if part.get("password") == SECRET:
            part["password"] = getattr(old, sec).password
    return raw
