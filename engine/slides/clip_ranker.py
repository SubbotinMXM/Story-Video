"""Локальный OpenCLIP: score(query, image) на MPS/CPU."""

from __future__ import annotations

import re
import threading
from pathlib import Path

from engine.core.logutil import log

DEFAULT_MODEL = "ViT-B-32"
DEFAULT_PRETRAINED = "laion2b_s34b_b79k"
# Soft threshold: ниже — всё равно берём best, но помечаем weak
DEFAULT_THRESHOLD = 0.20

_lock = threading.Lock()
_bundle: dict | None = None
# признаки уже виденных файлов (нормированные): один и тот же кадр оценивают по запросу, потом «красота»,
# «мусор», минус-запрос — каждый раз заново читать и кодировать его незачем. Ключ — размер и время файла
# (файл переезжает из временной папки в пул — содержимое то же). Результаты оценок не меняются
_FEATURE_CACHE_MAX = 6000
_features: "dict[tuple[int, int], object]" = {}


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


_users = 0  # сколько этапов подбора кадров сейчас работают с моделью


def hold_clip() -> None:
    """Этап подбора кадров начался: модель нужна (загрузится при первом обращении)."""
    global _users
    with _lock:
        _users += 1


def release_clip() -> None:
    """Этап закончился. Больше никто не подбирает кадры — выгружаем модель и её кэш из памяти:
    между роликами она не нужна, а держит сотни мегабайт (память Mac бережём — engine.core.memguard)."""
    global _users, _bundle
    with _lock:
        _users = max(0, _users - 1)
        if _users or _bundle is None:
            return
        torch = _bundle.get("torch")
        _bundle = None
        _features.clear()
    import gc

    gc.collect()
    try:
        if torch is not None and torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:  # noqa: BLE001 — освобождение памяти не должно ронять этап
        pass
    log("CLIP: модель выгружена из памяти (подбор кадров закончен)")


def _file_key(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def encode_images(paths: list[Path], bundle: dict) -> tuple[list[Path], object]:
    """Нормированные признаки картинок → (читаемые файлы, матрица [n, d]); новые кодируются одним пакетом.
    Вызывать БЕЗ _lock: кодирование берёт его само."""
    torch, model, preprocess, device = bundle["torch"], bundle["model"], bundle["preprocess"], bundle["device"]
    from PIL import Image

    valid: list[Path] = []
    rows: list[object | None] = []
    fresh: list[tuple[int, object]] = []  # (позиция в valid, тензор после preprocess)
    keys: list[tuple[int, int] | None] = []
    for p in paths:
        key = _file_key(p)
        hit = _features.get(key) if key is not None else None
        if hit is None:
            try:
                tensor = preprocess(Image.open(p).convert("RGB"))
            except Exception as exc:
                log(f"CLIP skip {p.name}: {exc}")
                continue
            fresh.append((len(valid), tensor))
        valid.append(p)
        rows.append(hit)
        keys.append(key)
    if fresh:
        with _lock:
            with torch.no_grad():
                enc = model.encode_image(torch.stack([t for _i, t in fresh]).to(device))
                enc = enc / enc.norm(dim=-1, keepdim=True)
        for (i, _t), vec in zip(fresh, enc):
            rows[i] = vec
            if keys[i] is not None:
                if len(_features) >= _FEATURE_CACHE_MAX:
                    _features.pop(next(iter(_features)), None)  # самый старый
                _features[keys[i]] = vec
    if not valid:
        return [], None
    return valid, torch.stack(rows)  # type: ignore[arg-type]


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
    model, tokenizer, device = bundle["model"], bundle["tokenizer"], bundle["device"]
    torch = bundle["torch"]

    valid_paths, image_features = encode_images(image_paths, bundle)
    if not valid_paths:
        return []

    # serialize encode — MPS/CPU не любят параллельные forward из worker-пула
    with _lock:
        text = tokenizer([q]).to(device)
        with torch.no_grad():
            text_features = model.encode_text(text)
            text_features = text_features / text_features.norm(dim=-1, keepdim=True)
            sims = (image_features @ text_features.T).squeeze(-1)
            scores = sims.detach().float().cpu().tolist()
            if isinstance(scores, float):
                scores = [scores]
            # водяной знак, логотип, надпись поверх, коллаж — в конец (если сам запрос не про текст)
            if not _TEXTUAL.search(q):
                junk = _junk_probs(image_features, model, tokenizer, device)
                scores = [s - junk_penalty(j) for s, j in zip(scores, junk)]

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


# «Цепляет ли кадр»: похож на профессиональную съёмку (пролёт дрона, золотой час, яркие
# чёткие виды) больше, чем на любительский, мыльный, скучный снимок или скриншот с текстом.
_BEAUTY_POS = (
    "a stunning professional cinematic shot",
    "breathtaking aerial drone view of a landscape",
    "beautiful high quality travel photography at golden hour",
    "award-winning photograph with vivid colors and sharp details",
)
_BEAUTY_NEG = (
    "a blurry low quality amateur photo",
    "a boring dull snapshot",
    "a screenshot or a page with text",
    "a dark noisy pixelated image",
)
_beauty_text = None


def beauty_scores(image_paths: list[Path]) -> dict[Path, float]:
    """Красота кадра ≈ −0.08…+0.08 (среднее сходство с «красивым» минус с «плохим»)."""
    global _beauty_text
    if not image_paths:
        return {}
    bundle = get_clip()
    torch, model, tokenizer, device = (bundle[k] for k in ("torch", "model", "tokenizer", "device"))
    valid, img = encode_images(image_paths, bundle)
    if not valid:
        return {}
    with _lock:
        with torch.no_grad():
            if _beauty_text is None:
                t = model.encode_text(tokenizer([*_BEAUTY_POS, *_BEAUTY_NEG]).to(device))
                _beauty_text = t / t.norm(dim=-1, keepdim=True)
            sims = (img @ _beauty_text.T).float().cpu()
            junk = _junk_probs(img, model, tokenizer, device)
    n = len(_BEAUTY_POS)
    vals = (sims[:, :n].mean(dim=1) - sims[:, n:].mean(dim=1)).tolist()
    return {p: float(v) - junk_penalty(j) for p, v, j in zip(valid, vals, junk)}


# Мусор, который раньше отсеивала только зрячая нейросеть: теперь его видит и локальный CLIP —
# работает всегда, и когда нейросеть недоступна, подбор остаётся чистым.
_JUNK = (
    "a photo with a visible watermark across it",
    "an image with a TV channel logo in the corner",
    "a picture with a large text caption or title written over it",
    "a collage of several different photos side by side",
    "a screenshot of a video with subtitles and interface",
    "a meme with text",
)
_CLEAN = (
    "a clean photograph without any text or logos",
    "a single clean photo of a scene",
    "a clean portrait photo of a person",
)
# запрос сам про текст (газета, письмо, вывеска) — надписи на кадре не минус
_TEXTUAL = re.compile(r"newspaper|letter|document|poster|sign|book|page|headline|text|map|газет|письм|документ|"
                      r"плакат|афиш|вывеск|книг|страниц|надпис|карт[аеуы]", re.I)
# проверено на пулах роликов: надписи, водяные знаки, коллажи — 0.99–1.0; чистые панорамы и групповые
# фото — до ~0.87. Поэтому штраф только для уверенного мусора
JUNK_FROM = 0.9
JUNK_MAX_PENALTY = 0.08
_junk_text = None


def junk_penalty(prob: float) -> float:
    """Вероятность «мусора» 0…1 → минус к баллу: до 0.9 — 0, дальше до −0.08 (ниже любого чистого кадра)."""
    return JUNK_MAX_PENALTY * max(0.0, prob - JUNK_FROM) / (1.0 - JUNK_FROM)


def _junk_probs(image_features, model, tokenizer, device) -> list[float]:
    """Для уже посчитанных признаков картинок (нормированных): вероятность «мусор, а не чистый кадр».
    Вызывать под _lock; текстовые признаки считаются один раз."""
    global _junk_text
    if _junk_text is None:
        t = model.encode_text(tokenizer([*_JUNK, *_CLEAN]).to(device))
        _junk_text = t / t.norm(dim=-1, keepdim=True)
    probs = (100.0 * image_features @ _junk_text.T).float().softmax(dim=-1).cpu()
    return probs[:, : len(_JUNK)].sum(dim=1).tolist()


def junk_scores(image_paths: list[Path]) -> dict[Path, float]:
    """Вероятность, что на картинке водяной знак, логотип, надпись или коллаж (0…1)."""
    if not image_paths:
        return {}
    bundle = get_clip()
    torch, model, tokenizer, device = (bundle[k] for k in ("torch", "model", "tokenizer", "device"))
    valid, img = encode_images(image_paths, bundle)
    if not valid:
        return {}
    with _lock:
        with torch.no_grad():
            probs = _junk_probs(img, model, tokenizer, device)
    return dict(zip(valid, probs))
