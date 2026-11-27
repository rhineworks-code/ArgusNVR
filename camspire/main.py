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

log = logging.getLogger("camspire")


def dump_cams(cfg) -> str:
    import json
    return json.dumps({k: v.model_dump(mode="json") for k, v in cfg.cameras.items()}, sort_keys=True)


class Context:
    def __init__(self) -> None:
        self.started = time.time()
        self.paths = Paths()
        self.paths.ensure()
        self.cfg = load_config(self.paths.config)
        self.db = Database(self.paths.db)
        self.hub = Hub()
        self.go2rtc = Go2rtc(self.cfg)
        from .nas import Storage
        self.storage = Storage(self.paths, self.cfg.storage)
        self.codecs: dict[str, dict] = {}
        self.detector = None
        self.faces = None
        self.mqtt = None
        self.recorders: dict[str, CameraRecorder] = {}
        self.workers: dict[str, CameraWorker] = {}
        self.events: EventManager | None = None
        self.retention: Retention | None = None
        self._apply_lock = threading.Lock()

    # ------------------------------------------------------------------ Aufbau
    def build(self) -> None:
        cfg, paths = self.cfg, self.paths
        log.info("CamSpire NVR %s – %d Kamera(s) – Provider: %s", __version__, len(cfg.cameras),
                 ", ".join(available_providers()))
        self.go2rtc.start()

        self.detector = self._load_detector(cfg)

        self._load_faces(cfg)
        self._start_mqtt(cfg)

        self.events = EventManager(cfg, paths, self.db, self.hub, self.mqtt, self.faces, self.recorders)
        # offene Ereignisse vom letzten Lauf abschließen
        self.db.execute("UPDATE events SET end_ts=start_ts+1 WHERE end_ts IS NULL")

        for cid, cam in cfg.cameras.items():
            self._make_camera(cid, cam, start=False)
        if not cfg.cameras:
            log.warning("Noch keine Kamera eingerichtet – http://<server>:5080/#/settings")

        self.storage.start()
        self.retention = Retention(cfg, paths, self.db, self.faces, self.storage)
        self.retention.measure()

    def start(self) -> None:
        time.sleep(1.5)   # go2rtc hochfahren lassen
        for r in self.recorders.values():
            r.start()
        RecordingJanitor(self.recorders).start()
        for cid, w in self.workers.items():
            w.start()
            threading.Thread(target=self._probe_codecs, args=(cid, self.cfg.cameras[cid]), daemon=True).start()
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


    # ------------------------------------------------------------------ Bausteine
    def _load_detector(self, cfg):
        model = cfg.detector.model or str(self.paths.models / "yolo.onnx")
        if not os.path.exists(model):
            log.error("Kein YOLO-Modell unter %s – Objekterkennung deaktiviert", model)
            return None
        from .detector import Detector
        d = Detector(model, cfg.detector.device, cfg.detector.gpu_id, cfg.detector.nms_iou,
                     cache_dir=str(self.paths.config.parent / "trt-cache"))
        log.info("Objekterkennung: %s auf %s (%.1f ms Warm-up)", os.path.basename(model), d.provider, d.infer_ms)
        return d

    def _load_faces(self, cfg) -> None:
        m = self.paths.models
        if not (cfg.faces.enabled and (m / "face_det.onnx").exists() and (m / "face_rec.onnx").exists()):
            self.faces = None
            return
        from .faces import FaceEngine
        try:
            self.faces = FaceEngine(cfg.faces, self.paths, self.db, cfg.detector.device, cfg.detector.gpu_id)
            log.info("Gesichtserkennung aktiv (%d Personen bekannt)", len(self.faces.names))
        except Exception:
            log.exception("Gesichtserkennung konnte nicht geladen werden")
            self.faces = None

    def _start_mqtt(self, cfg) -> None:
        self.mqtt = None
        if cfg.mqtt.enabled and cfg.mqtt.host:
            from .mqtt import Mqtt
            self.mqtt = Mqtt(cfg, self.set_detect)
            self.mqtt.start()

    def _make_camera(self, cid, cam, start: bool = True) -> None:
        cfg = self.cfg
        if cam.record_mode_effective(cfg.record) != "off":
            rec = CameraRecorder(cfg, cam, self.paths, self.db, self.storage)
            self.recorders[cid] = rec
            if start:
                rec.start()
        w = CameraWorker(cfg, cam, self.detector, self.events, self.faces, cfg.detector.gpu_id)
        self.workers[cid] = w
        if start:
            w.start()
            threading.Thread(target=self._probe_codecs, args=(cid, cam), daemon=True).start()

    def _probe_codecs(self, cid, cam) -> None:
        """Codec von Haupt- und Substream ermitteln (fürs Live-Bild: H.265 braucht ggf. Umwandlung)."""
        import subprocess
        time.sleep(3)
        out = {}
        for key, stream in (("main", cid), ("sub", f"{cid}_sub" if cam.sub else None)):
            if not stream:
                continue
            for _ in range(3):
                try:
                    r = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-timeout", "8000000",
                                        "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height",
                                        "-of", "csv=p=0", f"rtsp://127.0.0.1:8554/{stream}"],
                                       capture_output=True, text=True, timeout=20)
                    parts = r.stdout.strip().split(",")
                    if r.returncode == 0 and parts[0]:
                        out[key] = {"codec": parts[0], "width": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None,
                                    "height": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None}
                        break
                except Exception:
                    pass
                time.sleep(5)
        if self.workers.get(cid) is not None:
            self.codecs[cid] = out
            self.hub.publish({"type": "codecs", "camera": cid, "codecs": out})

    def _drop_camera(self, cid) -> None:
        w = self.workers.pop(cid, None)
        if w:
            w.stop()
        if self.events:
            self.events.end_camera(cid)
        r = self.recorders.pop(cid, None)
        if r:
            r.stop()

    # ------------------------------------------------------------------ Einstellungen live übernehmen
    def apply_config(self, new) -> dict:
        """Übernimmt eine neue Konfiguration ohne Container-Neustart. Nur Betroffenes wird neu gestartet."""
        import json
        from .config import save_config
        from .go2rtc import build_config
        with self._apply_lock:
            old = self.cfg
            dump = lambda m: json.dumps(m.model_dump(mode="json"), sort_keys=True)  # noqa: E731
            changed: list[str] = []
            save_config(self.paths.config, new)
            self.cfg = new

            # go2rtc
            g_old, g_new = build_config(old), build_config(new)
            if {k: v for k, v in g_old.items() if k != "streams"} != {k: v for k, v in g_new.items() if k != "streams"}:
                self.go2rtc.restart(new)
                changed.append("go2rtc")
            elif g_old["streams"] != g_new["streams"]:
                self.go2rtc.write(new)
                self.go2rtc.update_streams(g_old["streams"], g_new["streams"])
                changed.append("Streams")

            # Objekterkennung
            if dump(old.detector) != dump(new.detector):
                det = self._load_detector(new)
                self.detector = det
                changed.append("Objekterkennung")

            # Gesichter
            if old.faces.enabled != new.faces.enabled or old.detector.device != new.detector.device:
                self._load_faces(new)
                changed.append("Gesichtserkennung")
            elif self.faces:
                self.faces.cfg = new.faces
            # MQTT
            removed = set(old.cameras) - set(new.cameras)
            if self.mqtt and removed:
                for cid in removed:
                    self.mqtt.remove_camera(cid, old.cameras[cid])
            if dump(old.mqtt) != dump(new.mqtt):
                if self.mqtt:
                    self.mqtt.stop()
                self._start_mqtt(new)
                changed.append("MQTT")
            elif self.mqtt:
                self.mqtt.cfg = new
                if dump_cams(old) != dump_cams(new) or old.faces.enabled != new.faces.enabled:
                    self.mqtt._discovery()

            # Ereignis-Verwaltung & Aufräumen
            self.events.cfg = new
            self.events.mqtt = self.mqtt
            self.events.faces = self.faces
            if self.retention:
                self.retention.cfg = new
            if dump(old.storage) != dump(new.storage):
                self.storage.apply(new.storage)
                changed.append("Speicherort")
                self.retention.faces = self.faces

            # Kameras: nur geänderte neu starten
            global_changed = (dump(old.objects) != dump(new.objects) or dump(old.record) != dump(new.record)
                              or old.detector.min_score != new.detector.min_score)
            for cid in removed:
                self._drop_camera(cid)
                changed.append(f"Kamera {cid} entfernt")
            for cid, cam in new.cameras.items():
                o = old.cameras.get(cid)
                if o is None or global_changed or dump(o) != dump(cam):
                    self._drop_camera(cid)
                    self._make_camera(cid, cam)
                    changed.append(f"Kamera {cam.name}")
                else:
                    w = self.workers.get(cid)
                    if w:
                        w.cfg, w.cam = new, cam
                        w.detector, w.faces = self.detector, self.faces
                    r = self.recorders.get(cid)
                    if r:
                        r.cfg, r.cam, r.rc = new, cam, new.record
            # Schwellwerte für Gesichter / Detektor in laufenden Workern aktualisieren
            for w in self.workers.values():
                w.detector, w.faces = self.detector, self.faces
            log.info("Einstellungen übernommen: %s", ", ".join(changed) or "keine laufenden Dienste betroffen")
            self.hub.publish({"type": "config", "changed": changed})
            return {"changed": changed}

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
            "recording": {cid: {"last_segment": r.last_segment_ts, "restarts": r.restarts, "mode": r.mode,
                                "recording": r.mode == "continuous" or r.window is not None}
                          for cid, r in self.recorders.items()},
            "codecs": self.codecs,
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
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=int(os.environ.get("CAMSPIRE_PORT") or os.environ.get("ARGUS_PORT") or 5080),
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
