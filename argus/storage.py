"""Aufbewahrung: alte Aufnahmen löschen, harte Speicher-Obergrenze einhalten."""
from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path

from .config import Config, Paths
from .db import Database

log = logging.getLogger("argus.storage")


def dir_size(p: Path) -> int:
    total = 0
    if p.exists():
        for f in p.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                pass
    return total


class Retention(threading.Thread):
    def __init__(self, cfg: Config, paths: Paths, db: Database, faces=None, interval: int = 600) -> None:
        super().__init__(name="retention", daemon=True)
        self.cfg, self.paths, self.db, self.faces = cfg, paths, db, faces
        self.interval = interval
        self.usage: dict = {}

    def run(self) -> None:
        time.sleep(30)
        while True:
            try:
                self.cleanup()
            except Exception:
                log.exception("Aufräumen")
            time.sleep(self.interval)

    def _delete_event(self, ev: dict) -> None:
        for k in ("snapshot", "thumb"):
            if ev.get(k):
                (self.paths.media / ev[k]).unlink(missing_ok=True)
        self.db.execute("DELETE FROM events WHERE id=?", (ev["id"],))

    def _delete_clip(self, c: dict) -> None:
        (self.paths.media / c["path"]).unlink(missing_ok=True)
        self.db.execute("DELETE FROM clips WHERE id=?", (c["id"],))

    def cleanup(self) -> None:
        r = self.cfg.record.retention
        now = time.time()
        cutoff = now - r.events_days * 86400
        for ev in self.db.query("SELECT id, snapshot, thumb FROM events WHERE start_ts < ?", (cutoff,)):
            self._delete_event(ev)
        for c in self.db.query("SELECT id, path FROM clips WHERE end_ts < ?", (cutoff,)):
            self._delete_clip(c)
        # Daueraufnahme: ganze Tagesordner
        cont_cut = time.strftime("%Y-%m-%d", time.localtime(now - r.continuous_days * 86400))
        for cam_dir in self.paths.recordings.glob("*"):
            for day in cam_dir.glob("*"):
                if day.name < cont_cut:
                    shutil.rmtree(day, ignore_errors=True)
        # harte Obergrenze
        limit = r.max_storage_gb * 1e9
        disk = shutil.disk_usage(self.paths.media)
        used = self.measure()["total"]
        min_free = 0.05 * disk.total
        while used > limit * 0.98 or shutil.disk_usage(self.paths.media).free < min_free:
            freed = self._delete_oldest()
            if freed <= 0:
                break
            used -= freed
        # leere Ordner entfernen
        for base in (self.paths.clips, self.paths.snapshots, self.paths.recordings):
            for d in sorted(base.glob("*/*"), reverse=True):
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
        if self.faces:
            self.faces.cleanup()
        self.measure()

    def _delete_oldest(self) -> int:
        """Löscht die älteste Einheit: Daueraufnahme-Tag oder ältester Clip."""
        days = sorted((d for c in self.paths.recordings.glob("*") for d in c.glob("*")), key=lambda d: d.name)
        clip = self.db.one("SELECT id, path, bytes, start_ts FROM clips ORDER BY start_ts LIMIT 1")
        if days and (clip is None or days[0].name <= time.strftime("%Y-%m-%d", time.localtime(clip["start_ts"]))):
            size = dir_size(days[0])
            shutil.rmtree(days[0], ignore_errors=True)
            log.info("Speichergrenze: Daueraufnahme %s gelöscht", days[0])
            return size or 1
        if clip:
            self._delete_clip(clip)
            for ev in self.db.query("SELECT id, snapshot, thumb FROM events WHERE end_ts <= ?", (clip["start_ts"] + 1,)):
                self._delete_event(ev)
            log.info("Speichergrenze: Clip %s gelöscht", clip["path"])
            return clip["bytes"] or 1
        return 0

    def measure(self) -> dict:
        clips = self.db.one("SELECT COALESCE(SUM(bytes),0) AS b, COUNT(*) AS n FROM clips") or {"b": 0, "n": 0}
        u = {
            "clips": int(clips["b"]), "clip_count": int(clips["n"]),
            "recordings": dir_size(self.paths.recordings),
            "snapshots": dir_size(self.paths.snapshots),
            "faces": dir_size(self.paths.faces),
        }
        u["total"] = sum(v for k, v in u.items() if k != "clip_count")
        d = shutil.disk_usage(self.paths.media)
        u.update(disk_total=d.total, disk_free=d.free, limit=int(self.cfg.record.retention.max_storage_gb * 1e9))
        self.usage = u
        return u
