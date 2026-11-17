"""Полный цикл: TTS → Whisper → чанки → стоки+CLIP → mp4 (с resume)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from pathlib import Path
from typing import Callable

from engine.core.cancel import CancelToken
from engine.slides.chunks_pipeline import ChunksRequest, ChunksStageResult, run_chunks_stage
from engine.slides.llm_client import DEFAULT_BASE_URL, DEFAULT_MODEL
from engine.core.logutil import log
from engine.slides.media_pipeline import (
    MediaRequest,
    MediaStageResult,
    StockKeys,
    run_media_stage,
)
from engine.slides.render_pipeline import RenderRequest, RenderResult, run_render_stage
from engine.slides.resume import (
    drop_stock,
    describe_resume,
    detect_stage,
    load_chunks_result,
    load_media_result,
    load_tts_result,
    load_whisper_result,
    needs_stage,
    timeline_matches,
)
from engine.slides.tts_pipeline import TtsRequest, TtsResult, run_tts_stage
from engine.slides.whisper_pipeline import (
    WhisperRequest,
    WhisperStageResult,
    run_whisper_stage,
)
from engine.slides.whisper_timings import DEFAULT_MODEL_SIZE

if TYPE_CHECKING:
    from engine.slides.ai_gen import AiConfig

ProgressCb = Callable[[float, str], None]


@dataclass
class FullRunRequest:
    tts: TtsRequest
    llm_api_key: str
    llm_base_url: str = DEFAULT_BASE_URL
    llm_model: str = DEFAULT_MODEL
    whisper_model: str = DEFAULT_MODEL_SIZE
    stock_keys: StockKeys | None = None
    subscribe: bool = False
    subscribe_path: Path | None = None
    subtitles: bool = False
    intro_dir: Path | None = None
    outro_dir: Path | None = None
    # фоновая музыка (assets/music): по кругу под рассказом, в переводах тоже
    music: Path | None = None
    # AI-кадры (RoyalTechno): None — выключены
    ai: "AiConfig | None" = None
    speed_copies: int = 0
    strict_sync: bool = False
    # строгий подбор на весь ролик, а не только первые 3 мин (вместе со strict_sync)
    strict_full: bool = False
    # «Звёзды»: ролик про нескольких людей (engine/slides/stars_media)
    stars: bool = False
    # свои доли источников, % (None — как по умолчанию: по теме ролика)
    source_mix: dict | None = None
    # если задан — продолжаем этот run, пропуская готовые стадии
    resume_dir: Path | None = None
    # только озвучка + тайминги + тема (шаг 1), без кадров и рендера
    stop_after_chunks: bool = False


@dataclass
class FullRunResult:
    tts: TtsResult
    whisper: WhisperStageResult
    chunks: ChunksStageResult
    media: MediaStageResult | None
    render: RenderResult | None
    # ролики на других языках (перевод озвучки) и предупреждения о них
    dub_lines: list[str] = field(default_factory=list)

    @property
    def run_dir(self) -> Path:
        return self.tts.run_dir


def run_full_pipeline(
    req: FullRunRequest,
    *,
    cancel: CancelToken | None = None,
    on_progress: ProgressCb | None = None,
) -> FullRunResult:
    """Прогресс: 0–20 TTS, 20–35 Whisper, 35–50 chunks, 50–80 media, 80–100 render.

    С переводами на другие языки рендер занимает 80–90, ролики на языках — 90–100.
    """
    from engine.slides.dub_stage import run_dub_stage, source_language, start_dub
    from engine.tts import get_provider

    def span(lo: float, hi: float, pct: float, msg: str) -> None:
        if on_progress:
            on_progress(lo + (hi - lo) * max(0.0, min(1.0, pct)), msg)

    resume_dir = req.resume_dir
    stage = detect_stage(resume_dir) if resume_dir else "none"
    if resume_dir:
        log(describe_resume(resume_dir))

    # --- TTS ---
    if resume_dir and not needs_stage(stage, "tts_done"):
        tts = load_tts_result(resume_dir)
        span(0.0, 0.20, 1.0, f"Resume: TTS уже есть ({tts.audio_file.name})")
    else:
        tts_req = req.tts
        if resume_dir is not None:
            # добить озвучку в том же run (script_saved без voice)
            script = resume_dir / "script.txt"
            tts_req = TtsRequest(
                text=tts_req.text
                or (script.read_text(encoding="utf-8") if script.is_file() else ""),
                language=tts_req.language,
                provider=tts_req.provider,
                options=tts_req.options,
                title_hint=tts_req.title_hint,
                copy_audio_to_desktop=tts_req.copy_audio_to_desktop,
                run_dir=resume_dir,
                source_audio=tts_req.source_audio,
            )
        tts = run_tts_stage(
            tts_req,
            cancel=cancel,
            on_progress=lambda p, m: span(0.0, 0.20, p, m),
        )

    run_dir = tts.run_dir
    stage = detect_stage(run_dir)

    # --- Whisper ---
    if not needs_stage(stage, "timings_done"):
        whisper = load_whisper_result(run_dir)
        span(0.20, 0.35, 1.0, f"Resume: timings уже есть ({whisper.word_count} слов)")
    else:
        whisper = run_whisper_stage(
            WhisperRequest(
                run_dir=run_dir,
                language=tts.language,
                model_size=req.whisper_model,
                copy_preview_to_desktop=True,
            ),
            cancel=cancel,
            on_progress=lambda p, m: span(0.20, 0.35, p, m),
        )
    stage = detect_stage(run_dir)

    # --- Перевод: заказываем сразу, пока подбираются кадры, VoiceGen успеет перевести ---
    dub_wanted = not req.stop_after_chunks and bool(get_provider(req.tts.provider).dub_languages(req.tts.options))
    dub_language = ""
    if dub_wanted:
        script = run_dir / "script.txt"
        dub_language = source_language(
            whisper.language, script.read_text(encoding="utf-8") if script.is_file() else req.tts.text
        )
        try:
            start_dub(req.tts.provider, req.tts.options, run_dir, language=dub_language)
        except Exception as exc:  # не мешаем основному ролику; в конце попробуем ещё раз
            log(f"Перевод: заказ не создан ({exc}) — повторю после сборки ролика")

    # --- Chunks ---
    if not needs_stage(stage, "chunks_done"):
        chunks = load_chunks_result(run_dir)
        span(0.35, 0.50, 1.0, f"Resume: chunks уже есть ({chunks.chunk_count})")
    else:
        chunks = run_chunks_stage(
            ChunksRequest(
                run_dir=run_dir,
                llm_api_key=req.llm_api_key,
                llm_base_url=req.llm_base_url,
                llm_model=req.llm_model,
                copy_preview_to_desktop=True,
            ),
            cancel=cancel,
            on_progress=lambda p, m: span(0.35, 0.50, p, m),
        )
    stage = detect_stage(run_dir)

    if req.stop_after_chunks:
        return FullRunResult(tts=tts, whisper=whisper, chunks=chunks, media=None, render=None)

    if req.stock_keys is None:
        raise ValueError("Нет ключей стоков для полного цикла")

    # --- Media (частичный timeline тоже продолжается внутри) ---
    media_ready = not needs_stage(stage, "media_done") and (
        stage == "render_done" or timeline_matches(run_dir, strict=req.strict_sync, full=req.strict_full, stars=req.stars)
    )
    if media_ready:
        media = load_media_result(run_dir)
        span(0.50, 0.80, 1.0, f"Resume: media уже есть (ok={media.selected})")
    else:
        media = run_media_stage(
            MediaRequest(
                run_dir=run_dir,
                keys=req.stock_keys,
                copy_preview_to_desktop=True,
                llm_api_key=req.llm_api_key,
                llm_base_url=req.llm_base_url,
                llm_model=req.llm_model,
                strict_sync=req.strict_sync,
                strict_full=req.strict_full,
                stars=req.stars,
                source_mix=req.source_mix,
                ai=req.ai,
            ),
            cancel=cancel,
            on_progress=lambda p, m: span(0.50, 0.80, p, m),
        )
    stage = detect_stage(run_dir)

    # --- Render ---
    render_hi = 0.90 if dub_wanted else 1.0
    if not needs_stage(stage, "render_done"):
        from engine.slides.ffprobe_util import probe_duration

        final = run_dir / "final.mp4"
        span(0.80, render_hi, 1.0, "Resume: final.mp4 уже есть")
        render = RenderResult(
            run_dir=run_dir,
            video_file=final,
            desktop_video=None,
            duration=probe_duration(final),
            segments=0,
            size_bytes=final.stat().st_size,
        )
    else:
        render = run_render_stage(
            RenderRequest(
                run_dir=run_dir,
                subscribe=req.subscribe,
                subscribe_path=req.subscribe_path,
                subtitles=req.subtitles,
                intro_dir=req.intro_dir,
                outro_dir=req.outro_dir,
                music=req.music,
                copy_to_desktop=True,
                speed_copies=req.speed_copies,
                llm=(req.llm_api_key, req.llm_base_url, req.llm_model),
                keep_silent=dub_wanted,
                folder_language=dub_language if dub_wanted else "",
            ),
            cancel=cancel,
            on_progress=lambda p, m: span(0.80, render_hi, p, m),
        )

    # --- Ролики на других языках ---
    dub_lines: list[str] = []
    if dub_wanted:
        dub_lines = run_dub_stage(
            req.tts.provider,
            req.tts.options,
            RenderRequest(
                run_dir=run_dir,
                subscribe=req.subscribe,
                subscribe_path=req.subscribe_path,
                subtitles=req.subtitles,
                intro_dir=req.intro_dir,
                outro_dir=req.outro_dir,
                music=req.music,
                copy_to_desktop=True,
                speed_copies=req.speed_copies,
                llm=(req.llm_api_key, req.llm_base_url, req.llm_model),
            ),
            language=dub_language,
            cancel=cancel,
            on_progress=lambda p, m: span(0.90, 1.0, p, m),
        )

    # ролик и переводы собраны — скачанные кадры больше не нужны (повторная сборка скачает заново)
    drop_stock(run_dir)

    return FullRunResult(
        tts=tts,
        whisper=whisper,
        chunks=chunks,
        media=media,
        render=render,
        dub_lines=dub_lines,
    )
