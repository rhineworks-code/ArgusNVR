"""API: Anmeldung, eigenes Konto, Benutzerverwaltung, Passkeys, Push und Kamera-ID ändern."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from typing import Any

import cv2
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from .auth import PERMS


class LoginIn(BaseModel):
    username: str
    password: str
    remember: bool = True


class SetupIn(BaseModel):
    username: str
    password: str
    display_name: str = ""


class PasskeyVerifyIn(BaseModel):
    challenge_id: str
    credential: dict
    remember: bool = True
    name: str = ""


class MeIn(BaseModel):
    display_name: str | None = None
    password: str | None = None
    current_password: str | None = None


class UserIn(BaseModel):
    username: str | None = None
    password: str | None = None
    display_name: str | None = None
    role: str | None = None
    perms: list[str] | None = None
    cameras: list[str] | None = None
    all_cameras: bool | None = None
    disabled: bool | None = None


class TokenIn(BaseModel):
    name: str = "Home Assistant"


class PushSubIn(BaseModel):
    subscription: dict
    name: str = ""
    prefs: dict | None = None


class PushPatchIn(BaseModel):
    prefs: dict | None = None
    name: str | None = None


class RenameIn(BaseModel):
    new_id: str


def _origin(request: Request) -> tuple[str, str, bool]:
    """(rp_id, origin, https) aus Sicht des Browsers – auch hinter Nginx Proxy Manager / Cloudflare."""
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost").split(",")[0].strip()
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
    if request.headers.get("cf-visitor", "").find('"https"') >= 0:
        proto = "https"
    rp_id = host.split(":")[0]
    return rp_id, f"{proto}://{host}", proto == "https"


def _client_ip(request: Request) -> str:
    return (request.headers.get("cf-connecting-ip") or request.headers.get("x-real-ip")
            or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
            or (request.client.host if request.client else "?"))


def register(app: FastAPI, ctx, paths, db) -> None:
    auth = ctx.auth

    def _set_session(resp: Response, request: Request, user, remember: bool, method: str) -> None:
        token, ttl = auth.new_session(user, remember, request.headers.get("user-agent", ""), _client_ip(request), method)
        _, _, https = _origin(request)
        resp.set_cookie("camspire_session", token, httponly=True, samesite="lax", secure=https,
                        max_age=ttl if remember else None, path="/")

    def _me(request: Request):
        u = request.state.user
        if u is None:
            raise HTTPException(401, "Nicht angemeldet")
        return u

    def _admin(request: Request):
        u = _me(request)
        if not u.is_admin:
            raise HTTPException(403, "Nur für Admins")
        return u

    # ------------------------------------------------------------------ Anmeldung (öffentlich)
    @app.get("/api/auth/status")
    def status(request: Request):
        u = request.state.user
        rp_id, origin, https = _origin(request)
        return {"setup_required": not auth.has_users(), "user": u.public() if u else None,
                "secure": https or rp_id in ("localhost", "127.0.0.1"), "rp_id": rp_id}

    @app.post("/api/auth/setup")
    def setup(body: SetupIn, request: Request):
        if auth.has_users():
            raise HTTPException(409, "Die Einrichtung ist bereits abgeschlossen")
        try:
            uid = auth.create_user(body.username, body.password, role="admin", display_name=body.display_name)
        except ValueError as e:
            raise HTTPException(400, str(e))
        user = auth.get(uid)
        resp = JSONResponse({"ok": True, "user": user.public()})
        _set_session(resp, request, user, True, "password")
        return resp

    @app.post("/api/auth/login")
    def login(body: LoginIn, request: Request):
        ip = _client_ip(request)
        wait = auth.throttle(f"{ip}|{body.username.strip().lower()}")
        if wait > 0:
            raise HTTPException(429, f"Zu viele Versuche – bitte {int(wait) + 1} s warten")
        user = auth.login(body.username, body.password, ip)
        if not user:
            raise HTTPException(401, "Benutzername oder Passwort falsch")
        resp = JSONResponse({"ok": True, "user": user.public()})
        _set_session(resp, request, user, body.remember, "password")
        return resp

    @app.post("/api/auth/logout")
    def logout(request: Request):
        auth.end_session(request.cookies.get("camspire_session"))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie("camspire_session", path="/")
        return resp

    @app.post("/api/auth/passkey/options")
    def passkey_login_options(request: Request):
        rp_id, _, _ = _origin(request)
        return auth.passkey_login_options(rp_id)

    @app.post("/api/auth/passkey/verify")
    def passkey_login_verify(body: PasskeyVerifyIn, request: Request):
        rp_id, origin, _ = _origin(request)
        try:
            user = auth.passkey_login_verify(body.challenge_id, body.credential, rp_id, origin)
        except Exception as e:
            raise HTTPException(401, f"Passkey-Anmeldung fehlgeschlagen: {e}")
        resp = JSONResponse({"ok": True, "user": user.public()})
        _set_session(resp, request, user, body.remember, "passkey")
        return resp

    # ------------------------------------------------------------------ Eigenes Konto
    @app.get("/api/me")
    def me(request: Request):
        return _me(request).public()

    @app.put("/api/me")
    def update_me(body: MeIn, request: Request):
        u = _me(request)
        data: dict[str, Any] = {}
        if body.display_name is not None:
            data["display_name"] = body.display_name
        if body.password:
            row = db.one("SELECT password_hash FROM users WHERE id=?", (u.id,))
            from .auth import check_password
            if not check_password(body.current_password or "", row["password_hash"]):
                raise HTTPException(400, "Aktuelles Passwort ist falsch")
            data["password"] = body.password
        try:
            auth.update_user(u.id, data, u)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return auth.get(u.id).public()

    @app.get("/api/me/passkeys")
    def my_passkeys(request: Request):
        return auth.passkeys(_me(request).id)

    @app.post("/api/me/passkeys/options")
    def passkey_register_options(request: Request):
        rp_id, _, _ = _origin(request)
        return auth.passkey_register_options(_me(request), rp_id)

    @app.post("/api/me/passkeys")
    def passkey_register(body: PasskeyVerifyIn, request: Request):
        rp_id, origin, _ = _origin(request)
        try:
            auth.passkey_register_verify(_me(request), body.challenge_id, body.credential, rp_id, origin, body.name)
        except Exception as e:
            raise HTTPException(400, f"Passkey konnte nicht gespeichert werden: {e}")
        return {"ok": True}

    @app.delete("/api/me/passkeys/{pid}")
    def passkey_delete(pid: int, request: Request):
        auth.delete_passkey(_me(request).id, pid)
        return {"ok": True}

    @app.get("/api/me/sessions")
    def my_sessions(request: Request):
        return auth.sessions(_me(request).id, request.cookies.get("camspire_session"))

    @app.delete("/api/me/sessions/{sid}")
    def end_session(sid: str, request: Request):
        auth.end_session_by_prefix(_me(request).id, sid)
        return {"ok": True}

    @app.get("/api/me/tokens")
    def my_tokens(request: Request):
        return auth.tokens(_me(request).id)

    @app.post("/api/me/tokens")
    def create_token(body: TokenIn, request: Request):
        return {"token": auth.create_token(_me(request).id, body.name)}

    @app.delete("/api/me/tokens/{tid}")
    def delete_token(tid: int, request: Request):
        auth.delete_token(_me(request).id, tid)
        return {"ok": True}

    # ------------------------------------------------------------------ Benutzerverwaltung (Admin)
    @app.get("/api/users")
    def users(request: Request):
        _admin(request)
        return {"users": auth.list_users(), "perms": PERMS, "cameras": [{"id": c, "name": cam.name} for c, cam in ctx.cfg.cameras.items()]}

    @app.post("/api/users")
    def create_user(body: UserIn, request: Request):
        _admin(request)
        try:
            uid = auth.create_user(body.username or "", body.password or "", body.role or "user", body.display_name or "",
                                   body.perms, None if body.all_cameras or body.cameras is None else body.cameras)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"id": uid}

    @app.patch("/api/users/{uid}")
    def update_user(uid: int, body: UserIn, request: Request):
        actor = _admin(request)
        data = body.model_dump(exclude_none=True)
        if body.all_cameras:
            data["cameras"] = None
        data.pop("all_cameras", None)
        data.pop("username", None)
        try:
            auth.update_user(uid, data, actor)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.delete("/api/users/{uid}")
    def delete_user(uid: int, request: Request):
        try:
            auth.delete_user(uid, _admin(request))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.get("/api/users/{uid}/passkeys")
    def user_passkeys(uid: int, request: Request):
        _admin(request)
        return auth.passkeys(uid)

    @app.delete("/api/users/{uid}/passkeys/{pid}")
    def user_passkey_delete(uid: int, pid: int, request: Request):
        _admin(request)
        auth.delete_passkey(uid, pid, admin=True)
        return {"ok": True}

    # ------------------------------------------------------------------ Push
    push = ctx.push

    @app.get("/api/push/key")
    def push_key(request: Request):
        _me(request)
        return {"key": push.public_key}

    @app.get("/api/push/subs")
    def push_subs(request: Request):
        return push.list(_me(request).id)

    @app.post("/api/push/subs")
    def push_subscribe(body: PushSubIn, request: Request):
        try:
            sid = push.subscribe(_me(request).id, body.subscription, body.name, body.prefs)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"id": sid}

    @app.patch("/api/push/subs/{sid}")
    def push_update(sid: int, body: PushPatchIn, request: Request):
        try:
            push.update(_me(request).id, sid, body.prefs, body.name)
        except ValueError as e:
            raise HTTPException(404, str(e))
        return {"ok": True}

    @app.delete("/api/push/subs/{sid}")
    def push_delete(sid: int, request: Request):
        push.delete(_me(request).id, sid=sid)
        return {"ok": True}

    @app.post("/api/push/test")
    async def push_test(request: Request):
        uid = _me(request).id
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        return await asyncio.to_thread(push.test, uid, body.get("id"))

    @app.get("/api/push/image/{eid}")
    def push_image(eid: str, sig: str = ""):
        """Bild für die Benachrichtigung – über signierten Link, da das Handy ohne Cookie lädt."""
        if not auth.verify_sig(f"img:{eid}", sig):
            raise HTTPException(403)
        a = ctx.events.active.get(eid) if ctx.events else None
        if a is not None and a.best_frame is not None:
            img = a.best_frame
            if img.shape[1] > 960:
                img = cv2.resize(img, (960, int(img.shape[0] * 960 / img.shape[1])), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ok:
                return Response(buf.tobytes(), media_type="image/jpeg")
        r = db.one("SELECT snapshot FROM events WHERE id=?", (eid,))
        if r and r["snapshot"] and (paths.media / r["snapshot"]).exists():
            return FileResponse(paths.media / r["snapshot"], media_type="image/jpeg")
        raise HTTPException(404)

    # ------------------------------------------------------------------ Kamera-ID ändern
    @app.post("/api/settings/cameras/{old}/rename")
    async def rename_camera(old: str, body: RenameIn, request: Request):
        new = body.new_id.strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{1,40}", new):
            raise HTTPException(400, "Die ID darf nur a–z, 0–9 und _ enthalten")
        if old not in ctx.cfg.cameras:
            raise HTTPException(404, "Kamera nicht gefunden")
        if new == old:
            return {"ok": True, "id": new}
        if new in ctx.cfg.cameras:
            raise HTTPException(409, "Diese ID gibt es schon")
        return await asyncio.to_thread(_rename, old, new)

    def _rename(old: str, new: str) -> dict:
        from .config import Config
        raw = ctx.cfg.model_dump(mode="json")
        raw["cameras"] = {(new if k == old else k): v for k, v in raw["cameras"].items()}
        new_cfg = Config.model_validate(raw)
        # zuerst alte Kamera anhalten (Aufnahme schließt sauber ab), dann Daten umschreiben
        ctx._drop_camera(old)
        for tbl in ("events", "segments", "clips", "face_samples"):
            db.execute(f"UPDATE {tbl} SET camera=? WHERE camera=?", (new, old))
        # Ordner umbenennen, damit Pfade stimmig bleiben
        moves = [(paths.snapshots / old, paths.snapshots / new, "snapshots")]
        for root, base in ctx.storage.roots().items():
            moves.append((base / "recordings" / old, base / "recordings" / new, f"recordings@{root}"))
        for src, dst, kind in moves:
            try:
                if src.exists() and not dst.exists():
                    shutil.move(str(src), str(dst))
            except OSError:
                continue
            if kind == "snapshots":
                for col in ("snapshot", "thumb"):
                    db.execute(f"UPDATE events SET {col}=replace({col}, ?, ?) WHERE camera=?",
                               (f"snapshots/{old}/", f"snapshots/{new}/", new))
            else:
                root = kind.split("@")[1]
                db.execute("UPDATE segments SET path=replace(path, ?, ?) WHERE camera=? AND root=?",
                           (f"recordings/{old}/", f"recordings/{new}/", new, root))
        auth.rename_camera(old, new)
        for r in db.query("SELECT id, prefs FROM push_subs"):
            p = json.loads(r["prefs"] or "{}")
            if p.get("cameras") and old in p["cameras"]:
                p["cameras"] = [new if c == old else c for c in p["cameras"]]
                db.execute("UPDATE push_subs SET prefs=? WHERE id=?", (json.dumps(p), r["id"]))
        result = ctx.apply_config(new_cfg)
        ctx.codecs.pop(old, None)
        return {"ok": True, "id": new, **result}

    # ------------------------------------------------------------------ Kleine Helfer
    @app.get("/favicon.ico")
    def favicon():
        return FileResponse(paths_web() / "icons" / "icon-192.png", media_type="image/png")

    def paths_web():
        from .api import WEB
        return WEB

    _ = time
