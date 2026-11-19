"""Локальный Whisper → тайминги по словам (faster-whisper, model base)."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from engine.core.cancel import CancelToken, CancelledError, run_cancellable
from engine.core.logutil import log

DEFAULT_MODEL_SIZE = "base"
# На Mac (Apple Silicon) CTranslate2 обычно CPU; int8 быстрее и легче.
DEFAULT_DEVICE = "cpu"
DEFAULT_COMPUTE_TYPE = "int8"

ProgressCb = Callable[[float, str], None]

_model_lock = threading.Lock()
_transcribe_lock = threading.Lock()
_model_cache: dict[tuple[str, str, str], Any] = {}


@dataclass
class WordTiming:
    word: str
    start: float
    end: float
    probability: float = 0.0


@dataclass
class TimingsResult:
    words: list[WordTiming]
    language: str
    duration: float
    model_size: str
    text: str


class WhisperError(RuntimeError):
    pass


def get_model(
    *,
    model_size: str = DEFAULT_MODEL_SIZE,
    device: str = DEFAULT_DEVICE,
    compute_type: str = DEFAULT_COMPUTE_TYPE,
):
    """Ленивая загрузка модели (кэш на процесс)."""
    key = (model_size, device, compute_type)
    with _model_lock:
        cached = _model_cache.get(key)
        if cached is not None:
            return cached
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise WhisperError(
                "Нет пакета faster-whisper. Перезапусти ./run.sh "
                "(он поставит зависимости)."
            ) from exc
        log(
            f"Whisper: гружу модель size={model_size} "
            f"device={device} compute={compute_type}…"
        )
        model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
        )
        _model_cache[key] = model
        log("Whisper: модель готова")
        return model


def _load_audio_16k(src: Path) -> np.ndarray:
    """mp3/m4a/… → float32 mono @16k через ffmpeg.

    faster-whisper всегда зовёт PyAV на path; av 19 ломает metadata_errors.
    ndarray пропускает decode_audio целиком.
    """
    from engine.slides.ffmpeg_util import find_ffmpeg

    cmd = [
        find_ffmpeg(),
        "-nostdin",
        "-i",
        str(src),
        "-ar",
        "16000",
        "-ac",
        "1",
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-",
    ]
    proc = run_cancellable(cmd)
    if proc.returncode != 0 or not proc.stdout:
        err = (proc.stderr or b"").decode("utf-8", errors="replace")[-500:]
        raise WhisperError(f"Не удалось декодировать аудио для Whisper:\n{err}")
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe_words(
    audio: Path,
    *,
    language: str,
    model_size: str = DEFAULT_MODEL_SIZE,
    cancel: CancelToken | None = None,
    on_progress: ProgressCb | None = None,
) -> TimingsResult:
    """Распознать аудио → список слов с start/end (сек). Одна расшифровка за раз."""
    if not _transcribe_lock.acquire(blocking=False):
        if on_progress:
            on_progress(0.02, "Whisper: жду, пока распознаётся другой ролик…")
        while not _transcribe_lock.acquire(timeout=0.5):
            if cancel is not None:
                cancel.check()
    try:
        return _transcribe_words(
            audio, language=language, model_size=model_size, cancel=cancel, on_progress=on_progress
        )
    finally:
        _transcribe_lock.release()


def _transcribe_words(
    audio: Path,
    *,
    language: str,
    model_size: str,
    cancel: CancelToken | None,
    on_progress: ProgressCb | None,
) -> TimingsResult:
    if not audio.is_file():
        raise WhisperError(f"Нет аудио: {audio}")
    lang = (language or "").strip().lower() or None

    if on_progress:
        on_progress(0.05, "Whisper: загрузка модели…")
    if cancel:
        cancel.check()

    model = get_model(model_size=model_size)
    if cancel:
        cancel.check()
    if on_progress:
        on_progress(0.12, "Whisper: декодирую аудио…")
    samples = _load_audio_16k(audio)
    if cancel:
        cancel.check()
    if on_progress:
        on_progress(0.15, f"Whisper: распознаю ({model_size}, lang={lang or 'auto'})…")

    try:
        segments_gen, info = model.transcribe(
            samples,
            language=lang,
            word_timestamps=True,
            vad_filter=True,
            beam_size=5,
        )
    except CancelledError:
        raise
    except Exception as exc:
        raise WhisperError(f"Whisper transcribe failed: {exc}") from exc

    duration = float(getattr(info, "duration", 0.0) or 0.0)
    detected = str(getattr(info, "language", lang or "") or lang or "")
    if lang is None and on_progress:
        prob = float(getattr(info, "language_probability", 0.0) or 0.0)
        on_progress(0.16, f"Whisper: язык определён — {detected} ({prob:.0%})")
    words: list[WordTiming] = []
    text_parts: list[str] = []

    # Итерация генератора — тут идёт реальная работа
    last_pct = 0.15
    for segment in segments_gen:
        if cancel is not None:
            cancel.check()
        seg_text = (segment.text or "").strip()
        if seg_text:
            text_parts.append(seg_text)
        seg_words = getattr(segment, "words", None) or []
        for w in seg_words:
            token = (getattr(w, "word", None) or "").strip()
            if not token:
                continue
            words.append(
                WordTiming(
                    word=token,
                    start=float(w.start or 0.0),
                    end=float(w.end or 0.0),
                    probability=float(getattr(w, "probability", 0.0) or 0.0),
                )
            )
        if duration > 0 and on_progress:
            end = float(getattr(segment, "end", 0.0) or 0.0)
            pct = 0.15 + 0.80 * min(1.0, end / duration)
            if pct - last_pct >= 0.02:
                last_pct = pct
                on_progress(
                    pct, f"Whisper: {end:.0f}/{duration:.0f}с, слов: {len(words)}"
                )

    if on_progress:
        on_progress(0.97, f"Whisper: готово, слов={len(words)}")

    if not words:
        raise WhisperError(
            "Whisper не вернул ни одного слова. Проверь аудио и язык ролика."
        )

    return TimingsResult(
        words=words,
        language=detected or (lang or ""),
        duration=duration,
        model_size=model_size,
        text=" ".join(text_parts).strip(),
    )


def timings_to_dict(result: TimingsResult) -> dict[str, Any]:
    return {
        "language": result.language,
        "duration": round(result.duration, 3),
        "model_size": result.model_size,
        "word_count": len(result.words),
        "text": result.text,
        "words": [asdict(w) for w in result.words],
    }


def save_timings_json(path: Path, result: TimingsResult) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(timings_to_dict(result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def save_timings_preview(path: Path, result: TimingsResult, *, sample: int = 12) -> Path:
    """Человекочитаемый превью для проверки глазами."""
    lines: list[str] = [
        f"language: {result.language}",
        f"duration: {result.duration:.2f}s",
        f"model: {result.model_size}",
        f"words: {len(result.words)}",
        "",
        "=== начало ===",
    ]
    head = result.words[:sample]
    for w in head:
        lines.append(f"{w.start:8.2f}–{w.end:8.2f}  {w.word}")
    if len(result.words) > sample * 2:
        lines.append("...")
        mid = len(result.words) // 2
        for w in result.words[mid : mid + 4]:
            lines.append(f"{w.start:8.2f}–{w.end:8.2f}  {w.word}")
        lines.append("...")
    if len(result.words) > sample:
        lines.append("=== конец ===")
        for w in result.words[-sample:]:
            lines.append(f"{w.start:8.2f}–{w.end:8.2f}  {w.word}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
