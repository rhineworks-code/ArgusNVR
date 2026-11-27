"""Push-Benachrichtigungen (Web Push mit VAPID) – funktioniert in Chrome/Edge/Firefox und
auf iPhone/iPad, sobald CamSpire als App installiert ist (iOS 16.4+). Braucht HTTPS."""
from __future__ import annotations

import base64
import json
import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .db import Database

log = logging.getLogger("camspire.push")

SCHEMA = """
CREATE TABLE IF NOT EXISTS push_subs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint TEXT UNIQUE NOT NULL,
    p256dh TEXT NOT NULL, auth TEXT NOT NULL,
    name TEXT, prefs TEXT DEFAULT '{}',
    created REAL, last_ok REAL, last_error TEXT
);
"""

GROUP_DE = {"person": "Person", "vehicle": "Fahrzeug", "animal": "Tier"}
LABEL_DE = {"person": "Person", "car": "Auto", "truck": "LKW", "bus": "Bus", "motorcycle": "Motorrad",
            "bicycle": "Fahrrad", "dog": "Hund", "cat": "Katze", "bird": "Vogel", "horse": "Pferd"}
DEFAULT_PREFS = {"enabled": True, "cameras": None, "groups": ["person"], "faces": "all", "cooldown": 60,
                 "quiet_from": "", "quiet_to": ""}


class Push:
    def __init__(self, db: Database, key_dir: Path, auth, ctx) -> None:
        self.db, self.auth, self.ctx = db, auth, ctx
        self.db.conn.executescript(SCHEMA)
        self.key_file = key_dir / "vapid_private.pem"
        self.vapid = self._load_keys()
        self.q: "queue.Queue[tuple[float, dict]]" = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="push")
        self._last: dict[tuple, float] = {}
        self.sent = 0
        threading.Thread(target=self._loop, name="push", daemon=True).start()

    # ------------------------------------------------------------------ Schlüssel
    def _load_keys(self):
        from py_vapid import Vapid02
        self.key_file.parent.mkdir(parents=True, exist_ok=True)
        if self.key_file.exists():
            v = Vapid02.from_file(str(self.key_file))
        else:
            v = Vapid02()
            v.generate_keys()
            v.save_key(str(self.key_file))
            log.info("Push-Schlüssel erzeugt (%s)", self.key_file)
        return v

    @property
    def public_key(self) -> str:
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        raw = self.vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    # ------------------------------------------------------------------ Abos
    def subscribe(self, uid: int, sub: dict, name: str, prefs: dict | None) -> int:
        keys = sub.get("keys") or {}
        if not sub.get("endpoint") or not keys.get("p256dh") or not keys.get("auth"):
            raise ValueError("Ungültiges Push-Abo")
        p = {**DEFAULT_PREFS, **(prefs or {})}
        self.db.execute(
            "INSERT INTO push_subs (user_id, endpoint, p256dh, auth, name, prefs, created) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id, p256dh=excluded.p256dh, auth=excluded.auth, "
            "name=excluded.name", (uid, sub["endpoint"], keys["p256dh"], keys["auth"], name[:80], json.dumps(p), time.time()))
        r = self.db.one("SELECT id FROM push_subs WHERE endpoint=?", (sub["endpoint"],))
        return int(r["id"])

    def list(self, uid: int) -> list[dict]:
        rows = self.db.query("SELECT id, endpoint, name, prefs, created, last_ok, last_error FROM push_subs WHERE user_id=? "
                             "ORDER BY created", (uid,))
        for r in rows:
            r["prefs"] = {**DEFAULT_PREFS, **json.loads(r["prefs"] or "{}")}
        return rows

    def update(self, uid: int, sid: int, prefs: dict | None, name: str | None) -> None:
        r = self.db.one("SELECT prefs FROM push_subs WHERE id=? AND user_id=?", (sid, uid))
        if not r:
            raise ValueError("Gerät nicht gefunden")
        if prefs is not None:
            p = {**DEFAULT_PREFS, **json.loads(r["prefs"] or "{}"), **prefs}
            self.db.execute("UPDATE push_subs SET prefs=? WHERE id=?", (json.dumps(p), sid))
        if name is not None:
            self.db.execute("UPDATE push_subs SET name=? WHERE id=?", (name[:80], sid))

    def delete(self, uid: int, sid: int | None = None, endpoint: str | None = None) -> None:
        if sid is not None:
            self.db.execute("DELETE FROM push_subs WHERE id=? AND user_id=?", (sid, uid))
        elif endpoint:
            self.db.execute("DELETE FROM push_subs WHERE endpoint=? AND user_id=?", (endpoint, uid))

    # ------------------------------------------------------------------ Senden
    def event(self, ev: dict) -> None:
        """Neues Ereignis: kurz warten (bestes Bild, evtl. erkanntes Gesicht), dann senden."""
        self.q.put((time.time() + 2.5, ev))

    def _loop(self) -> None:
        pending: list[tuple[float, dict]] = []
        while True:
            try:
                timeout = max(0.1, min((t for t, _ in pending), default=time.time() + 1) - time.time())
                pending.append(self.q.get(timeout=timeout))
            except queue.Empty:
                pass
            now = time.time()
            due = [p for p in pending if p[0] <= now]
            pending = [p for p in pending if p[0] > now]
            for _, ev in due:
                try:
                    self._dispatch(ev)
                except Exception:
                    log.exception("Push")

    def _live_event(self, ev: dict) -> dict:
        """Aktuellen Stand holen (Gesicht kann inzwischen erkannt sein)."""
        em = getattr(self.ctx, "events", None)
        a = em.active.get(ev["id"]) if em else None
        if a is not None:
            ev = {**ev, **a.payload()}
        else:
            r = self.db.one("SELECT sub_label FROM events WHERE id=?", (ev["id"],))
            if r and r["sub_label"]:
                ev = {**ev, "sub_label": r["sub_label"]}
        return ev

    @staticmethod
    def _quiet(p: dict) -> bool:
        a, b = p.get("quiet_from") or "", p.get("quiet_to") or ""
        if not a or not b:
            return False
        now = time.strftime("%H:%M")
        return (a <= now < b) if a < b else (now >= a or now < b)

    def _dispatch(self, ev: dict) -> None:
        ev = self._live_event(ev)
        cams = self.ctx.cfg.cameras
        cam_name = cams[ev["camera"]].name if ev["camera"] in cams else ev["camera"]
        who = ev.get("sub_label")
        title = f"{who} – {cam_name}" if who else f"{LABEL_DE.get(ev['label'], ev['label'])} – {cam_name}"
        body = time.strftime("%H:%M:%S", time.localtime(ev["start_ts"]))
        if ev.get("zones"):
            body += " · " + ", ".join(ev["zones"])
        sig = self.auth.sign(f"img:{ev['id']}", ttl=7 * 86400)
        payload = {"title": title, "body": body, "tag": f"{ev['camera']}-{ev['grp']}",
                   "image": f"/api/push/image/{ev['id']}?sig={sig}", "url": f"/#/events?ev={ev['id']}",
                   "event_id": ev["id"], "ts": ev["start_ts"]}
        now = time.time()
        for s in self.db.query("SELECT * FROM push_subs"):
            p = {**DEFAULT_PREFS, **json.loads(s["prefs"] or "{}")}
            if not p["enabled"] or self._quiet(p):
                continue
            if p["cameras"] is not None and ev["camera"] not in p["cameras"]:
                continue
            if p["groups"] and ev["grp"] not in p["groups"]:
                continue
            if ev["grp"] == "person" and p["faces"] == "unknown" and who:
                continue
            if p["faces"] == "known" and (ev["grp"] != "person" or not who):
                continue
            user = self.auth.get(s["user_id"])
            if not user or not user.can("events") or not user.cam_ok(ev["camera"]):
                continue
            key = (s["id"], ev["camera"], ev["grp"], who or "")
            if now - self._last.get(key, 0) < int(p.get("cooldown") or 0):
                continue
            self._last[key] = now
            self.pool.submit(self._send, s, payload)

    def _send(self, s: dict, payload: dict) -> bool:
        from pywebpush import WebPushException, webpush
        info = {"endpoint": s["endpoint"], "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}}
        try:
            webpush(info, json.dumps(payload), vapid_private_key=self.vapid,
                    vapid_claims={"sub": "https://github.com/rhineworks-code/CamSpire-NVR"}, ttl=3600, timeout=15,
                    headers={"Urgency": "high"})
            self.db.execute("UPDATE push_subs SET last_ok=?, last_error=NULL WHERE id=?", (time.time(), s["id"]))
            self.sent += 1
            return True
        except WebPushException as e:
            code = getattr(e.response, "status_code", None)
            if code in (404, 410):
                log.info("Push-Abo abgelaufen – entfernt (%s)", s.get("name"))
                self.db.execute("DELETE FROM push_subs WHERE id=?", (s["id"],))
            else:
                self.db.execute("UPDATE push_subs SET last_error=? WHERE id=?", (f"{code}: {str(e)[:150]}", s["id"]))
                log.warning("Push fehlgeschlagen (%s): %s", code, e)
        except Exception as e:
            self.db.execute("UPDATE push_subs SET last_error=? WHERE id=?", (str(e)[:150], s["id"]))
            log.warning("Push fehlgeschlagen: %s", e)
        return False

    def test(self, uid: int, sid: int | None = None) -> dict[str, Any]:
        subs = self.db.query("SELECT * FROM push_subs WHERE user_id=?" + (" AND id=?" if sid else ""),
                             (uid, sid) if sid else (uid,))
        if not subs:
            return {"ok": False, "message": "Auf diesem Gerät sind keine Benachrichtigungen aktiv"}
        payload = {"title": "CamSpire NVR", "body": "Test-Benachrichtigung – alles funktioniert ✓", "tag": "test", "url": "/"}
        ok = sum(1 for s in subs if self._send(s, payload))
        return {"ok": ok > 0, "message": f"An {ok} von {len(subs)} Gerät(en) gesendet"}
