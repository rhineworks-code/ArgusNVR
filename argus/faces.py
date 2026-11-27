"""Lernende Gesichtserkennung.

Pipeline: Personen-Box -> SCRFD (Gesicht + 5 Landmarken) -> Ausrichtung 112x112 ->
ArcFace-Embedding (512-d) -> Vergleich mit Galerie (Cosinus).

Lernen:
  * Unbekannte Gesichter werden automatisch zu Gruppen (Clustern) zusammengefasst.
    Im UI benennst du eine Gruppe einmal -> alle Bilder werden der Person zugeordnet.
  * Sicher erkannte Gesichter (hohe Ähnlichkeit, gute Qualität, aber neuer Blickwinkel)
    werden automatisch als zusätzliche Referenz gelernt (Brille, Mütze, Licht, Jahreszeit).
  * Korrekturen im UI entfernen falsch gelernte Bilder sofort wieder.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

from .config import FacesCfg, Paths
from .db import Database
from .runtime import create_session

log = logging.getLogger("argus.faces")

ARC_DST = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                    [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)
GALLERY_SOURCES = ("manual", "auto", "confirmed")


# =============================================================================
# Modelle
# =============================================================================
class SCRFD:
    def __init__(self, path: str, device: str, gpu_id: int) -> None:
        self.session = create_session(path, device, gpu_id)
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        self.size = 640
        if isinstance(inp.shape[2], int) and inp.shape[2] > 0:
            self.size = int(inp.shape[2])
        self.output_names = [o.name for o in self.session.get_outputs()]
        n = len(self.output_names)
        self.fmc = 3
        self.strides = [8, 16, 32]
        self.use_kps = n in (9, 15)
        if n in (10, 15):
            self.fmc, self.strides = 5, [8, 16, 32, 64, 128]
        self.num_anchors = 2
        self._centers: dict[tuple[int, int], np.ndarray] = {}

    def detect(self, img: np.ndarray, thresh: float = 0.5) -> list[tuple[np.ndarray, float, np.ndarray]]:
        h, w = img.shape[:2]
        S = self.size
        if h / w > 1:
            nh, nw = S, max(1, int(S * w / h))
        else:
            nw, nh = S, max(1, int(S * h / w))
        scale = nh / h
        canvas = np.zeros((S, S, 3), np.uint8)
        canvas[:nh, :nw] = cv2.resize(img, (nw, nh))
        blob = cv2.dnn.blobFromImage(canvas, 1.0 / 128, (S, S), (127.5, 127.5, 127.5), swapRB=True)
        outs = self.session.run(self.output_names, {self.input_name: blob})
        boxes_all, scores_all, kps_all = [], [], []
        for i, stride in enumerate(self.strides):
            scores = outs[i].reshape(-1)
            bbox = outs[i + self.fmc].reshape(-1, 4) * stride
            kps = outs[i + self.fmc * 2].reshape(-1, 10) * stride if self.use_kps else None
            fh, fw = S // stride, S // stride
            key = (fh, stride)
            if key not in self._centers:
                c = np.stack(np.mgrid[:fh, :fw][::-1], axis=-1).astype(np.float32)
                c = (c * stride).reshape(-1, 2)
                c = np.repeat(c, self.num_anchors, axis=0) if self.num_anchors > 1 else c
                self._centers[key] = c
            centers = self._centers[key]
            pos = np.where(scores >= thresh)[0]
            if len(pos) == 0:
                continue
            c = centers[pos]
            b = bbox[pos]
            boxes = np.stack([c[:, 0] - b[:, 0], c[:, 1] - b[:, 1], c[:, 0] + b[:, 2], c[:, 1] + b[:, 3]], 1)
            boxes_all.append(boxes / scale)
            scores_all.append(scores[pos])
            if kps is not None:
                k = kps[pos].reshape(-1, 5, 2) + c[:, None, :]
                kps_all.append(k / scale)
        if not boxes_all:
            return []
        boxes = np.concatenate(boxes_all)
        scores = np.concatenate(scores_all)
        kpss = np.concatenate(kps_all) if kps_all else np.zeros((len(boxes), 5, 2), np.float32)
        rects = [[float(b[0]), float(b[1]), float(b[2] - b[0]), float(b[3] - b[1])] for b in boxes]
        keep = cv2.dnn.NMSBoxes(rects, scores.tolist(), thresh, 0.4)
        keep = np.array(keep).reshape(-1) if len(keep) else []
        return [(boxes[i], float(scores[i]), kpss[i]) for i in keep]


class ArcFace:
    def __init__(self, path: str, device: str, gpu_id: int) -> None:
        self.session = create_session(path, device, gpu_id)
        self.input_name = self.session.get_inputs()[0].name

    def embed(self, aligned: np.ndarray) -> np.ndarray:
        blob = cv2.dnn.blobFromImage(aligned, 1.0 / 127.5, (112, 112), (127.5, 127.5, 127.5), swapRB=True)
        e = self.session.run(None, {self.input_name: blob})[0][0].astype(np.float32)
        return e / (np.linalg.norm(e) + 1e-9)


def align(img: np.ndarray, kps: np.ndarray) -> np.ndarray:
    M, _ = cv2.estimateAffinePartial2D(kps.astype(np.float32), ARC_DST, method=cv2.LMEDS)
    if M is None:
        M = cv2.getAffineTransform(kps[:3].astype(np.float32), ARC_DST[:3])
    return cv2.warpAffine(img, M, (112, 112), borderValue=(0, 0, 0))


def face_quality(box: np.ndarray, kps: np.ndarray, det_score: float, aligned: np.ndarray) -> float:
    w = float(box[2] - box[0])
    size_f = min(1.0, w / 80.0)
    le, re, nose = kps[0], kps[1], kps[2]
    eye_d = max(1.0, float(np.linalg.norm(re - le)))
    mid = (le + re) / 2
    frontal = max(0.0, 1.0 - abs(float(nose[0] - mid[0])) / (eye_d * 0.6))
    gray = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
    sharp = min(1.0, cv2.Laplacian(gray, cv2.CV_64F).var() / 120.0)
    return float(det_score * (0.4 + 0.6 * size_f) * (0.3 + 0.7 * frontal) * (0.4 + 0.6 * sharp))


# =============================================================================
# Zustand pro verfolgter Person
# =============================================================================
@dataclass
class FaceSample:
    emb: np.ndarray
    quality: float
    img: np.ndarray


@dataclass
class FaceState:
    votes: dict[int, list[float]] = field(default_factory=dict)
    best: dict[int, tuple[float, FaceSample]] = field(default_factory=dict)  # person -> (sim, sample)
    unknown: list[FaceSample] = field(default_factory=list)
    best_face: FaceSample | None = None
    attempts: int = 0
    person_id: int | None = None
    name: str | None = None
    score: float = 0.0
    done: bool = False


# =============================================================================
# Engine
# =============================================================================
class FaceEngine:
    def __init__(self, cfg: FacesCfg, paths: Paths, db: Database, device: str, gpu_id: int) -> None:
        self.cfg, self.paths, self.db = cfg, paths, db
        self.det = SCRFD(str(paths.models / "face_det.onnx"), device, gpu_id)
        self.rec = ArcFace(str(paths.models / "face_rec.onnx"), device, gpu_id)
        self.provider = self.rec.session.get_providers()[0]
        self.lock = threading.RLock()
        self.q: "queue.Queue[tuple]" = queue.Queue(maxsize=12)
        self.G = np.zeros((0, 512), np.float32)
        self.G_pid = np.zeros((0,), np.int64)
        self.names: dict[int, str] = {}
        self.processed = 0
        self.dropped = 0
        self.reload()
        threading.Thread(target=self._worker, name="faces", daemon=True).start()

    # ------------------------------------------------------------------ Galerie
    def reload(self) -> None:
        rows = self.db.query(
            f"SELECT person_id, embedding FROM face_samples WHERE person_id IS NOT NULL "
            f"AND source IN ({','.join('?' * len(GALLERY_SOURCES))})", GALLERY_SOURCES)
        with self.lock:
            self.names = {r["id"]: r["name"] for r in self.db.query("SELECT id, name FROM persons")}
            if rows:
                self.G = np.stack([np.frombuffer(r["embedding"], np.float32) for r in rows])
                self.G_pid = np.array([r["person_id"] for r in rows], np.int64)
            else:
                self.G = np.zeros((0, 512), np.float32)
                self.G_pid = np.zeros((0,), np.int64)

    def match(self, emb: np.ndarray) -> list[tuple[int, float]]:
        """Liefert [(person_id, score)] absteigend. Score = Mittel der Top-3-Ähnlichkeiten."""
        with self.lock:
            if len(self.G) == 0:
                return []
            sims = self.G @ emb
            res = []
            for pid in np.unique(self.G_pid):
                s = np.sort(sims[self.G_pid == pid])[-3:]
                res.append((int(pid), float(s.mean() * 0.5 + s.max() * 0.5)))
        res.sort(key=lambda x: -x[1])
        return res

    # ------------------------------------------------------------------ Live-Verarbeitung
    def submit(self, track: Any, frame: np.ndarray, camera: str) -> None:
        st: FaceState = track.face or FaceState()
        track.face = st
        if st.done or st.attempts >= 25:
            return
        now = time.time()
        if now - track.last_face_ts < 0.4:
            return
        x1, y1, x2, y2 = [int(v) for v in track.box]
        bw, bh = x2 - x1, y2 - y1
        if bh < 2.5 * self.cfg.min_face_size or bw < self.cfg.min_face_size:
            return
        H, W = frame.shape[:2]
        px = int(bw * 0.15)
        cx1, cx2 = max(0, x1 - px), min(W, x2 + px)
        cy1, cy2 = max(0, y1 - int(bh * 0.05)), min(H, y1 + int(bh * 0.55))
        crop = frame[cy1:cy2, cx1:cx2].copy()
        track.last_face_ts = now
        try:
            self.q.put_nowait(("live", st, crop, camera))
            st.attempts += 1
        except queue.Full:
            self.dropped += 1

    def _worker(self) -> None:
        while True:
            job = self.q.get()
            try:
                if job[0] == "live":
                    self._process(job[1], job[2])
            except Exception:
                log.exception("Gesichtsverarbeitung")

    def analyze(self, img: np.ndarray) -> FaceSample | None:
        faces = self.det.detect(img, 0.5)
        if not faces:
            return None
        box, score, kps = max(faces, key=lambda f: (f[0][2] - f[0][0]) * f[1])
        if box[2] - box[0] < self.cfg.min_face_size:
            return None
        aligned = align(img, kps)
        q = face_quality(box, kps, score, aligned)
        return FaceSample(self.rec.embed(aligned), q, aligned)

    def _process(self, st: FaceState, crop: np.ndarray) -> None:
        fs = self.analyze(crop)
        self.processed += 1
        if fs is None or fs.quality < self.cfg.min_quality:
            return
        if st.best_face is None or fs.quality > st.best_face.quality:
            st.best_face = fs
        matches = self.match(fs.emb)
        thr = self.cfg.recognition_threshold
        if matches and matches[0][1] >= thr * 0.85:
            pid, sim = matches[0]
            st.votes.setdefault(pid, []).append(sim)
            prev = st.best.get(pid)
            if prev is None or fs.quality * sim > prev[1].quality * prev[0]:
                st.best[pid] = (sim, fs)
        else:
            st.unknown.append(fs)
            st.unknown.sort(key=lambda s: -s.quality)
            del st.unknown[3:]
        self._decide(st)

    def _decide(self, st: FaceState) -> None:
        thr = self.cfg.recognition_threshold
        best_pid, best_val = None, 0.0
        for pid, v in st.votes.items():
            mean = float(np.mean(v))
            ok = (len(v) >= 2 and mean >= thr) or max(v) >= thr + 0.08
            # Gegenstimmen: viele unbekannte Gesichter senken das Vertrauen
            if ok and len(v) >= len(st.unknown) and mean > best_val:
                best_pid, best_val = pid, mean
        if best_pid is not None:
            st.person_id, st.score = best_pid, best_val
            st.name = self.names.get(best_pid)
            if len(st.votes[best_pid]) >= 4 and best_val >= thr + 0.05:
                st.done = True

    # ------------------------------------------------------------------ Abschluss pro Ereignis
    def finalize(self, st: FaceState | None, event_id: str, camera: str) -> dict[str, Any]:
        """Lernen / Unbekannte gruppieren. Liefert Infos fürs Ereignis."""
        if st is None or st.best_face is None:
            return {}
        out: dict[str, Any] = {}
        now = time.time()
        if st.person_id is not None:
            sim, fs = st.best.get(st.person_id, (st.score, st.best_face))
            learn = (self.cfg.auto_learn and sim >= self.cfg.auto_learn_threshold
                     and fs.quality >= 0.5 and self._novel(st.person_id, fs.emb))
            src = "auto" if learn else "event"
            sid = self._save_sample(fs, st.person_id if learn else None, None, src, event_id, camera)
            if learn:
                self._enforce_cap(st.person_id)
                self.reload()
                log.info("Gesicht von %s dazugelernt (Ähnlichkeit %.2f)", st.name, sim)
            out.update(sub_label=st.name, sub_score=round(st.score, 3), person_id=st.person_id,
                       face_thumb=self._sample_path(sid))
        else:
            fs = st.unknown[0] if st.unknown else st.best_face
            cid = self._assign_cluster(fs.emb, now)
            sid = self._save_sample(fs, None, cid, "unknown", event_id, camera)
            self._trim_cluster(cid)
            out.update(face_thumb=self._sample_path(sid))
        return out

    def _novel(self, pid: int, emb: np.ndarray) -> bool:
        with self.lock:
            sims = self.G[self.G_pid == pid] @ emb if len(self.G) else np.array([])
        return len(sims) == 0 or float(sims.max()) < 0.88

    def _save_sample(self, fs: FaceSample, pid: int | None, cid: int | None, source: str,
                     event_id: str | None, camera: str | None) -> int:
        name = f"{uuid.uuid4().hex}.jpg"
        cv2.imwrite(str(self.paths.faces / name), fs.img, [cv2.IMWRITE_JPEG_QUALITY, 88])
        cur = self.db.execute(
            "INSERT INTO face_samples (person_id, cluster_id, embedding, quality, path, source, event_id, camera, created)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, cid, fs.emb.astype(np.float32).tobytes(), fs.quality, name, source, event_id, camera, time.time()))
        return int(cur.lastrowid)

    def _sample_path(self, sid: int) -> str | None:
        r = self.db.one("SELECT path FROM face_samples WHERE id=?", (sid,))
        return f"faces/{r['path']}" if r else None

    def _assign_cluster(self, emb: np.ndarray, now: float) -> int:
        rows = self.db.query("SELECT id, centroid, count FROM face_clusters")
        best, best_sim = None, 0.0
        for r in rows:
            s = float(np.frombuffer(r["centroid"], np.float32) @ emb)
            if s > best_sim:
                best, best_sim = r, s
        if best is not None and best_sim >= self.cfg.unknown_cluster_threshold:
            c = np.frombuffer(best["centroid"], np.float32) * best["count"] + emb
            c = (c / (np.linalg.norm(c) + 1e-9)).astype(np.float32)
            self.db.execute("UPDATE face_clusters SET centroid=?, count=count+1, last_seen=? WHERE id=?",
                            (c.tobytes(), now, best["id"]))
            return int(best["id"])
        cur = self.db.execute("INSERT INTO face_clusters (centroid, count, first_seen, last_seen) VALUES (?,1,?,?)",
                              (emb.astype(np.float32).tobytes(), now, now))
        return int(cur.lastrowid)

    def _trim_cluster(self, cid: int, keep: int = 16) -> None:
        rows = self.db.query("SELECT id, path FROM face_samples WHERE cluster_id=? ORDER BY quality DESC", (cid,))
        for r in rows[keep:]:
            self._delete_sample_row(r)

    def _enforce_cap(self, pid: int) -> None:
        rows = self.db.query("SELECT id, path, embedding, source FROM face_samples WHERE person_id=? "
                             "AND source IN ('manual','auto','confirmed')", (pid,))
        while len(rows) > self.cfg.max_samples_per_person:
            autos = [r for r in rows if r["source"] == "auto"]
            if not autos:
                break
            E = np.stack([np.frombuffer(r["embedding"], np.float32) for r in rows])
            red = E @ E.T
            np.fill_diagonal(red, 0)
            score = {rows[i]["id"]: red[i].mean() for i in range(len(rows))}
            victim = max(autos, key=lambda r: score[r["id"]])   # am wenigsten neue Info
            self._delete_sample_row(victim)
            rows = [r for r in rows if r["id"] != victim["id"]]

    def _delete_sample_row(self, r: dict) -> None:
        if r.get("path"):
            (self.paths.faces / r["path"]).unlink(missing_ok=True)
        self.db.execute("DELETE FROM face_samples WHERE id=?", (r["id"],))

    # ------------------------------------------------------------------ Verwaltung (API)
    def person_id_for(self, person_id: int | None, name: str | None) -> int | None:
        if person_id:
            return int(person_id)
        if name and name.strip():
            name = name.strip()
            r = self.db.one("SELECT id FROM persons WHERE name=?", (name,))
            if r:
                return int(r["id"])
            cur = self.db.execute("INSERT INTO persons (name, created) VALUES (?,?)", (name, time.time()))
            self.reload()
            return int(cur.lastrowid)
        return None

    def assign_cluster(self, cid: int, pid: int) -> int:
        name = self.db.one("SELECT name FROM persons WHERE id=?", (pid,))["name"]
        rows = self.db.query("SELECT id, event_id FROM face_samples WHERE cluster_id=?", (cid,))
        self.db.execute("UPDATE face_samples SET person_id=?, cluster_id=NULL, source='confirmed' WHERE cluster_id=?",
                        (pid, cid))
        self.db.execute("DELETE FROM face_clusters WHERE id=?", (cid,))
        evs = [r["event_id"] for r in rows if r["event_id"]]
        for e in evs:
            self.db.execute("UPDATE events SET sub_label=?, person_id=? WHERE id=?", (name, pid, e))
        self._enforce_cap(pid)
        self.reload()
        return len(rows)

    def delete_cluster(self, cid: int) -> None:
        for r in self.db.query("SELECT id, path FROM face_samples WHERE cluster_id=?", (cid,)):
            self._delete_sample_row(r)
        self.db.execute("DELETE FROM face_clusters WHERE id=?", (cid,))

    def assign_sample(self, sid: int, pid: int | None) -> None:
        if pid is None:
            r = self.db.one("SELECT id, path FROM face_samples WHERE id=?", (sid,))
            if r:
                self._delete_sample_row(r)
        else:
            self.db.execute("UPDATE face_samples SET person_id=?, cluster_id=NULL, source='confirmed' WHERE id=?",
                            (pid, sid))
        self.reload()

    def correct_event(self, event_id: str, pid: int | None) -> dict[str, Any]:
        """Korrektur aus dem UI: falsch gelernte Bilder entfernen, richtige Person lernen."""
        samples = self.db.query("SELECT id, path, person_id, source FROM face_samples WHERE event_id=?", (event_id,))
        name = None
        for r in samples:
            if pid is None:
                if r["source"] in ("auto", "event", "unknown"):
                    self._delete_sample_row(r)
            else:
                self.db.execute("UPDATE face_samples SET person_id=?, cluster_id=NULL, source='confirmed' WHERE id=?",
                                (pid, r["id"]))
        if pid is not None:
            name = self.db.one("SELECT name FROM persons WHERE id=?", (pid,))["name"]
            self._enforce_cap(pid)
        self.db.execute("UPDATE events SET sub_label=?, person_id=?, sub_score=NULL WHERE id=?", (name, pid, event_id))
        self.reload()
        return {"sub_label": name, "person_id": pid}

    def enroll_image(self, data: bytes, pid: int) -> bool:
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return False
        fs = self.analyze(img)
        if fs is None:
            return False
        self._save_sample(fs, pid, None, "manual", None, None)
        self.reload()
        return True

    def delete_person(self, pid: int) -> None:
        for r in self.db.query("SELECT id, path FROM face_samples WHERE person_id=?", (pid,)):
            self._delete_sample_row(r)
        self.db.execute("UPDATE events SET sub_label=NULL, person_id=NULL WHERE person_id=?", (pid,))
        self.db.execute("DELETE FROM persons WHERE id=?", (pid,))
        self.reload()

    def rename_person(self, pid: int, name: str) -> None:
        self.db.execute("UPDATE persons SET name=? WHERE id=?", (name.strip(), pid))
        self.db.execute("UPDATE events SET sub_label=? WHERE person_id=?", (name.strip(), pid))
        self.reload()

    def cleanup(self, max_age_days: int = 30) -> None:
        """Alte Einzel-Unbekannte und Referenzbilder ohne Ereignis aufräumen."""
        cutoff = time.time() - max_age_days * 86400
        for c in self.db.query("SELECT id FROM face_clusters WHERE count<=1 AND last_seen<?", (cutoff,)):
            self.delete_cluster(c["id"])
        for r in self.db.query("SELECT s.id, s.path FROM face_samples s LEFT JOIN events e ON e.id=s.event_id "
                               "WHERE s.source='event' AND e.id IS NULL"):
            self._delete_sample_row(r)
