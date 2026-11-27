"""Ereignis-Verwaltung: aus bestätigten Tracks werden Ereignisse mit Snapshot, Clip und Gesicht."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

import cv2
import numpy as np

from .config import GROUP_NAMES, Config, Paths
from .db import Database
from .tracker import Track

log = logging.getLogger("camspire.events")


class ActiveEvent:
    def __init__(self, eid: str, cam: str, track: Track) -> None:
        self.id = eid
        self.camera = cam
        self.track = track
        self.start_ts = track.first_ts
        self.best_val = 0.0
        self.best_frame: np.ndarray | None = None
        self.best_box = track.box
        self.top_score = track.max_score
        self.zones: set[str] = set()
        self.last_pub = 0.0
        self.sub_label: str | None = None

    def payload(self, end_ts: float | None = None) -> dict[str, Any]:
        t = self.track
        return {"id": self.id, "camera": self.camera, "label": t.label, "grp": t.group,
                "start_ts": self.start_ts, "end_ts": end_ts, "top_score": round(self.top_score, 3),
                "score": round(t.score, 3), "box": [round(v, 1) for v in self.best_box],
                "zones": sorted(self.zones), "current_zones": sorted(t.current_zones),
                "stationary": t.stationary, "sub_label": self.sub_label,
                "sub_score": round(t.face.score, 3) if (t.face and t.face.person_id) else None}


class EventManager:
    def __init__(self, cfg: Config, paths: Paths, db: Database, hub, mqtt, faces, recorders: dict) -> None:
        self.cfg, self.paths, self.db = cfg, paths, db
        self.hub, self.mqtt, self.faces, self.recorders = hub, mqtt, faces, recorders
        self.active: dict[str, ActiveEvent] = {}
        self.lock = threading.RLock()
        self.counts: dict[str, dict[str, int]] = {}

    # ------------------------------------------------------------------ pro Frame
    def process(self, cam: str, tracks: list[Track], ended: list[Track], frame: np.ndarray, ts: float) -> None:
        with self.lock:
            for t in tracks:
                if not t.confirmed:
                    continue
                if t.event_id is None and not t.stationary and t.eligible:
                    self._start(cam, t, frame, ts)
                elif t.event_id is not None:
                    ev = self.active.get(t.event_id)
                    if ev is None:
                        continue
                    if t.stationary:
                        self._end(ev, ts)
                        continue
                    self._update(ev, frame, ts)
            for t in ended:
                if t.event_id and t.event_id in self.active:
                    self._end(self.active[t.event_id], t.last_ts)
        self._publish_counts(cam, tracks)

    def _start(self, cam: str, t: Track, frame: np.ndarray, ts: float) -> None:
        eid = f"{int(ts)}-{uuid.uuid4().hex[:6]}"
        t.event_id = eid
        ev = ActiveEvent(eid, cam, t)
        ev.zones |= t.current_zones
        self.active[eid] = ev
        self._snapshot(ev, frame, force=True)
        if cam in self.recorders:
            self.recorders[cam].event_started(eid, ev.start_ts)
        p = ev.payload()
        self.db.upsert_event(p)
        log.info("[%s] Ereignis %s: %s (%.2f)", cam, eid, t.label, t.score)
        self.hub.publish({"type": "event", "action": "new", "event": p})
        if self.mqtt:
            self.mqtt.event("new", p)
            self.mqtt.snapshot(cam, t.group, ev.best_frame, ev.best_box)

    def _update(self, ev: ActiveEvent, frame: np.ndarray, ts: float) -> None:
        t = ev.track
        changed = False
        ev.top_score = max(ev.top_score, t.score)
        new_z = t.current_zones - ev.zones
        if new_z:
            ev.zones |= new_z
            changed = True
        if self._snapshot(ev, frame):
            changed = True
        name = t.face.name if (t.face and t.face.person_id) else None
        if name and name != ev.sub_label:
            ev.sub_label = name
            changed = True
            log.info("[%s] Gesicht erkannt: %s (%.2f)", ev.camera, name, t.face.score)
            if self.mqtt:
                self.mqtt.face(ev.camera, name, t.face.score, ev.id)
        if changed and ts - ev.last_pub > 2:
            ev.last_pub = ts
            p = ev.payload()
            self.hub.publish({"type": "event", "action": "update", "event": p})
            if self.mqtt:
                self.mqtt.event("update", p)
                self.mqtt.snapshot(ev.camera, t.group, ev.best_frame, ev.best_box)

    def _end(self, ev: ActiveEvent, ts: float) -> None:
        self.active.pop(ev.id, None)
        t = ev.track
        t.event_id = None   # bleibt das Objekt stehen, entsteht erst bei neuer Bewegung ein neues Ereignis
        end_ts = max(ts, ev.start_ts + 0.5)
        p = ev.payload(end_ts)
        snap, thumb = self._save_snapshot(ev)
        p.update(snapshot=snap, thumb=thumb)
        if self.faces and t.group == "person" and t.face is not None:
            try:
                p.update(self.faces.finalize(t.face, ev.id, ev.camera))
            except Exception:
                log.exception("Gesicht abschließen")
            t.face = None
        self.db.upsert_event(p)
        if ev.camera in self.recorders:
            self.recorders[ev.camera].event_ended(ev.id, end_ts)
        self.hub.publish({"type": "event", "action": "end", "event": p})
        if self.mqtt:
            self.mqtt.event("end", p)

    # ------------------------------------------------------------------ Snapshots
    def _snapshot(self, ev: ActiveEvent, frame: np.ndarray, force: bool = False) -> bool:
        t = ev.track
        x1, y1, x2, y2 = t.box
        H, W = frame.shape[:2]
        # Bewertung: Sicherheit * Größe, Abzug wenn die Box am Bildrand abgeschnitten ist
        edge = 0.7 if (x1 < 3 or y1 < 3 or x2 > W - 3 or y2 > H - 3) else 1.0
        val = t.score * np.sqrt(max(1.0, t.area)) * edge
        if t.face and t.face.best_face is not None:
            val *= 1 + t.face.best_face.quality * 0.5
        if force or val > ev.best_val * 1.05:
            ev.best_val = val
            ev.best_frame = frame.copy()
            ev.best_box = t.box
            return True
        return False

    def _save_snapshot(self, ev: ActiveEvent) -> tuple[str | None, str | None]:
        if ev.best_frame is None:
            return None, None
        day = time.strftime("%Y-%m-%d", time.localtime(ev.start_ts))
        d = self.paths.snapshots / ev.camera / day
        d.mkdir(parents=True, exist_ok=True)
        img = ev.best_frame
        H, W = img.shape[:2]
        mw = self.cfg.snapshots.max_width
        q = [cv2.IMWRITE_JPEG_QUALITY, self.cfg.snapshots.quality]
        full = img if W <= mw else cv2.resize(img, (mw, int(H * mw / W)), interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(d / f"{ev.id}.jpg"), full, q)
        x1, y1, x2, y2 = [int(v) for v in ev.best_box]
        bw, bh = x2 - x1, y2 - y1
        side = int(max(bw, bh) * 1.3) + 10
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        sx1, sy1 = max(0, cx - side // 2), max(0, cy - side // 2)
        crop = img[sy1:min(H, sy1 + side), sx1:min(W, sx1 + side)]
        if crop.size:
            s = 320 / max(crop.shape[:2])
            crop = cv2.resize(crop, (max(1, int(crop.shape[1] * s)), max(1, int(crop.shape[0] * s))),
                              interpolation=cv2.INTER_AREA)
            cv2.imwrite(str(d / f"{ev.id}_t.jpg"), crop, [cv2.IMWRITE_JPEG_QUALITY, 70])
        rel = f"snapshots/{ev.camera}/{day}/{ev.id}"
        ev.best_frame = None   # RAM freigeben
        return rel + ".jpg", rel + "_t.jpg"

    # ------------------------------------------------------------------ Zähler / HA
    def _publish_counts(self, cam: str, tracks: list[Track]) -> None:
        conf = [t for t in tracks if t.confirmed]
        counts = {g: sum(1 for t in conf if t.group == g) for g in GROUP_NAMES}
        cam_cfg = self.cfg.cameras.get(cam)
        for zname in (cam_cfg.zones if cam_cfg else {}):
            for g in GROUP_NAMES:
                counts[f"{zname}/{g}"] = sum(1 for t in conf if t.group == g and zname in t.current_zones)
        prev = self.counts.get(cam)
        if prev != counts:
            self.counts[cam] = counts
            self.hub.publish({"type": "counts", "camera": cam, "counts": counts})
            if self.mqtt:
                self.mqtt.counts(cam, counts, prev or {})

    def end_camera(self, cam: str) -> None:
        with self.lock:
            for ev in [e for e in self.active.values() if e.camera == cam]:
                self._end(ev, time.time())
        self.counts.pop(cam, None)

    def end_all(self) -> None:
        with self.lock:
            for ev in list(self.active.values()):
                self._end(ev, time.time())
