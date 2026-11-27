"""Benutzer, Anmeldung, Rechte, Passkeys (WebAuthn) und API-Tokens."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from .db import Database

log = logging.getLogger("camspire.auth")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
    display_name TEXT,
    password_hash TEXT,
    role TEXT NOT NULL DEFAULT 'user',      -- admin | user
    perms TEXT DEFAULT '[]',
    cameras TEXT,                           -- NULL = alle Kameras, sonst JSON-Liste
    disabled INTEGER DEFAULT 0,
    created REAL,
    last_login REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created REAL, expires REAL, last_seen REAL,
    user_agent TEXT, ip TEXT, method TEXT
);
CREATE TABLE IF NOT EXISTS passkeys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    credential_id BLOB UNIQUE NOT NULL,
    public_key BLOB NOT NULL,
    sign_count INTEGER DEFAULT 0,
    rp_id TEXT,
    name TEXT,
    created REAL, last_used REAL
);
CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT, token_hash TEXT UNIQUE NOT NULL, prefix TEXT,
    created REAL, last_used REAL
);
"""

# Rechte, die einem normalen Benutzer gegeben werden können (Admins haben immer alle)
PERMS: dict[str, str] = {
    "live": "Live-Bild ansehen",
    "history": "Verlauf & Aufnahmen abspielen",
    "events": "Ereignisse ansehen",
    "download": "Clips/Ausschnitte herunterladen",
    "events_manage": "Ereignisse löschen & Gesichter korrigieren",
    "faces": "Gesichter verwalten (Personen anlegen, benennen)",
    "detect_toggle": "Erkennung ein-/ausschalten",
    "system": "Systemseite ansehen",
    "settings": "Einstellungen ändern (Kameras, Speicher, MQTT …)",
}
DEFAULT_PERMS = ["live", "history", "events", "download"]
SESSION_DAYS = 30
SHORT_SESSION_H = 12


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 14, 8, 1
    h = hashlib.scrypt(pw.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${h.hex()}"


def check_password(pw: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, n, r, p, salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


@dataclass
class User:
    id: int
    username: str
    display_name: str
    role: str
    perms: set[str]
    cameras: list[str] | None
    via: str = "session"          # session | token

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def can(self, perm: str) -> bool:
        return self.is_admin or perm in self.perms

    def cam_ok(self, cam: str | None) -> bool:
        return self.is_admin or self.cameras is None or (cam is not None and cam in self.cameras)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "username": self.username, "display_name": self.display_name, "role": self.role,
                "perms": sorted(PERMS) if self.is_admin else sorted(self.perms),
                "cameras": None if self.is_admin else self.cameras, "is_admin": self.is_admin}


@dataclass
class _Attempts:
    fails: int = 0
    until: float = 0.0


class Auth:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.db.conn.executescript(SCHEMA)
        self.lock = threading.Lock()
        self._attempts: dict[str, _Attempts] = {}
        self._challenges: dict[str, tuple[bytes, float, int | None]] = {}
        self.secret = self._secret()

    def _secret(self) -> bytes:
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        r = self.db.one("SELECT v FROM kv WHERE k='secret'")
        if r:
            return bytes.fromhex(r["v"])
        s = secrets.token_bytes(32)
        self.db.execute("INSERT INTO kv (k, v) VALUES ('secret', ?)", (s.hex(),))
        return s

    # ------------------------------------------------------------------ Benutzer
    def has_users(self) -> bool:
        return bool(self.db.one("SELECT 1 AS x FROM users LIMIT 1"))

    def _user(self, row: dict) -> User:
        return User(row["id"], row["username"], row.get("display_name") or row["username"], row["role"],
                    set(json.loads(row.get("perms") or "[]")),
                    json.loads(row["cameras"]) if row.get("cameras") else None)

    def get(self, uid: int) -> User | None:
        r = self.db.one("SELECT * FROM users WHERE id=? AND disabled=0", (uid,))
        return self._user(r) if r else None

    def list_users(self) -> list[dict]:
        rows = self.db.query("SELECT id, username, display_name, role, perms, cameras, disabled, created, last_login, "
                             "(SELECT COUNT(*) FROM passkeys p WHERE p.user_id=users.id) AS passkeys "
                             "FROM users ORDER BY role, username COLLATE NOCASE")
        for r in rows:
            r["perms"] = json.loads(r["perms"] or "[]")
            r["cameras"] = json.loads(r["cameras"]) if r["cameras"] else None
            r["disabled"] = bool(r["disabled"])
        return rows

    def create_user(self, username: str, password: str, role: str = "user", display_name: str = "",
                    perms: list[str] | None = None, cameras: list[str] | None = None) -> int:
        username = username.strip()
        if not username or len(username) > 64:
            raise ValueError("Benutzername fehlt")
        if len(password) < 8:
            raise ValueError("Passwort muss mindestens 8 Zeichen haben")
        if self.db.one("SELECT 1 AS x FROM users WHERE username=?", (username,)):
            raise ValueError("Benutzername ist schon vergeben")
        perms = [p for p in (perms if perms is not None else DEFAULT_PERMS) if p in PERMS]
        cur = self.db.execute(
            "INSERT INTO users (username, display_name, password_hash, role, perms, cameras, created) VALUES (?,?,?,?,?,?,?)",
            (username, display_name.strip() or username, hash_password(password), role if role in ("admin", "user") else "user",
             json.dumps(perms), json.dumps(cameras) if cameras is not None else None, time.time()))
        log.info("Benutzer %s angelegt (%s)", username, role)
        return int(cur.lastrowid)

    def update_user(self, uid: int, data: dict, actor: User) -> None:
        row = self.db.one("SELECT * FROM users WHERE id=?", (uid,))
        if not row:
            raise ValueError("Benutzer nicht gefunden")
        sets, args = [], []
        if "display_name" in data:
            sets.append("display_name=?"); args.append((data["display_name"] or row["username"]).strip())
        if "role" in data and data["role"] in ("admin", "user"):
            if row["role"] == "admin" and data["role"] != "admin" and self._admins(exclude=uid) == 0:
                raise ValueError("Es muss mindestens ein Admin bleiben")
            sets.append("role=?"); args.append(data["role"])
        if "perms" in data:
            sets.append("perms=?"); args.append(json.dumps([p for p in data["perms"] if p in PERMS]))
        if "cameras" in data:
            sets.append("cameras=?"); args.append(json.dumps(data["cameras"]) if data["cameras"] is not None else None)
        if "disabled" in data:
            if data["disabled"] and uid == actor.id:
                raise ValueError("Du kannst dich nicht selbst sperren")
            if data["disabled"] and row["role"] == "admin" and self._admins(exclude=uid) == 0:
                raise ValueError("Es muss mindestens ein Admin aktiv bleiben")
            sets.append("disabled=?"); args.append(1 if data["disabled"] else 0)
            if data["disabled"]:
                self.db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        if data.get("password"):
            if len(data["password"]) < 8:
                raise ValueError("Passwort muss mindestens 8 Zeichen haben")
            sets.append("password_hash=?"); args.append(hash_password(data["password"]))
        if sets:
            self.db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", (*args, uid))

    def delete_user(self, uid: int, actor: User) -> None:
        row = self.db.one("SELECT role FROM users WHERE id=?", (uid,))
        if not row:
            return
        if uid == actor.id:
            raise ValueError("Du kannst dich nicht selbst löschen")
        if row["role"] == "admin" and self._admins(exclude=uid) == 0:
            raise ValueError("Es muss mindestens ein Admin bleiben")
        self.db.execute("DELETE FROM users WHERE id=?", (uid,))

    def _admins(self, exclude: int | None = None) -> int:
        r = self.db.one("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND disabled=0 AND id!=?", (exclude or -1,))
        return r["n"] if r else 0

    def rename_camera(self, old: str, new: str) -> None:
        for r in self.db.query("SELECT id, cameras FROM users WHERE cameras IS NOT NULL"):
            cams = json.loads(r["cameras"])
            if old in cams:
                self.db.execute("UPDATE users SET cameras=? WHERE id=?",
                                (json.dumps([new if c == old else c for c in cams]), r["id"]))

    # ------------------------------------------------------------------ Anmeldung
    def throttle(self, key: str) -> float:
        a = self._attempts.get(key)
        return max(0.0, a.until - time.time()) if a else 0.0

    def _failed(self, key: str) -> None:
        a = self._attempts.setdefault(key, _Attempts())
        a.fails += 1
        if a.fails >= 5:
            a.until = time.time() + min(900, 30 * 2 ** (a.fails - 5))

    def login(self, username: str, password: str, ip: str) -> User | None:
        key = f"{ip}|{username.lower()}"
        r = self.db.one("SELECT * FROM users WHERE username=? AND disabled=0", (username.strip(),))
        ok = check_password(password, r["password_hash"] if r else hash_password("x" * 9))  # gleiche Laufzeit
        if not (r and ok):
            self._failed(key)
            log.warning("Fehlgeschlagene Anmeldung für „%s“ von %s", username, ip)
            return None
        self._attempts.pop(key, None)
        return self._user(r)

    def new_session(self, user: User, remember: bool, ua: str, ip: str, method: str = "password") -> tuple[str, int]:
        token = secrets.token_urlsafe(32)
        ttl = SESSION_DAYS * 86400 if remember else SHORT_SESSION_H * 3600
        now = time.time()
        self.db.execute("INSERT INTO sessions (token_hash, user_id, created, expires, last_seen, user_agent, ip, method) "
                        "VALUES (?,?,?,?,?,?,?,?)", (_sha(token), user.id, now, now + ttl, now, ua[:200], ip, method))
        self.db.execute("UPDATE users SET last_login=? WHERE id=?", (now, user.id))
        self.db.execute("DELETE FROM sessions WHERE expires<?", (now,))
        return token, ttl

    def from_session(self, token: str | None) -> User | None:
        if not token:
            return None
        th = _sha(token)
        r = self.db.one("SELECT s.expires, s.last_seen, u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                        "WHERE s.token_hash=? AND u.disabled=0", (th,))
        if not r or r["expires"] < time.time():
            return None
        if time.time() - (r["last_seen"] or 0) > 300:
            self.db.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (time.time(), th))
        return self._user(r)

    def end_session(self, token: str | None) -> None:
        if token:
            self.db.execute("DELETE FROM sessions WHERE token_hash=?", (_sha(token),))

    def sessions(self, uid: int, current: str | None) -> list[dict]:
        rows = self.db.query("SELECT token_hash, created, last_seen, user_agent, ip, method, expires FROM sessions "
                             "WHERE user_id=? AND expires>? ORDER BY last_seen DESC", (uid, time.time()))
        cur = _sha(current) if current else ""
        for r in rows:
            r["current"] = r["token_hash"] == cur
            r["id"] = r.pop("token_hash")[:16]
        return rows

    def end_session_by_prefix(self, uid: int, prefix: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE user_id=? AND substr(token_hash,1,16)=?", (uid, prefix))

    # ------------------------------------------------------------------ API-Tokens (z. B. für Home Assistant)
    def create_token(self, uid: int, name: str) -> str:
        token = "csp_" + secrets.token_urlsafe(30)
        self.db.execute("INSERT INTO api_tokens (user_id, name, token_hash, prefix, created) VALUES (?,?,?,?,?)",
                        (uid, name.strip() or "Token", _sha(token), token[:10], time.time()))
        return token

    def from_token(self, token: str | None) -> User | None:
        if not token or not token.startswith("csp_"):
            return None
        r = self.db.one("SELECT t.id AS tid, t.last_used, u.* FROM api_tokens t JOIN users u ON u.id=t.user_id "
                        "WHERE t.token_hash=? AND u.disabled=0", (_sha(token),))
        if not r:
            return None
        if time.time() - (r["last_used"] or 0) > 300:
            self.db.execute("UPDATE api_tokens SET last_used=? WHERE id=?", (time.time(), r["tid"]))
        u = self._user(r)
        u.via = "token"
        return u

    def tokens(self, uid: int) -> list[dict]:
        return self.db.query("SELECT id, name, prefix, created, last_used FROM api_tokens WHERE user_id=? ORDER BY created DESC", (uid,))

    def delete_token(self, uid: int, tid: int) -> None:
        self.db.execute("DELETE FROM api_tokens WHERE id=? AND user_id=?", (tid, uid))

    # ------------------------------------------------------------------ Signierte Links (Push-Bilder)
    def sign(self, value: str, ttl: int = 3600) -> str:
        exp = int(time.time()) + ttl
        mac = hmac.new(self.secret, f"{value}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        return f"{exp}.{mac}"

    def verify_sig(self, value: str, sig: str) -> bool:
        try:
            exp, mac = sig.split(".")
            if int(exp) < time.time():
                return False
            calc = hmac.new(self.secret, f"{value}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
            return hmac.compare_digest(calc, mac)
        except Exception:
            return False

    # ------------------------------------------------------------------ Passkeys (Fingerabdruck / Face ID / Windows Hello)
    def _challenge(self, uid: int | None) -> tuple[str, bytes]:
        now = time.time()
        with self.lock:
            for k in [k for k, v in self._challenges.items() if v[1] < now]:
                del self._challenges[k]
            cid = secrets.token_urlsafe(16)
            ch = secrets.token_bytes(32)
            self._challenges[cid] = (ch, now + 300, uid)
        return cid, ch

    def _take_challenge(self, cid: str) -> tuple[bytes, int | None] | None:
        with self.lock:
            v = self._challenges.pop(cid, None)
        if not v or v[1] < time.time():
            return None
        return v[0], v[2]

    def passkey_register_options(self, user: User, rp_id: str) -> dict:
        from webauthn import generate_registration_options, options_to_json
        from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                              ResidentKeyRequirement, UserVerificationRequirement)
        cid, ch = self._challenge(user.id)
        existing = [PublicKeyCredentialDescriptor(id=r["credential_id"])
                    for r in self.db.query("SELECT credential_id FROM passkeys WHERE user_id=?", (user.id,))]
        opts = generate_registration_options(
            rp_id=rp_id, rp_name="CamSpire NVR", user_name=user.username, user_display_name=user.display_name,
            user_id=f"camspire-user-{user.id}".encode(), challenge=ch, exclude_credentials=existing,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED))
        return {"challenge_id": cid, "options": json.loads(options_to_json(opts))}

    def passkey_register_verify(self, user: User, cid: str, credential: dict, rp_id: str, origin: str, name: str) -> None:
        from webauthn import verify_registration_response
        c = self._take_challenge(cid)
        if not c or c[1] != user.id:
            raise ValueError("Anfrage abgelaufen – bitte erneut versuchen")
        v = verify_registration_response(credential=credential, expected_challenge=c[0], expected_rp_id=rp_id,
                                         expected_origin=origin, require_user_verification=True)
        self.db.execute("INSERT INTO passkeys (user_id, credential_id, public_key, sign_count, rp_id, name, created) "
                        "VALUES (?,?,?,?,?,?,?)", (user.id, v.credential_id, v.credential_public_key, v.sign_count,
                                                   rp_id, (name or "Passkey").strip()[:60], time.time()))

    def passkey_login_options(self, rp_id: str) -> dict:
        from webauthn import generate_authentication_options, options_to_json
        from webauthn.helpers.structs import UserVerificationRequirement
        cid, ch = self._challenge(None)
        opts = generate_authentication_options(rp_id=rp_id, challenge=ch,
                                               user_verification=UserVerificationRequirement.REQUIRED)
        return {"challenge_id": cid, "options": json.loads(options_to_json(opts))}

    def passkey_login_verify(self, cid: str, credential: dict, rp_id: str, origin: str) -> User:
        from webauthn import base64url_to_bytes, verify_authentication_response
        c = self._take_challenge(cid)
        if not c:
            raise ValueError("Anfrage abgelaufen – bitte erneut versuchen")
        raw_id = base64url_to_bytes(credential.get("rawId") or credential.get("id"))
        pk = self.db.one("SELECT * FROM passkeys WHERE credential_id=?", (raw_id,))
        if not pk:
            raise ValueError("Dieser Passkey ist hier nicht (mehr) registriert")
        v = verify_authentication_response(credential=credential, expected_challenge=c[0], expected_rp_id=rp_id,
                                           expected_origin=origin, credential_public_key=pk["public_key"],
                                           credential_current_sign_count=pk["sign_count"], require_user_verification=True)
        self.db.execute("UPDATE passkeys SET sign_count=?, last_used=? WHERE id=?", (v.new_sign_count, time.time(), pk["id"]))
        u = self.get(pk["user_id"])
        if not u:
            raise ValueError("Benutzer gesperrt")
        return u

    def passkeys(self, uid: int) -> list[dict]:
        return self.db.query("SELECT id, name, rp_id, created, last_used FROM passkeys WHERE user_id=? ORDER BY created", (uid,))

    def delete_passkey(self, uid: int, pid: int, admin: bool = False) -> None:
        if admin:
            self.db.execute("DELETE FROM passkeys WHERE id=?", (pid,))
        else:
            self.db.execute("DELETE FROM passkeys WHERE id=? AND user_id=?", (pid, uid))
