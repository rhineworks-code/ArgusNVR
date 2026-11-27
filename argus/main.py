"""Einstiegspunkt: alles verdrahten und starten."""
from __future__ import annotations

import logging
import os
import signal
import threading
import time

import uvicorn

from . import __version__
from .api import Hub, create_app
from .camera import CameraWorker
from .config import Paths, load_config
from .db import Database
from .events import EventManager
from .go2rtc import Go2rtc
from .recorder import CameraRecorder, RecordingJanitor
from .runtime import available_providers
from .storage import Retention

log = logging.getLogger("argus")


class Context:
    def __init__(self) -> None:
        self.started = time.time()
        self.paths = Paths()
        self.paths.ensure()
        self.cfg = load_config(self.paths.config)
        self.db = Database(self.paths.db)
        self.hub = Hub()
        self.go2rtc = Go2rtc(self.cfg)
        self.detector = None
        self.faces = None
        self.mqtt = None
        self.recorders: dict[str, CameraRecorder] = {}
        self.workers: dict[str, CameraWorker] = {}
        self.events: EventManager | None = None
        self.retention: Retention | None = None

    # ------------------------------------------------------------------ Aufbau
    def build(self) -> None:
        cfg, paths = self.cfg, self.paths
        log.info("Argus NVR %s – %d Kamera(s) – Provider: %s", __version__, len(cfg.cameras),
                 ", ".join(available_providers()))
        self.go2rtc.start()

        model = cfg.detector.model or str(paths.models / "yolo.onnx")
        if os.path.exists(model):
            from .detector import Detector
            self.detector = Detector(model, cfg.detector.device, cfg.detector.gpu_id, cfg.detector.nms_iou,
                                     cache_dir=str(paths.config.parent / "trt-cache"))
            log.info("Objekterkennung: %s auf %s (%.1f ms Warm-up)", os.path.basename(model),
                     self.detector.provider, self.detector.infer_ms)
        else:
            log.error("Kein YOLO-Modell unter %s – Objekterkennung deaktiviert", model)

        if cfg.faces.enabled and (paths.models / "face_det.onnx").exists() and (paths.models / "face_rec.onnx").exists():
            from .faces import FaceEngine
            try:
                self.faces = FaceEngine(cfg.faces, paths, self.db, cfg.detector.device, cfg.detector.gpu_id)
                log.info("Gesichtserkennung aktiv (%d Personen bekannt)", len(self.faces.names))
            except Exception:
                log.exception("Gesichtserkennung konnte nicht geladen werden")

        if cfg.mqtt.enabled:
            from .mqtt import Mqtt
            self.mqtt = Mqtt(cfg, self.set_detect)
            self.mqtt.start()

        if cfg.record.mode != "off":
            for cid, cam in cfg.cameras.items():
                if cam.record:
                    self.recorders[cid] = CameraRecorder(cfg, cam, paths, self.db,
                                                         on_clip=lambda c: self.hub.publish({"type": "clip", "clip": c}))

        self.events = EventManager(cfg, paths, self.db, self.hub, self.mqtt, self.faces, self.recorders)
        # offene Ereignisse vom letzten Lauf abschließen
        self.db.execute("UPDATE events SET end_ts=start_ts+1 WHERE end_ts IS NULL")

        for cid, cam in cfg.cameras.items():
            self.workers[cid] = CameraWorker(cfg, cam, self.detector, self.events, self.faces, cfg.detector.gpu_id)

        self.retention = Retention(cfg, paths, self.db, self.faces)
        self.retention.measure()

    def start(self) -> None:
        time.sleep(1.5)   # go2rtc hochfahren lassen
        for r in self.recorders.values():
            r.start()
        RecordingJanitor(list(self.recorders.values())).start()
        for w in self.workers.values():
            w.start()
        self.retention.start()
        threading.Thread(target=self._stats_loop, name="stats", daemon=True).start()

    def stop(self) -> None:
        log.info("Beende …")
        for w in self.workers.values():
            w.stop()
        if self.events:
            self.events.end_all()
        for r in self.recorders.values():
            r.stop()
        if self.mqtt:
            self.mqtt.stop()
        self.go2rtc.stop()

    # ------------------------------------------------------------------ Laufzeit
    def set_detect(self, cid: str, on: bool) -> None:
        w = self.workers.get(cid)
        if w:
            w.enabled = on
            log.info("[%s] Erkennung %s", cid, "an" if on else "aus")
            if self.mqtt:
                self.mqtt.detect_state(cid, on)
            self.hub.publish({"type": "detect", "camera": cid, "enabled": on})

    def stats(self, gpu=None) -> dict:
        d = self.detector
        return {
            "version": __version__, "uptime": int(time.time() - self.started),
            "detector": {"provider": d.provider if d else None, "infer_ms": round(d.infer_ms, 2) if d else None,
                         "input": d.size if d else None, "calls": d.calls if d else 0},
            "faces": {"enabled": bool(self.faces), "provider": self.faces.provider if self.faces else None,
                      "known": len(self.faces.names) if self.faces else 0,
                      "processed": self.faces.processed if self.faces else 0,
                      "dropped": self.faces.dropped if self.faces else 0},
            "cameras": {cid: w.stats() for cid, w in self.workers.items()},
            "recording": {cid: {"last_segment": r.last_segment_ts, "restarts": r.restarts,
                                "recording": r.session is not None} for cid, r in self.recorders.items()},
            "storage": self.retention.usage if self.retention else {},
            "mqtt": {"enabled": self.cfg.mqtt.enabled, "connected": bool(self.mqtt and self.mqtt.connected)},
            "go2rtc": bool(self.go2rtc.proc and self.go2rtc.proc.poll() is None),
            "active_events": len(self.events.active) if self.events else 0,
            "gpu": gpu,
        }

    def _stats_loop(self) -> None:
        while True:
            time.sleep(5)
            s = self.stats()
            self.hub.publish({"type": "stats", "stats": s})
            if self.mqtt and int(time.time()) % 60 < 5:
                self.mqtt.stats({k: s[k] for k in ("uptime", "detector", "cameras", "active_events")})


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    ctx = Context()
    ctx.build()
    ctx.start()
    app = create_app(ctx)
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=int(os.environ.get("ARGUS_PORT", 5080)),
                                           log_level="warning", proxy_headers=True, forwarded_allow_ips="*"))

    def _sig(*_):
        server.should_exit = True
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    try:
        server.run()
    finally:
        ctx.stop()


if __name__ == "__main__":
    main()
