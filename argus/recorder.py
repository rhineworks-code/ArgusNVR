"""Aufnahme ohne Neukodierung.

ffmpeg kopiert den Stream (-c copy) in 2-Sekunden-Segmente in einen RAM-Puffer (tmpfs).
Nur wenn ein Ereignis läuft, werden die Segmente (inkl. Vorlauf) auf die Platte
übernommen und am Ende verlustfrei zu einer MP4 zusammengefügt. Überlappende
Ereignisse einer Kamera teilen sich einen Clip -> kein doppelter Speicher.
"""
from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import CameraCfg, Config, Paths
from .db import Database

log = logging.getLogger("argus.recorder")

SEG_TIME = 2
FMT = "%Y%m%d%H%M%S"


def seg_start(p: Path) -> float | None:
    try:
        return time.mktime(time.strptime(p.stem, FMT))
    except ValueError:
        return None


@dataclass
class Session:
    id: str
    start: float
    dir: Path
    active: set[str] = field(default_factory=set)
    close_after: float | None = None
    segs: list[tuple[float, float, Path]] = field(default_factory=list)


class CameraRecorder:
    def __init__(self, cfg: Config, cam: CameraCfg, paths: Paths, db: Database,
                 on_clip: Callable[[dict], None] | None = None) -> None:
        self.cfg, self.cam, self.paths, self.db = cfg, cam, paths, db
        self.rc = cfg.record
        self.cache = paths.cache / cam.id
        self.tmp_root = paths.clips / ".tmp"
        self.on_clip = on_clip
        self.session: Session | None = None
        self.lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.running = False
        self.last_segment_ts = 0.0
        self.restarts = 0
        self.codec: str | None = None
        self._finalize_q: "queue.Queue[Session]" = queue.Queue()
        self._ends: dict[str, float | None] = {}

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.running = True
        shutil.rmtree(self.cache, ignore_errors=True)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.tmp_root.mkdir(parents=True, exist_ok=True)
        self._recover()
        threading.Thread(target=self._segmenter, name=f"rec-{self.cam.id}", daemon=True).start()
        threading.Thread(target=self._finalizer, name=f"clip-{self.cam.id}", daemon=True).start()

    def stop(self) -> None:
        self.running = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
        with self.lock:
            sess = self.session
            if sess:
                sess.active.clear()
                sess.close_after = sess.close_after or time.time()
                try:
                    self.tick()          # letzte Segmente aus dem RAM übernehmen
                except Exception:
                    pass
                self.session = None
                if sess.segs:
                    self._finalize(sess)
                else:
                    shutil.rmtree(sess.dir, ignore_errors=True)
        self._finalize_q.put(None)       # Finalizer-Thread beenden

    def _segmenter(self) -> None:
        src = f"rtsp://127.0.0.1:8554/{self.cam.record_stream(self.rc.source)}"
        while self.running:
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                   "-rtsp_transport", "tcp", "-timeout", "10000000", "-fflags", "+genpts",
                   "-i", src, "-map", "0:v:0"]
            if self.rc.audio:
                cmd += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "48k"]
            else:
                cmd += ["-an"]
            cmd += ["-c:v", "copy", "-f", "segment", "-segment_time", str(SEG_TIME),
                    "-segment_format", "mpegts", "-reset_timestamps", "1", "-strftime", "1",
                    str(self.cache / f"{FMT}.ts")]
            t0 = time.time()
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                _, err = self.proc.communicate()
                if time.time() - t0 > 60:
                    self.restarts = 0
                if self.running:
                    msg = (err or b"").decode(errors="ignore").strip().splitlines()[-1:] or [""]
                    log.warning("[%s] Aufnahme-Stream beendet: %s", self.cam.id, msg[0])
            except Exception as e:
                log.error("[%s] ffmpeg Aufnahme: %s", self.cam.id, e)
            self.restarts += 1
            time.sleep(min(30, 2 + self.restarts))

    # ------------------------------------------------------------------ events
    def event_started(self, event_id: str, ts: float) -> None:
        if not self.cam.record or self.rc.mode == "off":
            return
        with self.lock:
            if self.session is None:
                sid = f"{int(ts)}-{uuid.uuid4().hex[:6]}"
                d = self.tmp_root / f"{self.cam.id}__{sid}"
                d.mkdir(parents=True, exist_ok=True)
                self.session = Session(sid, ts - self.rc.pre_roll, d)
                log.info("[%s] Aufnahme gestartet (%s)", self.cam.id, sid)
            self.session.active.add(event_id)
            self.session.close_after = None

    def event_ended(self, event_id: str, ts: float) -> None:
        with self.lock:
            s = self.session
            if s is None:
                return
            s.active.discard(event_id)
            if not s.active:
                s.close_after = max(ts, time.time() - 1) + self.rc.post_roll

    # ------------------------------------------------------------------ janitor
    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        files = sorted(p for p in self.cache.glob("*.ts"))
        if files:
            self.last_segment_ts = files[-1].stat().st_mtime
        with self.lock:
            # Segment-Ende = Start des Nachfolgers; einmal gemerkt, damit es stimmt,
            # auch wenn der Nachfolger schon in einen Clip verschoben wurde
            for i, p in enumerate(files[:-1]):
                if p.name not in self._ends:
                    self._ends[p.name] = seg_start(files[i + 1])
            for p in files[:-1]:                    # letztes Segment wird noch geschrieben
                s = seg_start(p)
                e = self._ends.get(p.name)
                if s is None or e is None:
                    p.unlink(missing_ok=True)
                    continue
                sess = self.session
                if sess and e > sess.start and (sess.close_after is None or s < sess.close_after):
                    dst = sess.dir / p.name
                    shutil.move(str(p), dst)
                    sess.segs.append((s, e, dst))
                    if self.rc.mode == "continuous":
                        self._keep_continuous(dst, s, link=True)
                    if e - sess.start >= self.rc.max_clip_length:
                        self._rotate(sess, e)
                elif now - e > self.rc.pre_roll + 2 * SEG_TIME + 2:
                    if self.rc.mode == "continuous" and self.cam.record:
                        self._keep_continuous(p, s, link=False)
                    else:
                        p.unlink(missing_ok=True)
            alive = {p.name for p in files}
            for k in [k for k in self._ends if k not in alive]:
                del self._ends[k]
            sess = self.session
            if sess and not sess.active and sess.close_after is not None:
                covered = bool(sess.segs) and sess.segs[-1][1] >= sess.close_after
                if covered or now > sess.close_after + 20:
                    self.session = None
                    self._finalize_q.put(sess)

    def _rotate(self, sess: Session, at: float) -> None:
        """Maximale Clip-Länge erreicht: Clip abschließen, nahtlos neuen beginnen."""
        self._finalize_q.put(sess)
        sid = f"{int(at)}-{uuid.uuid4().hex[:6]}"
        d = self.tmp_root / f"{self.cam.id}__{sid}"
        d.mkdir(parents=True, exist_ok=True)
        self.session = Session(sid, at, d, active=set(sess.active), close_after=sess.close_after)

    def _keep_continuous(self, src: Path, start: float, link: bool) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(start))
        d = self.paths.recordings / self.cam.id / day
        d.mkdir(parents=True, exist_ok=True)
        dst = d / src.name
        try:
            if link:
                os.link(src, dst)
            else:
                shutil.move(str(src), dst)
        except OSError:
            shutil.copy2(src, dst)
            if not link:
                src.unlink(missing_ok=True)

    # ------------------------------------------------------------------ clips
    def _finalizer(self) -> None:
        while True:
            sess = self._finalize_q.get()
            if sess is None:
                return
            try:
                self._finalize(sess)
            except Exception:
                log.exception("[%s] Clip konnte nicht erstellt werden", self.cam.id)

    def _probe_codec(self, f: Path) -> str:
        if self.codec:
            return self.codec
        try:
            out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                  "stream=codec_name", "-of", "csv=p=0", str(f)],
                                 capture_output=True, text=True, timeout=15).stdout.strip()
            self.codec = out or "h264"
        except Exception:
            self.codec = "h264"
        return self.codec

    def _finalize(self, sess: Session) -> None:
        segs = sorted(sess.segs)
        if not segs:
            shutil.rmtree(sess.dir, ignore_errors=True)
            return
        start, end = segs[0][0], segs[-1][1]
        day = time.strftime("%Y-%m-%d", time.localtime(start))
        out_dir = self.paths.clips / self.cam.id / day
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{time.strftime('%H%M%S', time.localtime(start))}_{sess.id}.mp4"
        lst = sess.dir / "list.txt"
        lst.write_text("".join(f"file '{p.name}'\n" for _, _, p in segs))
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart"]
        if self._probe_codec(segs[0][2]) in ("hevc", "h265"):
            cmd += ["-tag:v", "hvc1"]
        cmd.append(str(out))
        r = subprocess.run(cmd, capture_output=True, timeout=300)
        if r.returncode != 0 or not out.exists():
            log.error("[%s] concat fehlgeschlagen: %s", self.cam.id, r.stderr.decode(errors="ignore")[-300:])
            return
        size = out.stat().st_size
        clip = {"id": sess.id, "camera": self.cam.id, "start_ts": start, "end_ts": end,
                "path": str(out.relative_to(self.paths.media)), "bytes": size}
        self.db.execute("INSERT OR REPLACE INTO clips (id,camera,start_ts,end_ts,path,bytes) VALUES (?,?,?,?,?,?)",
                        (clip["id"], clip["camera"], start, end, clip["path"], size))
        shutil.rmtree(sess.dir, ignore_errors=True)
        log.info("[%s] Clip gespeichert: %s (%.0fs, %.1f MB)", self.cam.id, out.name, end - start, size / 1e6)
        if self.on_clip:
            self.on_clip(clip)

    def _recover(self) -> None:
        """Nach Absturz/Neustart liegengebliebene Sitzungen noch zu Clips machen."""
        for d in self.tmp_root.glob(f"{self.cam.id}__*"):
            segs = []
            files = sorted(d.glob("*.ts"))
            for i, f in enumerate(files):
                s = seg_start(f)
                if s is None:
                    continue
                e = seg_start(files[i + 1]) if i + 1 < len(files) else s + SEG_TIME
                segs.append((s, e or s + SEG_TIME, f))
            sess = Session(d.name.split("__", 1)[1], segs[0][0] if segs else 0, d, segs=segs)
            self._finalize_q.put(sess)

    # ------------------------------------------------------------------ export (Daueraufnahme)
    def export(self, start: float, end: float) -> Path | None:
        files = []
        for day_dir in sorted((self.paths.recordings / self.cam.id).glob("*")):
            for f in sorted(day_dir.glob("*.ts")):
                s = seg_start(f)
                if s is not None and start - SEG_TIME <= s <= end:
                    files.append(f)
        if not files:
            return None
        out = self.paths.cache / f"export_{self.cam.id}_{int(start)}.mp4"
        lst = self.paths.cache / f"export_{self.cam.id}_{int(start)}.txt"
        lst.write_text("".join(f"file '{f}'\n" for f in files))
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
               "-i", str(lst), "-c", "copy", "-movflags", "+faststart"]
        if self._probe_codec(files[0]) in ("hevc", "h265"):
            cmd += ["-tag:v", "hvc1"]
        subprocess.run(cmd + [str(out)], capture_output=True, timeout=600)
        lst.unlink(missing_ok=True)
        return out if out.exists() else None


class RecordingJanitor(threading.Thread):
    def __init__(self, recorders: dict[str, CameraRecorder]) -> None:
        super().__init__(name="rec-janitor", daemon=True)
        self.recorders = recorders      # dict wird beim Live-Reload verändert

    def run(self) -> None:
        while True:
            for r in list(self.recorders.values()):
                if not r.running:
                    continue
                try:
                    r.tick()
                except Exception:
                    log.exception("[%s] janitor", r.cam.id)
            time.sleep(1.0)
