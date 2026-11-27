"""MQTT + Home-Assistant-Discovery.

Entitäten pro Kamera (Gerät "Argus <Kamera>"):
  binary_sensor  Person / Fahrzeug / Tier erkannt (+ pro Zone)
  sensor         Anzahl Personen / Fahrzeuge / Tiere, zuletzt erkanntes Gesicht
  camera         letzter Snapshot pro Gruppe
  switch         Erkennung an/aus
Topic argus/events liefert jedes Ereignis als JSON (new/update/end) für Automationen.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

import cv2
import numpy as np

from .config import Config

log = logging.getLogger("argus.mqtt")

GROUP_DE = {"person": "Person", "vehicle": "Fahrzeug", "animal": "Tier"}
ICONS = {"person": "mdi:account", "vehicle": "mdi:car", "animal": "mdi:paw"}


class Mqtt:
    def __init__(self, cfg: Config, on_detect_switch: Callable[[str, bool], None]) -> None:
        import paho.mqtt.client as mqtt
        self.cfg = cfg
        self.m = cfg.mqtt
        self.p = self.m.topic_prefix
        self.on_detect_switch = on_detect_switch
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"argus-{int(time.time())}")
        if self.m.username:
            self.client.username_pw_set(self.m.username, self.m.password)
        self.client.will_set(f"{self.p}/available", "offline", retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.connected = False
        self._snap_ts: dict[str, float] = {}
        self.lock = threading.Lock()

    def start(self) -> None:
        self.client.reconnect_delay_set(1, 60)
        self.client.connect_async(self.m.host, self.m.port, keepalive=30)
        self.client.loop_start()

    def stop(self) -> None:
        try:
            self.client.publish(f"{self.p}/available", "offline", retain=True).wait_for_publish(2)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    def _pub(self, topic: str, payload: Any, retain: bool = False) -> None:
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        self.client.publish(topic, payload, qos=0, retain=retain)

    # ------------------------------------------------------------------ Verbindung
    def _on_connect(self, client, userdata, flags, rc, props=None) -> None:
        if rc != 0:
            log.warning("MQTT-Verbindung abgelehnt: %s", rc)
            return
        self.connected = True
        log.info("MQTT verbunden mit %s:%s", self.m.host, self.m.port)
        self._pub(f"{self.p}/available", "online", retain=True)
        client.subscribe(f"{self.p}/+/detect/set")
        client.subscribe(f"{self.m.discovery_prefix}/status")
        self._discovery()

    def _on_message(self, client, userdata, msg) -> None:
        payload = msg.payload.decode(errors="ignore").strip()
        if msg.topic == f"{self.m.discovery_prefix}/status" and payload == "online":
            self._discovery()   # HA neu gestartet
            return
        parts = msg.topic.split("/")
        if len(parts) == 4 and parts[2] == "detect" and parts[3] == "set":
            on = payload.upper() == "ON"
            self.on_detect_switch(parts[1], on)
            self.detect_state(parts[1], on)

    # ------------------------------------------------------------------ Discovery
    def _discovery(self) -> None:
        for cid, cam in self.cfg.cameras.items():
            for topic, payload in self.discovery_items(cid, cam):
                self._pub(topic, payload, retain=True)
            self.detect_state(cid, cam.detect.enabled)

    def remove_camera(self, cid: str, cam) -> None:
        """Entitäten einer gelöschten/umbenannten Kamera aus Home Assistant entfernen."""
        for topic, _ in self.discovery_items(cid, cam, all_zones=True):
            self.client.publish(topic, b"", retain=True)

    def discovery_items(self, cid: str, cam, all_zones: bool = False) -> list[tuple[str, dict]]:
        dp = self.m.discovery_prefix
        avail = [{"topic": f"{self.p}/available"}]
        dev = {"identifiers": [f"argus_{cid}"], "name": f"Argus {cam.name}", "manufacturer": "Argus NVR",
               "model": "Kamera"}
        items: list[tuple[str, dict]] = []
        for g in ("person", "vehicle", "animal"):
            uid = f"argus_{cid}_{g}"
            items.append((f"{dp}/binary_sensor/{uid}/config", {
                "name": f"{GROUP_DE[g]} erkannt", "unique_id": uid, "device": dev, "availability": avail,
                "state_topic": f"{self.p}/{cid}/{g}", "device_class": "occupancy",
                "value_template": "{{ 'ON' if value|int(0) > 0 else 'OFF' }}", "icon": ICONS[g]}))
            items.append((f"{dp}/sensor/{uid}_count/config", {
                "name": f"Anzahl {GROUP_DE[g]}", "unique_id": f"{uid}_count", "device": dev, "availability": avail,
                "state_topic": f"{self.p}/{cid}/{g}", "state_class": "measurement", "icon": ICONS[g]}))
            items.append((f"{dp}/camera/{uid}_snapshot/config", {
                "name": f"Letzte {GROUP_DE[g]}", "unique_id": f"{uid}_snapshot", "device": dev,
                "availability": avail, "topic": f"{self.p}/{cid}/{g}/snapshot"}))
            for z in cam.zones:
                zuid = f"argus_{cid}_{z}_{g}"
                items.append((f"{dp}/binary_sensor/{zuid}/config", {
                    "name": f"{GROUP_DE[g]} in {z}", "unique_id": zuid, "device": dev, "availability": avail,
                    "state_topic": f"{self.p}/{cid}/zone/{z}/{g}", "device_class": "occupancy",
                    "value_template": "{{ 'ON' if value|int(0) > 0 else 'OFF' }}", "icon": ICONS[g]}))
        if all_zones or (cam.faces and self.cfg.faces.enabled):
            items.append((f"{dp}/sensor/argus_{cid}_face/config", {
                "name": "Erkanntes Gesicht", "unique_id": f"argus_{cid}_face", "device": dev,
                "availability": avail, "state_topic": f"{self.p}/{cid}/face",
                "json_attributes_topic": f"{self.p}/{cid}/face/attributes", "icon": "mdi:face-recognition"}))
        items.append((f"{dp}/switch/argus_{cid}_detect/config", {
            "name": "Erkennung", "unique_id": f"argus_{cid}_detect", "device": dev, "availability": avail,
            "command_topic": f"{self.p}/{cid}/detect/set", "state_topic": f"{self.p}/{cid}/detect/state",
            "icon": "mdi:motion-sensor"}))
        return items

    # ------------------------------------------------------------------ Zustände
    def counts(self, cam: str, counts: dict[str, int], prev: dict[str, int]) -> None:
        for k, v in counts.items():
            if prev.get(k) != v:
                if "/" in k:
                    z, g = k.split("/", 1)
                    self._pub(f"{self.p}/{cam}/zone/{z}/{g}", str(v), retain=True)
                else:
                    self._pub(f"{self.p}/{cam}/{k}", str(v), retain=True)

    def event(self, action: str, ev: dict) -> None:
        self._pub(f"{self.p}/events", {"type": action, **ev})

    def face(self, cam: str, name: str, score: float, event_id: str) -> None:
        self._pub(f"{self.p}/{cam}/face", name, retain=True)
        self._pub(f"{self.p}/{cam}/face/attributes",
                  {"score": round(score, 3), "event_id": event_id, "time": time.strftime("%Y-%m-%dT%H:%M:%S")},
                  retain=True)

    def snapshot(self, cam: str, group: str, frame: np.ndarray | None, box) -> None:
        if frame is None:
            return
        key = f"{cam}/{group}"
        now = time.time()
        if now - self._snap_ts.get(key, 0) < 2:
            return
        self._snap_ts[key] = now
        img = frame
        if img.shape[1] > 960:
            img = cv2.resize(img, (960, int(img.shape[0] * 960 / img.shape[1])), interpolation=cv2.INTER_AREA)
            s = 960 / frame.shape[1]
            box = [v * s for v in box]
        img = img.copy()
        x1, y1, x2, y2 = [int(v) for v in box]
        cv2.rectangle(img, (x1, y1), (x2, y2), (80, 200, 255), 2)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if ok:
            self.client.publish(f"{self.p}/{cam}/{group}/snapshot", buf.tobytes(), retain=True)

    def detect_state(self, cam: str, on: bool) -> None:
        self._pub(f"{self.p}/{cam}/detect/state", "ON" if on else "OFF", retain=True)

    def stats(self, data: dict) -> None:
        self._pub(f"{self.p}/stats", data)


def test_connection(host: str, port: int, username: str = "", password: str = "", timeout: float = 6) -> tuple[bool, str]:
    """Kurzer Verbindungstest für die Einstellungsseite."""
    import paho.mqtt.client as mqtt
    result: dict[str, Any] = {}
    done = threading.Event()
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"argus-test-{int(time.time())}")
    if username:
        c.username_pw_set(username, password)

    def on_connect(client, userdata, flags, rc, props=None):
        result["rc"] = rc
        done.set()
    c.on_connect = on_connect
    try:
        c.connect(host, int(port), keepalive=10)
    except Exception as e:
        return False, f"Server nicht erreichbar: {e}"
    c.loop_start()
    done.wait(timeout)
    c.loop_stop()
    try:
        c.disconnect()
    except Exception:
        pass
    if "rc" not in result:
        return False, "Keine Antwort vom Broker (Zeitüberschreitung)"
    rc = result["rc"]
    if rc == 0:
        return True, "Verbindung erfolgreich"
    msg = str(rc)
    if "auth" in msg.lower() or "credential" in msg.lower() or getattr(rc, "value", 0) in (4, 5, 134, 135):
        return False, "Benutzername oder Passwort falsch"
    return False, f"Broker lehnt ab: {msg}"
