"""Сборка задач: форма + сохранённые настройки → вызов пайплайна.

Проверки входа делаются при постановке в очередь (ValueError → 400),
сам пайплайн запускается позже в рабочем потоке.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

from engine.broll.output import run_output_dir
from engine.broll.pipeline import PROGRESS_NAME as BROLL_PROGRESS
from engine.broll.pipeline import BrollRequest, run_broll
from engine.core.cancel import CancelToken
from engine.core.config import load_config
from engine.core.logutil import log
from engine.core.text_jobs import TextJob
from engine.slides.dub_stage import source_language, start_dub
from engine.slides.music import track_path
from engine.slides.publish import lang_suffix
from engine.slides.full_pipeline import FullRunRequest, run_full_pipeline
from engine.slides.llm_client import DEFAULT_BASE_URL, DEFAULT_MODEL
from engine.slides.media_pipeline import MediaRequest, run_media_stage, stock_keys_from_config
from engine.slides.paths import RUNS_DIR, new_run_dir
from engine.slides.render_pipeline import RenderRequest, run_render_stage
from engine.slides.resume import describe_resume, detect_stage
from engine.slides.speed_copies import write_speed_copies
from engine.slides.tts_pipeline import TtsRequest
from engine.tts import get_provider, provider_options
from engine.tts.voicegen_dub import DUB_DIR
from engine.tts.voicegen_dub import SOURCE_NAME as DUB_SOURCE
from engine.tts.voicegen_dub import STATE_NAME as DUB_STATE
from engine.tts.voicegen_dub import pending_languages as pending_dubs
from server.jobs import Job

MAX_COPIES = 12


# ---------- настройки ----------


def _s(cfg: dict, key: str) -> str:
    return str(cfg.get(key) or "").strip()


def _opt_dir(raw: str) -> Path | None:
    return Path(raw).expanduser() if raw.strip() else None


def _tts(cfg: dict) -> tuple[str, dict[str, Any]]:
    """Выбранный сервис озвучки и его настройки — фиксируются при постановке в очередь."""
    provider_id = _s(cfg, "tts_provider") or "lumean"
    get_provider(provider_id)
    return provider_id, provider_options(cfg, provider_id)


def _copies(cfg: dict) -> int:
    if not cfg.get("speed_copies"):
        return 0
    try:
        n = int(cfg.get("speed_copies_count") or 1)
    except (TypeError, ValueError):
        raise ValueError("Число копий должно быть целым") from None
    if not 1 <= n <= MAX_COPIES:
        raise ValueError(f"Число копий от 1 до {MAX_COPIES}")
    return n


def _subscribe(cfg: dict) -> Path | None:
    if not cfg.get("subscribe"):
        return None
    raw = _s(cfg, "subscribe_path")
    if not raw:
        raise ValueError("Включён призыв подписаться, но не выбран файл анимации")
    path = Path(raw).expanduser()
    if not path.is_file():
        raise ValueError(f"Файл анимации подписки не найден: {path}")
    return path


def _audio(raw: str | None) -> Path | None:
    text = (raw or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_file():
        raise ValueError(f"Аудиофайл не найден: {path}")
    return path


def _llm(cfg: dict) -> tuple[str, str, str]:
    key = _s(cfg, "llm_api_key")
    if not key:
        raise ValueError("Нужен ключ LLM (Настройки → LLM)")
    return key, _s(cfg, "llm_base_url") or DEFAULT_BASE_URL, _s(cfg, "llm_model") or DEFAULT_MODEL


def _need_tts(cfg: dict, audio: Path | None) -> None:
    if audio is None:
        provider_id, opts = _tts(cfg)
        get_provider(provider_id).check(opts)


def _run_dir(name: str) -> Path:
    clean = Path((name or "").strip()).name
    path = RUNS_DIR / clean
    if not clean or not path.is_dir():
        raise ValueError(f"Нет такого запуска: {name}")
    return path


def _text_job(params: dict) -> TextJob:
    text = str(params.get("text") or "").strip()
    source = str(params.get("source") or "").strip() or None
    return TextJob(text=text, source=source)


def _title(job: TextJob, audio: Path | None) -> str:
    if not job.source and not job.text and audio is not None:
        return audio.stem
    return job.title_hint


def _tts_req(cfg: dict, tj: TextJob, audio: Path | None, *, run_dir: Path | None = None) -> TtsRequest:
    hint = _title(tj, audio)
    provider_id, opts = _tts(cfg)
    return TtsRequest(
        text=tj.text,
        provider=provider_id,
        options=opts,
        title_hint=hint[:40] if not tj.source else hint,
        copy_audio_to_desktop=True,
        run_dir=run_dir,
        source_audio=audio,
    )


def _ai(cfg: dict):
    """AI-кадры: включены галочкой, есть ключ и выбрана хоть одна модель — иначе None."""
    from engine.slides.ai_gen import AiConfig

    if not cfg.get("ai_gen_enabled"):
        return None
    ai = AiConfig(api_key=_s(cfg, "royaltechno_api_key"), image_model=_s(cfg, "ai_image_model"), video_model=_s(cfg, "ai_video_model"))
    if not ai.api_key:
        raise ValueError("Включены AI-кадры, но нет ключа RoyalTechno (Настройки → AI-кадры)")
    if not ai.active:
        raise ValueError("Включены AI-кадры, но не выбрана ни модель фото, ни модель видео")
    return ai


def _media_mode(cfg: dict) -> dict[str, bool]:
    mode = _s(cfg, "media_mode") or "theme"
    return {"strict_sync": mode in ("strict_3min", "strict_full"), "strict_full": mode == "strict_full"}


def _render_flags(cfg: dict) -> dict[str, Any]:
    sub = _subscribe(cfg)
    return {
        "subscribe": sub is not None,
        "subscribe_path": sub,
        "subtitles": bool(cfg.get("subtitles")),
        "intro_dir": _opt_dir(_s(cfg, "slides_intro_dir")),
        "outro_dir": _opt_dir(_s(cfg, "slides_outro_dir")),
        "music": track_path(_s(cfg, "music_track")),
    }


# ---------- результаты ----------


def _slides_result(render, extra: list[str] | None = None) -> dict[str, Any]:
    from engine.slides.ai_fill import report_lines

    folder = render.desktop_video or render.run_dir
    mb = render.size_bytes / 1e6
    ai_lines, warnings = report_lines(render.run_dir)
    summary = f"Готово: {render.duration:.0f} с, {mb:.1f} MB"
    return {
        "summary": summary + (" · AI-кадры не все — см. карточку" if warnings else ""),
        "folder": str(folder),
        "video": str(render.video_file),
        "lines": [
            f"Папка: {folder}",
            f"Длительность: {render.duration:.1f} с",
            f"Размер: {mb:.1f} MB",
            *ai_lines,
            *(extra or []),
        ],
        # оповещения: что не удалось и почему (ролик при этом собран)
        "warnings": warnings,
    }


# ---------- задачи ----------
#
# builder(cfg, params, state) -> (title, runner). state — то, что задача успела
# создать (рабочая папка). При «Продолжить» builder вызывается снова со свежими
# настройками и тем же state, поэтому готовые этапы пропускаются.


def build(kind: str, params: dict[str, Any], state: dict[str, Any]) -> tuple[str, Any]:
    builder = _BUILDERS.get(kind)
    if builder is None:
        raise ValueError(f"Неизвестный тип задачи: {kind}")
    return builder(load_config(), params, state)


def _state_dir(state: dict, key: str) -> Path | None:
    raw = state.get(key)
    path = Path(raw) if raw else None
    return path if path is not None and path.is_dir() else None


def _broll_stages(work: Path) -> list[str]:
    try:
        raw = json.loads((work / BROLL_PROGRESS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return raw if isinstance(raw, list) else []


def _broll(cfg: dict, params: dict, state: dict) -> tuple[str, Any]:
    work = _state_dir(state, "work_dir")
    tj = _text_job(params)
    audio_done = work is not None and "audio" in _broll_stages(work)
    audio = None if audio_done else _audio(params.get("audio"))
    if not tj.text and audio is None and not audio_done:
        raise ValueError("Нужен текст рассказа или готовое аудио")
    if not audio_done:
        _need_tts(cfg, audio)
    broll_dir = Path(_s(cfg, "broll_dir")).expanduser()
    if not _s(cfg, "broll_dir") or not broll_dir.is_dir():
        raise ValueError("Укажи папку футажей")
    sub = _subscribe(cfg)
    tts_id, tts_opts = _tts(cfg)
    title_hint = str(state.get("title_hint") or _title(tj, audio))
    req = BrollRequest(
        broll_dir=broll_dir,
        overlay_text=_s(cfg, "overlay_text"),
        head_dir=_opt_dir(_s(cfg, "head_dir")),
        intro_dir=_opt_dir(_s(cfg, "intro_dir")),
        outro_dir=_opt_dir(_s(cfg, "outro_dir")),
        subscribe=sub is not None,
        subscribe_path=sub,
        subtitles=bool(cfg.get("subtitles")),
        story_text=tj.text,
        source_audio=audio,
        title_hint=title_hint,
        tts_provider=tts_id,
        tts_options=tts_opts,
        speed_copies=_copies(cfg),
    )

    dub_wanted = bool(get_provider(tts_id).dub_languages(tts_opts))
    dub_language = source_language("", tj.text)
    if dub_wanted:
        # рядом будут папки переводов — у оригинала тоже язык в имени: «Монголия (RU)»
        req.title_hint = title_hint + lang_suffix(dub_language)

    def order_dub(out_dir: Path) -> None:
        try:
            start_dub(tts_id, tts_opts, out_dir, language=dub_language)
        except Exception as exc:  # основной ролик важнее; после него попробуем ещё раз
            log(f"Перевод: заказ не создан ({exc}) — повторю после сборки ролика")

    if dub_wanted:
        req.on_audio_ready = order_dub

    def run(job: Job, progress, cancel: CancelToken) -> dict[str, Any]:
        main_share = 0.75 if dub_wanted else 1.0
        work_dir = _state_dir(job.state, "work_dir")
        if job.state.get("main_done") and work_dir is not None:
            out_dir, video = work_dir, Path(str(job.state.get("main_video") or ""))
        else:
            if work_dir is None:
                work_dir = run_output_dir(req.title_hint)
                job.save_state(work_dir=str(work_dir), owned_dir=str(work_dir), title_hint=title_hint)
            req.work_dir = work_dir
            res = run_broll(req, cancel=cancel, on_progress=lambda p, m: progress(p * main_share, m))
            out_dir, video = res.output_dir, res.video
            # папка переименована в финальное имя — сбросом её больше не трогаем
            job.save_state(work_dir=str(out_dir), owned_dir=None, main_done=True, main_video=str(video))
        lines = [f"Папка: {out_dir}", f"Ролик: {video.name}"]
        if dub_wanted:
            lines += _broll_dubs(job, req, out_dir, title_hint, cancel=cancel,
                                 progress=lambda p, m: progress(main_share + p * (1 - main_share), m))
        return {
            "summary": f"Готово: {video.name}",
            "folder": str(out_dir),
            "video": str(video),
            "lines": lines,
        }

    return (tj.label if tj.text else title_hint), run


def _broll_dubs(job: Job, req: BrollRequest, main_dir: Path, title_hint: str, *, cancel: CancelToken, progress) -> list[str]:
    """Переводы озвучки футажного ролика: на каждый язык — свой ролик со своими футажами."""
    provider = get_provider(req.tts_provider)
    targets, warning = start_dub(req.tts_provider, req.tts_options, main_dir, language=source_language("", req.story_text))
    lines = [warning] if warning else []
    if not targets:
        return lines
    for track in provider.iter_dubs(req.tts_options, main_dir, cancel=cancel, on_progress=progress):
        lang = track.language.split("-")[0].lower()
        works = dict(job.state.get("dub_work") or {})
        work = Path(works[lang]) if works.get(lang) and Path(works[lang]).is_dir() else None
        if work is None:
            work = run_output_dir(title_hint + lang_suffix(lang))
            works[lang] = str(work)
            job.save_state(dub_work=works)
        text = track.text.read_text(encoding="utf-8").strip() if track.text is not None else ""
        res = run_broll(
            replace(
                req,
                story_text=text,
                source_audio=track.audio,
                title_hint=title_hint + lang_suffix(lang),
                language=lang,
                work_dir=work,
                on_audio_ready=None,
            ),
            cancel=cancel,
            on_progress=lambda p, m, L=lang.upper(): progress(0.0, f"{L}: {m}"),
        )
        provider.mark_dub_done(main_dir, track.language, str(res.output_dir))
        lines.append(f"Ролик {lang.upper()}: {res.output_dir}")
    if not pending_dubs(main_dir):
        # служебное перевода в папке ролика больше не нужно
        shutil.rmtree(main_dir / DUB_DIR, ignore_errors=True)
        for name in (DUB_STATE, DUB_SOURCE):
            (main_dir / name).unlink(missing_ok=True)
    return lines


def _slides_pipeline(cfg: dict, params: dict, state: dict, *, voice_only: bool) -> tuple[str, Any]:
    run_dir = _state_dir(state, "run_dir")
    tj = _text_job(params)
    has_voice = run_dir is not None and (run_dir / "voice.mp3").is_file()
    audio = None if has_voice else _audio(params.get("audio"))
    if not tj.text and audio is None and not has_voice:
        raise ValueError("Нужен текст сценария или готовое аудио")
    if not has_voice:
        _need_tts(cfg, audio)
    key, base, model = _llm(cfg)
    req = FullRunRequest(
        tts=_tts_req(cfg, tj, audio),
        llm_api_key=key,
        llm_base_url=base,
        llm_model=model,
        stock_keys=stock_keys_from_config(cfg),
        speed_copies=_copies(cfg),
        **_media_mode(cfg),
        ai=_ai(cfg),
        stop_after_chunks=voice_only,
        **_render_flags(cfg),
    )

    def run(job: Job, progress, cancel: CancelToken) -> dict[str, Any]:
        rd = _state_dir(job.state, "run_dir")
        if rd is None:
            rd = new_run_dir(title_hint=req.tts.title_hint)
            job.save_state(run_dir=str(rd), owned_dir=str(rd))
        req.resume_dir = rd
        res = run_full_pipeline(req, cancel=cancel, on_progress=progress)
        if voice_only:
            w = res.whisper
            return {
                "summary": f"Озвучка и тема готовы: {w.word_count} слов, {w.duration:.0f} с",
                "folder": str(rd),
                "run": rd.name,
                "lines": [f"Запуск: {rd.name}", f"Слов: {w.word_count}", f"Длительность: {w.duration:.1f} с"],
            }
        assert res.render is not None and res.media is not None
        out = _slides_result(res.render, [f"Кадров: {res.media.selected}", f"Запуск: {rd.name}", *res.dub_lines])
        out["run"] = rd.name
        return out

    title = tj.label if tj.text else _title(tj, audio)
    return (f"1. Озвучка + тема: {title}" if voice_only else title), run


def _slides_full(cfg: dict, params: dict, state: dict) -> tuple[str, Any]:
    return _slides_pipeline(cfg, params, state, voice_only=False)


def _slides_voice(cfg: dict, params: dict, state: dict) -> tuple[str, Any]:
    return _slides_pipeline(cfg, params, state, voice_only=True)


def _slides_resume(cfg: dict, params: dict, state: dict) -> tuple[str, Any]:
    run_dir = _run_dir(params.get("run", ""))
    if detect_stage(run_dir) == "render_done":
        raise ValueError("Этот запуск уже собран — final.mp4 есть")
    script = run_dir / "script.txt"
    text = script.read_text(encoding="utf-8").strip() if script.is_file() else ""
    has_voice = (run_dir / "voice.mp3").is_file()
    if not text and not has_voice:
        raise ValueError("В запуске нет ни текста, ни озвучки — начни заново")
    if not has_voice:
        _need_tts(cfg, None)
    key, base, model = _llm(cfg)
    req = FullRunRequest(
        tts=_tts_req(cfg, TextJob(text=text), None, run_dir=run_dir),
        llm_api_key=key,
        llm_base_url=base,
        llm_model=model,
        stock_keys=stock_keys_from_config(cfg),
        speed_copies=_copies(cfg),
        **_media_mode(cfg),
        ai=_ai(cfg),
        resume_dir=run_dir,
        **_render_flags(cfg),
    )

    def run(job: Job, progress, cancel: CancelToken) -> dict[str, Any]:
        describe_resume(run_dir)
        res = run_full_pipeline(req, cancel=cancel, on_progress=progress)
        assert res.render is not None
        out = _slides_result(res.render, [f"Запуск: {run_dir.name}", *res.dub_lines])
        out["run"] = run_dir.name
        return out

    return f"Продолжить: {run_dir.name}", run


def _slides_media(cfg: dict, params: dict, state: dict) -> tuple[str, Any]:
    run_dir = _run_dir(params.get("run", ""))
    if not (run_dir / "chunks.json").is_file():
        raise ValueError("В запуске нет темы (chunks.json) — сначала шаг 1")
    key, base, model = _llm(cfg)
    req = MediaRequest(
        run_dir=run_dir,
        keys=stock_keys_from_config(cfg),
        copy_preview_to_desktop=True,
        llm_api_key=key,
        llm_base_url=base,
        llm_model=model,
        **_media_mode(cfg),
        ai=_ai(cfg),
    )

    def run(job: Job, progress, cancel: CancelToken) -> dict[str, Any]:
        res = run_media_stage(req, cancel=cancel, on_progress=progress)
        from engine.slides.ai_fill import report_lines

        ai_lines, warnings = report_lines(run_dir)
        return {
            "warnings": warnings,
            "summary": f"Кадры: выбрано {res.selected}, пропущено {res.skipped}" + (" · AI-кадры не все" if warnings else ""),
            "folder": str(run_dir),
            "run": run_dir.name,
            "lines": [f"Выбрано: {res.selected}", f"Пропущено: {res.skipped}", f"Слабых: {res.weak}"],
        }

    return f"2. Кадры: {run_dir.name}", run


def _slides_render(cfg: dict, params: dict, state: dict) -> tuple[str, Any]:
    run_dir = _run_dir(params.get("run", ""))
    if not (run_dir / "timeline.json").is_file():
        raise ValueError("В запуске нет кадров (timeline.json) — сначала шаг 2")
    req = RenderRequest(run_dir=run_dir, copy_to_desktop=True, speed_copies=_copies(cfg), **_render_flags(cfg))

    def run(job: Job, progress, cancel: CancelToken) -> dict[str, Any]:
        res = run_render_stage(req, cancel=cancel, on_progress=progress)
        out = _slides_result(res, [f"Сегментов: {res.segments}"])
        out["run"] = run_dir.name
        return out

    return f"3. Сборка: {run_dir.name}", run


def _copies_job(cfg: dict, params: dict, state: dict) -> tuple[str, Any]:
    src = Path(str(params.get("video") or "").strip()).expanduser()
    if not str(params.get("video") or "").strip() or not src.is_file():
        raise ValueError("Выбери готовый ролик")
    raw_count = params.get("count")
    try:
        count = 1 if raw_count in (None, "") else int(raw_count)
    except (TypeError, ValueError):
        raise ValueError("Число копий должно быть целым") from None
    if not 1 <= count <= MAX_COPIES:
        raise ValueError(f"Число копий от 1 до {MAX_COPIES}")

    def run(job: Job, progress, cancel: CancelToken) -> dict[str, Any]:
        written = write_speed_copies(src, src.parent, count, base_name=src.name, cancel=cancel, on_progress=progress)
        return {
            "summary": f"Копий: {len(written)}",
            "folder": str(src.parent),
            "lines": [p.name for p in written],
        }

    return f"Копии: {src.name}", run


_BUILDERS = {
    "broll": _broll,
    "slides_full": _slides_full,
    "slides_resume": _slides_resume,
    "slides_voice": _slides_voice,
    "slides_media": _slides_media,
    "slides_render": _slides_render,
    "copies": _copies_job,
}
