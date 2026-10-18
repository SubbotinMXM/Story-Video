"""VoiceGen: перевод готовой озвучки на другие языки (дубляж).

Озвучка ролика делается обычной задачей VoiceGen (с полными таймингами). Сразу
после неё запускается дубляж этой задачи (source_task_id): оригинал повторно не
оплачивается, а фразы перевода VoiceGen подгоняет под время фраз оригинала —
поэтому к переводу подходит уже собранный видеоряд, меняются только голос и субтитры.

Состояние — в <папка ролика>/.dub.json: id заказа и какие языки уже стали роликами.
«Продолжить» после сбоя дожидается того же заказа, ничего не оплачивая заново.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from engine.core.cancel import CancelToken
from engine.core.logutil import log
from engine.core.lumean import tts_wait_timeout_sec
from engine.tts.base import DubTrack, ProgressCb
from engine.tts.voicegen import POLL_SEC, VoiceGenClient, VoiceGenError

# языки в интерфейсе (галочки); VoiceGen умеет больше, но эти нужны чаще всего
LANGUAGES: tuple[tuple[str, str], ...] = (
    ("ru", "Русский"),
    ("en", "Английский"),
    ("es", "Испанский"),
    ("de", "Немецкий"),
    ("fr", "Французский"),
    ("pl", "Польский"),
    ("it", "Итальянский"),
    ("ro", "Румынский"),
)
LANGUAGE_NAMES = dict(LANGUAGES)

SOURCE_NAME = ".voicegen_source.json"
STATE_NAME = ".dub.json"
DUB_DIR = ".dub"
# VoiceGen переводит только свежие озвучки (72 ч); запас на время до создания заказа
SOURCE_MAX_AGE_SEC = 72 * 3600 - 15 * 60
# дубляж поддерживает только стабильные модели
DUB_MODELS = ("sonic-3.6", "sonic-3.5", "sonic-3")
_VOICE_PREPARING_TRIES = 20
_POLL_ERRORS_MAX = 20
_FAILED = ("cancelled", "cancel_requested", "expired")


class DubUnavailable(VoiceGenError):
    """Перевести нельзя в принципе (нет исходной озвучки VoiceGen) — это не сбой."""


# ---------- состояние ----------


def write_source(work_dir: Path, *, task_id: str, template_id: str, text_chars: int) -> None:
    """Запомнить задачу озвучки: из неё потом делается перевод."""
    (work_dir / SOURCE_NAME).write_text(
        json.dumps({"task_id": task_id, "template_id": template_id, "text_chars": text_chars, "created": time.time()}),
        encoding="utf-8",
    )


def _read(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_state(work_dir: Path) -> dict:
    return _read(work_dir / STATE_NAME)


def _save_state(work_dir: Path, state: dict) -> None:
    (work_dir / STATE_NAME).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def mark_done(work_dir: Path, language: str, result: str) -> None:
    state = load_state(work_dir)
    state.setdefault("localized", {})[language] = result
    _save_state(work_dir, state)


def pending_languages(work_dir: Path) -> list[str]:
    state = load_state(work_dir)
    done = state.get("localized") or {}
    return [lang for lang in state.get("targets") or [] if lang not in done]


# ---------- запуск ----------


def _multiplier(raw: object, lo: float, hi: float) -> float:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1.0
    if value > 3:  # проценты (110 → 1.1)
        value /= 100
    return round(min(hi, max(lo, value)), 3)


def _dub_settings(template: dict) -> dict[str, Any]:
    """Шаблон бота в дубляж не передаётся — переносим из него модель, скорость и громкость."""
    settings = template.get("voice_engine_settings") if isinstance(template.get("voice_engine_settings"), dict) else {}
    model = str(template.get("model_id") or settings.get("model_id") or "")
    return {
        "model_id": model if model in DUB_MODELS else DUB_MODELS[0],
        "speed": _multiplier(settings.get("speed", 1.0), 0.6, 1.5),
        "volume": _multiplier(settings.get("volume", 1.0), 0.5, 2.0),
    }


def start(
    api_key: str,
    work_dir: Path,
    *,
    source_language: str,
    targets: list[str],
    base_url: str | None = None,
) -> list[str]:
    """Заказать перевод озвучки из work_dir. Повторный вызов не создаёт второй заказ."""
    state = load_state(work_dir)
    if state.get("job_id"):
        return list(state.get("targets") or [])
    # язык оригинала не переводим: это был бы второй такой же ролик за деньги
    targets = [t for t in dict.fromkeys(targets) if t and t != source_language]
    if not targets:
        return []
    src = _read(work_dir / SOURCE_NAME)
    if not src.get("task_id"):
        raise DubUnavailable(
            "перевод делается из озвучки VoiceGen, созданной для этого ролика, а здесь её нет "
            "(готовое аудио или другой сервис озвучки)"
        )
    if time.time() - float(src.get("created") or 0) > SOURCE_MAX_AGE_SEC:
        raise DubUnavailable("озвучке больше 72 часов — VoiceGen уже не может её перевести")

    client = VoiceGenClient(api_key, base_url) if base_url else VoiceGenClient(api_key)
    try:
        if not state.get("client_request_id"):
            template = client.template(str(src.get("template_id") or "")) if src.get("template_id") else {}
            state = {
                "client_request_id": f"story-video-{uuid.uuid4()}",
                "source_language": source_language or "auto",
                "targets": targets,
                "settings": _dub_settings(template),
                "text_chars": int(src.get("text_chars") or 0),
                "localized": {},
            }
            _save_state(work_dir, state)
        payload = {
            "client_request_id": state["client_request_id"],
            "source_task_id": src["task_id"],
            "source_language": state["source_language"],
            "target_languages": [{"language": t} for t in state["targets"]],
            # фразы — для запасных субтитров; основные строим сами по словам
            "timestamps_granularity": "phrase",
            **state["settings"],
        }
        names = ", ".join(t.upper() for t in state["targets"])
        log(f"VoiceGen: заказываю перевод {state['source_language'].upper()} → {names}")
        for attempt in range(1, _VOICE_PREPARING_TRIES + 1):
            try:
                job = client.create_dub(payload)
                break
            except VoiceGenError as exc:
                if "VoiceGen 422" in str(exc) and "subtitles" in str(exc).lower():
                    # озвучка сделана без таймингов (до исправления) — перевести её нельзя
                    raise DubUnavailable(
                        "озвучка этого ролика сделана без таймингов, VoiceGen не может её перевести. "
                        "Новые ролики озвучиваются с таймингами и переводятся"
                    ) from exc
                # голос ElevenLabs ещё готовится — VoiceGen просит повторить тот же запрос позже
                if "voice_preparing" not in str(exc) or attempt == _VOICE_PREPARING_TRIES:
                    raise
                log("VoiceGen: голос ещё готовится к переводу, жду…")
                time.sleep(15)
        state["job_id"] = str(job["job_id"])
        _save_state(work_dir, state)
        return list(state["targets"])
    finally:
        client.close()


# ---------- готовые переводы ----------


_ALIASES = {"ua": "uk", "ch": "zh"}


def _lang_key(code: object) -> str:
    key = str(code or "").lower().split("-")[0]
    return _ALIASES.get(key, key)


def parse_timings(raw: object) -> list[dict] | None:
    """Слова с таймингами из ответа VoiceGen → [{"word", "start", "end"}] в секундах."""
    items: object = raw
    if isinstance(raw, dict):
        items = next((raw[k] for k in ("words", "timings", "items", "segments") if isinstance(raw.get(k), list)), None)
    if not isinstance(items, list):
        return None
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        word = str(item.get("word") or item.get("text") or item.get("token") or "").strip()
        if not word:
            continue
        if "start_ms" in item or "end_ms" in item:
            start, end, scale = item.get("start_ms"), item.get("end_ms"), 1000.0
        else:
            start = item.get("start", item.get("start_time"))
            end = item.get("end", item.get("end_time"))
            scale = 1.0
        try:
            s, e = float(start) / scale, float(end) / scale  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        out.append({"word": word, "start": s, "end": max(e, s)})
    return out or None


def _download_track(client: VoiceGenClient, job_id: str, lang: str, folder: Path) -> DubTrack:
    folder.mkdir(parents=True, exist_ok=True)
    base = f"/dubbing/{job_id}/tracks/{lang}"
    audio = folder / "voice.mp3"
    part = folder / "voice.part.mp3"
    client.fetch(f"{base}/audio", part)
    part.replace(audio)
    track = DubTrack(language=lang, audio=audio)
    try:
        client.fetch(f"{base}/text", folder / "script.txt", min_size=1)
        track.text = folder / "script.txt"
    except (VoiceGenError, httpx.HTTPError) as exc:
        log(f"VoiceGen {lang}: текст перевода не скачался ({exc})")
    try:
        client.fetch(f"{base}/srt", folder / "subs_voicegen.srt", min_size=1)
        track.srt = folder / "subs_voicegen.srt"
    except (VoiceGenError, httpx.HTTPError) as exc:
        log(f"VoiceGen {lang}: субтитры не скачались ({exc})")
    try:
        raw_path = folder / "timings_voicegen.json"
        client.fetch(f"{base}/timings", raw_path, min_size=1)
        words = parse_timings(json.loads(raw_path.read_text(encoding="utf-8")))
        if words:
            (folder / "timings.json").write_text(json.dumps({"words": words}, ensure_ascii=False), encoding="utf-8")
            track.timings = folder / "timings.json"
        else:
            log(f"VoiceGen {lang}: тайминги слов в незнакомом виде — беру их субтитры")
    except (VoiceGenError, httpx.HTTPError, ValueError) as exc:
        log(f"VoiceGen {lang}: тайминги слов не скачались ({exc})")
    return track


def iter_ready(
    api_key: str,
    work_dir: Path,
    *,
    cancel: CancelToken | None = None,
    on_progress: ProgressCb | None = None,
    poll_sec: float = POLL_SEC,
    base_url: str | None = None,
) -> Iterator[DubTrack]:
    """Переводы по мере готовности. Пока вызывающий собирает ролик, остальные языки озвучиваются."""

    def progress(pct: float, msg: str) -> None:
        if on_progress:
            on_progress(pct, msg)

    state = load_state(work_dir)
    job_id = str(state.get("job_id") or "")
    if not job_id:
        return
    targets = list(state.get("targets") or [])
    client = VoiceGenClient(api_key, base_url) if base_url else VoiceGenClient(api_key)
    if cancel is not None:
        cancel.register(client)
    per_lang = tts_wait_timeout_sec(int(state.get("text_chars") or 0) * 3 // 2)
    deadline = time.monotonic() + per_lang * max(1, len(targets))
    resumed = False
    errors = 0
    last_note = ""
    try:
        while True:
            done = load_state(work_dir).get("localized") or {}
            pending = [t for t in targets if t not in done]
            if not pending:
                return
            try:
                job = client.get_dub(job_id)
                errors = 0
            except (httpx.TransportError, VoiceGenError) as exc:
                if cancel is not None:
                    cancel.check()
                errors += 1
                transient = isinstance(exc, httpx.TransportError) or any(f" {c}:" in f" {exc}" for c in ("409", "429", "502", "503", "504"))
                if not transient or errors > _POLL_ERRORS_MAX:
                    raise
                log(f"VoiceGen: временная ошибка при проверке перевода ({exc}), жду")
                _sleep(poll_sec, cancel)
                continue
            status = str(job.get("status") or "")
            if status == "needs_attention" and not resumed:
                # «Продолжить» после пополнения баланса: VoiceGen доделает с сохранённых фраз
                resumed = True
                log("VoiceGen: перевод был приостановлен — продолжаю заказ")
                client.resume_dub(job_id)
                continue
            tracks = {
                _lang_key(t.get("language")): t
                for t in job.get("tracks") or []
                if isinstance(t, dict) and not t.get("is_original")
            }
            ready = [lang for lang in pending if str((tracks.get(lang) or {}).get("status") or "") == "done"]
            if ready:
                lang = ready[0]
                progress(0.0, f"Перевод {lang.upper()} готов — скачиваю")
                yield _download_track(client, job_id, str(tracks[lang].get("language") or lang), work_dir / DUB_DIR / lang)
                continue
            why = str(job.get("error_message") or job.get("error_code") or "").strip()
            if status == "needs_attention":
                raise VoiceGenError(
                    f"Перевод остановлен VoiceGen: {why or 'нужно вмешательство'}. "
                    "Пополни баланс или поправь причину и нажми «Продолжить» — готовые языки не оплачиваются заново"
                )
            if status in _FAILED:
                raise VoiceGenError(f"Заказ перевода VoiceGen {status}: {why}".rstrip(": "))
            if status == "done":
                failed = ", ".join(lang.upper() for lang in pending)
                raise VoiceGenError(f"VoiceGen не перевёл: {failed}. {why}".strip())
            if time.monotonic() > deadline:
                raise VoiceGenError("VoiceGen долго не заканчивает перевод — нажми «Продолжить», заказ не потеряется")
            cur = _lang_key(job.get("current_language"))
            try:
                cur_pct = float(job.get("current_language_progress") or 0)
            except (TypeError, ValueError):
                cur_pct = 0.0
            ready_n = len(targets) - len(pending)
            note = f"Перевод VoiceGen: {cur.upper() or '…'} {cur_pct:.0f}% (готово {ready_n} из {len(targets)})"
            if note != last_note:
                progress(ready_n / max(1, len(targets)), note)
                log(note)
                last_note = note
            _sleep(poll_sec, cancel)
    finally:
        client.close()


def _sleep(sec: float, cancel: CancelToken | None) -> None:
    end = time.monotonic() + sec
    while time.monotonic() < end:
        if cancel is not None:
            cancel.check()
        time.sleep(min(0.5, sec))
