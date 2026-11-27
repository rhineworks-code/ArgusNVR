"""Günstige Bewegungserkennung auf der CPU – die GPU rechnet nur, wenn sich etwas tut."""
from __future__ import annotations

import cv2
import numpy as np


class MotionDetector:
    def __init__(self, frame_shape: tuple[int, int], threshold: int = 25, min_area: float = 0.002,
                 mask_polys: list[list[tuple[float, float]]] | None = None) -> None:
        h, w = frame_shape
        self.scale = 240 / max(h, 1)
        self.size = (max(1, int(w * self.scale)), 240)
        self.threshold = threshold
        self.min_area = min_area
        self.avg: np.ndarray | None = None
        self.mask: np.ndarray | None = None
        if mask_polys:
            m = np.full((self.size[1], self.size[0]), 255, np.uint8)
            for poly in mask_polys:
                pts = np.array([[x * self.size[0], y * self.size[1]] for x, y in poly], np.int32)
                cv2.fillPoly(m, [pts], 0)
            self.mask = m
        self.last_ratio = 0.0
        self.boxes: list[tuple[float, float, float, float]] = []   # normiert

    def detect(self, frame: np.ndarray) -> bool:
        small = cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        if self.avg is None:
            self.avg = gray.astype(np.float32)
            return False
        diff = cv2.absdiff(gray, cv2.convertScaleAbs(self.avg))
        # adaptive Hintergrund-Anpassung (Lichtwechsel, Wolken)
        cv2.accumulateWeighted(gray, self.avg, 0.08)
        _, th = cv2.threshold(diff, self.threshold, 255, cv2.THRESH_BINARY)
        if self.mask is not None:
            th = cv2.bitwise_and(th, self.mask)
        th = cv2.dilate(th, None, iterations=2)
        ratio = float(np.count_nonzero(th)) / th.size
        self.last_ratio = ratio
        self.boxes = []
        if ratio >= self.min_area:
            contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            W, H = self.size
            for c in contours:
                if cv2.contourArea(c) < self.min_area * th.size * 0.25:
                    continue
                x, y, w, h = cv2.boundingRect(c)
                self.boxes.append((x / W, y / H, (x + w) / W, (y + h) / H))
            return bool(self.boxes)
        return False
