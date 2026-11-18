"""Сборка задач: форма + сохранённые настройки → вызов пайплайна.

Проверки входа делаются при постановке в очередь (ValueError → 400),
сам пайплайн запускается позже в рабочем потоке.
"""

from __future__ import annotations

import json
import re
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
from engine.slides.music import track_path, usable
from engine.slides.publish import lang_suffix
from engine.slides.full_pipeline import FullRunRequest, run_full_pipeline
from engine.slides.llm_client import DEFAULT_BASE_URL, DEFAULT_MODEL
from engine.slides.media_pipeline import MediaRequest, run_media_stage, stock_keys_from_config
from engine.slides.paths import RUNS_DIR, new_run_dir
from engine.slides.render_pipeline import RenderRequest, run_render_stage
from engine.slides.resume import describe_resume, detect_stage
from engine.slides.speed_copies import BASE_NAME as BASE_VIDEO, write_speed_copies
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


def _rewrite_llm(cfg: dict) -> tuple[str, str, str] | None:
    """Встроенная нейросеть для рерайта и его проверки: своя модель из «Переписывания», пусто — общая (LLM)."""
    llm = _llm_opt(cfg)
    own = _s(cfg, "rewrite_llm_model")
    return (llm[0], llm[1], own) if llm and own else llm


def _llm_opt(cfg: dict) -> tuple[str, str, str] | None:
    """LLM для необязательных шагов (главы YouTube): нет ключа — None, ролик всё равно собирается."""
    try:
        return _llm(cfg)
    except ValueError:
        return None


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
    text_file = str(params.get("text_file") or "").strip()
    if not text and text_file:
        # текст рерайта: читаем при постановке в очередь (после правки в редакторе — уже исправленный)
        path = Path(text_file).expanduser()
        if not path.is_file():
            raise ValueError(f"Нет файла с текстом: {path}")
        text, source = path.read_text(encoding="utf-8").strip(), str(path)
    return TextJob(text=text, source=source)


def _attach(folder: str | Path | None, params: dict) -> None:
    """В папку готового ролика — сопроводительный пакет рерайта (названия, описание, выжимка)."""
    raw = str(params.get("package_file") or "").strip()
    if not folder or not raw:
        return
    src, dest = Path(raw).expanduser(), Path(folder)
    if src.is_file() and dest.is_dir():
        shutil.copy2(src, dest / "описание.txt")


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
    from engine.slides.ai_gen import AI_BUDGET_DEFAULT_CENTS, AiConfig

    if not cfg.get("ai_gen_enabled"):
        return None
    from engine.slides.source_mix import clean_mix

    try:
        budget = round(float(cfg["ai_budget_usd"]) * 100) if cfg.get("ai_budget_usd") is not None else AI_BUDGET_DEFAULT_CENTS
    except (TypeError, ValueError):
        budget = AI_BUDGET_DEFAULT_CENTS
    # потолок 50 центов AiConfig держит сам (и для шаблонов со старым $1)
    mix = clean_mix(cfg.get("source_mix"))
    share = mix["ai"] / 100 if mix else None
    ai = AiConfig(api_key=_s(cfg, "royaltechno_api_key"), image_model=_s(cfg, "ai_image_model"), video_model=_s(cfg, "ai_video_model"),
                  budget_cents=budget)
    if share is not None:
        # своя доля AI: цель — она, ради кусков с цифрами — на 3 пункта больше
        ai.share, ai.share_max = share, share + 0.03
    if not ai.api_key:
        raise ValueError("Включены AI-кадры, но нет ключа RoyalTechno (Настройки → AI-кадры)")
    if not ai.active:
        raise ValueError("Включены AI-кадры, но не выбрана ни модель фото, ни модель видео")
    return ai


def _media_mode(cfg: dict, params: dict | None = None) -> dict[str, Any]:
    """Режим подбора и доли источников. Режим запоминается в задаче при постановке в очередь:
    смена настройки потом не меняет уже поставленные ролики (и «Продолжить» идёт тем же режимом)."""
    from engine.slides.source_mix import clean_mix

    mode = str((params or {}).get("media_mode") or "") or _s(cfg, "media_mode") or "theme"
    if mode == "stars":
        mode = "theme"  # раздела «Звёзды» больше нет: ролик про людей распознаётся в любом режиме
    if params is not None:
        params.setdefault("media_mode", mode)
    return {
        "strict_sync": mode in ("strict_3min", "strict_full"),
        "strict_full": mode == "strict_full",
        # свои доли источников (None — по умолчанию, по теме ролика)
        "source_mix": clean_mix(cfg.get("source_mix")),
    }


def _render_flags(cfg: dict) -> dict[str, Any]:
    sub = _subscribe(cfg)
    track_id, note = usable(_s(cfg, "music_track"))
    if note:
        log(f"Музыка: {note}")
    return {
        "subscribe": sub is not None,
        "subscribe_path": sub,
        "subtitles": bool(cfg.get("subtitles")),
        "intro_dir": _opt_dir(_s(cfg, "slides_intro_dir")),
        "outro_dir": _opt_dir(_s(cfg, "slides_outro_dir")),
        "music": track_path(track_id),
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


# Снимок настроек задачи: делается при постановке в очередь, и задача идёт по нему до конца — ролик после
# рерайта, переводы, «Продолжить», перезапуск приложения. Поменял голос/копии/переводы для новой задачи —
# уже запущенные это не задевает. Живыми остаются только ключи и адреса сервисов (исправленный ключ — сразу).
SETTINGS_KEY = "_settings"
_LIVE_KEYS = frozenset({
    "llm_api_key", "llm_base_url", "llm_model", "pexels_api_keys", "pixabay_api_keys", "unsplash_api_keys",
    "youtube_api_keys", "royaltechno_api_key", "claude_model", "codex_model", "rewrite_llm_model",
    "adspower_url", "adspower_api_key",
    "stars_notes",  # уточнения о героях: вписал после вопроса программы — «Продолжить» их учтёт
})
_TTS_SECRET = "api_key"


def snapshot_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    """Настройки для снимка задачи — без ключей (они не замораживаются и не пишутся в очередь)."""
    import copy

    snap = {k: copy.deepcopy(v) for k, v in cfg.items() if k not in _LIVE_KEYS}
    snap["tts"] = {pid: {k: v for k, v in (opts or {}).items() if k != _TTS_SECRET}
                   for pid, opts in (cfg.get("tts") or {}).items() if isinstance(opts, dict)}
    return snap


def effective_settings(params: dict[str, Any]) -> dict[str, Any]:
    """Настройки задачи: её снимок поверх текущих (ключи — текущие). Без снимка — текущие."""
    live = load_config()
    snap = params.get(SETTINGS_KEY)
    if not isinstance(snap, dict):
        return live
    cfg = {**live, **{k: v for k, v in snap.items() if k not in _LIVE_KEYS}}
    tts = dict(live.get("tts") or {})
    for pid, opts in (snap.get("tts") or {}).items():
        secret = ((live.get("tts") or {}).get(pid) or {}).get(_TTS_SECRET)
        tts[pid] = {**(opts or {}), **({_TTS_SECRET: secret} if secret is not None else {})}
    cfg["tts"] = tts
    return cfg


def build(kind: str, params: dict[str, Any], state: dict[str, Any]) -> tuple[str, Any]:
    builder = _BUILDERS.get(kind)
    if builder is None:
        raise ValueError(f"Неизвестный тип задачи: {kind}")
    cfg = effective_settings(params)
    title, run = builder(cfg, params, state)
    dirs = {s: _s(cfg, f"{s}_output_dir") for s in ("broll", "slides")}

    def run_with_settings(job, progress, cancel):
        from engine.slides.paths import job_output_dirs

        # папка готовых роликов — тоже из снимка задачи (код сборки читает её сам)
        token = job_output_dirs.set(dirs)
        try:
            return run(job, progress, cancel)
        finally:
            job_output_dirs.reset(token)

    return title, run_with_settings


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


def _with_texts(cfg: dict, params: dict, section: str, text: str, *, audio: bool = False) -> tuple[str, list[str]]:
    """Тексты из полей раздела (перед началом / ~30-я секунда / в конце) — в текст для озвучки.
    Своя готовая озвучка — вставлять некуда: только пометка в журнале."""
    from engine.rewrite.cta import place_texts, section_texts
    from engine.rewrite.run import REWRITES_DIR

    texts = section_texts(cfg, section)
    if not texts.any():
        return text, []
    if audio or not text.strip():
        note = "тексты из полей не вставлены: ролик по готовой озвучке"
        log(note)
        return text, [note]
    return place_texts(text, texts, language=str(params.get("language") or "") or None, llm=_llm_opt(cfg),
                       cache_dir=REWRITES_DIR)


def _publish_copies(publish: dict) -> int:
    from engine.publish.run import copies_for

    return copies_for(publish["channels"])


def _queue_publish(job: Job, publish: dict, params: dict, versions: list[tuple[str, Path, Path]], copies: int) -> list[str]:
    """Готовый ролик (все его языковые версии: основной + переводы) → одна задача «Выкладка» сразу за ним
    в очереди (своя полоса: монтаж следующего не ждёт). versions — [(язык, папка, видео)], основной первым.
    Каждый канал получит версию своего языка (engine/publish/run.route)."""
    from engine.slides.speed_copies import copy_filename, copy_percents

    if job is not None and job.state.get("publish_job"):
        return ["Выкладка уже поставлена в очередь"]
    vers = []
    for lang, folder, video in versions:
        files = [(str(video), 100)] + [(str(folder / copy_filename(p, video.name)), p) for p in copy_percents(copies)]
        files = [(f, p) for f, p in files if Path(f).is_file()]
        if files:
            vers.append({"language": lang, "folder": str(folder), "files": files})
    if not vers:
        return ["Выкладка не поставлена: нет готовых файлов ролика"]
    main_folder = Path(vers[0]["folder"])
    pub = {**publish, "folder": vers[0]["folder"], "files": vers[0]["files"], "language": vers[0]["language"],
           "versions": vers, "plan_id": str(params.get("publish_plan") or ""),
           "video_name": Path(vers[0]["files"][0][0]).stem, SETTINGS_KEY: params.get(SETTINGS_KEY)}
    if publish.get("section") == "slides":
        # оригинал рерайта (его название и обложка — образец для наших)
        pub.update(source_url=str(params.get("source_url") or ""), run_dir=str(params.get("_run_dir") or ""),
                   video_name=main_folder.name)
    elif publish.get("materials") == "competitor":
        pub.update(source_url=str(params.get("source_url") or ""))  # конкурент — оригинал рерайта
    from server.jobs import get_manager

    mgr = get_manager()
    try:
        pjob = mgr.create("publish", pub)
    except ValueError as exc:
        return [f"Выкладка не поставлена: {exc}"]
    if job is not None:
        job.save_state(publish_job=pjob.id)
    mgr.add(pjob, after=job.id if job is not None else None)
    return [f"Выкладка на {len(publish['channels'])} канал(а/ов) — в очереди", *routing_preview(publish["channels"], vers)]


def _lang(value) -> str:
    v = str(value or "").strip().lower().split("-")[0]
    return "" if v in ("auto", "none") else v


def routing_preview(channels: list[str], vers: list[dict]) -> list[str]:
    """Какой язык на какой канал — сразу в итог задачи (и предупреждения: язык канала не указан / версии нет)."""
    from engine.publish.registry import normalize, resolve

    try:
        targets = resolve(channels)
    except ValueError:
        return []
    langs = [normalize(v["language"]) for v in vers]
    if len(vers) == 1:
        lang = langs[0]
        bad = [t for t in targets if normalize(t.channel.language) and lang and normalize(t.channel.language) != lang]
        return [f"⚠ {t.label()}: язык канала {normalize(t.channel.language).upper()}, а ролик на {lang.upper()} — "
                "на этот канал заливать не буду, выкладка спросит тебя" for t in bad]
    out = []
    for lang in langs:
        names = [t.channel.handle or t.channel.name for t in targets if normalize(t.channel.language) == lang]
        out.append(f"Ролик {lang.upper() or '?'} → " + (", ".join(names) if names else "нет каналов на этом языке — "
                                                       "останется на компьютере"))
    for t in targets:
        cl = normalize(t.channel.language)
        if not cl:
            out.append(f"⚠ {t.label()}: у канала не указан язык — выкладка спросит тебя (укажи язык канала)")
        elif cl not in langs:
            out.append(f"⚠ {t.label()}: ролика на {cl.upper()} нет — канал пропущен")
    return out


def _publish_job(cfg: dict, params: dict, state: dict) -> tuple[str, Any]:
    from engine.publish.run import PublishRequest, run_publish

    folder = Path(str(params.get("folder") or ""))
    files = [(str(f), int(p)) for f, p in params.get("files") or []]
    if not files:
        raise ValueError("Выкладка: нет файлов ролика")
    from engine.publish.run import Version

    versions = [Version(str(v.get("language") or ""), Path(str(v.get("folder") or "")),
                        [(str(f), int(p)) for f, p in v.get("files") or []]) for v in params.get("versions") or []]
    req = PublishRequest(
        folder=folder, files=files, channels=list(params.get("channels") or []), versions=versions,
        language=str(params.get("language") or ""),
        coauthors=list(params.get("coauthors") or []), thumbs_dir=str(params.get("thumbs_dir") or ""),
        titles_file=str(params.get("titles_file") or ""), descs_file=str(params.get("descs_file") or ""),
        plan_id=str(params.get("plan_id") or ""), visible=bool(params.get("visible", True)),
        keep_files=bool(params.get("keep_files", True)), comment=str(params.get("comment") or ""),
        run_dir=str(params.get("run_dir") or ""),
        llm=_llm_opt(cfg),
        adspower=(_s(cfg, "adspower_api_key"), _s(cfg, "adspower_url")),
    )
    if params.get("section") == "slides":
        req.prepare = _slides_materials(cfg, params)
    else:
        req.prepare = _broll_materials(cfg, params, req)

    def run(job: Job, progress, cancel: CancelToken) -> dict[str, Any]:
        return run_publish(req, job.state, job.save_state, cancel=cancel, progress=progress)

    return f"Выкладка: {params.get('video_name') or folder.name}", run


def _slides_materials(cfg: dict, params: dict):
    """Название, обложка и описания под ролик слайдов — готовятся в начале выкладки (один раз на версию)."""
    from engine.publish.slides_materials import prepare

    make_cover = bool(params.get("make_cover", True))
    # без ключа генератора задача не падает: обложка не сделается — ролик выйдет без неё, с предупреждением
    cover = _cover_settings(cfg, need_generator=False)
    run_dir = Path(str(params.get("run_dir") or ""))

    def run(version, base, *, cancel=None, progress=None) -> dict[str, Any]:
        folder = version.folder
        # текст версии: у перевода — свой сценарий в его папке; у основной — из запуска
        paths = (folder / "сценарий.txt",) if base is not None else (run_dir / "script.txt", folder / "сценарий.txt")
        script = next((p for p in paths if p.is_file()), None)
        story = script.read_text(encoding="utf-8") if script else ""
        return prepare(folder=folder, files=version.files, source_url=str(params.get("source_url") or ""),
                       language=version.language, story=story, cover=cover,
                       desc_cta=str(params.get("desc_cta") or ""), make_cover=make_cover, base=base,
                       cancel=cancel, progress=progress)

    return run


def _broll_materials(cfg: dict, params: dict, req):
    """«Рассказы»: обложка, название, описание — из твоих папок (один раз на ролик); для версии на другом
    языке (и если материалы на другом языке, чем ролик) — перевод: название, описание, надписи обложки.
    «Переделать конкурента» — обложка и название генерируются по ролику-оригиналу, описание — из файла."""
    from engine.publish import materials
    from engine.publish.localize import localize

    if params.get("materials") == "competitor":
        return _broll_competitor(cfg, params, req)

    llm = _llm_opt(cfg)
    try:
        cover = _cover_settings(cfg, need_generator=False) if llm else None
    except ValueError:
        cover = None

    def run(version, base, *, cancel=None, progress=None) -> dict[str, Any]:
        mat = dict(base) if base else materials.pick(req.thumbs_dir, req.titles_file, req.descs_file).as_dict()
        if not llm or not version.language:
            return mat
        return localize(mat, version.language, llm=llm, cs=cover, work=version.folder / "обложки",
                        force=base is not None)

    return run


def _story_text(folder: Path) -> str:
    """Текст рассказа из папки ролика (файл «<тема>.txt», который кладёт сборка) — для обложки/названия
    без оригинала. Главы, описание и материалы рерайта — не текст рассказа."""
    skip = ("главы", "описание", "youtube", "разбор", "оригинал")
    texts = [p for p in folder.glob("*.txt") if not p.name.lower().startswith(skip)]
    best = max(texts, key=lambda p: p.stat().st_size, default=None)
    return best.read_text(encoding="utf-8", errors="replace") if best else ""


def _broll_competitor(cfg: dict, params: dict, req):
    """«Переделать конкурента»: название — переписанное название оригинала (кликбейт, по смыслу), обложка —
    переделанная обложка оригинала (как в «Стоковых слайдах»); описание — из твоего файла описаний.
    Версия на другом языке — название заново на её языке, надписи обложки и описание — переводом."""
    from engine.publish import materials
    from engine.publish.localize import translate_text
    from engine.publish.slides_materials import COVER_DIR, title_and_cover

    cover = _cover_settings(cfg, need_generator=False)  # нет ключа генератора — ролик выйдет без обложки

    def run(version, base, *, cancel=None, progress=None) -> dict[str, Any]:
        folder = version.folder
        title, thumbnail, cover_text, lines = title_and_cover(
            folder=folder, source_url=str(params.get("source_url") or ""), language=version.language,
            story=_story_text(folder), cover=cover, base=base, cancel=cancel, progress=progress)
        if base is None:
            desc = materials.pick_description(req.descs_file)
        else:
            desc = str(base.get("description") or "")
            code = (version.language or "").split("-")[0].lower()
            if desc and code:
                try:
                    desc = translate_text(cover.llm, "", desc, code)[1]
                except Exception as exc:
                    lines.append(f"описание на {code.upper()} перевести не вышло ({str(exc)[:100]}) — оставлено как есть")
        if thumbnail:
            lines.append(f"Обложка и варианты — в папке ролика, «{COVER_DIR}»")
        from engine.publish.slides_materials import warnings_of

        return {"title": title, "thumbnail": thumbnail, "cover_text": cover_text, "description": desc,
                "descriptions": {}, "lines": lines, "warnings": warnings_of(lines)}

    return run


def _comments_job(cfg: dict, params: dict, state: dict) -> tuple[str, Any]:
    """«Ответить на комментарии» по выбранным каналам (сердечко + ответ каждому без ответа)."""
    from engine.publish.registry import resolve
    from engine.publish.run import run_comments

    channels = list(params.get("channels") or [])
    targets = resolve(channels)
    if not targets:
        raise ValueError("Выбери хотя бы один канал")
    if not _s(cfg, "adspower_api_key"):
        raise ValueError("Нет ключа AdsPower (Настройки → AdsPower)")
    adspower, llm = (_s(cfg, "adspower_api_key"), _s(cfg, "adspower_url")), _llm_opt(cfg)

    def run(job: Job, progress, cancel: CancelToken) -> dict[str, Any]:
        return run_comments(channels, adspower, llm, cancel=cancel, progress=progress)

    names = ", ".join(t.channel.handle or t.channel.name for t in targets[:3]) + (" …" if len(targets) > 3 else "")
    return f"Комментарии: {names}", run


def _polls_job(cfg: dict, params: dict, state: dict) -> tuple[str, Any]:
    """«Голосования»: опросы в сообществе канала — первый сразу, дальше каждые step_days дней."""
    from engine.publish import polls as pl
    from engine.publish.registry import resolve
    from engine.publish.run import run_polls
    from engine.slides.stocks_base import parse_keys

    channel = str(params.get("channel") or "")
    targets = resolve([channel]) if channel else []
    if not targets:
        raise ValueError("Выбери канал")
    if not _s(cfg, "adspower_api_key"):
        raise ValueError("Нет ключа AdsPower (Настройки → AdsPower)")
    polls = [p.as_dict() for p in (pl.clean(x) for x in params.get("polls") or []) if p]
    count = _int(params.get("count"), len(polls) or 3, 1, pl.COUNT_MAX)
    try:
        step_days = float(params.get("step_days") or 2)
    except (TypeError, ValueError):
        step_days = 2.0
    step_days = max(0.25, min(float(pl.STEP_MAX_DAYS), step_days))
    adspower, llm = (_s(cfg, "adspower_api_key"), _s(cfg, "adspower_url")), _llm_opt(cfg)
    keys = parse_keys(cfg.get("youtube_api_keys") or cfg.get("youtube_api_key"))

    def run(job: Job, progress, cancel: CancelToken) -> dict[str, Any]:
        memo = job.state.setdefault("polls_job", {})  # опросы, старт расписания, что уже выложено — на повтор
        return run_polls(channel, polls, step_days, count, adspower, llm, keys, memo=memo, save=job.save_state,
                         cancel=cancel, progress=progress)

    t = targets[0]
    return f"Голосования: {t.channel.handle or t.channel.name} — {max(count, len(polls))} шт., каждые {step_days:g} дн.", run


def _niches_job(cfg: dict, params: dict, state: dict) -> tuple[str, Any]:
    """«Поиск ниш»: каналы-ориентиры по ключевым словам (YouTube API) → до 15 каналов на ручной разбор."""
    from engine.research import niches
    from engine.slides.stocks_base import parse_keys

    niche = str(params.get("niche") or "").strip()
    raw = params.get("keywords") or params.get("subniches") or []
    keywords = [str(s).strip() for s in raw if str(s).strip()] if isinstance(raw, list) else []
    if not keywords and niche:
        keywords = [niche]
    if not keywords:
        raise ValueError("Впиши ключевые слова или выбери нишу")
    keys = parse_keys(cfg.get("youtube_api_keys") or cfg.get("youtube_api_key"))
    if not keys:
        raise ValueError("Нужен ключ YouTube Data API (Настройки → Ключи → YouTube)")
    f = niches.Filters.of(params.get("filters") or {"language": params.get("language")})
    llm = _llm_opt(cfg)
    label = niche or keywords[0]

    def run(job: Job, progress, cancel: CancelToken) -> dict[str, Any]:
        res = niches.find(niche, keywords, f, keys, llm, cancel=cancel, progress=progress)
        st = res["stats"]
        lines = [f"{c['title']} — {c['subs']} подп., медиана {c['recent_median']} просм. ({c['url']})"
                 for c in res["channels"]]
        return {"summary": f"Ниша «{label}»: каналов {len(res['channels'])} (роликов {st['videos']}, "
                           f"каналов {st['channels']}, разобрано {st['checked']}, подошли {st['passed']})", "lines": lines}

    return f"Поиск ниш: {label}", run


def _footage_dir(cfg: dict) -> Path:
    """Папка футажей с видео — проверяем до рерайта и озвучки, а не после них.
    В снимке задачи папка пустая, а в текущих настройках уже исправлена — берём исправленную."""
    from engine.compositor.utils import list_videos_soft

    raw = _s(cfg, "broll_dir")
    folder = Path(raw).expanduser()
    if raw and list_videos_soft(folder):
        return folder
    live = _s(load_config(), "broll_dir")
    if live and live != raw and list_videos_soft(Path(live).expanduser()):
        log(f"Футажи: в «{folder}» нет видео — беру папку из текущих настроек: {live}")
        return Path(live).expanduser()
    if not raw:
        raise ValueError("Укажи папку футажей — без неё ролик не собрать")
    same = raw == _s(cfg, "broll_output_dir")
    raise ValueError(f"В папке футажей нет видео: {folder}"
                     + (" — она совпадает с папкой для готовых роликов; выбери папку, где лежат футажи" if same else ""))


def _coauthors(cfg: dict, k: str) -> list[str]:
    """Соавторы раздела: тумблер «Добавить соавторов» выключен — никого (выбор при этом сохраняется)."""
    chosen = list(cfg.get(f"{k}_coauthors") or [])
    on = cfg.get(f"{k}_coauthors_on")
    if not (bool(chosen) if on is None else bool(on)):
        return []
    targets = set(cfg.get(f"{k}_channels") or [])
    return [c for c in chosen if c not in targets]  # канал — цель выкладки: соавтором он не бывает


def _publish_opts(cfg: dict, section: str = "broll") -> dict | None:
    """Выкладка раздела на YouTube: выключена — None; включена — проверка сразу (до рерайта и озвучки).
    «Рассказы» берут обложки/названия/описания из папок (или переделывают конкурента — описание всё равно
    из файла); «Стоковые слайды» готовят всё под ролик сами."""
    if not cfg.get(f"{section}_publish"):
        return None
    from engine.publish.run import validate

    k = f"{section}_publish"
    opts = {
        "section": section,
        "channels": list(cfg.get(f"{k}_channels") or []),
        "coauthors": _coauthors(cfg, k),
        "thumbs_dir": _s(cfg, f"{k}_thumbs"),
        "titles_file": _s(cfg, f"{k}_titles"),
        "descs_file": _s(cfg, f"{k}_descs"),
        "visible": bool(cfg.get(f"{k}_visible", True)),
        "keep_files": bool(cfg.get(f"{k}_keep", True)),
        "comment": str(cfg.get(f"{k}_comment") or ""),
    }
    own = section == "slides"
    if own:
        opts["desc_cta"] = str(cfg.get("slides_publish_desc_cta") or "")
        opts["make_cover"] = bool(cfg.get("slides_publish_cover", True))
        _cover_settings(cfg, need_generator=False)  # нейросеть для названия — сразу; без генератора — без обложки
    competitor = section == "broll" and _s(cfg, "broll_publish_materials") == "competitor"
    if competitor:
        # «Рассказы» → «Переделать конкурента»: обложка и название генерируются, описание — из файла
        opts["materials"] = "competitor"
        _cover_settings(cfg, need_generator=False)  # без ключа генератора — ролики выйдут без обложки
    validate(opts["channels"], opts["coauthors"], opts["thumbs_dir"], opts["titles_file"], opts["descs_file"],
             own_materials=own or competitor)
    if competitor:
        from engine.publish import materials

        materials.check_descs(opts["descs_file"])
    if not _s(cfg, "adspower_api_key"):
        raise ValueError("Выкладка на YouTube: нет ключа AdsPower (Настройки → AdsPower)")
    return opts


def royal_need(cfg: dict, jobs: list) -> tuple[int, list[str]]:
    """Сколько RoyalTechno может понадобиться на эти задачи (центы, по прайсу) и на что: AI-кадры стоков
    (бюджет ролика) и обложки (варианты × цена модели; переводы надписей — по одной на язык)."""
    from engine.publish.cover import DEFAULT_MODEL, DEFAULT_VARIANTS
    from engine.slides.ai_gen import entry, price_of

    def section_of(j) -> str:
        if j.kind == "rewrite":
            return str(j.params.get("section") or "broll")
        return "slides" if j.kind == "slides_full" else "broll" if j.kind == "broll" else ""

    snap = lambda j: j.params.get(SETTINGS_KEY) or cfg  # noqa: E731 — настройки на момент постановки
    ai_cents = cover_cents = 0
    n_ai = n_cover = 0
    for j in jobs:
        c = snap(j)
        sec = section_of(j)
        if not sec:
            continue
        if sec == "slides" and c.get("ai_gen_enabled"):
            try:
                ai_cents += min(50, round(float(c.get("ai_budget_usd") or 0.4) * 100))
            except (TypeError, ValueError):
                ai_cents += 40
            n_ai += 1
        covers = (sec == "slides" and c.get("slides_publish") and c.get("slides_publish_cover", True)) or \
                 (sec == "broll" and c.get("broll_publish") and str(c.get("broll_publish_materials") or "") == "competitor")
        if covers:
            e = entry(str(c.get("cover_model") or DEFAULT_MODEL))
            price = price_of(e) if e else 4
            variants = _int(c.get("cover_variants"), DEFAULT_VARIANTS, 1, 6)
            cover_cents += price * variants
            n_cover += 1
    parts = []
    if n_ai:
        parts.append(f"AI-кадры до ${ai_cents / 100:.2f} (роликов: {n_ai})")
    if n_cover:
        parts.append(f"обложки ~${cover_cents / 100:.2f} (роликов: {n_cover})")
    return ai_cents + cover_cents, parts


def royal_warning(cfg: dict, jobs: list) -> str:
    """Предупреждение при запуске: денег на RoyalTechno может не хватить (или баланс не узнать).
    Не мешает запуску: без денег AI-кадры заменятся живыми, ролик выложится без обложки (с пометкой)."""
    need, parts = royal_need(cfg, jobs)
    key = _s(cfg, "royaltechno_api_key")
    if not need:
        return ""
    if not key:
        return ("Нет ключа RoyalTechno (Настройки → AI-кадры): обложки не сгенерируются — ролики выложатся "
                "без обложки, программа предупредит и даст ссылку на оригинал.")
    from engine.slides.ai_gen import balance_cents

    bal = balance_cents(key)
    fallback = ("Без денег AI-кадры заменятся живыми кадрами, а ролик выложится без обложки — программа "
                "предупредит и даст ссылку на оригинал.")
    if bal is None:
        return f"Не смог проверить баланс RoyalTechno (нет связи?). На эти ролики нужно: {', '.join(parts)}. {fallback}"
    if bal < need:
        return (f"На балансе RoyalTechno ${bal / 100:.2f}, а на эти ролики может понадобиться ~${need / 100:.2f} "
                f"({', '.join(parts)}). Пополни баланс. {fallback}")
    return ""


def _int(value, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _cover_settings(cfg: dict, *, need_generator: bool = True):
    """Обложки по образцу: генератор RoyalTechno + нейросеть с «глазами». Нет ключей — сразу понятная ошибка.
    need_generator=False — обложку не генерируем (нужна только нейросеть для названия)."""
    from engine.publish.cover import DEFAULT_MODEL, DEFAULT_VARIANTS, CoverSettings

    if need_generator and not _s(cfg, "royaltechno_api_key"):
        raise ValueError("Для обложек нужен ключ RoyalTechno (Настройки → AI-кадры)")
    try:
        variants = int(cfg.get("cover_variants") or DEFAULT_VARIANTS)
    except (TypeError, ValueError):
        variants = DEFAULT_VARIANTS
    from engine.publish.cover_prompts import active

    return CoverSettings(api_key=_s(cfg, "royaltechno_api_key"), model=_s(cfg, "cover_model") or DEFAULT_MODEL,
                         variants=variants, llm=_llm(cfg), prompts=active(cfg),
                         title_variants=_int(cfg.get("slides_publish_title_variants"), 3, 1, 8))


def _broll(cfg: dict, params: dict, state: dict) -> tuple[str, Any]:
    work = _state_dir(state, "work_dir")
    tj = _text_job(params)
    audio_done = work is not None and "audio" in _broll_stages(work)
    audio = None if audio_done else _audio(params.get("audio"))
    if not tj.text and audio is None and not audio_done:
        raise ValueError("Нужен текст рассказа или готовое аудио")
    if not audio_done:
        _need_tts(cfg, audio)
    broll_dir = _footage_dir(cfg)
    publish = _publish_opts(cfg)
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
        # выкладка включена — копий ровно по числу каналов (настройка копий в этом случае не действует)
        speed_copies=_publish_copies(publish) if publish else _copies(cfg),
        llm=_llm_opt(cfg),
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
    texts_lines: list[str] = []

    def run(job: Job, progress, cancel: CancelToken) -> dict[str, Any]:
        nonlocal texts_lines
        main_share = 0.75 if dub_wanted else 1.0
        work_dir = _state_dir(job.state, "work_dir")
        if job.state.get("main_done") and work_dir is not None:
            out_dir, video = work_dir, Path(str(job.state.get("main_video") or ""))
        else:
            if work_dir is None:
                work_dir = run_output_dir(req.title_hint)
                job.save_state(work_dir=str(work_dir), owned_dir=str(work_dir), title_hint=title_hint)
            req.work_dir = work_dir
            if not audio_done:
                req.story_text, texts_lines = _with_texts(cfg, params, "broll", tj.text, audio=audio is not None)
            res = run_broll(req, cancel=cancel, on_progress=lambda p, m: progress(p * main_share, m))
            out_dir, video = res.output_dir, res.video
            # папка переименована в финальное имя — сбросом её больше не трогаем
            job.save_state(work_dir=str(out_dir), owned_dir=None, main_done=True, main_video=str(video))
        lines = [f"Папка: {out_dir}", f"Ролик: {video.name}", *[f"Тексты: {n}" for n in texts_lines]]
        if dub_wanted:
            lines += _broll_dubs(job, req, out_dir, title_hint, cancel=cancel,
                                 progress=lambda p, m: progress(main_share + p * (1 - main_share), m))
        _attach(out_dir, params)
        if publish:
            main_lang = _lang(params.get("language")) or _lang(dub_language)
            versions = [(main_lang, out_dir, video)] + [
                (lang, Path(d), Path(v)) for lang, (d, v) in (job.state.get("dub_outputs") or {}).items()
                if Path(v).is_file()]
            lines += _queue_publish(job, publish, params, versions, req.speed_copies)
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
                chapters_base=req.story_text,
            ),
            cancel=cancel,
            on_progress=lambda p, m, L=lang.upper(): progress(0.0, f"{L}: {m}"),
        )
        provider.mark_dub_done(main_dir, track.language, str(res.output_dir))
        outs = dict(job.state.get("dub_outputs") or {})
        outs[lang] = [str(res.output_dir), str(res.video)]
        job.save_state(dub_outputs=outs)  # для выкладки: версия на этом языке
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
    publish = None if voice_only else _publish_opts(cfg, "slides")
    req = FullRunRequest(
        tts=_tts_req(cfg, tj, audio),
        llm_api_key=key,
        llm_base_url=base,
        llm_model=model,
        stock_keys=stock_keys_from_config(cfg),
        # выкладка включена — копий скорости столько, сколько каналов (как в «Футажах»)
        speed_copies=_publish_copies(publish) if publish else _copies(cfg),
        **_media_mode(cfg, params),
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
        # тексты из полей — один раз в исходный текст (повтор задачи даёт тот же результат: перевод запомнен)
        text, texts_lines = _with_texts(cfg, params, "slides", tj.text, audio=audio is not None or has_voice)
        req.tts = replace(req.tts, text=text) if text != req.tts.text else req.tts
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
        extra = [f"Кадров: {res.media.selected}", f"Запуск: {rd.name}", *[f"Тексты: {n}" for n in texts_lines],
                 *res.dub_lines]
        if publish:
            folder = res.render.desktop_video
            video = folder / BASE_VIDEO if folder is not None else None
            if video is None or not video.is_file():
                extra.append("Выкладка не поставлена: нет готовой папки ролика на компьютере")
            else:
                from engine.core.langdetect import detect_language
                from engine.tts.voicegen_dub import load_state

                script = rd / "script.txt"
                main_lang = _lang(params.get("language")) or _lang(
                    detect_language(script.read_text(encoding="utf-8")) if script.is_file() else "")
                versions = [(main_lang, folder, video)] + [
                    (_lang(lang), Path(d), Path(d) / BASE_VIDEO)
                    for lang, d in (load_state(rd).get("localized") or {}).items()
                    if d and (Path(d) / BASE_VIDEO).is_file()]
                extra += _queue_publish(job, publish, {**params, "_run_dir": str(rd)}, versions, req.speed_copies)
        out = _slides_result(res.render, extra)
        out["run"] = rd.name
        _attach(out.get("folder"), params)
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
        **_media_mode(cfg, params),
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
        **_media_mode(cfg, params),
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
    req = RenderRequest(run_dir=run_dir, copy_to_desktop=True, speed_copies=_copies(cfg), llm=_llm_opt(cfg), **_render_flags(cfg))

    def run(job: Job, progress, cancel: CancelToken) -> dict[str, Any]:
        res = run_render_stage(req, cancel=cancel, on_progress=progress)
        from engine.slides.resume import drop_stock

        drop_stock(run_dir)  # ролик собран — скачанные кадры больше не нужны
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


def _thumbnail_job(cfg: dict, params: dict, state: dict) -> tuple[str, Any]:
    from engine.slides.paths import DESKTOP
    from engine.slides.thumbnail import build_thumbnail, parse_names, thumbnail_path

    names = parse_names(str(params.get("names") or ""))
    if not names:
        raise ValueError("Впиши имена людей для превью")
    color = bool(params.get("color", True))

    def run(job: Job, progress, cancel: CancelToken) -> dict[str, Any]:
        path, used, missing = build_thumbnail(
            names, keys=stock_keys_from_config(cfg), dest=thumbnail_path(DESKTOP), color=color,
            llm=_llm_opt(cfg), cancel=cancel, progress=progress,
        )
        return {
            "summary": f"Превью: {len(used)} фото, {'цветные' if color else 'чёрно-белые'}",
            "folder": str(path),
            "image": str(path),
            "lines": [f"На превью: {', '.join(used)}"],
            "warnings": [f"Нет на превью: {m}" for m in missing],
        }

    title = ", ".join(names[:3]) + (f" и ещё {len(names) - 3}" if len(names) > 3 else "")
    return f"Превью: {title}", run


def _cover_job(cfg: dict, params: dict, state: dict) -> tuple[str, Any]:
    """Раздел «Обложки»: образец → варианты → лучший → переводы надписей. Папка — Desktop/Обложки/…"""
    from datetime import datetime

    from engine.publish.cover import model_label, output_root, run_cover_job

    image = Path(str(params.get("image") or "")).expanduser() if params.get("image") else None
    url = str(params.get("url") or "").strip()
    if image is not None and not image.is_file():
        raise ValueError(f"Нет файла образца: {image}")
    if image is None and not url:
        raise ValueError("Дай образец: картинку или ссылку на ролик YouTube")
    cs = _cover_settings(cfg)
    try:
        cs.variants = max(1, min(6, int(params.get("variants") or cs.variants)))
    except (TypeError, ValueError):
        pass
    language = str(params.get("language") or "").strip()
    translations = [str(x) for x in params.get("translations") or [] if str(x).strip()]

    def run(job: Job, progress, cancel: CancelToken) -> dict[str, Any]:
        work = _state_dir(job.state, "work_dir")
        if work is None:
            work = output_root() / f"{datetime.now():%d.%m.%Y %H-%M-%S} — {_safe_dir(model_label(cs.model))}"
            work.mkdir(parents=True, exist_ok=True)
            job.save_state(work_dir=str(work))
        out = run_cover_job(cs, image=image, url=url, language=language, translations=translations, work=work,
                            cancel=cancel, progress=progress)
        images = [out["path"], *out["translations"].values()]
        return {
            "model": out["model_label"],
            "summary": f"Обложка готова ({out['model_label']}): лучший из {len(out['variants'])}"
                       + (f", переводы: {', '.join(out['translations'])}" if out["translations"] else ""),
            "folder": str(work),
            "image": out["path"],
            "images": images,
            "variants": out["variants"],
            "lines": [*( [f"Оригинал: {out['title']}"] if out["title"] else []),
                      "Надпись: " + " / ".join(out["text"]) if out["text"] else "Без надписей",
                      f"Модель: {out['model_label']}", f"Папка: {work}"],
            "warnings": out["notes"],
        }

    what = url or (image.name if image else "")
    return f"Обложка · {model_label(cs.model)}: {what[:60]}", run


def _scan_job(cfg: dict, params: dict, state: dict) -> tuple[str, Any]:
    """«Найти каналы» профиля — задачей в очереди (полоса выкладки: профиль не открывается двумя задачами
    сразу). Всегда скрыто; ход по шагам, «Стоп», сторож зависаний (engine/publish/scan.in_profile)."""
    from engine.publish import registry
    from engine.publish.scan import scan_profile

    pid = str(params.get("profile_id") or "")
    key, url = _s(cfg, "adspower_api_key"), _s(cfg, "adspower_url")
    if not key:
        raise ValueError("Нужен ключ AdsPower (Настройки → AdsPower)")
    prof = registry.load().get(pid)
    if prof is None:
        raise ValueError("Нет такого профиля — нажми «Синхронизировать с AdsPower»")

    def run(job: Job, progress, cancel: CancelToken) -> dict[str, Any]:
        from engine.publish.youtube import env_warnings

        env: dict = {}
        channels = scan_profile(key, url, pid, headless=True, cancel=cancel, progress=progress, env=env)
        registry.set_channels(pid, channels, studio=env or None)
        names = [f"{c.name} ({c.handle})" for c in channels]
        bad = [f"{c.name}: плейлистов {len(c.playlists)} — нужен ровно один" for c in channels if len(c.playlists) != 1]
        bad += [f"№{prof.number} {prof.name}: {w}" for w in env_warnings(env)]
        if env:
            names.append(f"Studio: язык {env.get('lang')}, {'12' if env.get('hour12') else '24'} ч, {env.get('tz')}, "
                         f"отпечаток {env.get('platform')}")
        return {"summary": f"Каналов найдено: {len(channels)}", "lines": names, "warnings": bad}

    return f"Найти каналы: №{prof.number} {prof.name}", run


def _analytics_job(cfg: dict, params: dict, state: dict) -> tuple[str, Any]:
    """«Аналитика» → «Синхронизировать всё»: все каналы профилей AdsPower (или выбранные профили), скрыто."""
    from engine.publish import analytics

    key, url = _s(cfg, "adspower_api_key"), _s(cfg, "adspower_url")
    if not key:
        raise ValueError("Нужен ключ AdsPower (Настройки → AdsPower)")
    profiles = [str(p) for p in params.get("profiles") or [] if str(p).strip()]
    group = str(params.get("group") or "")
    title = "Аналитика каналов"
    if group:  # только каналы группы «Аналитики» (так ходит автообновление по расписанию)
        from engine.publish.analytics_auto import group_channels

        name, _ = group_channels(cfg, group)
        title += f": группа «{name or 'удалена'}»" + (" (по расписанию)" if params.get("auto") else "")

    def run(job: Job, progress, cancel: CancelToken) -> dict[str, Any]:
        only = None
        if group:
            from engine.publish.analytics_auto import group_channels

            name, only = group_channels(load_config(), group)  # состав группы — на момент запуска
            if not only:
                return {"summary": f"Аналитика: в группе «{name or group}» нет каналов", "lines": [], "warnings": []}
        out = analytics.sync(key, url, profile_ids=profiles or None, cancel=cancel, progress=progress,
                             channel_ids=only)
        n_ok = len(out["ok"])
        new = out.get("added") or []
        return {
            "summary": f"Аналитика: обновлено каналов {n_ok} из {out['total']}"
                       + (f", новых найдено: {len(new)}" if new else ""),
            "lines": [*(f"Новый канал: {x}" for x in new), *(f"Не обновлён — {x}" for x in out["failed"]),
                      *out.get("notes", [])],
            "warnings": [*(f"{x}" for x in out["failed"]), *out.get("notes", [])],
        }

    return title, run


def _safe_dir(name: str) -> str:
    import re

    return re.sub(r'[\\/:*?"<>|]+', " ", name).strip()[:60] or "модель"


_SECTION_KIND = {"broll": "broll", "slides": "slides_full"}


def _rewrite_job(cfg: dict, params: dict, state: dict) -> tuple[str, Any]:
    """Ролик YouTube → уникальный текст по промпту раздела; дальше — проверка текста или сразу сборка."""
    from engine.rewrite.cta import NO_CTA, section_texts, strip_cta
    from engine.rewrite.prompts import pick_prompt
    from engine.rewrite.run import RewriteRequest, rewrite_video, safe_name

    url = str(params.get("url") or "").strip()
    # вместо ссылки — расшифровка из файла (таймкоды YouTube уберёт программа)
    source_text = str(params.get("text") or "")
    source_name = str(params.get("name") or "").strip()
    if source_text.strip():
        from engine.rewrite.transcript import clean_transcript

        if len(clean_transcript(source_text).split()) < 50:
            raise ValueError(f"{source_name or 'Текст'}: после удаления таймкодов почти нет речи — переписывать нечего")
    elif not re.match(r"https?://", url):
        raise ValueError("Нужна ссылка на ролик YouTube или файл с текстом")
    section = str(params.get("section") or "broll")
    if section not in _SECTION_KIND:
        raise ValueError(f"Неизвестный раздел: {section}")
    prompt = pick_prompt(cfg.get("rewrite_prompts"), cfg.get("rewrite_prompt"), section, str(params.get("prompt_id") or ""))
    engine = str(params.get("engine") or cfg.get("rewrite_engine") or "claude_code")
    params.setdefault("engine", engine)
    params.setdefault("review", bool(cfg.get("rewrite_review", True)))
    if section == "slides":
        params.setdefault("media_mode", _s(cfg, "media_mode") or "theme")  # режим кадров — на момент постановки
    # настройки сборки проверяем сразу, а не после рерайта на 10 минут
    if section == "broll":
        _footage_dir(cfg)
        _publish_opts(cfg)
    else:
        _llm(cfg)
        _publish_opts(cfg, "slides")
    _need_tts(cfg, None)
    if engine == "llm" and not _llm_opt(cfg):
        raise ValueError("Нет ключа встроенной нейросети (Настройки → LLM) — или выбери Claude / Codex через подписку")
    # призывы — только тексты из полей раздела (вставляются при сборке ролика), нейросеть своих не пишет
    prompt_text = str(prompt["text"]) + NO_CTA
    req = RewriteRequest(
        url=url, prompt=prompt_text, language=str(params.get("language") or cfg.get("rewrite_language") or ""),
        words=str(params.get("words") or cfg.get("rewrite_words") or "7000–8000"), engine=engine,
        llm=_rewrite_llm(cfg), claude_model=_s(cfg, "claude_model"), codex_model=_s(cfg, "codex_model"),
        fallback=bool(cfg.get("rewrite_fallback", True)), section=section,
        source_text=source_text, source_name=source_name,
    )

    def run(job: Job, progress, cancel: CancelToken) -> dict[str, Any]:
        from engine.slides.paths import DESKTOP

        out = rewrite_video(req, cancel=cancel, progress=progress)
        if out.source.origin != "text":  # ролик переписан — с канала конкурента повторно не возьмётся
            from engine.rewrite.channels import mark_done

            mark_done(out.source.video_id, title=out.source.title)
        story, package = out.story_file, out.package_file
        # призывы исходника и нейросети — прочь; тексты из полей раздела добавит сборка ролика
        text, removed = strip_cta(story.read_text(encoding="utf-8"))
        story.write_text(text.strip() + "\n", encoding="utf-8")
        framing = [f"убрано из рассказа: {removed}"] if removed else []
        framing.append("тексты из полей раздела добавятся при сборке ролика" if section_texts(cfg, section).any()
                       else "поля текстов раздела пустые — в ролике не будет призывов")
        follow = {"text_file": str(story), "package_file": str(package or ""), "source_url": url,
                  "language": out.language,  # тексты из полей — на язык рассказа
                  "publish_plan": params.get("publish_plan"),  # даты выкладки этого запуска
                  # ролик по рерайту — с настройками на момент постановки рерайта, а не на момент его окончания
                  SETTINGS_KEY: params.get(SETTINGS_KEY)}
        if section == "slides":
            follow["media_mode"] = params.get("media_mode")
        lines = [f"Исходник: «{out.source.title}» — {out.source.words} слов",
                 f"Рерайт: {out.words} слов, язык {out.language}, промпт «{prompt.get('name')}»",
                 f"Писал: {out.engine_note}", *([out.review] if out.review else []), *[f"Призывы: {n}" for n in framing]]
        if params.get("review"):
            # на проверку: копия на Desktop — открыть, поправить, сохранить и нажать «Собрать ролик»
            review = DESKTOP / "Рерайты"
            review.mkdir(parents=True, exist_ok=True)
            story = review / story.name
            shutil.copy2(out.story_file, story)
            follow["text_file"] = str(story)
            if package is not None:
                pkg = review / f"{safe_name(out.source.title)} — описание.txt"
                shutil.copy2(package, pkg)
                follow["package_file"] = str(pkg)
            lines.append("Проверь текст и нажми «Собрать ролик»")
        elif job is not None and job.state.get("follow_job"):
            lines.append("Ролик уже поставлен в очередь")  # повтор после сбоя — второй раз не ставим
        else:
            from server.jobs import get_manager

            mgr = get_manager()
            try:
                follow_job = mgr.create(_SECTION_KIND[section], follow)
            except ValueError as exc:
                # текст готов — не теряем его: собрать можно кнопкой, когда поправишь настройки
                lines.append(f"Ролик не поставлен в очередь: {exc}. Исправь и нажми «Собрать ролик»")
                params["review"] = True
            else:
                if job is not None:
                    job.save_state(follow_job=follow_job.id)
                # ролик этой ссылки — следующим в очереди: каждая ссылка проходит полный цикл по порядку
                mgr.add(follow_job, after=job.id if job is not None else None)
                lines.append("Ролик собирается следующим в очереди")
        return {
            "summary": f"Рерайт: {out.words} слов",
            "folder": str(story),
            "text_file": str(story),
            "follow": {"kind": _SECTION_KIND[section], "params": follow},
            "lines": lines,
        }

    from engine.rewrite.prompts import SECTIONS

    return f"Рерайт ({SECTIONS[section]}): {url or source_name or 'текст'}", run


_BUILDERS = {
    "broll": _broll,
    "slides_full": _slides_full,
    "slides_resume": _slides_resume,
    "slides_voice": _slides_voice,
    "slides_media": _slides_media,
    "slides_render": _slides_render,
    "copies": _copies_job,
    "thumbnail": _thumbnail_job,
    "rewrite": _rewrite_job,
    "cover": _cover_job,
    "publish": _publish_job,
    "comments": _comments_job,
    "polls": _polls_job,
    "niches": _niches_job,
    "analytics": _analytics_job,
    "scan": _scan_job,
}
