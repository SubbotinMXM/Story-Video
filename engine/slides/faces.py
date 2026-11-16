"""Лица на кадрах: кто герой ролика и есть ли он на картинке/видео.

OpenCV: YuNet (поиск лиц) + SFace (сравнение лиц), обе модели — из opencv_zoo
(MIT / Apache 2.0), ~39 МБ, качаются один раз в data/models.

Герой определяется без эталона: среди найденных по его имени фото выбираем
лицо, которое встречается на большем числе разных картинок. Здания, пейзажи и
другие люди этому лицу не соответствуют и отсекаются.
"""

from __future__ import annotations

import threading
from pathlib import Path

import httpx
import numpy as np

from engine.core.config import DATA_DIR
from engine.core.logutil import log

MODELS_DIR = DATA_DIR / "models"
_ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models"
MODELS = {
    "face_detection_yunet_2023mar.onnx": (f"{_ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx", 200_000),
    "face_recognition_sface_2021dec.onnx": (f"{_ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx", 30_000_000),
}
# порог SFace (cosine) из документации OpenCV: выше — один и тот же человек (для опознания героя)
SAME_PERSON = 0.363
# строже — для приёма кадра в ролик: похожие люди и двойники из шоу набирают 0.37–0.43,
# настоящие фото героя — обычно 0.47–0.8 (замер на ролике про Anda Călugăreanu)
HERO_MATCH = 0.45
# герой не главное лицо в кадре (стоит сбоку) — нужна ещё бо́льшая уверенность
HERO_MATCH_SIDE = 0.55
# лицо меньше этой доли короткой стороны кадра — толпа на фоне, не считаем
MIN_FACE_FRAC = 0.05
# герой должен найтись хотя бы на стольких разных картинках, иначе «опознать» не вышло
MIN_HERO_IMAGES = 3
_MAX_SIDE = 1280

_lock = threading.Lock()
_models: tuple | None = None


def ensure_models() -> Path:
    """Скачать модели, если их ещё нет (run.sh делает это при установке)."""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    for name, (url, min_size) in MODELS.items():
        path = MODELS_DIR / name
        if path.is_file() and path.stat().st_size >= min_size:
            continue
        log(f"Лица: скачиваю модель {name}…")
        part = path.with_suffix(".part")
        with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(120.0, connect=30.0)) as resp:
            resp.raise_for_status()
            with part.open("wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    fh.write(chunk)
        if part.stat().st_size < min_size:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"Модель лиц {name} скачалась не полностью")
        part.replace(path)
    return MODELS_DIR


def _get():
    global _models
    if _models is None:
        import cv2

        ensure_models()
        det = cv2.FaceDetectorYN.create(str(MODELS_DIR / "face_detection_yunet_2023mar.onnx"), "", (320, 320), 0.8, 0.3, 50)
        rec = cv2.FaceRecognizerSF.create(str(MODELS_DIR / "face_recognition_sface_2021dec.onnx"), "")
        _models = (cv2, det, rec)
    return _models


def face_embeddings(path: Path) -> list[np.ndarray]:
    """Лица на картинке (крупные сначала) → нормированные векторы SFace."""
    with _lock:  # детектор OpenCV не потокобезопасен
        cv2, det, rec = _get()
        img = cv2.imread(str(path))
        if img is None:
            return []
        h, w = img.shape[:2]
        scale = min(1.0, _MAX_SIDE / max(h, w))
        if scale < 1.0:
            img = cv2.resize(img, (int(w * scale), int(h * scale)))
        det.setInputSize((img.shape[1], img.shape[0]))
        _ok, faces = det.detect(img)
        if faces is None:
            return []
        short = min(img.shape[:2])
        out: list[tuple[float, np.ndarray]] = []
        for face in faces:
            fw, fh = float(face[2]), float(face[3])
            if min(fw, fh) < short * MIN_FACE_FRAC:
                continue
            vec = rec.feature(rec.alignCrop(img, face)).flatten().astype(np.float32)
            norm = float(np.linalg.norm(vec))
            if norm > 0:
                out.append((fw * fh, vec / norm))
    return [vec for _area, vec in sorted(out, key=lambda x: -x[0])]


def find_hero(per_image: list[list[np.ndarray]]) -> np.ndarray | None:
    """Лицо, которое встречается на наибольшем числе разных картинок → его средний вектор."""
    faces = [(i, vec) for i, vecs in enumerate(per_image) for vec in vecs]
    if not faces:
        return None
    mat = np.stack([vec for _i, vec in faces])
    sims = mat @ mat.T
    owners = np.array([i for i, _vec in faces])
    best_n, best_k = 0, -1
    for k in range(len(faces)):
        n = len(set(owners[sims[k] >= SAME_PERSON]))
        if n > best_n:
            best_n, best_k = n, k
    if best_n < MIN_HERO_IMAGES:
        log(f"Лица: героя не опознать — одно лицо максимум на {best_n} картинках")
        return None
    # по одному лучшему лицу с каждой картинки, где он есть
    picked: dict[int, np.ndarray] = {}
    for idx in np.flatnonzero(sims[best_k] >= SAME_PERSON):
        i = int(owners[idx])
        if i not in picked or sims[best_k][idx] > float(picked[i] @ mat[best_k]):
            picked[i] = mat[idx]
    ref = np.mean(np.stack(list(picked.values())), axis=0)
    log(f"Лица: герой найден на {len(picked)} картинках")
    return ref / float(np.linalg.norm(ref))


def is_hero_shot(vecs: list[np.ndarray], hero: np.ndarray) -> bool:
    """Кадр с героем: он главное лицо и узнан уверенно — или узнан очень уверенно рядом с другими."""
    if not vecs:
        return False
    if float(vecs[0] @ hero) >= HERO_MATCH:
        return True
    return hero_score(vecs[1:], hero) >= HERO_MATCH_SIDE


def hero_score(vecs: list[np.ndarray], hero: np.ndarray) -> float:
    """Насколько лицо героя есть среди этих лиц (максимум cosine)."""
    return max((float(v @ hero) for v in vecs), default=-1.0)


def face_boxes(path: Path) -> list[tuple[tuple[float, float, float, float], np.ndarray, float]]:
    """Лица с рамками в координатах исходной картинки: [((x, y, w, h), вектор SFace, анфас 0–1)].
    Анфас — насколько нос посередине между глазами (1 — смотрит прямо в камеру)."""
    with _lock:
        cv2, det, rec = _get()
        img = cv2.imread(str(path))
        if img is None:
            return []
        h, w = img.shape[:2]
        scale = min(1.0, _MAX_SIDE / max(h, w))
        small = cv2.resize(img, (int(w * scale), int(h * scale))) if scale < 1.0 else img
        det.setInputSize((small.shape[1], small.shape[0]))
        _ok, faces = det.detect(small)
        if faces is None:
            return []
        short = min(small.shape[:2])
        out = []
        for face in faces:
            fw, fh = float(face[2]), float(face[3])
            if min(fw, fh) < short * MIN_FACE_FRAC:
                continue
            vec = rec.feature(rec.alignCrop(small, face)).flatten().astype(np.float32)
            norm = float(np.linalg.norm(vec))
            if norm <= 0:
                continue
            (rx, ry), (lx, ly), (nx, _ny) = face[4:6], face[6:8], face[8:10]
            eyes = abs(lx - rx) or 1.0
            frontal = max(0.0, 1.0 - abs(nx - (rx + lx) / 2) / (eyes / 2))
            box = tuple(float(v) / scale for v in face[:4])
            out.append((box, vec / norm, frontal))
    return sorted(out, key=lambda f: -f[0][2] * f[0][3])
