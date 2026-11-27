"""Speicherort für Aufnahmen: lokal, Netzwerkfestplatte (SMB/NFS) oder ein eingebundener Ordner.

Die Freigabe wird im Container eingebunden (braucht cap_add: SYS_ADMIN, siehe docker-compose.yml).
Fällt das NAS aus, schreibt CamSpire automatisch lokal weiter und bindet es im Hintergrund neu ein.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .config import Paths, StorageCfg

log = logging.getLogger("camspire.storage")

MOUNT = Path("/mnt/camspire-nas")
CRED = Path("/run/camspire-smb.cred")


def _mount_cmd(sc: StorageCfg, target: Path, cred: Path) -> list[str]:
    if sc.type == "smb":
        share = sc.share.strip().strip("/").replace("\\", "/")
        cred.write_text(f"username={sc.username}\npassword={sc.password}\n" + (f"domain={sc.domain}\n" if sc.domain else ""))
        os.chmod(cred, 0o600)
        opts = [f"credentials={cred}", f"vers={sc.smb_version or '3.0'}", "uid=0", "gid=0", "iocharset=utf8",
                "file_mode=0660", "dir_mode=0770", "noserverino", "soft", "nobrl"]
        if not sc.username:
            opts[0] = "guest"
        if sc.options:
            opts.append(sc.options)
        return ["mount", "-t", "cifs", f"//{sc.server.strip()}/{share}", str(target), "-o", ",".join(opts)]
    if sc.type == "nfs":
        export = "/" + sc.share.strip().lstrip("/")
        opts = ["soft", "timeo=100", "retrans=3", "nolock"] + ([sc.options] if sc.options else [])
        return ["mount", "-t", "nfs", f"{sc.server.strip()}:{export}", str(target), "-o", ",".join(opts)]
    raise ValueError(sc.type)


def _explain(err: str) -> str:
    e = err.lower()
    if "mount error(13)" in e or "logon failure" in e:
        return "Anmeldung abgelehnt – Benutzer oder Passwort falsch"
    if "mount error(2)" in e or "no such file" in e:
        return "Freigabe/Pfad auf dem NAS nicht gefunden"
    if "mount error(112)" in e or "host is down" in e or "mount error(113)" in e or "no route" in e or "timed out" in e:
        return "NAS nicht erreichbar – IP-Adresse prüfen"
    if "mount error(95)" in e or "operation not supported" in e:
        return "SMB-Version passt nicht – z. B. 2.0 oder 3.0 probieren"
    if "permission denied" in e or "not permitted" in e or "must be superuser" in e or "only root" in e:
        return ("Container darf nichts einbinden – docker-compose.yml aktualisieren (cap_add: SYS_ADMIN) "
                "oder NAS auf dem Host einbinden und Typ „Ordner“ wählen")
    if "unknown filesystem type" in e:
        mod = "nfs" if "nfs" in e else "cifs"
        return (f"Dem Server fehlt das Kernelmodul „{mod}“ – einmalig auf dem Host ausführen: "
                f"sudo modprobe {mod} && echo {mod} | sudo tee /etc/modules-load.d/{mod}.conf")
    if "access denied" in e:
        return "Zugriff verweigert – NFS-Freigabe für die IP des Servers erlauben"
    lines = [ln.strip() for ln in err.strip().splitlines() if ln.strip() and "dmesg" not in ln]
    return lines[-1] if lines else "Unbekannter Fehler"


def _writable(root: Path, timeout: int = 10) -> tuple[bool, str]:
    """Schreibtest mit Zeitlimit (ein hängendes NAS darf CamSpire nicht blockieren)."""
    probe = root / ".camspire-probe"
    try:
        r = subprocess.run(["timeout", str(timeout), "sh", "-c",
                            f'mkdir -p "{root}" && echo ok > "{probe}" && cat "{probe}" && rm -f "{probe}"'],
                           capture_output=True, text=True, timeout=timeout + 5)
        if r.returncode == 0 and "ok" in r.stdout:
            return True, "ok"
        return False, (r.stderr.strip() or "Schreiben nicht möglich")
    except subprocess.TimeoutExpired:
        return False, "Zeitüberschreitung beim Schreiben"


def _usage(root: Path) -> dict | None:
    try:
        r = subprocess.run(["timeout", "5", "df", "-B1", "--output=size,avail", str(root)],
                           capture_output=True, text=True, timeout=8)
        total, free = r.stdout.strip().splitlines()[-1].split()
        return {"total": int(total), "free": int(free)}
    except Exception:
        return None


class Storage:
    def __init__(self, paths: Paths, cfg: StorageCfg) -> None:
        self.paths = paths
        self.cfg = cfg
        self.ok = cfg.type == "local"
        self.error = ""
        self.lock = threading.RLock()
        self._last_try = 0.0
        self.usage: dict | None = None

    # ------------------------------------------------------------------ Ziele
    @property
    def remote_root(self) -> Path | None:
        sc = self.cfg
        if sc.type == "local":
            return None
        base = Path(sc.path) if sc.type == "path" else MOUNT
        return base / sc.subfolder.strip("/") if sc.subfolder.strip("/") else base

    def roots(self) -> dict[str, Path]:
        r = {"local": self.paths.media}
        if self.remote_root:
            r["nas"] = self.remote_root
        return r

    def target(self) -> tuple[str, Path]:
        """Wohin das nächste Segment geschrieben wird."""
        if self.cfg.type != "local" and self.ok:
            return "nas", self.remote_root  # type: ignore[return-value]
        return "local", self.paths.media

    def resolve(self, root: str, rel: str) -> Path:
        base = self.roots().get(root)
        if base is None:
            base = MOUNT / self.cfg.subfolder.strip("/") if root == "nas" else self.paths.media
        return base / rel

    def report_failure(self, err: str) -> None:
        with self.lock:
            if self.ok and self.cfg.type != "local":
                log.error("Netzwerkspeicher nicht beschreibbar (%s) – schreibe lokal weiter", err)
            self.ok = self.cfg.type == "local"
            self.error = err

    # ------------------------------------------------------------------ Einbinden
    def start(self) -> None:
        self.apply(self.cfg, force=True)
        threading.Thread(target=self._watchdog, name="storage", daemon=True).start()

    def apply(self, cfg: StorageCfg, force: bool = False) -> None:
        with self.lock:
            changed = cfg.model_dump() != self.cfg.model_dump()
            self.cfg = cfg
            if not (changed or force):
                return
            self._umount()
            self.ok, self.error = cfg.type == "local", ""
            if cfg.type == "local":
                return
            self._connect()

    def _connect(self) -> None:
        sc = self.cfg
        self._last_try = time.time()
        if sc.type == "path":
            p = Path(sc.path)
            if not sc.path or not p.is_dir():
                self.ok, self.error = False, f"Ordner {sc.path or '(leer)'} existiert im Container nicht"
                log.error("Speicherort: %s", self.error)
                return
        else:
            MOUNT.mkdir(parents=True, exist_ok=True)
            if not os.path.ismount(MOUNT):
                try:
                    r = subprocess.run(_mount_cmd(sc, MOUNT, CRED), capture_output=True, text=True, timeout=30)
                except subprocess.TimeoutExpired:
                    self.ok, self.error = False, "NAS antwortet nicht (Zeitüberschreitung)"
                    return
                if r.returncode != 0:
                    self.ok, self.error = False, _explain(r.stderr + r.stdout)
                    log.error("NAS einbinden fehlgeschlagen: %s", self.error)
                    return
        ok, msg = _writable(self.remote_root)  # type: ignore[arg-type]
        self.ok, self.error = ok, ("" if ok else msg)
        if ok:
            log.info("Aufnahmen werden auf %s gespeichert", self.describe())
            self.usage = _usage(self.remote_root)  # type: ignore[arg-type]
        else:
            log.error("Netzwerkspeicher nicht beschreibbar: %s", msg)

    def _umount(self) -> None:
        if os.path.ismount(MOUNT):
            subprocess.run(["umount", "-l", str(MOUNT)], capture_output=True, timeout=20)

    def _watchdog(self) -> None:
        while True:
            time.sleep(30)
            try:
                with self.lock:
                    if self.cfg.type == "local":
                        continue
                    if self.ok:
                        ok, msg = _writable(self.remote_root)  # type: ignore[arg-type]
                        if not ok:
                            self.report_failure(msg)
                        else:
                            self.usage = _usage(self.remote_root)  # type: ignore[arg-type]
                    elif time.time() - self._last_try > 60:
                        self._umount()
                        self._connect()
            except Exception:
                log.exception("Speicher-Überwachung")

    def describe(self) -> str:
        sc = self.cfg
        if sc.type == "smb":
            return f"//{sc.server}/{sc.share.strip('/')}/{sc.subfolder}"
        if sc.type == "nfs":
            return f"{sc.server}:/{sc.share.strip('/')}/{sc.subfolder}"
        if sc.type == "path":
            return str(self.remote_root)
        return str(self.paths.media)

    def status(self) -> dict:
        return {"type": self.cfg.type, "ok": self.ok, "error": self.error, "target": self.describe(),
                "writing_to": self.target()[0], "usage": self.usage}

    # ------------------------------------------------------------------ Test aus den Einstellungen
    @staticmethod
    def test(sc: StorageCfg) -> dict:
        if sc.type == "local":
            return {"ok": True, "message": "Lokaler Speicher"}
        if sc.type == "path":
            p = Path(sc.path) / sc.subfolder.strip("/")
            if not sc.path or not Path(sc.path).is_dir():
                return {"ok": False, "message": f"Ordner {sc.path or '(leer)'} gibt es im Container nicht – "
                                                 "in docker-compose.yml als Volume einbinden"}
            ok, msg = _writable(p)
            u = _usage(p) if ok else None
            return {"ok": ok, "message": "Ordner beschreibbar" if ok else msg, "usage": u}
        if not sc.server or not sc.share:
            return {"ok": False, "message": "Server und Freigabe eintragen"}
        tmp = Path(tempfile.mkdtemp(prefix="camspire-nas-test-"))
        cred = Path(tempfile.mktemp(prefix="camspire-cred-"))
        try:
            try:
                r = subprocess.run(_mount_cmd(sc, tmp, cred), capture_output=True, text=True, timeout=30)
            except subprocess.TimeoutExpired:
                return {"ok": False, "message": "NAS antwortet nicht (Zeitüberschreitung)"}
            if r.returncode != 0:
                return {"ok": False, "message": _explain(r.stderr + r.stdout)}
            target = tmp / sc.subfolder.strip("/") if sc.subfolder.strip("/") else tmp
            ok, msg = _writable(target)
            u = _usage(tmp)
            return {"ok": ok, "message": "Verbindung erfolgreich, Schreiben funktioniert" if ok
                    else f"Eingebunden, aber nicht beschreibbar: {msg}", "usage": u}
        finally:
            subprocess.run(["umount", "-l", str(tmp)], capture_output=True, timeout=20)
            shutil.rmtree(tmp, ignore_errors=True)
            cred.unlink(missing_ok=True)
