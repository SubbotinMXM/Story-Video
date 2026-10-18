"""Готовый ролик: одна папка на Desktop, имя «Архимед 30.09»."""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path

from engine.core.logutil import log
from engine.slides.paths import DESKTOP, mkdir_unique
from engine.slides.theme_plan import THEME_NAME

_BAD = re.compile(r"[\\/:*?\"<>|\n\r\t]+")
# любые буквы любого языка (ł, ñ, ü…), без цифр и подчёркиваний
_TOKEN = re.compile(r"[^\W\d_](?:[^\W\d_]|-){3,}")
_STOP = frozenset(
    {
        "этот", "эта", "это", "эти", "как", "его", "её", "ее", "она", "они", "оно",
        "был", "была", "было", "были", "если", "когда", "после", "перед", "только",
        "также", "который", "которая", "которые", "которое", "человек", "представьте",
        "однако", "потому", "затем", "здесь", "там", "ещё", "еще", "весь", "вся",
        "все", "один", "одна", "между", "через", "около", "более", "менее", "очень",
        "можно", "нужно", "чтобы", "что", "или", "для", "при", "над", "под", "без",
        "уже", "вот", "так", "нет", "да", "мы", "вы", "их", "им", "ему", "ней",
        "них", "the", "and", "this", "that", "with", "from", "into",
    }
)

# исходник в run → имя в папке ролика
_PACK = (
    ("final.mp4", "ролик.mp4"),
    ("voice.mp3", "озвучка.mp3"),
    ("script.txt", "сценарий.txt"),
    ("subs.srt", "субтитры.srt"),
    ("music.mp3", "музыка.mp3"),
    ("timings_preview.txt", "тайминги.txt"),
    ("chunks_preview.txt", "тема.txt"),
    ("timeline_preview.txt", "кадры.txt"),
    ("credits.txt", "источники.txt"),
)

_PROVIDER_NAMES = {
    "pexels": "Pexels",
    "pixabay": "Pixabay",
    "unsplash": "Unsplash",
    "wikimedia": "Wikimedia Commons",
    "archive": "Internet Archive",
    "google": "Google Images",
    "youtube": "YouTube",
}


def write_credits(run_dir: Path, *, music_line: str = "") -> Path | None:
    """credits.txt: авторы YouTube-нарезок (CC BY требует указать) + сводка источников."""
    path = run_dir / "timeline.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    items = [*(raw.get("slots") or []), *(raw.get("loop_block") or [])] or [
        *(raw.get("videos") or []),
        *(raw.get("images") or []),
    ]
    youtube: dict[str, dict] = {}
    counts: Counter[str] = Counter()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("fallback"):
            continue
        provider = str(item.get("provider") or "")
        counts[provider] += 1
        if provider == "youtube" and item.get("id"):
            youtube.setdefault(str(item["id"]), item)
    lines: list[str] = []
    if youtube:
        lines += [
            "Видеофрагменты YouTube (лицензия Creative Commons Attribution, CC BY).",
            "Укажите авторов в описании ролика:",
            "",
        ]
        for vid, item in youtube.items():
            title = str(item.get("source_title") or "").strip() or "без названия"
            author = str(item.get("source_author") or "").strip() or "автор не указан"
            lines.append(f"• «{title}» — {author} — https://www.youtube.com/watch?v={vid}")
        lines.append("")
    if counts:
        lines.append("Источники кадров: " + ", ".join(
            f"{_PROVIDER_NAMES.get(p, p)} {n}" for p, n in counts.most_common()
        ))
    if music_line:
        lines += ["", music_line]
    if not lines:
        return None
    out = run_dir / "credits.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def video_keyword(run_dir: Path) -> str:
    title = _theme_title(run_dir)
    if title:
        return title
    script = ""
    script_path = run_dir / "script.txt"
    if script_path.is_file():
        script = script_path.read_text(encoding="utf-8")
    guessed = keyword_from_script(script)
    if guessed:
        return guessed
    return "ролик"


def keyword_from_script(text: str) -> str:
    counts: Counter[str] = Counter()
    seen: dict[str, str] = {}
    for token in _TOKEN.findall(text or ""):
        if token[0].islower():
            continue
        key = token.lower().replace("ё", "е")
        if key in _STOP:
            continue
        counts[key] += 1
        seen.setdefault(key, token)
    if not counts:
        return ""
    best, _n = counts.most_common(1)[0]
    return seen[best]


def lang_suffix(language: str) -> str:
    """« (EN)» для имени папки; пусто, если язык неизвестен."""
    code = (language or "").strip().split("-")[0].upper()
    return f" ({code})" if code and code != "AUTO" else ""


def export_ready_folder(
    run_dir: Path,
    *,
    loose: list[str] | None = None,
    when: datetime | None = None,
    language: str = "",
) -> Path | None:
    """Копирует готовые файлы в Desktop/<тема ДД.ММ>/. Россыпь на столе убирает.

    language — когда есть переводы: «Монголия 14.10 (RU)» рядом с «(EN)», «(DE)».
    """
    if not (run_dir / "final.mp4").is_file():
        return None
    stamp = (when or datetime.now()).strftime("%d.%m")
    label = _folder_label(video_keyword(run_dir))
    folder = _unique_dir(DESKTOP, f"{label} {stamp}{lang_suffix(language)}")
    copied = 0
    for src_name, dest_name in _PACK:
        src = run_dir / src_name
        if not src.is_file() or src.stat().st_size < 1:
            continue
        shutil.copy2(src, folder / dest_name)
        copied += 1
    _drop_loose(loose or [], keep=folder)
    log(f"Готово: {folder} ({copied} файлов)")
    return folder


def export_dub_folder(
    run_dir: Path,
    lang_dir: Path,
    language: str,
    *,
    when: datetime | None = None,
) -> Path | None:
    """Ролик на другом языке → Desktop/<тема ДД.ММ (EN)>/ с тем же набором файлов."""
    if not (lang_dir / "final.mp4").is_file():
        return None
    stamp = (when or datetime.now()).strftime("%d.%m")
    label = _folder_label(video_keyword(run_dir))
    folder = _unique_dir(DESKTOP, f"{label} {stamp}{lang_suffix(language)}")
    copied = 0
    for src_name, dest_name in _PACK:
        # кадры и их источники — общие с оригиналом, остальное своё у каждого языка
        src = (run_dir if src_name == "credits.txt" else lang_dir) / src_name
        if not src.is_file() or src.stat().st_size < 1:
            continue
        shutil.copy2(src, folder / dest_name)
        copied += 1
    log(f"Готово ({language.upper()}): {folder} ({copied} файлов)")
    return folder


def _theme_title(run_dir: Path) -> str:
    path = run_dir / THEME_NAME
    if not path.is_file():
        return ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(raw, dict):
        return ""
    ctx = raw.get("story_context")
    if not isinstance(ctx, dict):
        return ""
    return str(ctx.get("title") or "").strip()


def _folder_label(text: str) -> str:
    cleaned = _BAD.sub(" ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    words = cleaned.split()[:3]
    label = " ".join(words)[:40].strip(" .")
    return label or "ролик"


def _unique_dir(parent: Path, name: str) -> Path:
    return mkdir_unique(parent, name, sep=" ")


def _drop_loose(paths: list[str], *, keep: Path) -> None:
    desk = DESKTOP.resolve()
    keep_resolved = keep.resolve()
    for raw in paths:
        if not raw:
            continue
        path = Path(raw)
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not path.is_file():
            continue
        if resolved.parent != desk or keep_resolved in resolved.parents:
            continue
        path.unlink(missing_ok=True)
