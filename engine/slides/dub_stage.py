"""Ролики на других языках из перевода озвучки (VoiceGen).

Перевод подогнан под время фраз оригинала, поэтому видеоряд берётся готовый
(video_silent.mp4 из рендера) — меняются только голос и субтитры.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from engine.core.cancel import CancelToken
from engine.core.langdetect import detect_language
from engine.core.logutil import log
from engine.core.throttle import heavy_stage
from engine.slides.publish import export_dub_folder
from engine.slides.render_pipeline import SILENT_NAME, RenderRequest, build_silent, export_chapters, finish_video
from engine.slides.speed_copies import write_speed_copies
from engine.slides.subtitles import timings_to_srt
from engine.tts import get_provider
from engine.tts.base import DubTrack

ProgressCb = Callable[[float, str], None]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def source_language(heard: str, text: str) -> str:
    """Язык оригинала: что услышал Whisper, иначе — по словам текста; не знаем — «auto»."""
    heard = (heard or "").strip().lower().split("-")[0]
    return heard or detect_language(text) or "auto"


def start_dub(provider_id: str, opts: dict, work_dir: Path, *, language: str) -> tuple[list[str], str]:
    """Заказать перевод. → (языки, предупреждение). Ошибки сервиса пробрасываются."""
    from engine.tts.voicegen_dub import DubUnavailable

    provider = get_provider(provider_id)
    wanted = provider.dub_languages(opts)
    if not wanted:
        return [], ""
    try:
        targets = provider.start_dub(opts, work_dir, source_language=language, targets=wanted)
    except DubUnavailable as exc:
        log(f"Перевод: {exc}")
        return [], f"Переводы не сделаны: {exc}"
    skipped = [w.upper() for w in wanted if w == language]
    if skipped:
        log(f"Перевод: {', '.join(skipped)} — язык самого текста, ролик на нём уже делается")
    return targets, ""


def dub_subtitles(track: DubTrack, dest: Path) -> Path | None:
    """Субтитры перевода в том же стиле, что у оригинала: свои фразы по точным таймингам слов."""
    if track.timings is not None:
        srt = timings_to_srt(track.timings, dest)
        if srt is not None:
            return srt
    if track.srt is not None and track.srt.is_file():
        shutil.copy2(track.srt, dest)
        return dest
    return None


def render_dub(
    req: RenderRequest,
    track: DubTrack,
    *,
    cancel: CancelToken | None = None,
    on_progress: ProgressCb | None = None,
) -> Path | None:
    """Готовый перевод → ролик на этом языке в отдельной папке на Desktop."""

    def progress(pct: float, msg: str) -> None:
        log(msg)
        if on_progress:
            on_progress(pct, msg)

    run_dir = req.run_dir
    lang = track.language.upper()
    lang_dir = track.audio.parent
    if track.text is not None and track.text.name != "script.txt":
        shutil.copy2(track.text, lang_dir / "script.txt")
    with heavy_stage(f"Ролик {lang}", cancel):
        silent = run_dir / SILENT_NAME
        if not silent.is_file():
            # видеоряд удалили (старый запуск) — собираем тот же самый заново
            build_silent(replace(req, keep_segments=False), cancel=cancel, progress=lambda p, m: progress(p * 0.6, f"{lang}: {m}"))
        srt = dub_subtitles(track, lang_dir / "subs.srt") if req.subtitles else None
        final = finish_video(
            req,
            silent=silent,
            voice=track.audio,
            srt_path=srt,
            dest=lang_dir / "final.mp4",
            progress=lambda p, m: progress(0.6 + p * 0.3, f"{lang}: {m}"),
        )
        folder = export_dub_folder(run_dir, lang_dir, track.language) if req.copy_to_desktop else None
        if folder is not None:
            # копии со сдвигом скорости — в папку своего языка, как у оригинала
            if req.speed_copies > 0:
                write_speed_copies(final, folder, req.speed_copies, cancel=cancel,
                                   on_progress=lambda p, m: progress(0.9 + p * 0.1, f"{lang}: {m}"))
            export_chapters(
                req, folder, script=lang_dir / "script.txt", timings=track.timings, voice=track.audio, video=final,
                language=track.language, cache=lang_dir / "chapters.json",
                # главы оригинала: переводим названия, а не размечаем заново (экономия запросов LLM)
                base_text=_read(run_dir / "script.txt"),
            )
            final.unlink(missing_ok=True)  # копия уже на Desktop — не держим второй ролик в запуске
    return folder


def run_dub_stage(
    provider_id: str,
    opts: dict,
    req: RenderRequest,
    *,
    language: str,
    cancel: CancelToken | None = None,
    on_progress: ProgressCb | None = None,
) -> list[str]:
    """Дождаться переводов и собрать по ролику на язык. → строки для итога задачи."""

    def progress(pct: float, msg: str) -> None:
        if on_progress:
            on_progress(max(0.0, min(1.0, pct)), msg)

    run_dir = req.run_dir
    provider = get_provider(provider_id)
    targets, warning = start_dub(provider_id, opts, run_dir, language=language)
    lines = [warning] if warning else []
    if targets:
        n = len(targets)
        for track in provider.iter_dubs(opts, run_dir, cancel=cancel, on_progress=lambda p, m: progress(p, m)):
            from engine.tts.voicegen_dub import load_state

            done = len(load_state(run_dir).get("localized") or {})
            folder = render_dub(
                req,
                track,
                cancel=cancel,
                on_progress=lambda p, m, d=done: progress((d + p) / n, m),
            )
            provider.mark_dub_done(run_dir, track.language, str(folder or ""))
        from engine.tts.voicegen_dub import load_state

        for lang, folder in (load_state(run_dir).get("localized") or {}).items():
            lines.append(f"Ролик {lang.upper()}: {folder or 'готов'}")
    (run_dir / SILENT_NAME).unlink(missing_ok=True)
    return lines
