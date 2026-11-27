"""go2rtc als Unterprozess: eine Verbindung pro Kamera, verteilt an Erkennung, Aufnahme, Live-Bild und HA."""
from __future__ import annotations

import copy
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

import yaml

from .config import Config

log = logging.getLogger("camspire.go2rtc")


def build_config(cfg: Config) -> dict:
    streams: dict[str, list[str]] = {}
    for cid, cam in cfg.cameras.items():
        streams[cid] = [cam.main]
        if cam.sub:
            streams[f"{cid}_sub"] = [cam.sub]
        if cam.live_transcode != "off":
            # H.264-Variante fürs Live-Bild (Browser können H.265 oft nicht). go2rtc startet die
            # Umwandlung nur, wenn jemand zuschaut – per NVENC auf der Grafikkarte.
            streams[f"{cid}_h264"] = [f"ffmpeg:{cid}#video=h264#hardware"]
            if cam.sub:
                streams[f"{cid}_sub_h264"] = [f"ffmpeg:{cid}_sub#video=h264#hardware"]
    conf = {
        "api": {"listen": ":1984", "base_path": "/go2rtc", "origin": "*"},
        "rtsp": {"listen": ":8554"},
        "webrtc": {"listen": ":8555"},
        "log": {"level": "warn"},
        "streams": streams,
    }
    if cfg.webrtc.candidates:
        conf["webrtc"]["candidates"] = list(cfg.webrtc.candidates) + ["stun:8555"]
    extra = copy.deepcopy(cfg.go2rtc or {})
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(conf.get(k), dict):
            conf[k].update(v)
        else:
            conf[k] = v
    return conf


class Go2rtc:
    def __init__(self, cfg: Config, path: Path = Path("/tmp/go2rtc.yaml")) -> None:
        self.cfg = cfg
        self.path = path
        self.proc: subprocess.Popen | None = None
        self.running = False
        self.available = shutil.which("go2rtc") is not None

    def start(self) -> None:
        self.path.write_text(yaml.safe_dump(build_config(self.cfg), sort_keys=False))
        if not self.available:
            log.warning("go2rtc nicht gefunden – Live-Bild/Restream deaktiviert")
            return
        self.running = True
        threading.Thread(target=self._run, name="go2rtc", daemon=True).start()

    def _run(self) -> None:
        while self.running:
            log.info("starte go2rtc")
            self.proc = subprocess.Popen(["go2rtc", "-config", str(self.path)],
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            assert self.proc.stdout
            for line in self.proc.stdout:
                line = line.strip()
                if line:
                    log.info("go2rtc: %s", line[-300:])
            self.proc.wait()
            if self.running:
                if getattr(self, "_quick", False):
                    self._quick = False
                    time.sleep(0.3)
                    continue
                log.warning("go2rtc beendet (Code %s) – Neustart in 3 s", self.proc.returncode)
                time.sleep(3)

    def stop(self) -> None:
        self.running = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    # ------------------------------------------------------------------ Live-Änderungen
    def write(self, cfg: Config) -> None:
        self.cfg = cfg
        self.path.write_text(yaml.safe_dump(build_config(cfg), sort_keys=False))

    def restart(self, cfg: Config) -> None:
        """Neue Konfiguration schreiben und go2rtc neu starten (z. B. WebRTC-Kandidaten geändert)."""
        self.write(cfg)
        if self.proc and self.proc.poll() is None:
            self._quick = True
            self.proc.terminate()
        elif self.available and not self.running:
            self.start()

    def _api(self, method: str, **params) -> bool:
        import httpx
        try:
            r = httpx.request(method, "http://127.0.0.1:1984/go2rtc/api/streams", params=params, timeout=10)
            return r.status_code < 400
        except Exception as e:
            log.warning("go2rtc API %s: %s", method, e)
            return False

    def put_stream(self, name: str, src: str) -> bool:
        return self._api("PUT", name=name, src=src)

    def delete_stream(self, name: str) -> bool:
        return self._api("DELETE", src=name)

    def update_streams(self, old: dict[str, list[str]], new: dict[str, list[str]]) -> None:
        """Nur geänderte Streams anfassen – andere Kameras laufen ungestört weiter."""
        for name in old.keys() - new.keys():
            self.delete_stream(name)
        for name, src in new.items():
            if old.get(name) != src:
                self.delete_stream(name)
                self.put_stream(name, src[0])
