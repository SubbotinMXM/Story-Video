"""Тряска камеры в клипе: любительская съёмка с рук против плавного пролёта/штатива.

Сдвиг кадра между соседними моментами (фазовая корреляция OpenCV). Плавная панорама
или пролёт дрона — сдвиг ровный (скорость почти постоянна); съёмка с рук — сдвиг
скачет туда-сюда. Мера — средний скачок скорости в пикселях на кадре шириной 320.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from engine.core.logutil import log

_W = 320
_FPS = 8.0
_MAX_SEC = 5.0
# выше — заметно трясётся (по замеру: штатив/дрон/гимбал ≈ 0–1, рука ≈ 2–6)
SHAKY = 2.0


def shakiness(video: Path) -> float | None:
    """Средний скачок скорости камеры (пиксели при ширине 320). None — не смог прочитать."""
    try:
        import cv2
    except ImportError:
        return None
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return None
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        step = max(1, round(fps / _FPS))
        limit = int(fps * _MAX_SEC)
        prev = None
        moves: list[tuple[float, float]] = []
        window = None
        for i in range(limit):
            ok = cap.grab()
            if not ok:
                break
            if i % step:
                continue
            ok, frame = cap.retrieve()
            if not ok or frame is None:
                break
            h, w = frame.shape[:2]
            gray = cv2.cvtColor(cv2.resize(frame, (_W, max(1, round(h * _W / w)))), cv2.COLOR_BGR2GRAY)
            cur = np.float32(gray)
            if window is None:
                window = cv2.createHanningWindow(cur.shape[::-1], cv2.CV_32F)
            if prev is not None:
                (dx, dy), _resp = cv2.phaseCorrelate(prev, cur, window)
                moves.append((dx, dy))
            prev = cur
    except Exception as exc:
        log(f"Тряска: не смог разобрать {video.name} ({exc})")
        return None
    finally:
        cap.release()
    if len(moves) < 4:
        return None
    v = np.array(moves)
    jerk = np.abs(np.diff(v, axis=0)).sum(axis=1)
    # медиана: одна склейка внутри клипа (большой скачок) не делает его «трясущимся»
    return float(np.median(jerk))
