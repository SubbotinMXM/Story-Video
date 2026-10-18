"""Стадия: весь текст → тема ролика и запросы на пул (без покадровых чанков)."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from engine.slides import checkpoint as cp
from engine.core.cancel import CancelToken
from engine.slides.theme_plan import THEME_NAME, build_theme_plan, save_theme
from engine.slides.llm_client import DEFAULT_MODEL, LLMClient
from engine.core.logutil import log
from engine.slides.paths import (
    CHUNKS_NAME,
    CHUNKS_PREVIEW_NAME,
    TIMINGS_NAME,
    desktop_copy_name,
)
from engine.slides.whisper_timings import WordTiming

ProgressCb = Callable[[float, str], None]


@dataclass
class ChunksRequest:
    run_dir: Path
    llm_api_key: str
    llm_base_url: str
    llm_model: str = DEFAULT_MODEL
    copy_preview_to_desktop: bool = True


@dataclass
class ChunksStageResult:
    run_dir: Path
    chunks_file: Path
    preview_file: Path
    desktop_preview: Path | None
    chunk_count: int
    with_query: int


class ChunksError(RuntimeError):
    pass


def run_chunks_stage(
    req: ChunksRequest,
    *,
    cancel: CancelToken | None = None,
    on_progress: ProgressCb | None = None,
) -> ChunksStageResult:
    def progress(pct: float, msg: str) -> None:
        log(msg)
        if on_progress:
            on_progress(pct, msg)

    run_dir = req.run_dir
    timings_path = run_dir / TIMINGS_NAME
    if not timings_path.is_file():
        raise ChunksError(f"Нет {TIMINGS_NAME} в {run_dir}")

    key = (req.llm_api_key or "").strip()
    if not key:
        raise ChunksError("Нет LLM API key")

    progress(0.05, "Тема: читаю текст целиком…")
    if cancel:
        cancel.check()
    _load_words(timings_path)

    script_path = run_dir / "script.txt"
    script_text = script_path.read_text(encoding="utf-8") if script_path.is_file() else ""
    if not script_text.strip():
        words = _load_words(timings_path)
        script_text = " ".join(w.word for w in words).strip()
        if script_text:
            script_path.write_text(script_text + "\n", encoding="utf-8")
            log(f"Сценарий собран из таймингов: {len(script_text)} символов")
    if not script_text.strip():
        raise ChunksError("Нет текста сценария для темы")

    client = LLMClient(
        api_key=key,
        model=req.llm_model or DEFAULT_MODEL,
        base_url=req.llm_base_url,
    )
    if cancel:
        cancel.register(client)
    try:
        progress(0.2, "Тема: эпоха, место, запросы на пул…")
        plan = build_theme_plan(script_text, client)
    finally:
        client.close()
    save_theme(run_dir / THEME_NAME, plan)

    chk = cp.load(run_dir)
    n_q = len(plan.video_queries) + len(plan.image_queries)
    payload = {
        "chunk_count": n_q,
        "mode": "theme_pool",
        "chunks": [],
        "meta": {
            "language": chk.language,
            "duration": chk.duration,
            "word_count": chk.word_count,
            "llm_model": req.llm_model,
            "summary": plan.summary,
            "story_context": plan.context.to_dict(),
            "video_queries": plan.video_queries,
            "image_queries": plan.image_queries,
        },
    }
    chunks_file = run_dir / CHUNKS_NAME
    chunks_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    preview_file = run_dir / CHUNKS_PREVIEW_NAME
    preview_lines = [
        plan.summary,
        f"historical={plan.context.historical} era={plan.context.era} place={plan.context.place}",
        "",
        "VIDEO",
        *plan.video_queries,
        "",
        "IMAGES",
        *plan.image_queries,
        "",
    ]
    preview_file.write_text("\n".join(preview_lines), encoding="utf-8")
    progress(0.95, f"Сохранено: {THEME_NAME}")

    desktop_preview: Path | None = None
    if req.copy_preview_to_desktop:
        dest = desktop_copy_name(f"theme_{run_dir.name}", ".txt")
        shutil.copy2(preview_file, dest)
        desktop_preview = dest
        progress(0.97, f"Превью на Desktop: {dest.name}")

    with_query = n_q
    cp.mark_chunks_done(
        run_dir,
        language=chk.language,
        voice_id=chk.voice_id,
        text_chars=chk.text_chars,
        audio_bytes=chk.audio_bytes,
        desktop_audio=chk.desktop_audio,
        word_count=chk.word_count,
        duration=chk.duration,
        desktop_preview=chk.desktop_preview,
        chunk_count=n_q,
        desktop_chunks=str(desktop_preview or ""),
    )
    cp.write_meta(
        run_dir,
        {
            "language": chk.language,
            "voice_id": chk.voice_id,
            "text_chars": chk.text_chars,
            "audio_bytes": chk.audio_bytes,
            "word_count": chk.word_count,
            "duration": chk.duration,
            "chunk_count": n_q,
            "chunks_with_query": with_query,
            "llm_model": req.llm_model,
            "desktop_audio": chk.desktop_audio,
            "desktop_preview": chk.desktop_preview,
            "desktop_chunks": str(desktop_preview or ""),
            "stage": "chunks_done",
        },
    )
    progress(1.0, f"Тема готова: {plan.summary}")
    return ChunksStageResult(
        run_dir=run_dir,
        chunks_file=chunks_file,
        preview_file=preview_file,
        desktop_preview=desktop_preview,
        chunk_count=n_q,
        with_query=with_query,
    )


def _load_words(path: Path) -> list[WordTiming]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get("words") if isinstance(raw, dict) else None
    if not isinstance(items, list) or not items:
        raise ChunksError("timings.json без words[]")
    out: list[WordTiming] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        w = str(item.get("word") or "").strip()
        if not w:
            continue
        out.append(
            WordTiming(
                word=w,
                start=float(item.get("start") or 0.0),
                end=float(item.get("end") or 0.0),
                probability=float(item.get("probability") or 0.0),
            )
        )
    if not out:
        raise ChunksError("timings.json: пустой список слов")
    return out
