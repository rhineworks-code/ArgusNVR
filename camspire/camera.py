"""Kamera-Pipeline: Dekodieren (NVDEC) -> Bewegung -> YOLO (GPU) -> Tracking -> Zonen -> Ereignisse/Gesichter."""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time

import cv2
import numpy as np

from .config import CameraCfg, Config
from .detector import Detection, Detector
from .motion import MotionDetector
from .tracker import Tracker

log = logging.getLogger("camspire.camera")

COLORS = {"person": (80, 200, 255), "vehicle": (255, 170, 60), "animal": (120, 230, 120), "other": (200, 200, 200)}


def probe_resolution(url: str) -> tuple[int, int] | None:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-timeout", "8000000",
                              "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", url],
                             capture_output=True, text=True, timeout=20).stdout
        s = json.loads(out)["streams"][0]
        return int(s["width"]), int(s["height"])
    except Exception:
        return None


class CameraWorker:
    def __init__(self, cfg: Config, cam: CameraCfg, detector: Detector | None, events, faces,
                 gpu_id: int = 0, source_url: str | None = None) -> None:
        self.cfg, self.cam, self.detector, self.events, self.faces = cfg, cam, detector, events, faces
        self.gpu_id = gpu_id
        self.url = source_url or f"rtsp://127.0.0.1:8554/{cam.detect_stream}"
        self.labels = set(cfg.camera_objects(cam))
        self.min_score = min([cfg.detector.min_score] + [cfg.objects.filter_for(l).min_score for l in self.labels])
        self.enabled = cam.detect.enabled
        self.width = self.height = 0
        self.tracker = Tracker(cfg.objects.min_hits, 5, cfg.objects.stationary_seconds)
        self.motion: MotionDetector | None = None
        self.zones_px: dict[str, np.ndarray] = {}
        self.masks_px: list[np.ndarray] = []
        self.hwaccel = cam.hwaccel == "cuda"
        self.hw_failures = 0

        self._frame: np.ndarray | None = None
        self._frame_ts = 0.0
        self._frame_seq = 0
        self._cv = threading.Condition()
        self.running = False
        self.proc: subprocess.Popen | None = None

        self.status = "starting"
        self.decode_fps = 0.0
        self.detect_fps = 0.0
        self.motion_active = False
        self.last_tracks: list = []
        self._dec_count = 0
        self._det_count = 0
        self._last_stat = time.time()

    # ------------------------------------------------------------------ Start
    def start(self) -> None:
        self.running = True
        threading.Thread(target=self._reader, name=f"read-{self.cam.id}", daemon=True).start()
        threading.Thread(target=self._loop, name=f"det-{self.cam.id}", daemon=True).start()

    def stop(self) -> None:
        self.running = False
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    def _setup_geometry(self) -> None:
        w, h = self.cam.detect.width, self.cam.detect.height
        if not (w and h):
            res = None
            for _ in range(5):
                res = probe_resolution(self.url)
                if res or not self.running:
                    break
                time.sleep(3)
            w, h = res or (1280, 720)
            if w > 1920:   # 4K-Hauptstream für die Erkennung verkleinern
                h, w = int(h * 1920 / w), 1920
        self.width, self.height = int(w) // 2 * 2, int(h) // 2 * 2
        W, H = self.width, self.height
        self.motion = MotionDetector((H, W), self.cam.motion.threshold, self.cam.motion.min_area, self.cam.masks)
        self.zones_px = {n: np.array([[x * W, y * H] for x, y in z.points], np.float32)
                         for n, z in self.cam.zones.items()}
        self.masks_px = [np.array([[x * W, y * H] for x, y in m], np.float32) for m in self.cam.masks]
        log.info("[%s] Erkennung mit %dx%d @ %d fps (%s)", self.cam.id, W, H, self.cam.detect.fps,
                 "NVDEC" if self.hwaccel else "CPU-Dekodierung")

    # ------------------------------------------------------------------ Dekodierung
    def _reader(self) -> None:
        self._setup_geometry()
        size = self.width * self.height * 3
        backoff = 2
        while self.running:
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
            if self.hwaccel:
                cmd += ["-hwaccel", "cuda", "-hwaccel_device", str(self.gpu_id)]
            if self.url.startswith("rtsp"):
                cmd += ["-rtsp_transport", "tcp", "-timeout", "10000000", "-fflags", "nobuffer", "-flags", "low_delay"]
            else:   # Datei/Testquelle
                cmd += ["-re", "-stream_loop", "-1"]
            cmd += ["-i", self.url, "-an", "-vf", f"fps={self.cam.detect.fps},scale={self.width}:{self.height}",
                    "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]
            got = 0
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=size)
                self.status = "connecting"
                buf = bytearray(size)
                view = memoryview(buf)
                while self.running:
                    n = 0
                    while n < size:
                        r = self.proc.stdout.readinto(view[n:])
                        if not r:
                            raise EOFError
                        n += r
                    frame = np.frombuffer(bytes(buf), np.uint8).reshape(self.height, self.width, 3)
                    got += 1
                    self.status = "online"
                    backoff = 2
                    with self._cv:
                        self._frame, self._frame_ts = frame, time.time()
                        self._frame_seq += 1
                        self._cv.notify_all()
                    self._dec_count += 1
            except EOFError:
                pass
            except Exception as e:
                log.warning("[%s] Leser: %s", self.cam.id, e)
            err = b""
            if self.proc:
                try:
                    self.proc.kill()
                    err = self.proc.stderr.read()[-400:] if self.proc.stderr else b""
                except Exception:
                    pass
            if not self.running:
                break
            self.status = "offline"
            if got == 0 and self.hwaccel:
                self.hw_failures += 1
                if self.hw_failures >= 3:
                    log.warning("[%s] NVDEC nicht nutzbar -> CPU-Dekodierung (%s)", self.cam.id,
                                err.decode(errors="ignore").strip()[-200:])
                    self.hwaccel = False
            elif err:
                log.warning("[%s] Stream getrennt: %s", self.cam.id, err.decode(errors="ignore").strip()[-200:])
            time.sleep(backoff)
            backoff = min(30, backoff * 2)

    # ------------------------------------------------------------------ Verarbeitung
    def _loop(self) -> None:
        seq = 0
        last_detect = 0.0
        while self.running:
            with self._cv:
                self._cv.wait_for(lambda: self._frame_seq != seq or not self.running, timeout=2)
                if self._frame_seq == seq:
                    self._stats()
                    continue
                seq, frame, ts = self._frame_seq, self._frame, self._frame_ts
            try:
                self._process(frame, ts, last_detect)
                if self._did_detect:
                    last_detect = ts
            except Exception:
                log.exception("[%s] Verarbeitung", self.cam.id)
            self._stats()

    _did_detect = False

    def _process(self, frame: np.ndarray, ts: float, last_detect: float) -> None:
        self._did_detect = False
        assert self.motion is not None
        self.motion_active = self.motion.detect(frame)
        if not self.enabled or self.detector is None:
            if self.tracker.tracks:
                ended, self.tracker.tracks = self.tracker.tracks, []
                self.events.process(self.cam.id, [], ended, frame, ts)
            self.last_tracks = []
            return
        has_tracks = bool(self.tracker.tracks)
        run = (self.motion_active or self.tracker.has_moving()
               or (has_tracks and ts - last_detect > 10) or ts - last_detect > 30)
        if not run:
            return
        self._did_detect = True
        self._det_count += 1
        dets = self.detector.detect(frame, self.min_score, self.labels)
        dets = [d for d in dets if self._keep(d)]
        tracks, ended = self.tracker.update(dets, ts)
        for t in tracks:
            cx, by = float((t.box[0] + t.box[2]) / 2), float(t.box[3])
            t.current_zones = {n for n, poly in self.zones_px.items()
                               if self._zone_ok(n, t.label) and cv2.pointPolygonTest(poly, (cx, by), False) >= 0}
            t.zones |= t.current_zones
            if (self.faces and self.cam.faces and t.label == "person" and t.confirmed
                    and not t.stationary and t.misses == 0):
                self.faces.submit(t, frame, self.cam.id)
            # require_zone: Ereignis nur, wenn das Objekt mindestens einmal in einer Zone war
            t.eligible = (not self.cam.require_zone) or bool(t.zones)
        self.events.process(self.cam.id, tracks, ended, frame, ts)
        self.last_tracks = [(t.label, t.group, t.box, t.score, t.id, t.stationary, t.confirmed,
                             t.face.name if (t.face and t.face.person_id) else None) for t in tracks]

    def _zone_ok(self, zone: str, label: str) -> bool:
        objs = self.cam.zones[zone].objects
        return not objs or label in objs

    def _keep(self, d: Detection) -> bool:
        f = self.cfg.objects.filter_for(d.label)
        if d.score < f.min_score or not (f.min_area <= d.area <= f.max_area):
            return False
        if self.masks_px:
            cx, by = float((d.box[0] + d.box[2]) / 2), float(d.box[3])
            if any(cv2.pointPolygonTest(m, (cx, by), False) >= 0 for m in self.masks_px):
                return False
        return True

    def _stats(self) -> None:
        now = time.time()
        dt = now - self._last_stat
        if dt >= 2:
            self.decode_fps = self._dec_count / dt
            self.detect_fps = self._det_count / dt
            self._dec_count = self._det_count = 0
            self._last_stat = now
            if now - self._frame_ts > 10 and self.status == "online":
                self.status = "stalled"

    # ------------------------------------------------------------------ Vorschau
    def latest_jpeg(self, annotate: bool = True, max_w: int = 960, quality: int = 70) -> bytes | None:
        with self._cv:
            frame = self._frame
        if frame is None:
            return None
        img = frame.copy()
        if annotate:
            H, W = img.shape[:2]
            for name, poly in self.zones_px.items():
                cv2.polylines(img, [poly.astype(np.int32)], True, (255, 120, 200), 2)
                cv2.putText(img, name, tuple(poly[0].astype(int) + [4, 18]), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                            (255, 120, 200), 2)
            for m in self.masks_px:
                overlay = img.copy()
                cv2.fillPoly(overlay, [m.astype(np.int32)], (40, 40, 40))
                img = cv2.addWeighted(overlay, 0.5, img, 0.5, 0)
            if self.motion and self.motion_active:
                for x1, y1, x2, y2 in self.motion.boxes:
                    cv2.rectangle(img, (int(x1 * W), int(y1 * H)), (int(x2 * W), int(y2 * H)), (60, 60, 230), 1)
            for label, grp, box, score, tid, stationary, confirmed, face in self.last_tracks:
                c = COLORS.get(grp, COLORS["other"])
                if not confirmed:
                    c = tuple(int(v * 0.5) for v in c)
                x1, y1, x2, y2 = [int(v) for v in box]
                cv2.rectangle(img, (x1, y1), (x2, y2), c, 2)
                txt = f"{face or label} {score:.0%}" + (" (steht)" if stationary else "")
                (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
                cv2.rectangle(img, (x1, max(0, y1 - th - 8)), (x1 + tw + 8, y1), c, -1)
                cv2.putText(img, txt, (x1 + 4, y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (20, 20, 20), 1, cv2.LINE_AA)
        if img.shape[1] > max_w:
            img = cv2.resize(img, (max_w, int(img.shape[0] * max_w / img.shape[1])), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes() if ok else None

    def latest_frame(self) -> np.ndarray | None:
        with self._cv:
            return self._frame

    def stats(self) -> dict:
        return {"status": self.status, "decode_fps": round(self.decode_fps, 1), "detect_fps": round(self.detect_fps, 1),
                "motion": self.motion_active, "enabled": self.enabled, "hwaccel": self.hwaccel,
                "resolution": [self.width, self.height], "tracks": len(self.last_tracks)}
