"""REST-API, WebSocket-Push, go2rtc-Proxy und Auslieferung der Web-Oberfläche."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

from . import __version__

log = logging.getLogger("argus.api")
WEB = Path(__file__).resolve().parent / "web"
GO2RTC = "127.0.0.1:1984"
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "content-length", "content-encoding", "authorization", "cookie"}


class Hub:
    """Thread-sicherer Push an alle verbundenen Browser."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[asyncio.Queue] = set()

    def publish(self, msg: dict) -> None:
        if self.loop and self.clients:
            self.loop.call_soon_threadsafe(self._fanout, msg)

    def _fanout(self, msg: dict) -> None:
        for q in list(self.clients):
            if q.qsize() < 200:
                q.put_nowait(msg)


class FaceAssign(BaseModel):
    person_id: int | None = None
    name: str | None = None


class DetectToggle(BaseModel):
    enabled: bool


class PersonIn(BaseModel):
    name: str


class StreamTest(BaseModel):
    url: str


class MqttTest(BaseModel):
    host: str
    port: int = 1883
    username: str = ""
    password: str = ""


def create_app(ctx) -> FastAPI:
    app = FastAPI(title="Argus NVR", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")
    paths, db = ctx.paths, ctx.db
    secret = secrets.token_bytes(32)
    http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))
    gpu_cache: dict[str, Any] = {"ts": 0, "data": None}

    # ------------------------------------------------------------------ Auth
    def _token(user: str) -> str:
        return hmac.new(secret, user.encode(), hashlib.sha256).hexdigest()

    def _authorized(headers, cookies) -> bool:
        a = ctx.cfg.auth
        if not a.username:
            return True
        if hmac.compare_digest(cookies.get("argus_session", ""), _token(a.username)):
            return True
        h = headers.get("authorization", "")
        if h.lower().startswith("basic "):
            try:
                u, _, p = base64.b64decode(h[6:]).decode().partition(":")
                return hmac.compare_digest(u, a.username) and hmac.compare_digest(p, a.password)
            except Exception:
                return False
        return False

    @app.middleware("http")
    async def auth_mw(request: Request, call_next):
        if request.url.path == "/api/health" or _authorized(request.headers, request.cookies):
            resp = await call_next(request)
            if ctx.cfg.auth.username and "argus_session" not in request.cookies:
                resp.set_cookie("argus_session", _token(ctx.cfg.auth.username), httponly=True, samesite="lax",
                                max_age=30 * 86400)
            return resp
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Argus"'})

    @app.on_event("startup")
    async def _startup() -> None:
        ctx.hub.loop = asyncio.get_running_loop()

    # ------------------------------------------------------------------ Basis
    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    def _cam_info(cid: str) -> dict:
        cam = ctx.cfg.cameras[cid]
        w = ctx.workers.get(cid)
        mode = cam.record_mode_effective(ctx.cfg.record)
        tc = cam.live_transcode
        return {"id": cid, "name": cam.name, "has_sub": bool(cam.sub), "faces": cam.faces and bool(ctx.faces),
                "record": mode != "off", "record_mode": mode,
                "zones": {n: z.points for n, z in cam.zones.items()},
                "live_stream": f"{cid}_sub" if cam.sub else cid, "main_stream": cid,
                # H.264-Varianten (Umwandlung auf der GPU), falls der Browser H.265 nicht kann
                "live_stream_h264": (f"{cid}_sub_h264" if cam.sub else f"{cid}_h264") if tc != "off" else None,
                "main_stream_h264": f"{cid}_h264" if tc != "off" else None,
                "transcode": tc, "codecs": ctx.codecs.get(cid, {}),
                "stats": w.stats() if w else None, "counts": ctx.events.counts.get(cid, {})}

    @app.get("/api/config")
    def config():
        return {"version": __version__, "cameras": [_cam_info(c) for c in ctx.cfg.cameras],
                "record": ctx.cfg.record.model_dump(), "faces": ctx.cfg.faces.model_dump(), "faces_active": bool(ctx.faces),
                "objects": ctx.cfg.objects.track, "mqtt": {"enabled": ctx.cfg.mqtt.enabled, "prefix": ctx.cfg.mqtt.topic_prefix}}

    @app.get("/api/cameras")
    def cameras():
        return [_cam_info(c) for c in ctx.cfg.cameras]

    @app.get("/api/cameras/{cid}/latest.jpg")
    def latest(cid: str, boxes: int = 1, w: int = 960):
        wk = ctx.workers.get(cid)
        if not wk:
            raise HTTPException(404)
        data = wk.latest_jpeg(annotate=bool(boxes), max_w=max(160, min(w, 1920)))
        if data is None:
            raise HTTPException(503, "Noch kein Bild")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/cameras/{cid}/detect")
    def toggle_detect(cid: str, body: DetectToggle):
        if cid not in ctx.workers:
            raise HTTPException(404)
        ctx.set_detect(cid, body.enabled)
        return {"enabled": body.enabled}

    # ------------------------------------------------------------------ Aufnahmen / Zeitachse
    from .recorder import export_mp4, hls_playlist, merge_ranges, query_segments

    def _export(cid: str, start: float, end: float, name: str):
        if end <= start or end - start > 3 * 3600:
            raise HTTPException(400, "Ungültiger Zeitraum (max. 3 h)")
        segs = query_segments(db, cid, start, end)
        if not segs:
            raise HTTPException(404, "Keine Aufnahme in diesem Zeitraum")
        out = paths.cache / f"export_{cid}_{int(start)}_{int(time.time() * 1000) % 100000}.mp4"
        res = export_mp4(segs, ctx.storage.resolve, out)
        if not res:
            raise HTTPException(500, "Export fehlgeschlagen")
        return FileResponse(res, media_type="video/mp4", filename=name,
                            background=BackgroundTask(res.unlink, missing_ok=True))

    @app.get("/api/cameras/{cid}/export.mp4")
    def export(cid: str, start: float, end: float):
        t = time.strftime("%Y-%m-%d_%H-%M", time.localtime(start))
        return _export(cid, start, end, f"{cid}_{t}.mp4")

    @app.get("/api/timeline")
    def timeline(camera: str, start: float, end: float):
        segs = db.query("SELECT start_ts, end_ts, event FROM segments WHERE camera=? AND end_ts>? AND start_ts<? "
                        "ORDER BY start_ts", (camera, start, end))
        evs = db.query("SELECT id, label, grp, start_ts, end_ts, sub_label, top_score, thumb FROM events "
                       "WHERE camera=? AND COALESCE(end_ts, ?)>? AND start_ts<? ORDER BY start_ts",
                       (camera, time.time(), start, end))
        for e in evs:
            e["thumb_url"] = f"/media/{e['thumb']}" if e.get("thumb") else None
        first = db.one("SELECT MIN(start_ts) AS t FROM segments WHERE camera=?", (camera,))
        return {"ranges": merge_ranges(segs), "events": evs, "first": first["t"] if first else None,
                "now": time.time(), "pre_roll": ctx.cfg.record.pre_roll}

    @app.get("/api/cameras/{cid}/vod.m3u8")
    def vod(cid: str, start: float, end: float):
        segs = query_segments(db, cid, start, min(end, start + 6 * 3600), limit=12000)
        if not segs:
            raise HTTPException(404, "Keine Aufnahme in diesem Zeitraum")
        body = hls_playlist(segs, lambda s: f"/api/segments/{s['id']}.ts")
        return Response(body, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})

    @app.get("/api/segments/{sid}.ts")
    def segment(sid: int):
        s = db.one("SELECT root, path FROM segments WHERE id=?", (sid,))
        if not s:
            raise HTTPException(404)
        p = ctx.storage.resolve(s["root"], s["path"])
        if not p.exists():
            raise HTTPException(404, "Segment nicht gefunden (Speicherort erreichbar?)")
        return FileResponse(p, media_type="video/mp2t", headers={"Cache-Control": "public, max-age=86400"})

    # ------------------------------------------------------------------ Ereignisse
    def _clip_for(ev: dict) -> dict | None:
        return db.one("SELECT id, start_ts, end_ts, bytes FROM clips WHERE camera=? AND start_ts<=? AND end_ts>=? "
                      "ORDER BY start_ts DESC LIMIT 1", (ev["camera"], ev["start_ts"] + 2, ev["start_ts"]))

    def _event_range(ev: dict) -> tuple[float, float]:
        rc = ctx.cfg.record
        return ev["start_ts"] - rc.pre_roll, (ev.get("end_ts") or time.time()) + rc.post_roll

    def _event_out(ev: dict) -> dict:
        ev = db.event_out(ev)
        ev["clip"] = None
        if ev.get("end_ts"):
            a, b = _event_range(ev)
            seg = db.one("SELECT MIN(start_ts) AS s FROM segments WHERE camera=? AND end_ts>? AND start_ts<?",
                         (ev["camera"], a, b))
            if seg and seg["s"] is not None:
                ev["clip"] = {"hls": f"/api/cameras/{ev['camera']}/vod.m3u8?start={a:.1f}&end={b:.1f}",
                              "url": f"/api/events/{ev['id']}/clip.mp4", "start": seg["s"],
                              "offset": max(0.0, ev["start_ts"] - seg["s"] - 2)}
            else:
                clip = _clip_for(ev)      # MP4-Clips aus älteren Versionen
                if clip:
                    ev["clip"] = {"id": clip["id"], "url": f"/api/clips/{clip['id']}.mp4", "hls": None,
                                  "offset": max(0.0, ev["start_ts"] - clip["start_ts"] - 2)}
        for k in ("snapshot", "thumb", "face_thumb"):
            ev[k + "_url"] = f"/media/{ev[k]}" if ev.get(k) else None
        ev["active"] = ev.get("end_ts") is None
        return ev

    @app.get("/api/events")
    def events(camera: str | None = None, grp: str | None = None, label: str | None = None,
               person: str | None = None, before: float | None = None, after: float | None = None,
               limit: int = 60):
        sql, args = "SELECT * FROM events WHERE 1=1", []
        for col, val in (("camera", camera), ("grp", grp), ("label", label)):
            if val:
                vals = val.split(",")
                sql += f" AND {col} IN ({','.join('?' * len(vals))})"
                args += vals
        if person == "__known__":
            sql += " AND sub_label IS NOT NULL"
        elif person == "__unknown__":
            sql += " AND grp='person' AND sub_label IS NULL AND face_thumb IS NOT NULL"
        elif person:
            sql += " AND sub_label=?"
            args.append(person)
        if before:
            sql += " AND start_ts<?"
            args.append(before)
        if after:
            sql += " AND start_ts>?"
            args.append(after)
        sql += " ORDER BY start_ts DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        return [_event_out(e) for e in db.query(sql, args)]

    @app.get("/api/events/summary")
    def events_summary(days: int = 7):
        since = time.time() - days * 86400
        rows = db.query("SELECT camera, grp, COUNT(*) AS n FROM events WHERE start_ts>? GROUP BY camera, grp", (since,))
        hours = db.query("SELECT CAST(strftime('%H', start_ts, 'unixepoch', 'localtime') AS INTEGER) AS h, "
                         "COUNT(*) AS n FROM events WHERE start_ts>? GROUP BY h", (since,))
        people = db.query("SELECT sub_label AS name, COUNT(*) AS n, MAX(start_ts) AS last FROM events "
                          "WHERE start_ts>? AND sub_label IS NOT NULL GROUP BY sub_label ORDER BY n DESC", (since,))
        return {"by_camera": rows, "by_hour": hours, "people": people}

    @app.get("/api/events/{eid}")
    def event(eid: str):
        ev = db.one("SELECT * FROM events WHERE id=?", (eid,))
        if not ev:
            raise HTTPException(404)
        return _event_out(ev)

    @app.delete("/api/events/{eid}")
    def delete_event(eid: str):
        ev = db.one("SELECT * FROM events WHERE id=?", (eid,))
        if not ev:
            raise HTTPException(404)
        for k in ("snapshot", "thumb"):
            if ev.get(k):
                (paths.media / ev[k]).unlink(missing_ok=True)
        db.execute("DELETE FROM events WHERE id=?", (eid,))
        for s in db.query("SELECT id, path FROM face_samples WHERE event_id=? AND source IN ('event','unknown')", (eid,)):
            (paths.faces / s["path"]).unlink(missing_ok=True)
            db.execute("DELETE FROM face_samples WHERE id=?", (s["id"],))
        # Aufnahme dieses Ereignisses freigeben, wenn kein anderes Ereignis sie braucht
        if ev.get("end_ts"):
            a, b = _event_range(ev)
            rc = ctx.cfg.record
            others = db.one("SELECT COUNT(*) AS n FROM events WHERE camera=? AND start_ts-?<? AND COALESCE(end_ts,?)+?>?",
                            (ev["camera"], rc.pre_roll, b, time.time(), rc.post_roll, a))
            if others and others["n"] == 0:
                cam = ctx.cfg.cameras.get(ev["camera"])
                mode = cam.record_mode_effective(rc) if cam else "events"
                rows = db.query("SELECT id, root, path, bytes FROM segments WHERE camera=? AND end_ts>? AND start_ts<? "
                                "AND event=1", (ev["camera"], a, b))
                if mode == "continuous":
                    for r in rows:
                        db.execute("UPDATE segments SET event=0 WHERE id=?", (r["id"],))
                elif ctx.retention:
                    ctx.retention._delete_segments(rows)
        if ev.get("end_ts"):
            clip = _clip_for(ev)
            if clip:
                others = db.one("SELECT COUNT(*) AS n FROM events WHERE camera=? AND start_ts BETWEEN ? AND ?",
                                (ev["camera"], clip["start_ts"] - 1, clip["end_ts"]))
                if others and others["n"] == 0:
                    c = db.one("SELECT path FROM clips WHERE id=?", (clip["id"],))
                    if c:
                        (paths.media / c["path"]).unlink(missing_ok=True)
                    db.execute("DELETE FROM clips WHERE id=?", (clip["id"],))
        return {"ok": True}

    @app.get("/api/events/{eid}/clip.mp4")
    def event_clip(eid: str):
        ev = db.one("SELECT * FROM events WHERE id=?", (eid,))
        if not ev:
            raise HTTPException(404)
        a, b = _event_range(ev)
        t = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(ev["start_ts"]))
        return _export(ev["camera"], a, b, f"{ev['camera']}_{t}_{ev['label']}.mp4")

    @app.post("/api/events/{eid}/face")
    def correct_face(eid: str, body: FaceAssign):
        if not ctx.faces:
            raise HTTPException(400, "Gesichtserkennung ist deaktiviert")
        pid = ctx.faces.person_id_for(body.person_id, body.name)
        return ctx.faces.correct_event(eid, pid)

    @app.get("/api/clips/{cid}.mp4")
    def clip(cid: str):
        c = db.one("SELECT path FROM clips WHERE id=?", (cid,))
        if not c:
            raise HTTPException(404)
        p = paths.media / c["path"]
        if not p.exists():
            raise HTTPException(404)
        return FileResponse(p, media_type="video/mp4")

    @app.get("/media/{rel:path}")
    def media(rel: str):
        p = (paths.media / rel).resolve()
        if paths.media.resolve() not in p.parents or not p.is_file() or p.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            raise HTTPException(404)
        return FileResponse(p, headers={"Cache-Control": "public, max-age=86400"})

    # ------------------------------------------------------------------ Gesichter
    def _faces():
        if not ctx.faces:
            raise HTTPException(400, "Gesichtserkennung ist deaktiviert")
        return ctx.faces

    @app.get("/api/faces/persons")
    def persons():
        rows = db.query("""
            SELECT p.id, p.name, p.created,
              (SELECT COUNT(*) FROM face_samples s WHERE s.person_id=p.id AND s.source IN ('manual','auto','confirmed')) AS samples,
              (SELECT COUNT(*) FROM face_samples s WHERE s.person_id=p.id AND s.source='auto') AS learned,
              (SELECT path FROM face_samples s WHERE s.person_id=p.id ORDER BY quality DESC LIMIT 1) AS cover,
              (SELECT MAX(start_ts) FROM events e WHERE e.person_id=p.id) AS last_seen,
              (SELECT COUNT(*) FROM events e WHERE e.person_id=p.id) AS events
            FROM persons p ORDER BY p.name COLLATE NOCASE""")
        for r in rows:
            r["cover_url"] = f"/media/faces/{r['cover']}" if r["cover"] else None
        return rows

    @app.post("/api/faces/persons")
    def create_person(body: PersonIn):
        pid = _faces().person_id_for(None, body.name)
        return {"id": pid}

    @app.patch("/api/faces/persons/{pid}")
    def rename_person(pid: int, body: PersonIn):
        _faces().rename_person(pid, body.name)
        return {"ok": True}

    @app.delete("/api/faces/persons/{pid}")
    def delete_person(pid: int):
        _faces().delete_person(pid)
        return {"ok": True}

    @app.get("/api/faces/persons/{pid}/samples")
    def person_samples(pid: int):
        rows = db.query("SELECT id, quality, path, source, event_id, camera, created FROM face_samples "
                        "WHERE person_id=? AND source IN ('manual','auto','confirmed') ORDER BY created DESC", (pid,))
        for r in rows:
            r["url"] = f"/media/faces/{r['path']}"
        return rows

    @app.post("/api/faces/persons/{pid}/upload")
    async def upload(pid: int, files: list[UploadFile] = File(...)):
        f = _faces()
        ok = 0
        for up in files:
            data = await up.read()
            if await asyncio.to_thread(f.enroll_image, data, pid):
                ok += 1
        return {"added": ok, "total": len(files)}

    @app.post("/api/faces/samples/{sid}/assign")
    def assign_sample(sid: int, body: FaceAssign):
        f = _faces()
        f.assign_sample(sid, f.person_id_for(body.person_id, body.name))
        return {"ok": True}

    @app.delete("/api/faces/samples/{sid}")
    def delete_sample(sid: int):
        _faces().assign_sample(sid, None)
        return {"ok": True}

    @app.get("/api/faces/clusters")
    def clusters():
        rows = db.query("SELECT id, count, first_seen, last_seen FROM face_clusters ORDER BY last_seen DESC LIMIT 200")
        out = []
        for c in rows:
            s = db.query("SELECT id, path, quality, event_id, camera, created FROM face_samples WHERE cluster_id=? "
                         "ORDER BY quality DESC LIMIT 8", (c["id"],))
            if not s:
                continue
            for x in s:
                x["url"] = f"/media/faces/{x['path']}"
            suggestion = None
            if ctx.faces:
                import numpy as np
                cen = db.one("SELECT centroid FROM face_clusters WHERE id=?", (c["id"],))
                m = ctx.faces.match(np.frombuffer(cen["centroid"], np.float32))
                if m and m[0][1] >= ctx.cfg.faces.recognition_threshold * 0.75:
                    suggestion = {"person_id": m[0][0], "name": ctx.faces.names.get(m[0][0]), "score": round(m[0][1], 2)}
            out.append({**c, "samples": s, "suggestion": suggestion})
        return out

    @app.post("/api/faces/clusters/{cid}/assign")
    def assign_cluster(cid: int, body: FaceAssign):
        f = _faces()
        pid = f.person_id_for(body.person_id, body.name)
        if pid is None:
            raise HTTPException(400, "Name oder Person fehlt")
        n = f.assign_cluster(cid, pid)
        return {"assigned": n, "person_id": pid}

    @app.delete("/api/faces/clusters/{cid}")
    def delete_cluster(cid: int):
        _faces().delete_cluster(cid)
        return {"ok": True}

    # ------------------------------------------------------------------ System
    def _gpu() -> list[dict] | None:
        if time.time() - gpu_cache["ts"] < 5:
            return gpu_cache["data"]
        data = None
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,utilization.decoder,memory.used,"
                                  "memory.total,temperature.gpu,power.draw", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=3).stdout.strip()
            data = []
            for line in out.splitlines():
                p = [x.strip() for x in line.split(",")]
                num = lambda v: float(v) if v.replace(".", "", 1).isdigit() else None  # noqa: E731
                data.append({"name": p[0], "util": num(p[1]), "decoder": num(p[2]), "mem_used": num(p[3]),
                             "mem_total": num(p[4]), "temp": num(p[5]), "power": num(p[6]) if len(p) > 6 else None})
        except Exception:
            data = None
        gpu_cache.update(ts=time.time(), data=data)
        return data

    @app.get("/api/stats")
    def stats():
        return ctx.stats(gpu=_gpu())

    @app.post("/api/storage/cleanup")
    async def storage_cleanup():
        await asyncio.to_thread(ctx.retention.cleanup)
        return ctx.retention.usage

    # ------------------------------------------------------------------ Einstellungen
    from .config import COCO, GROUPS, SECRET, Config, dump_config, merge_secrets, public_config

    def _validation_msg(e: Exception) -> str:
        errs = getattr(e, "errors", None)
        if callable(errs):
            out = []
            for er in errs()[:6]:
                loc = ".".join(str(x) for x in er.get("loc", ()))
                out.append(f"{loc}: {er.get('msg')}")
            return "; ".join(out)
        return str(e)

    @app.get("/api/settings")
    def get_settings():
        from .runtime import available_providers
        return {
            "config": public_config(ctx.cfg),
            "meta": {
                "labels": [l for l in COCO if l in GROUPS], "groups": GROUPS,
                "providers": available_providers(),
                "first_run": not paths.config.exists() or not ctx.cfg.cameras,
                "config_path": str(paths.config),
                "models": {"yolo": (paths.models / "yolo.onnx").exists(),
                           "faces": (paths.models / "face_det.onnx").exists()},
                "go2rtc": ctx.go2rtc.available,
                "storage": ctx.storage.status(),
            },
        }

    @app.put("/api/settings")
    async def put_settings(request: Request):
        raw = await request.json()
        raw = merge_secrets(raw, ctx.cfg)
        try:
            new = Config.model_validate(raw)
        except Exception as e:
            raise HTTPException(422, _validation_msg(e))
        result = await asyncio.to_thread(ctx.apply_config, new)
        return {"ok": True, **result, "config": public_config(ctx.cfg)}

    @app.get("/api/settings/export")
    def export_settings():
        import yaml
        data = yaml.safe_dump(dump_config(ctx.cfg), sort_keys=False, allow_unicode=True)
        return Response(data, media_type="application/x-yaml",
                        headers={"Content-Disposition": 'attachment; filename="argus-config.yml"'})

    @app.post("/api/settings/import")
    async def import_settings(file: UploadFile = File(...)):
        import yaml
        try:
            new = Config.model_validate(yaml.safe_load(await file.read()) or {})
        except Exception as e:
            raise HTTPException(422, _validation_msg(e))
        result = await asyncio.to_thread(ctx.apply_config, new)
        return {"ok": True, **result}

    @app.post("/api/settings/test-stream")
    def test_stream(body: StreamTest):
        import uuid as _uuid
        url = body.url.strip()
        if not url:
            raise HTTPException(400, "Keine Adresse angegeben")
        name = f"__argus_test_{_uuid.uuid4().hex[:6]}"
        via = url
        used = False
        if ctx.go2rtc.available and ctx.go2rtc.proc and ctx.go2rtc.proc.poll() is None:
            used = ctx.go2rtc.put_stream(name, url)
            if used:
                via = f"rtsp://127.0.0.1:8554/{name}"
        rtsp = ["-rtsp_transport", "tcp", "-timeout", "10000000"] if via.startswith("rtsp") else []
        try:
            pr = subprocess.run(["ffprobe", "-v", "error", *rtsp, "-show_entries",
                                 "stream=codec_type,codec_name,width,height,avg_frame_rate", "-of", "json", via],
                                capture_output=True, text=True, timeout=25)
            if pr.returncode != 0:
                err = (pr.stderr or "").strip().splitlines()
                msg = err[-1] if err else "Stream nicht erreichbar"
                if "401" in msg or "Unauthorized" in msg:
                    msg = "Zugangsdaten falsch (401 Unauthorized)"
                elif "404" in msg or "Not Found" in msg:
                    msg = "Pfad nicht gefunden (404) – Stream-Adresse prüfen"
                elif "timed out" in msg.lower() or "Connection refused" in msg:
                    msg = "Kamera nicht erreichbar – IP/Port prüfen"
                return {"ok": False, "error": msg}
            streams = json.loads(pr.stdout or "{}").get("streams", [])
            v = next((x for x in streams if x.get("codec_type") == "video"), None)
            a = next((x for x in streams if x.get("codec_type") == "audio"), None)
            if not v:
                return {"ok": False, "error": "Kein Videostream gefunden"}
            fps = None
            try:
                n, d = (v.get("avg_frame_rate") or "0/1").split("/")
                fps = round(int(n) / int(d), 1) if int(d) else None
            except Exception:
                pass
            img = None
            sn = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", *rtsp, "-i", via, "-frames:v", "1",
                                 "-vf", "scale=960:-2", "-f", "image2", "-c:v", "mjpeg", "-q:v", "5", "pipe:1"],
                                capture_output=True, timeout=25)
            if sn.returncode == 0 and sn.stdout:
                img = "data:image/jpeg;base64," + base64.b64encode(sn.stdout).decode()
            return {"ok": True, "codec": v.get("codec_name"), "width": v.get("width"), "height": v.get("height"),
                    "fps": fps, "audio": a.get("codec_name") if a else None, "image": img}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Zeitüberschreitung – Kamera antwortet nicht"}
        finally:
            if used:
                ctx.go2rtc.delete_stream(name)

    @app.post("/api/settings/test-mqtt")
    def test_mqtt(body: MqttTest):
        from .mqtt import test_connection
        pw = ctx.cfg.mqtt.password if body.password == SECRET else body.password
        ok, msg = test_connection(body.host, body.port, body.username, pw)
        return {"ok": ok, "message": msg}

    @app.post("/api/settings/test-storage")
    async def test_storage(request: Request):
        from .config import StorageCfg
        from .nas import Storage
        raw = await request.json()
        if raw.get("password") == SECRET:
            raw["password"] = ctx.cfg.storage.password
        try:
            sc = StorageCfg.model_validate(raw)
        except Exception as e:
            raise HTTPException(422, _validation_msg(e))
        return await asyncio.to_thread(Storage.test, sc)

    @app.get("/api/settings/onvif")
    async def onvif(src: str | None = None):
        try:
            r = await http.get(f"http://{GO2RTC}/go2rtc/api/onvif", params={"src": src} if src else None, timeout=20)
        except httpx.HTTPError:
            raise HTTPException(502, "go2rtc nicht erreichbar")
        if r.status_code == 404:
            return {"sources": []}
        if r.status_code >= 400:
            raise HTTPException(400, r.text.strip() or "ONVIF-Abfrage fehlgeschlagen")
        return r.json()

    @app.get("/api/cameras/{cid}/frame.jpg")
    def raw_frame(cid: str, w: int = 1280):
        """Unbearbeitetes Bild (für den Zonen-Editor)."""
        wk = ctx.workers.get(cid)
        if not wk:
            raise HTTPException(404)
        data = wk.latest_jpeg(annotate=False, max_w=max(160, min(w, 1920)), quality=80)
        if data is None:
            raise HTTPException(503, "Noch kein Bild")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------ WebSocket Push
    @app.websocket("/api/ws")
    async def ws(sock: WebSocket):
        if not _authorized(sock.headers, sock.cookies):
            await sock.close(code=4401)
            return
        await sock.accept()
        q: asyncio.Queue = asyncio.Queue()
        ctx.hub.clients.add(q)
        try:
            await sock.send_json({"type": "hello", "version": __version__})
            while True:
                msg = await q.get()
                await sock.send_text(json.dumps(msg, default=str))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            ctx.hub.clients.discard(q)

    # ------------------------------------------------------------------ go2rtc Proxy (ein Port für alles)
    @app.websocket("/go2rtc/{path:path}")
    async def go2rtc_ws(sock: WebSocket, path: str):
        if not _authorized(sock.headers, sock.cookies):
            await sock.close(code=4401)
            return
        import websockets
        await sock.accept()
        url = f"ws://{GO2RTC}/go2rtc/{path}" + (f"?{sock.url.query}" if sock.url.query else "")
        try:
            async with websockets.connect(url, max_size=None, ping_interval=None) as up:
                async def c2u():
                    while True:
                        m = await sock.receive()
                        if m["type"] == "websocket.disconnect":
                            return
                        if m.get("text") is not None:
                            await up.send(m["text"])
                        elif m.get("bytes") is not None:
                            await up.send(m["bytes"])

                async def u2c():
                    async for m in up:
                        if isinstance(m, str):
                            await sock.send_text(m)
                        else:
                            await sock.send_bytes(m)

                tasks = [asyncio.create_task(c2u()), asyncio.create_task(u2c())]
                _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
        except Exception as e:
            log.debug("go2rtc ws: %s", e)
        finally:
            try:
                await sock.close()
            except Exception:
                pass

    @app.api_route("/go2rtc/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def go2rtc_http(path: str, request: Request):
        url = f"http://{GO2RTC}/go2rtc/{path}"
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        try:
            req = http.build_request(request.method, url, params=request.query_params, headers=headers,
                                     content=await request.body())
            r = await http.send(req, stream=True)
        except httpx.HTTPError:
            return JSONResponse({"error": "go2rtc nicht erreichbar"}, status_code=502)
        out_headers = {k: v for k, v in r.headers.items() if k.lower() not in HOP}
        return StreamingResponse(r.aiter_raw(), status_code=r.status_code, headers=out_headers,
                                 background=BackgroundTask(r.aclose))

    # ------------------------------------------------------------------ Web-UI
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest")
    def manifest():
        return JSONResponse({"name": "Argus NVR", "short_name": "Argus", "start_url": "/", "display": "standalone",
                             "background_color": "#0b1020", "theme_color": "#0b1020",
                             "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}]})

    return app
