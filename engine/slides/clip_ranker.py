"""Локальный OpenCLIP: score(query, image) на MPS/CPU."""

from __future__ import annotations

import threading
from pathlib import Path

from engine.core.logutil import log

DEFAULT_MODEL = "ViT-B-32"
DEFAULT_PRETRAINED = "laion2b_s34b_b79k"
# Soft threshold: ниже — всё равно берём best, но помечаем weak
DEFAULT_THRESHOLD = 0.20

_lock = threading.Lock()
_bundle: dict | None = None


class ClipError(RuntimeError):
    pass


def _pick_device():
    import torch

    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def get_clip(
    *,
    model_name: str = DEFAULT_MODEL,
    pretrained: str = DEFAULT_PRETRAINED,
):
    global _bundle
    with _lock:
        if _bundle is not None:
            return _bundle
        try:
            import open_clip
            import torch
        except ImportError as exc:
            raise ClipError(
                "Нет torch/open_clip. Перезапусти ./run.sh после обновления "
                "requirements (torch + open-clip-torch)."
            ) from exc

        device = _pick_device()
        log(f"CLIP: гружу {model_name}/{pretrained} на {device}…")
        model, _, preprocess = open_clip.create_model_and_transforms(
            model_name, pretrained=pretrained
        )
        tokenizer = open_clip.get_tokenizer(model_name)
        model = model.to(device)
        model.eval()
        _bundle = {
            "model": model,
            "preprocess": preprocess,
            "tokenizer": tokenizer,
            "device": device,
            "torch": torch,
        }
        log("CLIP: готов")
        return _bundle


def score_image_paths(
    query: str,
    image_paths: list[Path],
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[tuple[Path, float]]:
    """Вернуть (path, cosine) отсортированный по убыванию score."""
    q = (query or "").strip()
    if not q or not image_paths:
        return []
    bundle = get_clip()
    torch = bundle["torch"]
    model = bundle["model"]
    preprocess = bundle["preprocess"]
    tokenizer = bundle["tokenizer"]
    device = bundle["device"]

    from PIL import Image

    tensors = []
    valid_paths: list[Path] = []
    for p in image_paths:
        try:
            img = Image.open(p).convert("RGB")
            tensors.append(preprocess(img))
            valid_paths.append(p)
        except Exception as exc:
            log(f"CLIP skip {p.name}: {exc}")
    if not tensors:
        return []

    # serialize encode — MPS/CPU не любят параллельные forward из worker-пула
    with _lock:
        batch = torch.stack(tensors).to(device)
        text = tokenizer([q]).to(device)
        with torch.no_grad():
            image_features = model.encode_image(batch)
            text_features = model.encode_text(text)
            image_features = image_features / image_features.norm(dim=-1, keepdim=True)
            text_features = text_features / text_features.norm(dim=-1, keepdim=True)
            sims = (image_features @ text_features.T).squeeze(-1)
            scores = sims.detach().float().cpu().tolist()
            if isinstance(scores, float):
                scores = [scores]

    ranked = sorted(
        zip(valid_paths, [float(s) for s in scores], strict=True),
        key=lambda x: x[1],
        reverse=True,
    )
    _ = threshold  # caller decides cutoff
    return ranked


def score_image_paths_contextual(
    pos_query: str,
    image_paths: list[Path],
    *,
    neg_query: str = "",
    neg_weight: float = 0.9,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[tuple[Path, float]]:
    """pos CLIP − neg_weight * anachronism CLIP → меньше йогуртовых отцов."""
    pos = score_image_paths(pos_query, image_paths, threshold=threshold)
    if not pos:
        return []
    neg_q = (neg_query or "").strip()
    if not neg_q:
        return pos
    neg = score_image_paths(neg_q, [p for p, _ in pos], threshold=threshold)
    neg_map = {p: s for p, s in neg}
    adjusted = [
        (p, float(s) - neg_weight * float(neg_map.get(p, 0.0))) for p, s in pos
    ]
    adjusted.sort(key=lambda x: x[1], reverse=True)
    return adjusted


def score_one(query: str, image_path: Path) -> float:
    ranked = score_image_paths(query, [image_path])
    return ranked[0][1] if ranked else 0.0
