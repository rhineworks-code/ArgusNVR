"""YOLO-Objekterkennung (ONNX). Unterstützt klassische YOLO-Ausgabe (1, 4+C, N)
und NMS-freie End-to-End-Modelle (1, N, 6) wie YOLO26/YOLOv10."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np

from .config import COCO
from .runtime import create_session

log = logging.getLogger("camspire.detector")


@dataclass
class Detection:
    label: str
    score: float
    box: tuple[float, float, float, float]   # x1, y1, x2, y2 in Pixel

    @property
    def area(self) -> float:
        return max(0.0, self.box[2] - self.box[0]) * max(0.0, self.box[3] - self.box[1])


class Detector:
    def __init__(self, model_path: str, device: str = "auto", gpu_id: int = 0,
                 nms_iou: float = 0.45, cache_dir: str | None = None) -> None:
        self.session = create_session(model_path, device, gpu_id, cache_dir)
        self.provider = self.session.get_providers()[0]
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        shape = inp.shape
        self.size = int(shape[2]) if isinstance(shape[2], int) else 640
        self.fp16 = "float16" in inp.type
        self.nms_iou = nms_iou
        self.names = self._read_names() or COCO
        self._stats_lock = threading.Lock()
        self.infer_ms = 0.0
        self.calls = 0
        # Warm-up (CUDA-Kernel kompilieren, damit das erste echte Bild nicht hängt)
        self.detect(np.zeros((self.size, self.size, 3), np.uint8), min_score=0.99)

    def _read_names(self) -> list[str] | None:
        try:
            meta = self.session.get_modelmeta().custom_metadata_map
            if "names" in meta:
                import ast
                d = ast.literal_eval(meta["names"])
                return [d[i] for i in sorted(d)]
        except Exception:
            pass
        return None

    def _letterbox(self, img: np.ndarray) -> tuple[np.ndarray, float, int, int]:
        h, w = img.shape[:2]
        r = min(self.size / h, self.size / w)
        nh, nw = int(round(h * r)), int(round(w * r))
        resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        top, left = (self.size - nh) // 2, (self.size - nw) // 2
        canvas = np.full((self.size, self.size, 3), 114, np.uint8)
        canvas[top:top + nh, left:left + nw] = resized
        return canvas, r, left, top

    def detect(self, frame: np.ndarray, min_score: float = 0.4,
               labels: set[str] | None = None) -> list[Detection]:
        img, r, dx, dy = self._letterbox(frame)
        blob = img[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float16 if self.fp16 else np.float32)
        blob /= 255.0
        t0 = time.perf_counter()
        out = self.session.run(None, {self.input_name: np.ascontiguousarray(blob)})[0]
        dt = (time.perf_counter() - t0) * 1000
        with self._stats_lock:
            self.calls += 1
            self.infer_ms = dt if self.calls == 1 else self.infer_ms * 0.9 + dt * 0.1

        out = np.asarray(out, dtype=np.float32)[0]
        if out.ndim != 2:
            return []
        if out.shape[1] == 6 and out.shape[0] > out.shape[1]:
            boxes_xyxy, scores, cls = out[:, :4], out[:, 4], out[:, 5].astype(int)
            keep = scores >= min_score
            boxes_xyxy, scores, cls = boxes_xyxy[keep], scores[keep], cls[keep]
            idx = np.arange(len(scores))
        else:
            if out.shape[0] < out.shape[1]:
                out = out.T                       # (N, 4+C)
            cls_scores = out[:, 4:]
            cls = cls_scores.argmax(1)
            scores = cls_scores[np.arange(len(cls)), cls]
            keep = scores >= min_score
            xywh, scores, cls = out[keep, :4], scores[keep], cls[keep]
            boxes_xyxy = np.empty_like(xywh)
            boxes_xyxy[:, 0] = xywh[:, 0] - xywh[:, 2] / 2
            boxes_xyxy[:, 1] = xywh[:, 1] - xywh[:, 3] / 2
            boxes_xyxy[:, 2] = xywh[:, 0] + xywh[:, 2] / 2
            boxes_xyxy[:, 3] = xywh[:, 1] + xywh[:, 3] / 2
            if len(scores) == 0:
                return []
            # klassenweise NMS (Offset-Trick)
            off = boxes_xyxy + (cls[:, None] * 4096.0)
            nms_boxes = [[float(b[0]), float(b[1]), float(b[2] - b[0]), float(b[3] - b[1])] for b in off]
            idx = cv2.dnn.NMSBoxes(nms_boxes, scores.tolist(), min_score, self.nms_iou)
            idx = np.array(idx).reshape(-1) if len(idx) else np.array([], int)

        h, w = frame.shape[:2]
        dets: list[Detection] = []
        for i in idx:
            c = int(cls[i])
            name = self.names[c] if 0 <= c < len(self.names) else str(c)
            if labels is not None and name not in labels:
                continue
            x1, y1, x2, y2 = boxes_xyxy[i]
            x1 = float(np.clip((x1 - dx) / r, 0, w)); x2 = float(np.clip((x2 - dx) / r, 0, w))
            y1 = float(np.clip((y1 - dy) / r, 0, h)); y2 = float(np.clip((y2 - dy) / r, 0, h))
            if x2 - x1 < 2 or y2 - y1 < 2:
                continue
            dets.append(Detection(name, float(scores[i]), (x1, y1, x2, y2)))
        return dets
