"""Стадия Whisper: voice.mp3 → timings.json (+ preview)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from engine.slides import checkpoint as cp
from engine.core.cancel import CancelToken
from engine.core.logutil import log
from engine.slides.paths import TIMINGS_NAME, TIMINGS_PREVIEW_NAME, desktop_copy_name
from engine.slides.subtitles import mark_script_origin
from engine.slides.whisper_timings import (
    DEFAULT_MODEL_SIZE,
    TimingsResult,
    WhisperError,
    save_timings_json,
    save_timings_preview,
    transcribe_words,
)

ProgressCb = Callable[[float, str], None]


@dataclass
class WhisperRequest:
    run_dir: Path
    language: str
    model_size: str = DEFAULT_MODEL_SIZE
    copy_preview_to_desktop: bool = True


@dataclass
class WhisperStageResult:
    run_dir: Path
    audio_file: Path
    timings_file: Path
    preview_file: Path
    desktop_preview: Path | None
    word_count: int
    duration: float
    language: str
    result: TimingsResult


def run_whisper_stage(
    req: WhisperRequest,
    *,
    cancel: CancelToken | None = None,
    on_progress: ProgressCb | None = None,
) -> WhisperStageResult:
    def progress(pct: float, msg: str) -> None:
        log(msg)
        if on_progress:
            on_progress(pct, msg)

    run_dir = req.run_dir
    audio = cp.audio_path(run_dir)
    if not audio.is_file():
        raise WhisperError(f"Нет voice.mp3 в run: {run_dir}")

    chk = cp.load(run_dir)
    language = (req.language or chk.language or "").strip()

    progress(0.02, f"Whisper: {audio.name}")

    def whisper_prog(pct: float, msg: str) -> None:
        # map 0..1 → 0.05..0.90
        progress(0.05 + pct * 0.85, msg)

    result = transcribe_words(
        audio,
        language=language,
        model_size=req.model_size or DEFAULT_MODEL_SIZE,
        cancel=cancel,
        on_progress=whisper_prog,
    )

    timings_file = run_dir / TIMINGS_NAME
    preview_file = run_dir / TIMINGS_PREVIEW_NAME
    save_timings_json(timings_file, result)
    save_timings_preview(preview_file, result)
    _ensure_script(run_dir, result.text)
    progress(0.93, f"Тайминги: {timings_file.name} ({len(result.words)} слов)")

    desktop_preview: Path | None = None
    if req.copy_preview_to_desktop:
        dest = desktop_copy_name(f"timings_{run_dir.name}", ".txt")
        shutil.copy2(preview_file, dest)
        desktop_preview = dest
        progress(0.96, f"Превью на Desktop: {dest.name}")

    cp.mark_timings_done(
        run_dir,
        language=result.language or language,
        voice_id=chk.voice_id,
        text_chars=chk.text_chars,
        audio_bytes=chk.audio_bytes or (audio.stat().st_size if audio.is_file() else 0),
        desktop_audio=chk.desktop_audio,
        word_count=len(result.words),
        duration=result.duration,
        desktop_preview=str(desktop_preview or ""),
    )
    meta = {
        "language": result.language or language,
        "voice_id": chk.voice_id,
        "text_chars": chk.text_chars,
        "audio_bytes": chk.audio_bytes,
        "word_count": len(result.words),
        "duration": result.duration,
        "whisper_model": result.model_size,
        "desktop_audio": chk.desktop_audio,
        "desktop_preview": str(desktop_preview or ""),
        "stage": "timings_done",
    }
    cp.write_meta(run_dir, meta)
    progress(1.0, f"Whisper готов: {len(result.words)} слов, {result.duration:.1f}с")

    return WhisperStageResult(
        run_dir=run_dir,
        audio_file=audio,
        timings_file=timings_file,
        preview_file=preview_file,
        desktop_preview=desktop_preview,
        word_count=len(result.words),
        duration=result.duration,
        language=result.language or language,
        result=result,
    )


def _ensure_script(run_dir: Path, transcript: str) -> None:
    """Если текст не вставляли, сценарий для темы — расшифровка Whisper."""
    script = cp.script_path(run_dir)
    if script.is_file() and script.read_text(encoding="utf-8").strip():
        return
    text = (transcript or "").strip()
    if not text:
        raise WhisperError("Whisper не разобрал речь, текст сценария пустой")
    script.write_text(text + "\n", encoding="utf-8")
    mark_script_origin(run_dir, "whisper")
    log(f"Сценарий из расшифровки: {len(text)} символов")


__all__ = [
    "WhisperRequest",
    "WhisperStageResult",
    "WhisperError",
    "run_whisper_stage",
]
