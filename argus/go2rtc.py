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

log = logging.getLogger("argus.go2rtc")


def build_config(cfg: Config) -> dict:
    streams: dict[str, list[str]] = {}
    for cid, cam in cfg.cameras.items():
        streams[cid] = [cam.main]
        if cam.sub:
            streams[f"{cid}_sub"] = [cam.sub]
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
                log.warning("go2rtc beendet (Code %s) – Neustart in 3 s", self.proc.returncode)
                time.sleep(3)

    def stop(self) -> None:
        self.running = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
