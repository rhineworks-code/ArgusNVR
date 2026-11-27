"""Aufbewahrung: alte Aufnahmen löschen, harte Speicher-Obergrenze einhalten."""
from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path

from .config import Config, Paths
from .db import Database

log = logging.getLogger("camspire.storage")


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
    def __init__(self, cfg: Config, paths: Paths, db: Database, faces=None, storage=None, interval: int = 300) -> None:
        super().__init__(name="retention", daemon=True)
        self.cfg, self.paths, self.db, self.faces, self.storage = cfg, paths, db, faces, storage
        self.interval = interval
        self.usage: dict = {}
        self._small: dict = {}
        self._small_ts = 0.0

    def run(self) -> None:
        time.sleep(30)
        while True:
            try:
                self.cleanup()
            except Exception:
                log.exception("Aufräumen")
            time.sleep(self.interval)

    # ------------------------------------------------------------------ Löschen
    def _resolve(self, root: str, rel: str) -> Path:
        if self.storage:
            return self.storage.resolve(root, rel)
        return self.paths.media / rel

    def _delete_segments(self, rows: list[dict]) -> int:
        freed = 0
        for r in rows:
            try:
                self._resolve(r["root"], r["path"]).unlink(missing_ok=True)
            except OSError:
                pass
            freed += r.get("bytes") or 0
        ids = [r["id"] for r in rows]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            self.db.execute(f"DELETE FROM segments WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        return freed

    def _delete_event(self, ev: dict) -> None:
        for k in ("snapshot", "thumb"):
            if ev.get(k):
                (self.paths.media / ev[k]).unlink(missing_ok=True)
        self.db.execute("DELETE FROM events WHERE id=?", (ev["id"],))

    def cleanup(self) -> None:
        g = self.cfg.record.retention
        now = time.time()
        cams = {r["camera"] for r in self.db.query("SELECT DISTINCT camera FROM segments")} | set(self.cfg.cameras)
        removed = 0
        for cid in cams:
            cam = self.cfg.cameras.get(cid)
            ev_days = (cam.events_days if cam and cam.events_days else None) or g.events_days
            co_days = (cam.continuous_days if cam and cam.continuous_days else None) or g.continuous_days
            ev_cut, co_cut = now - ev_days * 86400, now - co_days * 86400
            while True:
                rows = self.db.query("SELECT id, root, path, bytes FROM segments WHERE camera=? AND "
                                     "((event=0 AND end_ts<?) OR end_ts<?) LIMIT 2000", (cid, co_cut, ev_cut))
                if not rows:
                    break
                self._delete_segments(rows)
                removed += len(rows)
            for ev in self.db.query("SELECT id, snapshot, thumb FROM events WHERE camera=? AND start_ts<?", (cid, ev_cut)):
                self._delete_event(ev)
        # Alt: MP4-Clips aus Version 1.0/1.1
        for c in self.db.query("SELECT id, path FROM clips WHERE end_ts<?", (now - g.events_days * 86400,)):
            (self.paths.media / c["path"]).unlink(missing_ok=True)
            self.db.execute("DELETE FROM clips WHERE id=?", (c["id"],))
        if removed:
            log.info("Aufbewahrung: %d alte Segmente gelöscht", removed)
        self._enforce_limit()
        self._remove_empty_dirs()
        if self.faces:
            self.faces.cleanup()
        self.measure(force=True)

    def _enforce_limit(self) -> None:
        limit = self.cfg.record.retention.max_storage_gb * 1e9
        for _ in range(200):
            used = (self.db.one("SELECT COALESCE(SUM(bytes),0) AS b FROM segments") or {"b": 0})["b"]
            low_disk = False
            for root, base in (self.storage.roots().items() if self.storage else [("local", self.paths.media)]):
                try:
                    d = shutil.disk_usage(base)
                    if d.free < 0.05 * d.total:
                        low_disk = True
                except OSError:
                    pass
            if used <= limit * 0.98 and not low_disk:
                return
            rows = self.db.query("SELECT id, root, path, bytes FROM segments ORDER BY start_ts LIMIT 300")
            if not rows:
                return
            self._delete_segments(rows)
            log.info("Speichergrenze erreicht: älteste Aufnahmen gelöscht")

    def _remove_empty_dirs(self) -> None:
        bases = [self.paths.snapshots] + [b / "recordings" for b in
                                          (self.storage.roots().values() if self.storage else [self.paths.media])]
        for base in bases:
            if not base.exists():
                continue
            for d in sorted((p for p in base.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                try:
                    if not any(d.iterdir()):
                        d.rmdir()
                except OSError:
                    pass

    # ------------------------------------------------------------------ Messen
    def measure(self, force: bool = False) -> dict:
        rows = self.db.query("SELECT root, event, COALESCE(SUM(bytes),0) AS b, COUNT(*) AS n, MIN(start_ts) AS first "
                             "FROM segments GROUP BY root, event")
        rec = sum(r["b"] for r in rows)
        ev = sum(r["b"] for r in rows if r["event"])
        nas = sum(r["b"] for r in rows if r["root"] == "nas")
        first = min([r["first"] for r in rows if r["first"]] or [None]) if rows else None
        if force or time.time() - self._small_ts > 600:
            self._small = {"snapshots": dir_size(self.paths.snapshots), "faces": dir_size(self.paths.faces),
                           "clips": dir_size(self.paths.clips)}
            self._small_ts = time.time()
        u = {"recordings": rec, "events": ev, "continuous": rec - ev, "on_nas": nas, **self._small,
             "oldest": first, "segments": sum(r["n"] for r in rows)}
        u["total"] = rec + sum(self._small.values())
        d = shutil.disk_usage(self.paths.media)
        u.update(disk_total=d.total, disk_free=d.free, limit=int(self.cfg.record.retention.max_storage_gb * 1e9))
        if self.storage:
            u["storage"] = self.storage.status()
        self.usage = u
        return u
