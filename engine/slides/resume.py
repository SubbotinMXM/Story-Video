"""Resume полного цикла с чекпоинта / файлов run-папки."""

from __future__ import annotations

import json
from pathlib import Path

from engine.slides import checkpoint as cp
from engine.slides.chunks_pipeline import ChunksStageResult
from engine.core.logutil import log
from engine.slides.media_pipeline import MediaStageResult
from engine.slides.paths import (
    AUDIO_NAME,
    CHUNKS_NAME,
    CHUNKS_PREVIEW_NAME,
    RUNS_DIR,
    SCRIPT_NAME,
    TIMELINE_NAME,
    TIMINGS_NAME,
    TIMINGS_PREVIEW_NAME,
)
from engine.slides.tts_pipeline import TtsResult
from engine.slides.whisper_pipeline import WhisperStageResult
from engine.slides.whisper_timings import TimingsResult, WordTiming

# порядок стадий для resume
STAGE_ORDER = (
    "none",
    "script_saved",
    "tts_done",
    "timings_done",
    "chunks_done",
    "media_done",
    "render_done",
)


def detect_stage(run_dir: Path) -> str:
    """Стадия по файлам (надёжнее, чем один checkpoint при падении mid-stage)."""
    if not run_dir.is_dir():
        return "none"
    final = run_dir / "final.mp4"
    if final.is_file() and final.stat().st_size > 1000:
        return "render_done"
    timeline = run_dir / TIMELINE_NAME
    if timeline.is_file():
        try:
            raw = json.loads(timeline.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and _timeline_done(raw):
                return "media_done"
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    if (run_dir / CHUNKS_NAME).is_file():
        return "chunks_done"
    if (run_dir / TIMINGS_NAME).is_file():
        return "timings_done"
    if (run_dir / AUDIO_NAME).is_file():
        return "tts_done"
    if (run_dir / SCRIPT_NAME).is_file():
        return "script_saved"
    chk = cp.load(run_dir)
    return chk.stage if chk.stage in STAGE_ORDER else "none"


def timeline_matches(run_dir: Path, *, strict: bool, full: bool = False, stars: bool = False) -> bool:
    """Уже собранный timeline подходит под текущий режим подбора."""
    raw = _read_timeline(run_dir)
    if raw is None:
        return False
    if stars or raw.get("variant") == "stars":
        return stars and raw.get("variant") == "stars" and bool(raw.get("slots"))
    if raw.get("person") and raw.get("mode") == "theme_pool" and raw.get("images"):
        return True  # пул «только герой» собирается одинаково для любого режима
    if strict:
        slots = raw.get("slots")
        variant = raw.get("variant") or "3min"  # старые запуски — без поля
        return (
            raw.get("mode") == "strict_sync"
            and variant == ("full" if full else "3min")
            and isinstance(slots, list)
            and len(slots) > 0
        )
    return raw.get("mode") == "theme_pool" and bool(raw.get("images"))


def _timeline_done(raw: dict) -> bool:
    if raw.get("mode") == "strict_sync":
        slots = raw.get("slots")
        return isinstance(slots, list) and len(slots) > 0
    return raw.get("mode") == "theme_pool" and bool(raw.get("images"))


def _read_timeline(run_dir: Path) -> dict | None:
    timeline = run_dir / TIMELINE_NAME
    if not timeline.is_file():
        return None
    try:
        raw = json.loads(timeline.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return raw if isinstance(raw, dict) else None


def stage_index(stage: str) -> int:
    try:
        return STAGE_ORDER.index(stage)
    except ValueError:
        return 0


def needs_stage(current: str, required: str) -> bool:
    """Нужно ли выполнять required, если уже на current."""
    return stage_index(current) < stage_index(required)


def list_runs() -> list[Path]:
    if not RUNS_DIR.is_dir():
        return []
    return sorted(
        (p for p in RUNS_DIR.iterdir() if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )


def latest_resumable_run() -> Path | None:
    """Самый свежий run, который ещё не доведён до render_done."""
    for run in list_runs():
        stage = detect_stage(run)
        if stage != "render_done" and stage != "none":
            return run
    return None


def load_tts_result(run_dir: Path) -> TtsResult:
    audio = run_dir / AUDIO_NAME
    script = run_dir / SCRIPT_NAME
    if not audio.is_file():
        raise FileNotFoundError(f"Нет {AUDIO_NAME} в {run_dir.name}")
    chk = cp.load(run_dir)
    text = script.read_text(encoding="utf-8") if script.is_file() else ""
    return TtsResult(
        run_dir=run_dir,
        script_file=script,
        audio_file=audio,
        desktop_audio=Path(chk.desktop_audio) if chk.desktop_audio else None,
        text_chars=chk.text_chars or len(text.strip()),
        audio_bytes=chk.audio_bytes or audio.stat().st_size,
        language=chk.language,
        voice_id=chk.voice_id or "",
    )


def load_whisper_result(run_dir: Path) -> WhisperStageResult:
    timings_file = run_dir / TIMINGS_NAME
    if not timings_file.is_file():
        raise FileNotFoundError(f"Нет {TIMINGS_NAME}")
    raw = json.loads(timings_file.read_text(encoding="utf-8"))
    words = [
        WordTiming(
            word=str(w.get("word") or ""),
            start=float(w.get("start") or 0.0),
            end=float(w.get("end") or 0.0),
            probability=float(w.get("probability") or 0.0),
        )
        for w in (raw.get("words") or [])
        if isinstance(w, dict)
    ]
    result = TimingsResult(
        words=words,
        language=str(raw.get("language") or ""),
        duration=float(raw.get("duration") or 0.0),
        model_size=str(raw.get("model_size") or "base"),
        text=str(raw.get("text") or ""),
    )
    preview = run_dir / TIMINGS_PREVIEW_NAME
    chk = cp.load(run_dir)
    return WhisperStageResult(
        run_dir=run_dir,
        audio_file=run_dir / AUDIO_NAME,
        timings_file=timings_file,
        preview_file=preview if preview.is_file() else timings_file,
        desktop_preview=Path(chk.desktop_preview) if chk.desktop_preview else None,
        word_count=len(words),
        duration=result.duration,
        language=result.language,
        result=result,
    )


def load_chunks_result(run_dir: Path) -> ChunksStageResult:
    chunks_file = run_dir / CHUNKS_NAME
    if not chunks_file.is_file():
        raise FileNotFoundError(f"Нет {CHUNKS_NAME}")
    raw = json.loads(chunks_file.read_text(encoding="utf-8"))
    items = raw.get("chunks") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        items = []
    with_query = sum(1 for c in items if isinstance(c, dict) and c.get("query"))
    preview = run_dir / CHUNKS_PREVIEW_NAME
    chk = cp.load(run_dir)
    return ChunksStageResult(
        run_dir=run_dir,
        chunks_file=chunks_file,
        preview_file=preview if preview.is_file() else chunks_file,
        desktop_preview=Path(chk.desktop_chunks) if chk.desktop_chunks else None,
        chunk_count=len(items),
        with_query=with_query,
    )


def load_media_result(run_dir: Path) -> MediaStageResult:
    timeline = run_dir / TIMELINE_NAME
    if not timeline.is_file():
        raise FileNotFoundError(f"Нет {TIMELINE_NAME}")
    raw = json.loads(timeline.read_text(encoding="utf-8"))
    preview = run_dir / "timeline_preview.txt"
    chk = cp.load(run_dir)
    return MediaStageResult(
        run_dir=run_dir,
        timeline_file=timeline,
        preview_file=preview if preview.is_file() else timeline,
        desktop_preview=Path(chk.desktop_timeline) if chk.desktop_timeline else None,
        selected=int(raw.get("selected") or chk.selected_media or 0),
        skipped=int(raw.get("skipped") or 0),
        weak=int(raw.get("weak") or 0),
    )


def describe_resume(run_dir: Path) -> str:
    stage = detect_stage(run_dir)
    nxt = {
        "script_saved": "озвучка",
        "tts_done": "Whisper",
        "timings_done": "чанки",
        "chunks_done": "кадры",
        "media_done": "рендер",
        "render_done": "готово",
    }.get(stage, stage)
    log(f"Resume {run_dir.name}: stage={stage} → дальше: {nxt}")
    return f"{run_dir.name} (сейчас: {stage}, дальше: {nxt})"
