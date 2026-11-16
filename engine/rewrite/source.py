"""Текст ролика YouTube по ссылке: субтитры (ручные, иначе автоматические), а если их нет —
звук ролика и локальный Whisper.

Автоматические субтитры — сплошной текст без знаков препинания; расставить их — дело
переписывания (LLM всё равно пишет текст заново).
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from engine.core.cancel import CancelToken
from engine.core.logutil import log

ProgressCb = Callable[[float, str], None]


@dataclass
class Source:
    url: str
    video_id: str
    title: str
    channel: str
    language: str  # код языка ролика (ru, en…) или ""
    text: str
    origin: str  # subtitles | auto_subtitles | whisper

    @property
    def words(self) -> int:
        return len(self.text.split())


class SourceError(RuntimeError):
    pass


def _ydl_opts() -> dict:
    from engine.slides.source_youtube import _SilentLogger

    return {"quiet": True, "no_warnings": True, "skip_download": True, "logger": _SilentLogger(),
            "socket_timeout": 30, "retries": 3}


def video_info(url: str) -> dict:
    try:
        import yt_dlp
    except ImportError as exc:
        raise SourceError("Нет yt-dlp — переустанови зависимости (run.sh)") from exc
    try:
        with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
            return ydl.extract_info(url, download=False) or {}
    except Exception as exc:
        msg = str(exc)
        if "not a bot" in msg or "Sign in" in msg:
            raise SourceError("YouTube просит подтвердить, что это не робот (временное ограничение с этого адреса) — "
                              "повторю сам позже; если не пройдёт — другая сеть или повтор через пару часов") from exc
        raise SourceError(f"Не открыл ролик: {msg[:200]}") from exc


def _pick_track(info: dict) -> tuple[str, str, list[dict]] | None:
    """(язык, вид, форматы): ручные субтитры на языке ролика → любые ручные → автоматические на языке ролика."""
    lang = str(info.get("language") or "").split("-")[0]
    manual: dict = info.get("subtitles") or {}
    auto: dict = info.get("automatic_captions") or {}
    manual = {k: v for k, v in manual.items() if k != "live_chat"}

    def find(table: dict, code: str) -> str | None:
        for key in (code, f"{code}-orig"):
            if key in table:
                return key
        return next((k for k in table if k.split("-")[0] == code), None)

    if lang and (key := find(manual, lang)):
        return key, "subtitles", manual[key]
    if manual:
        key = next(iter(manual))
        return key, "subtitles", manual[key]
    # автоматические: оригинал (у YouTube есть ещё десятки машинных переводов — их не берём)
    orig = next((k for k in auto if k.endswith("-orig")), None) or (find(auto, lang) if lang else None)
    if orig:
        return orig, "auto_subtitles", auto[orig]
    return None


def _json3_text(raw: str) -> str:
    data = json.loads(raw)
    parts: list[str] = []
    for ev in data.get("events") or []:
        for seg in ev.get("segs") or []:
            t = str(seg.get("utf8") or "")
            if t.strip():
                parts.append(t)
    return _clean("".join(parts))


def _vtt_text(raw: str) -> str:
    lines: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or "-->" in line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")) or line.isdigit():
            continue
        line = re.sub(r"<[^>]+>", "", line)
        if lines and lines[-1] == line:  # автоматические субтитры повторяют строку
            continue
        lines.append(line)
    return _clean(" ".join(lines))


def _clean(text: str) -> str:
    text = re.sub(r"\[[^\]]{0,40}\]", " ", text)  # [музыка], [аплодисменты], [♪♪♪]
    text = text.replace("♪", " ")
    return re.sub(r"\s+", " ", text).strip()


def _subtitle_text(formats: list[dict]) -> str:
    by_ext = {f.get("ext"): f for f in formats if f.get("url")}
    for ext, parse in (("json3", _json3_text), ("vtt", _vtt_text)):
        fmt = by_ext.get(ext)
        if fmt is None:
            continue
        try:
            resp = httpx.get(fmt["url"], timeout=60.0, follow_redirects=True)
            resp.raise_for_status()
            text = parse(resp.text)
            if text:
                return text
        except Exception as exc:
            log(f"Субтитры: формат {ext} не скачался ({exc})")
    return ""


def _whisper_text(url: str, language: str, cancel: CancelToken | None, progress: ProgressCb | None) -> str:
    """Субтитров нет: скачать только звук и распознать локально."""
    import yt_dlp

    from engine.slides.ffmpeg_util import find_ffmpeg
    from engine.slides.whisper_timings import transcribe_words

    with tempfile.TemporaryDirectory() as tmp:
        opts = {**_ydl_opts(), "skip_download": False, "format": "bestaudio/best",
                "outtmpl": str(Path(tmp) / "audio.%(ext)s"), "ffmpeg_location": str(Path(find_ffmpeg()).parent)}
        if progress:
            progress(0.1, "Субтитров нет — скачиваю звук ролика для распознавания…")
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        audio = next(Path(tmp).glob("audio.*"), None)
        if audio is None:
            raise SourceError("Не скачал звук ролика")
        res = transcribe_words(audio, language=language or "", cancel=cancel,
                               on_progress=(lambda p, m: progress(0.15 + 0.35 * p, m)) if progress else None)
        return _clean(res.text)


def fetch_source(url: str, *, cancel: CancelToken | None = None, progress: ProgressCb | None = None) -> Source:
    """Ссылка на ролик → его текст."""
    url = (url or "").strip()
    if not re.match(r"https?://", url):
        raise SourceError("Нужна ссылка на ролик YouTube (https://…)")
    if progress:
        progress(0.02, "Открываю ролик…")
    info = video_info(url)
    if info.get("_type") == "playlist" or info.get("entries"):
        raise SourceError("Это ссылка на канал или плейлист, а нужна ссылка на один ролик")
    title = str(info.get("title") or "ролик")
    lang = str(info.get("language") or "").split("-")[0]
    track = _pick_track(info)
    text, origin = "", ""
    if track is not None:
        key, origin, formats = track
        if progress:
            progress(0.05, f"Субтитры ({'ручные' if origin == 'subtitles' else 'автоматические'}, {key})…")
        text = _subtitle_text(formats)
        lang = lang or key.split("-")[0]
    if len(text.split()) < 50:
        origin = "whisper"
        text = _whisper_text(url, lang, cancel, progress)
    if len(text.split()) < 50:
        raise SourceError("В ролике почти нет речи — переписывать нечего")
    if not lang:
        from engine.core.langdetect import detect_language

        lang = detect_language(text) or ""
    log(f"Исходник: «{title}» — {len(text.split())} слов ({origin}, язык {lang or '?'})")
    return Source(url=url, video_id=str(info.get("id") or ""), title=title, channel=str(info.get("channel") or info.get("uploader") or ""),
                  language=lang, text=text, origin=origin)
