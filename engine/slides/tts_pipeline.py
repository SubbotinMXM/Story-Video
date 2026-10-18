"""Стадия 1–2: сохранить скрипт → Lumean TTS → копия на Desktop."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from engine.slides import checkpoint as cp
from engine.core.cancel import CancelToken, CancelledError
from engine.slides.ffmpeg_util import find_ffmpeg
from engine.slides.ffprobe_util import run_ffmpeg
from engine.core.logutil import log
from engine.tts import DEFAULT_PROVIDER, get_provider
from engine.slides.paths import desktop_copy_name, new_run_dir
from engine.slides.subtitles import mark_script_origin

ProgressCb = Callable[[float, str], None]


@dataclass
class TtsRequest:
    text: str
    # сервис озвучки и его настройки (engine/tts)
    provider: str = DEFAULT_PROVIDER
    options: dict = field(default_factory=dict)
    # пусто — язык определит Whisper по аудио
    language: str = ""
    title_hint: str = ""
    copy_audio_to_desktop: bool = True
    # resume: продолжить в существующей run-папке
    run_dir: Path | None = None
    # готовый файл вместо Lumean
    source_audio: Path | None = None


@dataclass
class TtsResult:
    run_dir: Path
    script_file: Path
    audio_file: Path
    desktop_audio: Path | None
    text_chars: int
    audio_bytes: int
    language: str
    voice_id: str


def run_tts_stage(
    req: TtsRequest,
    *,
    cancel: CancelToken | None = None,
    on_progress: ProgressCb | None = None,
) -> TtsResult:
    def progress(pct: float, msg: str) -> None:
        log(msg)
        if on_progress:
            on_progress(pct, msg)

    def check() -> None:
        if cancel:
            cancel.check()

    text = (req.text or "").strip()
    if req.source_audio is not None:
        return _import_existing_audio(req, text, progress=progress, check=check)
    if not text:
        raise ValueError("Пустой текст сценария")
    provider = get_provider(req.provider)
    provider.check(req.options)

    check()
    if req.run_dir is not None:
        run_dir = req.run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        run_dir = new_run_dir(title_hint=req.title_hint or text[:40])
    progress(0.05, f"Run: {run_dir.name}")

    script_file = cp.script_path(run_dir)
    script_file.write_text(text + "\n", encoding="utf-8")
    mark_script_origin(run_dir, "user")
    voice_id = provider.voice_tag(req.options)
    language = (req.language or "").strip()
    cp.mark_script_saved(
        run_dir,
        text_chars=len(text),
        language=language,
        voice_id=voice_id,
    )
    cp.write_meta(
        run_dir,
        {
            "language": language,
            "voice_id": voice_id,
            "tts_provider": provider.id,
            "text_chars": len(text),
            "stage": "script_saved",
        },
    )
    progress(0.15, f"Скрипт сохранён ({len(text)} символов)")

    audio_file = cp.audio_path(run_dir)
    progress(0.25, f"Озвучка: {provider.label}…")

    check()
    provider.synthesize(
        text,
        audio_file,
        req.options,
        state_file=run_dir / "tts_order.json",
        cancel=cancel,
        on_progress=progress,
    )

    audio_bytes = audio_file.stat().st_size
    desktop_audio: Path | None = None
    if req.copy_audio_to_desktop:
        dest = desktop_copy_name(f"voice_{run_dir.name}", ".mp3")
        shutil.copy2(audio_file, dest)
        desktop_audio = dest
        progress(0.90, f"Копия на Desktop: {dest.name}")

    cp.mark_tts_done(
        run_dir,
        audio_bytes=audio_bytes,
        language=language,
        voice_id=voice_id,
        text_chars=len(text),
        desktop_audio=str(desktop_audio or ""),
    )
    cp.write_meta(
        run_dir,
        {
            "language": language,
            "voice_id": voice_id,
            "tts_provider": provider.id,
            "text_chars": len(text),
            "audio_bytes": audio_bytes,
            "desktop_audio": str(desktop_audio or ""),
            "stage": "tts_done",
        },
    )
    progress(1.0, f"Готово: {audio_file.name} ({audio_bytes} bytes)")
    return TtsResult(
        run_dir=run_dir,
        script_file=script_file,
        audio_file=audio_file,
        desktop_audio=desktop_audio,
        text_chars=len(text),
        audio_bytes=audio_bytes,
        language=language,
        voice_id=voice_id,
    )


def _import_existing_audio(
    req: TtsRequest,
    text: str,
    *,
    progress: ProgressCb,
    check,
) -> TtsResult:
    src = Path(req.source_audio) if req.source_audio else None
    if src is None or not src.is_file():
        raise ValueError(f"Нет аудиофайла: {req.source_audio}")
    check()
    if req.run_dir is not None:
        run_dir = req.run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        run_dir = new_run_dir(title_hint=req.title_hint or text[:40] or src.stem)
    progress(0.05, f"Run: {run_dir.name}")

    script_file = cp.script_path(run_dir)
    language = (req.language or "").strip()
    voice_id = "imported"
    if text:
        script_file.write_text(text + "\n", encoding="utf-8")
        mark_script_origin(run_dir, "user")
        progress(0.2, f"Скрипт сохранён, беру аудио: {src.name}")
    else:
        progress(0.2, f"Текста нет, сценарий снимет Whisper. Аудио: {src.name}")
    cp.mark_script_saved(
        run_dir,
        text_chars=len(text),
        language=language,
        voice_id=voice_id,
    )

    audio_file = cp.audio_path(run_dir)
    _place_voice(src, audio_file)
    check()
    audio_bytes = audio_file.stat().st_size
    if audio_bytes < 1000:
        raise ValueError("Аудиофайл пустой или битый")

    desktop_audio: Path | None = None
    if req.copy_audio_to_desktop:
        dest = desktop_copy_name(f"voice_{run_dir.name}", ".mp3")
        if dest.resolve() != audio_file.resolve():
            shutil.copy2(audio_file, dest)
            desktop_audio = dest

    cp.mark_tts_done(
        run_dir,
        audio_bytes=audio_bytes,
        language=language,
        voice_id=voice_id,
        text_chars=len(text),
        desktop_audio=str(desktop_audio or ""),
    )
    cp.write_meta(
        run_dir,
        {
            "language": language,
            "voice_id": voice_id,
            "text_chars": len(text),
            "audio_bytes": audio_bytes,
            "source_audio": str(src),
            "stage": "tts_done",
        },
    )
    progress(1.0, f"Готовое аудио: {audio_file.name} ({audio_bytes} bytes)")
    return TtsResult(
        run_dir=run_dir,
        script_file=script_file,
        audio_file=audio_file,
        desktop_audio=desktop_audio,
        text_chars=len(text),
        audio_bytes=audio_bytes,
        language=language,
        voice_id=voice_id,
    )


def _place_voice(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() == dest.resolve():
        return
    if src.suffix.lower() == ".mp3":
        shutil.copy2(src, dest)
        return
    ffmpeg = find_ffmpeg()
    run_ffmpeg(
        [
            ffmpeg,
            "-y",
            "-i",
            str(src),
            "-vn",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(dest),
        ],
        label="import-audio",
    )


# re-export for callers
__all__ = [
    "TtsRequest",
    "TtsResult",
    "run_tts_stage",
    "CancelledError",
]
