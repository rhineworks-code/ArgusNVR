# CamSpire NVR

Selbst gehosteter Netzwerk-Videorekorder in **einem** Docker-Container – mit KI-Erkennung auf der NVIDIA-GPU,
lernender Gesichtserkennung und Home-Assistant-Anbindung über MQTT.

| | |
|---|---|
| **Kameras** | alles, was RTSP/ONVIF/RTMP/HTTP kann – go2rtc holt jeden Stream nur **einmal** ab und verteilt ihn |
| **Dekodierung** | NVDEC auf der GPU (automatischer Rückfall auf CPU) |
| **Objekte** | YOLO11 (ONNX, CUDA): Personen, Fahrzeuge (Auto, LKW, Bus, Motorrad, Fahrrad), Tiere (Hund, Katze, Vogel, Pferd …) |
| **Gesichter** | SCRFD + ArcFace (512-d), Gruppierung unbekannter Gesichter, Auto-Lernen, Korrektur per Klick |
| **Speicher** | Aufnahme nur bei Ereignissen, ohne Neukodierung, Vorlauf aus dem RAM, harte GB-Obergrenze |
| **Live** | WebRTC / MSE im Browser, Restream per RTSP für Home Assistant |
| **HA** | MQTT-Discovery: Sensoren, Belegung pro Zone, Snapshots, erkannte Person, Erkennung an/aus |

## Umstieg von „Argus NVR“

CamSpire hieß vorher Argus. Beim ersten Start übernimmt CamSpire alles automatisch:
Datenbank (`argus.db` → `camspire.db`), Einstellungen, Aufnahmen, Gesichter. Das MQTT-Präfix wechselt von
`argus` auf `camspire`, die alten `argus_*`-Entitäten werden aus Home Assistant entfernt und als `camspire_*` neu angelegt.
Alte `.env`-Einträge (`ARGUS_RTSP_PORT` …) funktionieren weiter.

## Warum so sparsam?

* **Bewegungs-Vorfilter:** Die GPU rechnet nur, wenn sich im Bild etwas bewegt (oder ein Objekt verfolgt wird).
  Ein ruhiger Hof kostet praktisch nichts.
* **Keine Neukodierung:** Aufnahmen sind 1:1-Kopien des Kamera-Streams (`-c copy`), H.264 und H.265.
  H.265 an der Kamera einstellen halbiert den Platzbedarf nochmal.
* **Vorlauf im RAM:** Die letzten Sekunden liegen in einem tmpfs. Auf die Platte kommt nur, was zu einem Ereignis gehört.
* **Ein Clip für überlappende Ereignisse:** Laufen Person und Auto gleichzeitig durchs Bild, entsteht nur **ein** Clip.
* **Stillstand:** Ein geparktes Auto erzeugt nach 30 s kein Dauer-Ereignis mehr.
* **Aufräumen:** Tage-Limit + harte Obergrenze in GB + mind. 5 % frei auf dem Datenträger.
* Optional `record.source: sub` – dann wird nur der Substream gespeichert (etwa 1/10 der Größe).

Faustregel: 4-MP-Kamera, H.265, ~40 Ereignisse/Tag à 20 s → **etwa 150–300 MB pro Tag und Kamera**.

## Voraussetzungen (Host)

1. NVIDIA-Treiber **≥ 525** (`nvidia-smi` funktioniert auf dem Host)
2. [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html):
   ```bash
   sudo apt install nvidia-container-toolkit
   sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
   docker run --rm --gpus all ubuntu nvidia-smi   # Test
   ```
3. Läuft Docker in einer **Proxmox-LXC**: Die GPU-Geräte (`/dev/nvidia*`) müssen in den Container durchgereicht
   und dort derselbe Treiber (ohne Kernelmodul, `--no-kernel-module`) installiert sein. In einer VM mit PCI-Passthrough
   gilt einfach Punkt 1–2.

CUDA/cuDNN bringt das Image selbst mit (als pip-Pakete) – auf dem Host muss nichts davon installiert werden.

## Installation

GitHub Actions baut bei jedem Push auf `main` das Image `ghcr.io/rhineworks-code/camspire-nvr:latest`.
Da das Repo privat ist, braucht der Server einmalig einen Login (GitHub-Token mit `read:packages`):

```bash
echo <TOKEN> | docker login ghcr.io -u rhineworks-code --password-stdin

git clone https://github.com/rhineworks-code/CamSpireNVR.git ~/camspire-nvr
cd ~/camspire-nvr
docker compose pull && docker compose up -d
docker compose logs -f camspire
```

Danach **http://SERVER:5080** öffnen – beim ersten Start landest du direkt in den **Einstellungen**
und richtest dort Kameras, MQTT/Home Assistant und alles Weitere ein.

**Updates:** `./update.sh` (holt Repo + neuestes Image, startet neu, räumt alte Images weg).

Selbst bauen statt laden (z. B. anderes YOLO-Modell): `docker compose build && docker compose up -d` – erster Build ~10 min.

Oberfläche: **http://SERVER:5080** · API-Doku: **http://SERVER:5080/api/docs**

## Verlauf (Wiedergabe mit Zeitachse)

Unter **Verlauf** läuft rechts eine Zeitachse (oben = jetzt, nach unten älter):
türkis = Aufnahme mit Ereignis, blau = Daueraufnahme, farbige Marker = Ereignisse
(gelb Person, blau Fahrzeug, grün Tier). Ziehen oder scrollen verschiebt die Zeit,
Strg + Mausrad bzw. +/− zoomt, Klick auf einen Marker springt zum Ereignis.
Abspielen mit 1×–16×, ⏮/⏭ springt zum vorigen/nächsten Ereignis, „Ausschnitt“ lädt die Stelle als MP4.
Tastatur: Leertaste = Play/Pause, ←/→ = 5 s (mit Shift 60 s).

## Speicherort: lokal oder Netzwerkfestplatte

**Einstellungen → Aufnahme & Speicher → Speicherort**: Lokal, Netzwerkfestplatte (SMB, z. B. Synology),
NFS oder ein Ordner, der auf dem Host eingebunden ist. „Verbindung testen“ prüft Anmeldung und Schreibrechte.
Datenbank, Snapshots und Gesichter bleiben immer lokal. Fällt das NAS aus, schreibt CamSpire lokal weiter und
verbindet sich automatisch neu.

* Der Container braucht dafür `cap_add: SYS_ADMIN` (steht in `docker-compose.yml`).
* Meldet der Test „Kernelmodul fehlt“: einmalig auf dem Server `sudo modprobe cifs`
  (dauerhaft: `echo cifs | sudo tee /etc/modules-load.d/cifs.conf`).
* Läuft Docker in einer **unprivilegierten Proxmox-LXC**, klappt Einbinden im Container nicht –
  dann die Freigabe auf dem Proxmox-Host einbinden, per Bind-Mount in die LXC geben, in `docker-compose.yml`
  als Volume (z. B. `- /mnt/nas:/nas`) eintragen und als Ziel „Ordner“ `/nas` wählen.

## Aufnahme & Speicherdauer

* Modus global oder **pro Kamera**: nur Ereignisse, Daueraufnahme oder aus.
* **Speicherdauer** getrennt für Ereignisse und Daueraufnahme (global und pro Kamera überschreibbar).
  Bei Daueraufnahme bleiben die Abschnitte mit Ereignissen so lange wie Ereignisse, der Rest wird früher gelöscht.
* Harte Obergrenze in GB – die ältesten Aufnahmen werden zuerst gelöscht.

## Einstellungen in der Oberfläche

Alles lässt sich unter **Einstellungen** eintragen – Änderungen wirken sofort, ohne Container-Neustart.
Neu gestartet wird nur, was betroffen ist (z. B. nur die geänderte Kamera).

* **Kameras:** Hersteller-Vorlagen (Hikvision, Dahua, Reolink, Tapo, Axis, Foscam, Uniview, ONVIF) erzeugen die
  RTSP-Adressen aus IP/Benutzer/Passwort. „Testen“ zeigt Codec, Auflösung, fps und ein Vorschaubild.
* **Zonen & Masken** werden direkt ins Kamerabild geklickt (Punkte ziehen, Doppelklick löscht).
* **ONVIF-Suche** findet Kameras im Netzwerk – dafür muss der Container im Host-Netzwerk laufen
  (`network_mode: host` in `docker-compose.yml`, siehe Kommentar dort).
* **Erkennung, Gesichter, Aufnahme & Speicher, Home Assistant (mit Verbindungstest), Zugang & WebRTC.**
* Gespeichert wird in `config/config.yml` (vorheriger Stand als `config.yml.bak`), Export/Import oben rechts.
  Die Datei kann weiterhin per Hand bearbeitet werden – dann den Container neu starten.

Beim Start im Log prüfen:

```
Objekterkennung: yolo.onnx auf CUDAExecutionProvider (…)
[einfahrt] Erkennung mit 1280x720 @ 5 fps (NVDEC)
```

Steht dort `CPUExecutionProvider`, sieht der Container die GPU nicht (Toolkit/`deploy`-Block prüfen).

### Modellgröße wählen

In `docker-compose.yml` → `YOLO_MODEL`:

| Modell | VRAM | Zeit/Bild (Mittelklasse-GPU) | Wann |
|---|---|---|---|
| `yolo11n` | ~150 MB | ~2 ms | viele Kameras, kleine GPU |
| `yolo11s` *(Standard)* | ~250 MB | ~3 ms | guter Kompromiss |
| `yolo11m` | ~500 MB | ~6 ms | weit entfernte/kleine Objekte |

Danach `docker compose build && docker compose up -d`.

## Kameras einrichten

* **Substream für die Erkennung**, Hauptstream für Aufnahme und Vollbild – so ist die Last minimal.
* **Für Gesichter** sollte der Erkennungsstream mind. 720p haben (oder `detect.stream: main` setzen,
  das wird automatisch auf max. 1920 px verkleinert). Gesichter brauchen ~40 px Breite.
* **I-Frame-Intervall** an der Kamera auf 1–2 s stellen (= fps × 2). Dann beginnt der Vorlauf exakt.
* **Zonen und Masken** sind normierte Koordinaten (0–1). Die KI-Ansicht im Live-Bild (Knopf „KI“) zeigt
  Zonen, Masken, Bewegungsbereiche und Erkennungsrahmen – ideal zum Feinjustieren.
* `require_zone: true` → Ereignisse nur, wenn das Objekt in einer Zone war (z. B. Gehweg ignorieren).

## Gesichtserkennung – so lernt sie

1. Unbekannte Gesichter landen unter **Gesichter → Unbekannt**, ähnliche werden automatisch gruppiert.
2. Gruppe einmal benennen (oder einer bestehenden Person zuordnen) → alle Bilder werden zur Referenz.
3. Ab dann erkennt CamSpire die Person. Bei sicheren Treffern mit **neuem** Blickwinkel/Licht lernt es selbst weitere
   Referenzen dazu (im UI mit „Auto“ markiert, max. 40 pro Person – die redundantesten fliegen zuerst raus).
4. Falsch erkannt? Im Ereignis den richtigen Namen eintragen oder „Niemand davon“ → falsch gelernte Bilder
   werden sofort entfernt.
5. Alternativ: Person anlegen und 3–5 Fotos hochladen.

Stellschrauben in `faces:` – `recognition_threshold` höher = weniger Verwechslungen, aber öfter „unbekannt“.

> Hinweis: Wenn Kameras öffentlichen Raum oder Nachbargrundstücke erfassen, ist Gesichtserkennung dort rechtlich
> heikel (DSGVO). Mit `faces: false` pro Kamera und Masken lässt sich das gezielt ausschließen.

## Home Assistant

### MQTT (automatisch)

`mqtt.enabled: true` setzen – CamSpire legt per Discovery pro Kamera ein Gerät **„CamSpire &lt;Kamera&gt;“** an:

| Entität | Beispiel |
|---|---|
| `binary_sensor` Person/Fahrzeug/Tier erkannt | `binary_sensor.camspire_einfahrt_person_erkannt` |
| `binary_sensor` pro Zone | `binary_sensor.camspire_einfahrt_person_in_einfahrt` |
| `sensor` Anzahl | `sensor.camspire_einfahrt_anzahl_person` |
| `sensor` Erkanntes Gesicht (+ Attribute) | `sensor.camspire_einfahrt_erkanntes_gesicht` |
| `camera` letzter Snapshot pro Gruppe | `camera.camspire_einfahrt_letzte_person` |
| `switch` Erkennung an/aus | `switch.camspire_einfahrt_erkennung` |

Jedes Ereignis kommt außerdem als JSON auf `camspire/events` (`type`: `new` / `update` / `end`).

### Live-Bild in HA

CamSpire stellt jeden Stream per RTSP bereit – die Kamera wird dadurch nicht zusätzlich belastet:

* **Generische Kamera**: Stream `rtsp://SERVER:8554/einfahrt`, Standbild
  `http://SERVER:5080/api/cameras/einfahrt/latest.jpg?boxes=0`
* oder die *WebRTC Camera*-Karte mit `url: rtsp://SERVER:8554/einfahrt_sub`

### Beispiel-Automation

```yaml
alias: Unbekannte Person nachts an der Einfahrt
trigger:
  - platform: mqtt
    topic: camspire/events
condition:
  - "{{ trigger.payload_json.type == 'end' }}"
  - "{{ trigger.payload_json.label == 'person' }}"
  - "{{ trigger.payload_json.sub_label is none }}"
  - "{{ 'einfahrt' in trigger.payload_json.zones }}"
  - condition: time
    after: "22:00:00"
    before: "06:00:00"
action:
  - service: notify.mobile_app_handy
    data:
      title: "Unbekannte Person an der Einfahrt"
      message: "{{ now().strftime('%H:%M') }} Uhr"
      data:
        image: "/api/camera_proxy/camera.camspire_einfahrt_letzte_person"
```

## Aufbau

```
Kamera ──RTSP──► go2rtc ──┬──► ffmpeg (NVDEC) ► Bewegung ► YOLO (CUDA) ► Tracker ► Ereignisse ► MQTT/UI
                          │                                                   └─► Gesichter (SCRFD+ArcFace)
                          ├──► ffmpeg -c copy ► RAM-Segmente ──(bei Ereignis)──► MP4-Clip auf Platte
                          ├──► WebRTC/MSE ► Browser
                          └──► RTSP :8554 ► Home Assistant
```

| Pfad | Inhalt |
|---|---|
| `config/config.yml` | Konfiguration |
| `media/clips/<kamera>/<tag>/` | Ereignis-Clips (MP4) |
| `media/snapshots/…` | bestes Bild + Vorschau pro Ereignis |
| `media/faces/` | Gesichts-Referenzen (112×112) |
| `media/recordings/` | nur bei `record.mode: continuous` |
| `media/camspire.db` | SQLite (Ereignisse, Personen, Embeddings) |

## API (Auszug)

| | |
|---|---|
| `GET /api/events?camera=&grp=person&person=Max` | Ereignisse filtern |
| `GET /api/cameras/<id>/latest.jpg?boxes=1` | aktuelles Bild mit Erkennungsrahmen |
| `GET /api/cameras/<id>/export.mp4?start=&end=` | Export aus Daueraufnahme |
| `POST /api/cameras/<id>/detect {"enabled": false}` | Erkennung pausieren |
| `GET /api/stats` | GPU, Inferenzzeit, fps, Speicher |

Vollständig unter `/api/docs`.

## Fehlersuche

| Problem | Lösung |
|---|---|
| `CPUExecutionProvider` im Log | GPU nicht im Container: `docker run --rm --gpus all ubuntu nvidia-smi` testen |
| `NVDEC nicht nutzbar -> CPU-Dekodierung` | `NVIDIA_DRIVER_CAPABILITIES` muss `video` enthalten (ist im Image gesetzt); sonst einfach `hwaccel: none` |
| Live-Bild lädt nicht | `webrtc.candidates` auf die Server-IP setzen, Port 8555 TCP+UDP freigeben; ohne WebRTC läuft MSE über Port 5080 |
| Zu viele Fehlalarme | `objects.filters` (min_score, min_area), Masken für Bäume/Straße, `motion.threshold` erhöhen |
| Gesichter werden nicht erkannt | Erkennungsauflösung erhöhen, `faces.min_face_size` senken, mehr Referenzbilder |
| Hinter Nginx Proxy Manager | „Websockets Support“ aktivieren |
| `port is already allocated` (z. B. Frigate läuft parallel) | `.env` neben `docker-compose.yml` anlegen: `CAMSPIRE_RTSP_PORT=8564` und `CAMSPIRE_WEBRTC_PORT=8565`; in den Einstellungen als WebRTC-Adresse dann `SERVER-IP:8565` eintragen |
