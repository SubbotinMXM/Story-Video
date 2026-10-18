"""Чекпоинты стадий пайплайна внутри run-папки."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from engine.core.logutil import log
from engine.slides.paths import AUDIO_NAME, CHECKPOINT_NAME, SCRIPT_NAME

Stage = Literal[
    "none",
    "script_saved",
    "tts_done",
    "timings_done",
    "chunks_done",
    "media_done",
    "render_done",
]
_VALID_STAGES = frozenset(
    {
        "none",
        "script_saved",
        "tts_done",
        "timings_done",
        "chunks_done",
        "media_done",
        "render_done",
    }
)


@dataclass
class Checkpoint:
    stage: Stage = "none"
    language: str = ""
    voice_id: str = ""
    audio_bytes: int = 0
    text_chars: int = 0
    desktop_audio: str = ""
    word_count: int = 0
    duration: float = 0.0
    desktop_preview: str = ""
    chunk_count: int = 0
    desktop_chunks: str = ""
    selected_media: int = 0
    desktop_timeline: str = ""
    desktop_video: str = ""
    video_bytes: int = 0

    @property
    def can_resume_tts(self) -> bool:
        return self.stage == "script_saved"

    @property
    def can_run_whisper(self) -> bool:
        return self.stage in {
            "tts_done",
            "timings_done",
            "chunks_done",
            "media_done",
            "render_done",
        }

    @property
    def can_run_chunks(self) -> bool:
        return self.stage in {
            "timings_done",
            "chunks_done",
            "media_done",
            "render_done",
        }

    @property
    def can_run_media(self) -> bool:
        return self.stage in {"chunks_done", "media_done", "render_done"}

    @property
    def can_run_render(self) -> bool:
        return self.stage in {"media_done", "render_done"}


def load(run_dir: Path) -> Checkpoint:
    path = run_dir / CHECKPOINT_NAME
    if not path.is_file():
        return Checkpoint()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Checkpoint()
    if not isinstance(raw, dict):
        return Checkpoint()
    stage = str(raw.get("stage") or "none")
    if stage not in _VALID_STAGES:
        stage = "none"
    return Checkpoint(
        stage=stage,  # type: ignore[arg-type]
        language=str(raw.get("language") or ""),
        voice_id=str(raw.get("voice_id") or ""),
        audio_bytes=int(raw.get("audio_bytes") or 0),
        text_chars=int(raw.get("text_chars") or 0),
        desktop_audio=str(raw.get("desktop_audio") or ""),
        word_count=int(raw.get("word_count") or 0),
        duration=float(raw.get("duration") or 0.0),
        desktop_preview=str(raw.get("desktop_preview") or ""),
        chunk_count=int(raw.get("chunk_count") or 0),
        desktop_chunks=str(raw.get("desktop_chunks") or ""),
        selected_media=int(raw.get("selected_media") or 0),
        desktop_timeline=str(raw.get("desktop_timeline") or ""),
        desktop_video=str(raw.get("desktop_video") or ""),
        video_bytes=int(raw.get("video_bytes") or 0),
    )


def save(run_dir: Path, cp: Checkpoint) -> None:
    path = run_dir / CHECKPOINT_NAME
    path.write_text(
        json.dumps(asdict(cp), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def mark_script_saved(
    run_dir: Path,
    *,
    text_chars: int,
    language: str,
    voice_id: str,
) -> None:
    cp = Checkpoint(
        stage="script_saved",
        text_chars=text_chars,
        language=language,
        voice_id=voice_id,
    )
    save(run_dir, cp)
    log(f"Чекпоинт: script_saved ({text_chars} chars)")


def mark_tts_done(
    run_dir: Path,
    *,
    audio_bytes: int,
    language: str,
    voice_id: str,
    text_chars: int,
    desktop_audio: str = "",
) -> None:
    cp = Checkpoint(
        stage="tts_done",
        audio_bytes=audio_bytes,
        language=language,
        voice_id=voice_id,
        text_chars=text_chars,
        desktop_audio=desktop_audio,
    )
    save(run_dir, cp)
    log(f"Чекпоинт: tts_done ({audio_bytes} bytes)")


def mark_timings_done(
    run_dir: Path,
    *,
    language: str,
    voice_id: str,
    text_chars: int,
    audio_bytes: int,
    desktop_audio: str = "",
    word_count: int = 0,
    duration: float = 0.0,
    desktop_preview: str = "",
) -> None:
    cp = Checkpoint(
        stage="timings_done",
        language=language,
        voice_id=voice_id,
        text_chars=text_chars,
        audio_bytes=audio_bytes,
        desktop_audio=desktop_audio,
        word_count=word_count,
        duration=duration,
        desktop_preview=desktop_preview,
    )
    save(run_dir, cp)
    log(f"Чекпоинт: timings_done ({word_count} words, {duration:.1f}s)")


def mark_chunks_done(
    run_dir: Path,
    *,
    language: str,
    voice_id: str,
    text_chars: int,
    audio_bytes: int,
    desktop_audio: str = "",
    word_count: int = 0,
    duration: float = 0.0,
    desktop_preview: str = "",
    chunk_count: int = 0,
    desktop_chunks: str = "",
) -> None:
    cp = Checkpoint(
        stage="chunks_done",
        language=language,
        voice_id=voice_id,
        text_chars=text_chars,
        audio_bytes=audio_bytes,
        desktop_audio=desktop_audio,
        word_count=word_count,
        duration=duration,
        desktop_preview=desktop_preview,
        chunk_count=chunk_count,
        desktop_chunks=desktop_chunks,
    )
    save(run_dir, cp)
    log(f"Чекпоинт: chunks_done ({chunk_count} chunks)")


def mark_media_done(
    run_dir: Path,
    *,
    language: str,
    voice_id: str,
    text_chars: int,
    audio_bytes: int,
    desktop_audio: str = "",
    word_count: int = 0,
    duration: float = 0.0,
    desktop_preview: str = "",
    chunk_count: int = 0,
    desktop_chunks: str = "",
    selected_media: int = 0,
    desktop_timeline: str = "",
) -> None:
    cp = Checkpoint(
        stage="media_done",
        language=language,
        voice_id=voice_id,
        text_chars=text_chars,
        audio_bytes=audio_bytes,
        desktop_audio=desktop_audio,
        word_count=word_count,
        duration=duration,
        desktop_preview=desktop_preview,
        chunk_count=chunk_count,
        desktop_chunks=desktop_chunks,
        selected_media=selected_media,
        desktop_timeline=desktop_timeline,
    )
    save(run_dir, cp)
    log(f"Чекпоинт: media_done (selected={selected_media})")


def mark_render_done(
    run_dir: Path,
    *,
    language: str,
    voice_id: str,
    text_chars: int,
    audio_bytes: int,
    desktop_audio: str = "",
    word_count: int = 0,
    duration: float = 0.0,
    desktop_preview: str = "",
    chunk_count: int = 0,
    desktop_chunks: str = "",
    selected_media: int = 0,
    desktop_timeline: str = "",
    desktop_video: str = "",
    video_bytes: int = 0,
) -> None:
    cp = Checkpoint(
        stage="render_done",
        language=language,
        voice_id=voice_id,
        text_chars=text_chars,
        audio_bytes=audio_bytes,
        desktop_audio=desktop_audio,
        word_count=word_count,
        duration=duration,
        desktop_preview=desktop_preview,
        chunk_count=chunk_count,
        desktop_chunks=desktop_chunks,
        selected_media=selected_media,
        desktop_timeline=desktop_timeline,
        desktop_video=desktop_video,
        video_bytes=video_bytes,
    )
    save(run_dir, cp)
    log(f"Чекпоинт: render_done ({video_bytes} bytes)")


def script_path(run_dir: Path) -> Path:
    return run_dir / SCRIPT_NAME


def audio_path(run_dir: Path) -> Path:
    return run_dir / AUDIO_NAME


def write_meta(run_dir: Path, data: dict[str, Any]) -> Path:
    path = run_dir / "meta.json"
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path
