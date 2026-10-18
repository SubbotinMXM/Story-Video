"""TTS → Whisper → чанки+queries."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from engine.core.cancel import CancelToken
from engine.slides.chunks_pipeline import ChunksRequest, ChunksStageResult, run_chunks_stage
from engine.slides.llm_client import DEFAULT_BASE_URL, DEFAULT_MODEL
from engine.slides.tts_pipeline import TtsRequest, TtsResult, run_tts_stage
from engine.slides.whisper_pipeline import (
    WhisperRequest,
    WhisperStageResult,
    run_whisper_stage,
)
from engine.slides.whisper_timings import DEFAULT_MODEL_SIZE

ProgressCb = Callable[[float, str], None]


@dataclass
class VoiceStageResult:
    tts: TtsResult
    whisper: WhisperStageResult
    chunks: ChunksStageResult | None = None

    @property
    def run_dir(self) -> Path:
        return self.tts.run_dir


def run_tts_then_whisper(
    tts_req: TtsRequest,
    *,
    whisper_model: str = DEFAULT_MODEL_SIZE,
    cancel: CancelToken | None = None,
    on_progress: ProgressCb | None = None,
) -> VoiceStageResult:
    """0–55% TTS, 55–100% Whisper."""

    def tts_prog(pct: float, msg: str) -> None:
        if on_progress:
            on_progress(pct * 0.55, msg)

    def whisper_prog(pct: float, msg: str) -> None:
        if on_progress:
            on_progress(0.55 + pct * 0.45, msg)

    tts = run_tts_stage(tts_req, cancel=cancel, on_progress=tts_prog)
    whisper = run_whisper_stage(
        WhisperRequest(
            run_dir=tts.run_dir,
            language=tts.language,
            model_size=whisper_model,
            copy_preview_to_desktop=True,
        ),
        cancel=cancel,
        on_progress=whisper_prog,
    )
    return VoiceStageResult(tts=tts, whisper=whisper)


def run_tts_whisper_chunks(
    tts_req: TtsRequest,
    *,
    llm_api_key: str,
    llm_base_url: str = DEFAULT_BASE_URL,
    llm_model: str = DEFAULT_MODEL,
    whisper_model: str = DEFAULT_MODEL_SIZE,
    cancel: CancelToken | None = None,
    on_progress: ProgressCb | None = None,
) -> VoiceStageResult:
    """0–40% TTS, 40–70% Whisper, 70–100% chunks."""

    def tts_prog(pct: float, msg: str) -> None:
        if on_progress:
            on_progress(pct * 0.40, msg)

    def whisper_prog(pct: float, msg: str) -> None:
        if on_progress:
            on_progress(0.40 + pct * 0.30, msg)

    def chunks_prog(pct: float, msg: str) -> None:
        if on_progress:
            on_progress(0.70 + pct * 0.30, msg)

    tts = run_tts_stage(tts_req, cancel=cancel, on_progress=tts_prog)
    whisper = run_whisper_stage(
        WhisperRequest(
            run_dir=tts.run_dir,
            language=tts.language,
            model_size=whisper_model,
            copy_preview_to_desktop=True,
        ),
        cancel=cancel,
        on_progress=whisper_prog,
    )
    chunks = run_chunks_stage(
        ChunksRequest(
            run_dir=tts.run_dir,
            llm_api_key=llm_api_key,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            copy_preview_to_desktop=True,
        ),
        cancel=cancel,
        on_progress=chunks_prog,
    )
    return VoiceStageResult(tts=tts, whisper=whisper, chunks=chunks)
