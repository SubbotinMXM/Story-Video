"""Рассказ с футажами: текст → TTS → ролик, либо сразу готовое аудио.

Работа идёт по этапам в рабочей папке; готовые этапы отмечаются в .progress.json.
Повторный запуск с тем же work_dir пропускает готовое (озвучка не оплачивается дважды).
"""

from __future__ import annotations

import json
import random
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from engine.broll.output import (
    audio_output_path,
    finalize_run_dir,
    run_output_dir,
    story_output_path,
    video_output_path,
)
from engine.compositor.defaults import (
    SUBSCRIBE_BLEND,
    SUBSCRIBE_CHROMA,
    SUBSCRIBE_SIMILARITY,
    SUBSCRIBE_START_MAX,
    SUBSCRIBE_START_MIN,
)
from engine.compositor.pipeline import (
    ComposeRequest,
    _maybe_append_outro,
    _maybe_prepend_intro,
    _overlay_subscribe,
    build_and_run,
)
from engine.compositor.utils import probe_chromakey_crop
from engine.core.cancel import CancelToken
from engine.core.logutil import log
from engine.core.throttle import heavy_stage
from engine.slides.ffmpeg_util import has_filter
from engine.slides.render_mux import burn_subtitles
from engine.slides.speed_copies import write_speed_copies
from engine.slides.subtitles import mark_script_origin, timings_to_srt
from engine.slides.whisper_timings import save_timings_json, transcribe_words
from engine.tts import DEFAULT_PROVIDER, get_provider

ProgressCb = Callable[[float, str], None]

PROGRESS_NAME = ".progress.json"
TTS_ORDER_NAME = ".tts_order.json"


@dataclass
class BrollRequest:
    broll_dir: Path
    overlay_text: str = ""
    head_dir: Path | None = None
    intro_dir: Path | None = None
    outro_dir: Path | None = None
    subscribe: bool = False
    subscribe_path: Path | None = None
    subtitles: bool = False
    story_text: str = ""
    source_audio: Path | None = None
    title_hint: str = ""
    # сервис озвучки и его настройки (engine/tts)
    tts_provider: str = DEFAULT_PROVIDER
    tts_options: dict = field(default_factory=dict)
    speed_copies: int = 0
    # пусто — язык определит Whisper по аудио
    language: str = ""
    # рабочая папка: задана — продолжаем в ней, иначе создаём новую на Desktop
    work_dir: Path | None = None
    # озвучка готова (папка результатов) — например, заказать её перевод, пока собирается видео
    on_audio_ready: Callable[[Path], None] | None = None


@dataclass
class BrollResult:
    video: Path
    output_dir: Path
    text_file: Path | None
    audio_file: Path | None


class _Stages:
    def __init__(self, folder: Path) -> None:
        self.path = folder / PROGRESS_NAME
        try:
            self.done: set[str] = set(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError):
            self.done = set()

    def __contains__(self, stage: str) -> bool:
        return stage in self.done

    def mark(self, stage: str) -> None:
        self.done.add(stage)
        self.path.write_text(json.dumps(sorted(self.done)), encoding="utf-8")


def run_broll(
    req: BrollRequest,
    *,
    cancel: CancelToken | None = None,
    on_progress: ProgressCb | None = None,
) -> BrollResult:
    def progress(pct: float, msg: str) -> None:
        log(msg)
        if on_progress:
            on_progress(pct, msg)

    def check() -> None:
        if cancel:
            cancel.check()

    text = (req.story_text or "").strip()
    audio_src = req.source_audio
    if audio_src is not None and not audio_src.is_file():
        raise FileNotFoundError(f"Нет аудиофайла: {audio_src}")
    # продолжение: озвучка уже в рабочей папке — ни текст, ни исходный файл не нужны
    audio_ready = req.work_dir is not None and req.work_dir.is_dir() and "audio" in _Stages(req.work_dir)
    if audio_src is None and not text and not audio_ready:
        raise ValueError("Вставь текст или выбери готовое аудио")
    if not req.broll_dir.is_dir():
        raise FileNotFoundError(f"Папка футажей не найдена: {req.broll_dir}")

    title = (req.title_hint or "").strip()
    if not title:
        title = audio_src.stem if audio_src is not None else (text[:60] or "ролик")

    check()
    if req.work_dir is not None:
        out_dir = req.work_dir
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = run_output_dir(title)
    stages = _Stages(out_dir)
    if stages.done:
        progress(0.03, f"Продолжаю: готово {', '.join(sorted(stages.done))}")
    progress(0.05, f"Папка результатов: {out_dir.name}")

    text_file = None
    if text:
        text_file = story_output_path(out_dir=out_dir, title_hint=title)
        text_file.write_text(text, encoding="utf-8")

    audio_file = audio_output_path(out_dir=out_dir, title_hint=title)
    if "audio" not in stages or not audio_file.is_file():
        if audio_src is None:
            provider = get_provider(req.tts_provider)
            progress(0.1, f"Озвучка: {provider.label}…")
            provider.synthesize(
                text,
                audio_file,
                req.tts_options,
                state_file=out_dir / TTS_ORDER_NAME,
                cancel=cancel,
                on_progress=lambda p, m: progress(0.1 + p * 0.45, m),
            )
        else:
            progress(0.2, f"Беру готовое аудио: {audio_src.name}")
            shutil.copy2(audio_src, audio_file)
        stages.mark("audio")
    progress(0.55, f"Аудио: {audio_file.name} ({audio_file.stat().st_size} bytes)")
    if req.on_audio_ready is not None:
        req.on_audio_ready(out_dir)

    video = video_output_path(out_dir=out_dir, title_hint=title)
    # Субтитры вшиваются прямо в проходе сборки: без второго полного пережатия
    # ролика (это и время, и ещё один файл размером с ролик на диске).
    subs_srt: Path | None = None
    if req.subtitles and "subs" not in stages and ("compose" not in stages or not video.is_file()) and has_filter("subtitles"):
        check()
        progress(0.55, "Субтитры: Whisper…")
        subs_srt = _make_broll_srt(
            audio=audio_file,
            script=text_file,
            out_dir=out_dir,
            language=req.language,
            cancel=cancel,
            on_progress=progress,
            span=(0.55, 0.58),
        )

    # сборка видео — по одной на приложение (озвучка выше идёт параллельно)
    with heavy_stage("Сборка видео", cancel):

        def video_prog(pct: float, msg: str) -> None:
            check()
            progress(0.58 + pct * 0.22, msg)

        if "compose" not in stages or not video.is_file():
            check()
            progress(0.58, "Сборка видео…")
            build_and_run(
                ComposeRequest(
                    audio=audio_file,
                    broll_dir=req.broll_dir,
                    head_dir=req.head_dir,
                    text=(req.overlay_text or "").strip(),
                    output=video,
                    # всё в одном проходе: интро, аутро, подписка и субтитры не пережимают ролик заново
                    subscribe=bool(req.subscribe) and req.subscribe_path is not None and req.subscribe_path.is_file(),
                    subscribe_path=req.subscribe_path,
                    intro_dir=req.intro_dir,
                    outro_dir=req.outro_dir,
                    srt=subs_srt,
                ),
                on_progress=video_prog,
            )
            for stage in ("compose", "intro", "outro", "subscribe"):
                stages.mark(stage)
            if subs_srt is not None:
                stages.mark("subs")

        if req.subtitles and "subs" not in stages:
            check()
            progress(0.82, "Субтитры: Whisper…")
            _burn_broll_subs(
                video=video,
                audio=audio_file,
                script=text_file,
                out_dir=out_dir,
                language=req.language,
                cancel=cancel,
                on_progress=progress,
            )
            stages.mark("subs")

        rng = random.Random()
        if "intro" not in stages:
            check()
            progress(0.90, "Интро…")
            _maybe_prepend_intro(video, req.intro_dir, rng=rng, on_progress=progress)
            stages.mark("intro")
        if "outro" not in stages:
            check()
            progress(0.92, "Аутро…")
            _maybe_append_outro(video, req.outro_dir, rng=rng, on_progress=progress)
            stages.mark("outro")

        subscribe = bool(req.subscribe) and req.subscribe_path is not None and req.subscribe_path.is_file()
        if subscribe and "subscribe" not in stages:
            check()
            progress(0.94, "Подписка…")
            sub_path = req.subscribe_path
            assert sub_path is not None
            crop = probe_chromakey_crop(
                sub_path,
                chroma=SUBSCRIBE_CHROMA,
                similarity=SUBSCRIBE_SIMILARITY,
                blend=SUBSCRIBE_BLEND,
                pad=8,
            )
            _overlay_subscribe(
                video,
                sub_path,
                start=rng.uniform(SUBSCRIBE_START_MIN, SUBSCRIBE_START_MAX),
                crop=crop,
                on_progress=progress,
            )
            stages.mark("subscribe")

        if req.speed_copies > 0 and "copies" not in stages:
            check()
            progress(0.96, f"Копии скорости: {req.speed_copies}")
            write_speed_copies(
                video,
                out_dir,
                req.speed_copies,
                base_name=video.name,
                cancel=cancel,
                on_progress=lambda p, m: progress(0.96 + p * 0.03, m),
            )
            stages.mark("copies")

    # всё готово: убрать служебное и дать папке финальное имя
    check()
    stages.path.unlink(missing_ok=True)
    (out_dir / TTS_ORDER_NAME).unlink(missing_ok=True)
    old_dir = out_dir
    out_dir = finalize_run_dir(out_dir, title, datetime.now())
    if out_dir != old_dir:
        video = out_dir / video.name
        audio_file = out_dir / audio_file.name
        if text_file is not None:
            text_file = out_dir / text_file.name
    progress(1.0, f"Готово: {out_dir.name}/{video.name}")
    return BrollResult(
        video=video,
        output_dir=out_dir,
        text_file=text_file,
        audio_file=audio_file,
    )


def _make_broll_srt(
    *,
    audio: Path,
    script: Path | None,
    out_dir: Path,
    language: str,
    cancel: CancelToken | None,
    on_progress: ProgressCb,
    span: tuple[float, float] = (0.86, 0.92),
) -> Path | None:
    """Whisper по озвучке → subs.srt (текстом сценария, если он есть)."""
    lo, hi = span
    timings = transcribe_words(
        audio,
        language=language,
        cancel=cancel,
        on_progress=lambda p, m: on_progress(lo + p * (hi - lo), m),
    )
    timings_path = out_dir / "timings.json"
    save_timings_json(timings_path, timings)
    if script is not None and script.is_file():
        mark_script_origin(out_dir, "user")
    srt = timings_to_srt(
        timings_path,
        out_dir / "subs.srt",
        script_path=script,
        run_dir=out_dir,
    )
    if srt is None:
        log("Субтитры: пустой SRT, пропускаю")
    return srt


def _burn_broll_subs(
    *,
    video: Path,
    audio: Path,
    script: Path | None,
    out_dir: Path,
    language: str,
    cancel: CancelToken | None,
    on_progress: ProgressCb,
) -> None:
    """Запасной путь: вшить субтитры в уже собранный ролик (лишнее пережатие)."""
    srt = _make_broll_srt(
        audio=audio, script=script, out_dir=out_dir, language=language, cancel=cancel, on_progress=on_progress
    )
    if srt is None:
        return
    on_progress(0.93, "Субтитры: вшиваю…")
    burn_subtitles(video, srt)
