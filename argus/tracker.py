"""Leichter Multi-Objekt-Tracker (IoU + Distanz, gierige Zuordnung) mit Stillstands-Erkennung."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .config import GROUPS
from .detector import Detection

_ids = itertools.count(1)


def iou(a, b) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


@dataclass
class Track:
    label: str
    box: tuple[float, float, float, float]
    score: float
    first_ts: float
    last_ts: float
    id: int = field(default_factory=lambda: next(_ids))
    hits: int = 1
    misses: int = 0
    max_score: float = 0.0
    confirmed: bool = False
    stationary: bool = False
    anchor_box: tuple[float, float, float, float] | None = None
    anchor_ts: float = 0.0
    zones: set[str] = field(default_factory=set)
    current_zones: set[str] = field(default_factory=set)
    event_id: str | None = None
    eligible: bool = True
    face: Any = None              # FaceState (gesetzt vom Gesichts-Modul)
    last_face_ts: float = 0.0

    @property
    def group(self) -> str:
        return GROUPS.get(self.label, "other")

    @property
    def area(self) -> float:
        return (self.box[2] - self.box[0]) * (self.box[3] - self.box[1])


class Tracker:
    def __init__(self, min_hits: int = 3, max_misses: int = 5, stationary_seconds: float = 30,
                 match_iou: float = 0.2) -> None:
        self.tracks: list[Track] = []
        self.min_hits = min_hits
        self.max_misses = max_misses
        self.stationary_seconds = stationary_seconds
        self.match_iou = match_iou

    def has_moving(self) -> bool:
        return any(not t.stationary for t in self.tracks)

    def update(self, dets: list[Detection], ts: float) -> tuple[list[Track], list[Track]]:
        """Gibt (aktive Tracks, beendete Tracks) zurück."""
        unmatched = set(range(len(dets)))
        pairs = []
        for ti, t in enumerate(self.tracks):
            for di, d in enumerate(dets):
                if d.label != t.label:
                    continue
                s = iou(t.box, d.box)
                if s < self.match_iou:
                    # Fallback: Mittelpunkt-Distanz relativ zur Boxgröße (schnelle Objekte bei 5 fps)
                    tcx, tcy = (t.box[0] + t.box[2]) / 2, (t.box[1] + t.box[3]) / 2
                    dcx, dcy = (d.box[0] + d.box[2]) / 2, (d.box[1] + d.box[3]) / 2
                    diag = np.hypot(t.box[2] - t.box[0], t.box[3] - t.box[1])
                    dist = np.hypot(tcx - dcx, tcy - dcy)
                    if dist < 0.6 * diag:
                        s = 0.01 + 0.19 * (1 - dist / (0.6 * diag))
                    else:
                        continue
                pairs.append((s, ti, di))
        pairs.sort(reverse=True)
        used_t: set[int] = set()
        for s, ti, di in pairs:
            if ti in used_t or di not in unmatched:
                continue
            used_t.add(ti)
            unmatched.discard(di)
            t, d = self.tracks[ti], dets[di]
            # leichte Glättung der Box
            t.box = tuple(0.3 * np.array(t.box) + 0.7 * np.array(d.box))  # type: ignore
            t.score = d.score
            t.max_score = max(t.max_score, d.score)
            t.hits += 1
            t.misses = 0
            t.last_ts = ts
            if t.hits >= self.min_hits:
                t.confirmed = True
            self._stationary(t, ts)

        ended: list[Track] = []
        for ti, t in enumerate(self.tracks):
            if ti not in used_t:
                t.misses += 1
        keep = []
        for t in self.tracks:
            if t.misses > self.max_misses:
                ended.append(t)
            else:
                keep.append(t)
        self.tracks = keep
        for di in unmatched:
            d = dets[di]
            t = Track(d.label, d.box, d.score, ts, ts, max_score=d.score)
            t.anchor_box, t.anchor_ts = d.box, ts
            if self.min_hits <= 1:
                t.confirmed = True
            self.tracks.append(t)
        return self.tracks, ended

    def _stationary(self, t: Track, ts: float) -> None:
        if t.anchor_box is None or iou(t.anchor_box, t.box) < 0.8:
            t.anchor_box, t.anchor_ts = t.box, ts
            t.stationary = False
        elif ts - t.anchor_ts > self.stationary_seconds:
            t.stationary = True
