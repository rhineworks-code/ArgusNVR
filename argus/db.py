"""SQLite-Datenbank (eine Verbindung, thread-sicher über Lock, WAL-Modus)."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    camera TEXT NOT NULL,
    label TEXT NOT NULL,
    grp TEXT NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL,
    top_score REAL,
    box TEXT,
    zones TEXT DEFAULT '[]',
    sub_label TEXT,
    sub_score REAL,
    person_id INTEGER,
    snapshot TEXT,
    thumb TEXT,
    face_thumb TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_start ON events(start_ts DESC);
CREATE INDEX IF NOT EXISTS ix_events_cam ON events(camera, start_ts DESC);

CREATE TABLE IF NOT EXISTS clips (
    id TEXT PRIMARY KEY,
    camera TEXT NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    path TEXT NOT NULL,
    bytes INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_clips_cam ON clips(camera, start_ts);

CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera TEXT NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    root TEXT NOT NULL DEFAULT 'local',   -- local | nas
    path TEXT NOT NULL,
    bytes INTEGER DEFAULT 0,
    event INTEGER DEFAULT 0               -- 1 = gehört zu einem Ereignis (längere Aufbewahrung)
);
CREATE INDEX IF NOT EXISTS ix_seg_cam ON segments(camera, start_ts);
CREATE INDEX IF NOT EXISTS ix_seg_start ON segments(start_ts);

CREATE TABLE IF NOT EXISTS persons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    created REAL
);

CREATE TABLE IF NOT EXISTS face_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    centroid BLOB NOT NULL,
    count INTEGER DEFAULT 0,
    first_seen REAL,
    last_seen REAL
);

CREATE TABLE IF NOT EXISTS face_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER REFERENCES persons(id) ON DELETE CASCADE,
    cluster_id INTEGER REFERENCES face_clusters(id) ON DELETE CASCADE,
    embedding BLOB NOT NULL,
    quality REAL,
    path TEXT,
    source TEXT,          -- manual | auto | confirmed | unknown
    event_id TEXT,
    camera TEXT,
    created REAL
);
CREATE INDEX IF NOT EXISTS ix_samples_person ON face_samples(person_id);
CREATE INDEX IF NOT EXISTS ix_samples_cluster ON face_samples(cluster_id);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ---- Events -----------------------------------------------------------
    def upsert_event(self, ev: dict[str, Any]) -> None:
        cols = ["id", "camera", "label", "grp", "start_ts", "end_ts", "top_score", "box", "zones",
                "sub_label", "sub_score", "person_id", "snapshot", "thumb", "face_thumb"]
        vals = []
        for c in cols:
            v = ev.get(c)
            if c in ("box", "zones") and v is not None and not isinstance(v, str):
                v = json.dumps(v)
            vals.append(v)
        placeholders = ",".join("?" * len(cols))
        updates = ",".join(f"{c}=excluded.{c}" for c in cols[1:])
        self.execute(f"INSERT INTO events ({','.join(cols)}) VALUES ({placeholders}) "
                     f"ON CONFLICT(id) DO UPDATE SET {updates}", vals)

    @staticmethod
    def event_out(row: dict[str, Any]) -> dict[str, Any]:
        for k in ("box", "zones"):
            if isinstance(row.get(k), str):
                try:
                    row[k] = json.loads(row[k])
                except ValueError:
                    pass
        return row
