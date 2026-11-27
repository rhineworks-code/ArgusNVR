"""Aufnahme ohne Neukodierung, als Segmente mit Zeitindex.

ffmpeg kopiert den Stream (-c copy) in 2-Sekunden-Segmente in einen RAM-Puffer (tmpfs).
* Modus „events“: nur Segmente rund um Ereignisse (inkl. Vorlauf aus dem RAM) werden gespeichert.
* Modus „continuous“: alle Segmente werden gespeichert; Ereignis-Segmente werden markiert
  und können länger aufbewahrt werden als der Rest.
Jedes Segment steht mit Start/Ende in der Datenbank -> Zeitachse, HLS-Wiedergabe und
MP4-Export beliebiger Zeiträume, ohne doppelten Speicher.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import CameraCfg, Config, Paths
from .db import Database

log = logging.getLogger("camspire.recorder")

SEG_TIME = 2
FMT = "%Y%m%d%H%M%S"


def seg_start(p: Path) -> float | None:
    try:
        return time.mktime(time.strptime(p.stem, FMT))
    except ValueError:
        return None


@dataclass
class Window:
    start: float
    active: set[str] = field(default_factory=set)
    close_after: float | None = None


class CameraRecorder:
    def __init__(self, cfg: Config, cam: CameraCfg, paths: Paths, db: Database, storage) -> None:
        self.cfg, self.cam, self.paths, self.db, self.storage = cfg, cam, paths, db, storage
        self.rc = cfg.record
        self.cache = paths.cache / cam.id
        self.window: Window | None = None
        self.lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.running = False
        self.last_segment_ts = 0.0
        self.restarts = 0
        self.stored = 0
        self._ends: dict[str, float | None] = {}

    @property
    def mode(self) -> str:
        return self.cam.record_mode_effective(self.cfg.record)

    @property
    def session(self):          # für Statistik/Kompatibilität
        return self.window

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.running = True
        shutil.rmtree(self.cache, ignore_errors=True)
        self.cache.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self._segmenter, name=f"rec-{self.cam.id}", daemon=True).start()

    def stop(self) -> None:
        self.running = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
        with self.lock:
            if self.window:
                self.window.active.clear()
                self.window.close_after = self.window.close_after or time.time()
            try:
                self.tick(final=True)       # letzte Segmente aus dem RAM übernehmen
            except Exception:
                log.exception("[%s] letzte Segmente", self.cam.id)
            self.window = None

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
                    "-segment_format", "mpegts", "-strftime", "1", str(self.cache / f"{FMT}.ts")]
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

    # ------------------------------------------------------------------ Ereignisse
    def event_started(self, event_id: str, ts: float) -> None:
        if self.mode == "off":
            return
        start = ts - self.rc.pre_roll
        with self.lock:
            if self.window is None:
                self.window = Window(start)
            self.window.active.add(event_id)
            self.window.close_after = None
        # bereits gespeicherte Segmente (Daueraufnahme) als Ereignis markieren
        self.db.execute("UPDATE segments SET event=1 WHERE camera=? AND end_ts>? AND event=0", (self.cam.id, start))

    def event_ended(self, event_id: str, ts: float) -> None:
        with self.lock:
            w = self.window
            if w is None:
                return
            w.active.discard(event_id)
            if not w.active:
                w.close_after = max(ts, time.time() - 1) + self.rc.post_roll

    # ------------------------------------------------------------------ Janitor (1×/s)
    def tick(self, now: float | None = None, final: bool = False) -> None:
        now = now or time.time()
        files = sorted(self.cache.glob("*.ts"))
        if files:
            try:
                self.last_segment_ts = files[-1].stat().st_mtime
            except OSError:
                pass
        mode = self.mode
        with self.lock:
            for i, p in enumerate(files[:-1]):
                if p.name not in self._ends:
                    self._ends[p.name] = seg_start(files[i + 1])
            complete = files if final else files[:-1]
            for p in complete:
                s = seg_start(p)
                e = self._ends.get(p.name) or (s + SEG_TIME if final and s else None)
                if s is None or e is None:
                    p.unlink(missing_ok=True)
                    continue
                w = self.window
                in_event = bool(w and e > w.start and (w.close_after is None or s < w.close_after))
                if in_event or mode == "continuous":
                    self._store(p, s, e, in_event)
                elif now - e > self.rc.pre_roll + 2 * SEG_TIME + 2 or final:
                    p.unlink(missing_ok=True)
            alive = {p.name for p in files}
            for k in [k for k in self._ends if k not in alive]:
                del self._ends[k]
            w = self.window
            if w and not w.active and w.close_after is not None and now > w.close_after + 2 * SEG_TIME + 2:
                self.window = None

    def _store(self, src: Path, start: float, end: float, event: bool) -> None:
        lt = time.localtime(start)
        rel = f"recordings/{self.cam.id}/{time.strftime('%Y-%m-%d', lt)}/{time.strftime('%H', lt)}/{src.name}"
        root, base = self.storage.target()
        size = src.stat().st_size
        try:
            dst = base / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), dst)
        except OSError as e:
            if root == "local":
                log.error("[%s] Segment konnte nicht gespeichert werden: %s", self.cam.id, e)
                src.unlink(missing_ok=True)
                return
            self.storage.report_failure(str(e))
            root, base = "local", self.paths.media
            dst = base / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), dst)
        self.db.execute("INSERT INTO segments (camera,start_ts,end_ts,root,path,bytes,event) VALUES (?,?,?,?,?,?,?)",
                        (self.cam.id, start, end, root, rel, size, 1 if event else 0))
        self.stored += 1


# ---------------------------------------------------------------------- Abfragen & Export
def query_segments(db: Database, camera: str, start: float, end: float, limit: int = 5000) -> list[dict]:
    return db.query("SELECT id, start_ts, end_ts, root, path, bytes, event FROM segments "
                    "WHERE camera=? AND end_ts>? AND start_ts<? ORDER BY start_ts LIMIT ?",
                    (camera, start, end, limit))


def merge_ranges(segs: list[dict], gap: float = 1.5) -> list[list]:
    out: list[list] = []
    for s in segs:
        if out and s["start_ts"] - out[-1][1] <= gap and out[-1][2] == s["event"]:
            out[-1][1] = max(out[-1][1], s["end_ts"])
        else:
            out.append([s["start_ts"], s["end_ts"], s["event"]])
    return out


def hls_playlist(segs: list[dict], url_for) -> str:
    import math
    from datetime import datetime, timezone
    target = max([math.ceil(s["end_ts"] - s["start_ts"]) for s in segs] + [SEG_TIME])
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{target}", "#EXT-X-MEDIA-SEQUENCE:0",
             "#EXT-X-PLAYLIST-TYPE:VOD"]
    prev_end = None
    for s in segs:
        if prev_end is not None and abs(s["start_ts"] - prev_end) > 1.5:
            lines.append("#EXT-X-DISCONTINUITY")
        dt = datetime.fromtimestamp(s["start_ts"], tz=timezone.utc).isoformat(timespec="milliseconds")
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{dt.replace('+00:00', 'Z')}")
        lines.append(f"#EXTINF:{s['end_ts'] - s['start_ts']:.3f},")
        lines.append(url_for(s))
        prev_end = s["end_ts"]
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def export_mp4(segs: list[dict], resolve, out: Path) -> Path | None:
    files = [resolve(s["root"], s["path"]) for s in segs]
    files = [f for f in files if f.exists()]
    if not files:
        return None
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{f.resolve()}'\n" for f in files))
    codec = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
                            "-of", "csv=p=0", str(files[0])], capture_output=True, text=True, timeout=15).stdout.strip()
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0",
           "-i", str(lst), "-c", "copy", "-movflags", "+faststart"]
    if codec in ("hevc", "h265"):
        cmd += ["-tag:v", "hvc1"]
    subprocess.run(cmd + [str(out)], capture_output=True, timeout=900)
    lst.unlink(missing_ok=True)
    return out if out.exists() and out.stat().st_size > 0 else None


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
