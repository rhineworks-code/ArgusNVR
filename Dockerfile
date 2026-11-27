# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# CamSpire NVR – ein Container: go2rtc + ffmpeg (NVDEC) + YOLO/Gesichter auf CUDA
# ---------------------------------------------------------------------------

# ---------- Stage 1: Modelle bauen (wird verworfen, landet nicht im Image) ----
FROM python:3.12-slim AS models
ARG YOLO_MODEL=yolo11s
ARG YOLO_IMGSZ=640
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl unzip libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir ultralytics onnx onnxslim
WORKDIR /models
# Objekt-Erkennung: YOLO -> ONNX (Personen, Fahrzeuge, Tiere aus COCO)
RUN yolo export model=${YOLO_MODEL}.pt format=onnx imgsz=${YOLO_IMGSZ} opset=17 simplify=True \
 && mv ${YOLO_MODEL}.onnx yolo.onnx && rm -f *.pt
# Gesichter: SCRFD (Detektion + Landmarken) und ArcFace (512-d Embedding)
RUN curl -fsSL -o buffalo_l.zip https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip \
 && unzip -q buffalo_l.zip -d buffalo \
 && find buffalo -name det_10g.onnx -exec mv {} face_det.onnx \; \
 && find buffalo -name w600k_r50.onnx -exec mv {} face_rec.onnx \; \
 && rm -rf buffalo buffalo_l.zip && ls -la

# ---------- Stage 2: Laufzeit-Image ----------------------------------------
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility \
    CAMSPIRE_CONFIG=/config/config.yml \
    CAMSPIRE_MEDIA=/media/camspire \
    CAMSPIRE_CACHE=/tmp/cache \
    CAMSPIRE_MODELS=/models

# ffmpeg mit NVDEC/CUDA (BtbN-Build) + go2rtc
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl xz-utils tzdata libgl1 libglib2.0-0 cifs-utils nfs-common \
 && curl -fsSL https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz \
      | tar -xJ -C /tmp \
 && mv /tmp/ffmpeg-*/bin/ffmpeg /tmp/ffmpeg-*/bin/ffprobe /usr/local/bin/ && rm -rf /tmp/ffmpeg-* \
 && curl -fsSL -o /usr/local/bin/go2rtc https://github.com/AlexxIT/go2rtc/releases/latest/download/go2rtc_linux_amd64 \
 && chmod +x /usr/local/bin/go2rtc \
 && apt-get purge -y xz-utils && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=models /models /models
COPY camspire ./camspire

EXPOSE 5080 1984 8554 8555/tcp 8555/udp
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD curl -fs http://127.0.0.1:5080/api/health || exit 1
CMD ["python", "-m", "camspire.main"]
