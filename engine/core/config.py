"""Настройки приложения: один JSON в data/config.json."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_PATH = DATA_DIR / "config.json"
# старое CTk-приложение: ключи забираем один раз при первом запуске
LEGACY_CONFIG = PROJECT_ROOT.parent / "story-rewriter" / ".config.json"

# Всё, что знает приложение. Ключи вне списка отбрасываются при сохранении.
DEFAULTS: dict[str, Any] = {
    # озвучка: выбранный сервис и настройки каждого сервиса (см. engine/tts)
    "tts_provider": "lumean",
    "tts": {},
    # LLM
    "llm_api_key": "",
    "llm_base_url": "",
    "llm_model": "",
    # стоки
    "pexels_api_keys": "",
    "pixabay_api_keys": "",
    "unsplash_api_keys": "",
    # TMDB (фото людей кино и кадры фильмов): токен «Ключ доступа к API» или короткий ключ, по одному на строку
    "tmdb_api_keys": "",
    # YouTube Data API: по ключу на строку; кончилась суточная квота — берётся следующий
    "youtube_api_keys": "",
    # общие опции ролика
    "subtitles": False,
    # переходы между фото в стоковых роликах: calm — спокойные, flash — со светлыми вспышками
    "transition": "calm",
    "subscribe": False,
    "subscribe_path": "",
    "speed_copies": False,
    "speed_copies_count": 1,
    # куда класть папки готовых роликов каждого раздела; пусто — рабочий стол
    "broll_output_dir": "",
    "slides_output_dir": "",
    # футажи
    "overlay_text": "",
    "broll_dir": "",
    "head_dir": "",
    "intro_dir": "",
    "outro_dir": "",
    # слайды
    # подбор кадров: theme — по смыслу; strict_3min — строго первые 3 мин; strict_full — строго весь ролик;
    # stars — ролик про нескольких людей: на часть про каждого — его фото
    "media_mode": "theme",
    # ролики про людей: уточнения, кто есть кто (тёзки) — программа спрашивает, если не уверена
    "stars_notes": "",
    # тексты в озвучку, раздельно для футажей и стоков: пусто — ничего не вставляется
    "broll_text_start": "",
    "broll_text_mid": "",
    "broll_text_end": "",
    "slides_text_start": "",
    "slides_text_mid": "",
    "slides_text_end": "",
    "slides_intro_dir": "",
    "slides_outro_dir": "",
    # фоновая музыка стоковых роликов: id трека из assets/music ("" — без музыки)
    "music_track": "",
    # переписывание сценария по ссылке на ролик (engine/rewrite)
    # чем переписывать: claude_code (Claude) или codex. Не смог — другая подписка, потом встроенная нейросеть
    # (она — только запасной вариант, выбрать её первой нельзя)
    "rewrite_engine": "claude_code",
    "rewrite_review": True,  # показать текст перед сборкой; False — сразу собирать ролик
    "rewrite_prompts": [],  # своя библиотека промптов; пусто — промпты по умолчанию
    "rewrite_prompt": {"broll": "default-broll", "slides": "default-slides"},  # выбранный промпт раздела
    "rewrite_language": "",  # язык рерайта по умолчанию; пусто — язык оригинала
    "rewrite_words": "7000–8000",
    "rewrite_channels": {"broll": "", "slides": ""},  # «По каналам»: ссылки на каналы конкурентов (по строке)
    "rewrite_channels_count": 5,  # сколько роликов брать за запуск ВСЕГО (со всех каналов-доноров по очереди)
    "claude_model": "",  # модель Claude Code для рерайта: opus / sonnet / haiku; пусто — по умолчанию подписки
    "codex_model": "",  # модель Codex (подписка ChatGPT) для рерайта; пусто — по умолчанию Codex
    "rewrite_llm_model": "",  # модель встроенной нейросети для рерайта; пусто — как в разделе LLM (или авто)
    # Claude недоступен / кончился лимит / оборвался — дописать встроенной нейросетью
    "rewrite_fallback": True,  # модель Claude Code (пусто — по умолчанию подписки)
    # вкладка «Превью»: фото людей цветные (как нашлись) или чёрно-белые
    # AI-кадры (RoyalTechno AI): генерация фото/видео для кусков без хорошего живого кадра
    "royaltechno_api_key": "",
    # AdsPower (браузерные профили с каналами YouTube) — Local API на этом Mac
    "adspower_url": "http://local.adspower.com:50325",
    "adspower_api_key": "",
    # выкладка роликов «Футажей» на YouTube (настраивается перед каждым запуском; включена — главнее копий)
    "broll_publish": False,
    "broll_publish_channels": [],  # «профиль/ID канала»
    "broll_publish_coauthors": [],
    "broll_publish_coauthors_on": None,  # «Добавить соавторов»; None — как было: включено, если соавторы выбраны
    # откуда обложка и название рассказа: files — из папки/файла на компьютере; competitor — переделать
    # обложку и название ролика-конкурента (из рерайта), как в «Стоковых слайдах». Описания — из файла всегда
    "broll_publish_materials": "files",
    "broll_publish_thumbs": "",  # папка обложек
    "broll_publish_titles": "",  # файл названий «1. …»
    "broll_publish_descs": "",  # файл описаний «1. …»
    "broll_publish_dates": [],  # свои даты запуска (YYYY-MM-DD); пусто — каждый день подряд
    "broll_publish_time": "",  # ЧЧ:ММ по Москве; пусто — случайное 16:00–20:00
    "broll_publish_step": 1,  # дней между роликами на канале: 1 — каждый день, 2 — через день, 3 — через два…
    "broll_publish_visible": True,  # браузер при выкладке видно
    "broll_publish_keep": True,  # оставлять ролик на компьютере после выкладки
    # комментарий-призыв от имени канала под вышедшими роликами (закрепляется); пусто — не писать
    "broll_publish_comment": "Подписывайтесь на канал, чтобы не пропустить новые выпуски, а также включайте "
                             "колокольчик уведомлений 🔔",
    # выкладка роликов «Стоковых слайдов»: каналы общие с «Футажами», галочки свои; название, обложка
    # и описание готовятся под каждый ролик (по оригиналу рерайта)
    "slides_publish": False,
    "slides_publish_channels": [],
    "slides_publish_coauthors": [],
    "slides_publish_coauthors_on": None,
    "slides_publish_dates": [],
    "slides_publish_time": "",
    "slides_publish_step": 1,
    "slides_publish_visible": True,
    "slides_publish_keep": True,
    "slides_publish_cover": True,
    "slides_publish_title_variants": 3,  # сколько вариантов названия пишет нейросеть (выбирается лучший)  # генерировать обложку под ролик; выключено — без обложки (ставишь сам)
    "slides_publish_comment": "Подписывайтесь на канал, чтобы не пропустить новые выпуски, а также включайте "
                              "колокольчик уведомлений 🔔",
    # первым абзацем описания (дальше — главы с тайм-кодами); переводится на язык ролика
    "slides_publish_desc_cta": "🔔 ПОДПИШИТЕСЬ НА КАНАЛ И ВКЛЮЧИТЕ КОЛОКОЛЬЧИК — новые истории выходят каждый день!",
    # обложки по образцу (раздел «Обложки» и выкладка слайдов): модель RoyalTechno и число вариантов
    "cover_model": "nano-banana-pro-2k",
    "cover_variants": 3,
    "cover_prompts": [],  # свои промпты обложек [{id, name, role, text}]; пусто — стандартные
    "cover_prompt": {},  # выбранный промпт роли {роль: id}
    "ai_gen_enabled": False,
    "ai_image_model": "",
    "ai_video_model": "",
    # потолок трат на AI-кадры одного ролика, $ (вместе с повторными генерациями)
    "ai_budget_usd": 0.4,  # не больше 0.5 (engine/slides/ai_gen.AI_BUDGET_MAX_CENTS)
    # свои доли источников, % {ai, stocks, youtube, google, wikimedia, archive}; пусто — по умолчанию
    # (доли сами подстраиваются под тему ролика, см. engine/slides/source_mix.DEFAULT_MIX)
    "source_mix": {},
    # вкладка «Аналитика»: свои группы каналов [{id, name, channels: [id канала]}] (канал — хоть в нескольких)
    "analytics_groups": [],
    # её вид: фильтр по группе ("" — все, "none" — без группы, иначе id группы) и сортировка
    "last_seen_version": "",  # какую версию «Что нового» человек уже видел (engine/core/changelog.py)
    "analytics_view": {"group": "", "profile": "", "sort": "views_48h", "dir": "desc"},
    # автообновление аналитики: каждый день в time (МСК) — синхронизация каналов группы (engine/publish/analytics_auto)
    "analytics_auto": {"enabled": False, "group": "", "time": "07:00"},
    # автообновление программы: каждый день в time (МСК) — git pull и перезапуск, когда очередь свободна (engine/updater)
    "update_auto": {"enabled": False, "time": "04:00"},
}

# модели, которые сервис убрал: сохранённый выбор сбрасывается на модель по умолчанию
_DEAD_MODELS = {"gpt-5.4", "gpt-5.4-mini"}
_lock = threading.Lock()


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _from_legacy(raw: dict[str, Any]) -> dict[str, Any]:
    out = dict(raw)
    for dest, src in (
        ("llm_api_key", "api_key"),
        ("llm_base_url", "base_url"),
        ("llm_model", "model"),
    ):
        if not out.get(dest) and raw.get(src):
            out[dest] = raw[src]
    return out


def _migrate_lumean(raw: dict[str, Any]) -> dict[str, Any]:
    """Старые плоские lumean_* → tts.lumean (голоса и шаблон с именами)."""
    tts = raw.get("tts") if isinstance(raw.get("tts"), dict) else {}
    if "lumean" in tts or not any(k.startswith("lumean_") for k in raw):
        return raw
    if not str(raw.get("lumean_api_key") or "").strip():
        # файл перезаписала старая версия приложения с пустыми полями —
        # берём Lumean из настроек story-rewriter, если они там есть
        legacy = _read(LEGACY_CONFIG)
        if str(legacy.get("lumean_api_key") or "").strip():
            raw = {**raw, **{k: v for k, v in legacy.items() if k.startswith("lumean_")}}
    voices = [
        {"id": str(v.get("id") or v.get("voice_id") or ""), "name": str(v.get("name") or "")}
        for v in raw.get("lumean_voices") or []
        if isinstance(v, dict)
    ]
    template = str(raw.get("lumean_template_id") or "").strip()
    out = dict(raw)
    out["tts"] = {
        **tts,
        "lumean": {
            "api_key": raw.get("lumean_api_key") or "",
            "voice": {"items": voices, "selected": raw.get("lumean_voice_id") or ""},
            "template": {
                "items": [{"id": template, "name": "Основной шаблон"}] if template else [],
                "selected": template,
            },
            "speed": raw.get("lumean_voice_speed") or 1.0,
        },
    }
    return out


MEDIA_MODES = ("theme", "strict_3min", "strict_full", "stars")
PICKABLE_ENGINES = ("claude_code", "codex")


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    raw = _migrate_lumean(raw)
    if not str(raw.get("youtube_api_keys") or "").strip() and str(raw.get("youtube_api_key") or "").strip():
        # раньше был один ключ YouTube — переносим в список
        raw = {**raw, "youtube_api_keys": str(raw["youtube_api_key"]).strip()}
    if raw.get("rewrite_engine") not in PICKABLE_ENGINES:
        # встроенная нейросеть теперь только запасная: первым идёт Claude (не смог — Codex, потом встроенная)
        raw = {**raw, "rewrite_engine": "claude_code"}
    if raw.get("media_mode") not in MEDIA_MODES:
        # старый чекбокс «строгое соответствие»
        raw = {**raw, "media_mode": "strict_3min" if raw.get("strict_sync") else "theme"}
    if str(raw.get("output_dir") or "").strip():
        # была одна папка роликов в настройках — теперь своя у каждого раздела
        old = str(raw["output_dir"]).strip()
        raw = {**raw, "broll_output_dir": raw.get("broll_output_dir") or old,
               "slides_output_dir": raw.get("slides_output_dir") or old}
    cfg = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in raw and raw[key] is not None:
            cfg[key] = raw[key]
    try:
        if float(cfg.get("ai_budget_usd")) > 0.5:
            # потолок AI на ролик — 50 центов; старое значение ($1) — к новому по умолчанию
            cfg["ai_budget_usd"] = DEFAULTS["ai_budget_usd"]
    except (TypeError, ValueError):
        cfg["ai_budget_usd"] = DEFAULTS["ai_budget_usd"]
    for key in ("broll_publish_step", "slides_publish_step"):
        try:
            cfg[key] = max(1, min(30, int(cfg.get(key))))
        except (TypeError, ValueError):
            cfg[key] = 1
    for key in ("broll_publish_channels", "broll_publish_coauthors", "broll_publish_dates",
                "slides_publish_channels", "slides_publish_coauthors", "slides_publish_dates"):
        value = cfg.get(key)
        cfg[key] = [str(x) for x in value if str(x).strip()] if isinstance(value, list) else []
    try:  # неизменённые копии стандартных промптов обложек не храним (см. cover_prompts.prune)
        from engine.publish.cover_prompts import prune

        cfg["cover_prompts"] = prune(cfg.get("cover_prompts") if isinstance(cfg.get("cover_prompts"), list) else [])
    except Exception:
        cfg["cover_prompts"] = []
    cfg["analytics_groups"] = _groups(cfg.get("analytics_groups"))
    view = cfg.get("analytics_view") if isinstance(cfg.get("analytics_view"), dict) else {}
    cfg["analytics_view"] = {**DEFAULTS["analytics_view"], **{k: str(v) for k, v in view.items()
                                                              if k in DEFAULTS["analytics_view"]}}
    from engine.publish.analytics_auto import normalize as _auto

    cfg["analytics_auto"] = _auto(cfg.get("analytics_auto"))
    from engine.updater import normalize as _upd

    cfg["update_auto"] = _upd(cfg.get("update_auto"))
    if str(cfg.get("llm_model") or "").strip() in _DEAD_MODELS:
        cfg["llm_model"] = ""
    from engine.tts import PROVIDERS  # здесь, а не наверху: engine.tts тянет клиенты сервисов

    if cfg.get("tts_provider") not in PROVIDERS:
        # выбран всегда ровно один сервис озвучки
        cfg["tts_provider"] = DEFAULTS["tts_provider"]
    return cfg


def _groups(raw: Any) -> list[dict[str, Any]]:
    """Группы каналов аналитики: у каждой id и имя; каналы — без пустых и повторов."""
    out, seen = [], set()
    for g in raw if isinstance(raw, list) else []:
        if not isinstance(g, dict):
            continue
        gid, name = str(g.get("id") or "").strip(), str(g.get("name") or "").strip()
        if not gid or not name or gid in seen:
            continue
        seen.add(gid)
        chans = list(dict.fromkeys(str(c) for c in g.get("channels") or [] if str(c).strip()))
        out.append({"id": gid, "name": name[:60], "channels": chans})
    return out


def _write(cfg: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def load_config() -> dict[str, Any]:
    with _lock:
        if CONFIG_PATH.is_file():
            return _normalize(_read(CONFIG_PATH))
        cfg = _normalize(_from_legacy(_read(LEGACY_CONFIG)))
        _write(cfg)
        return cfg


def save_config(patch: dict[str, Any]) -> dict[str, Any]:
    """Слить patch в сохранённые настройки, вернуть итог.

    Ключи, которых эта версия не знает, остаются в файле как были:
    другая (более новая или старая) версия приложения не потеряет свои данные.
    """
    with _lock:
        stored = _read(CONFIG_PATH)
        current = _normalize(stored) if CONFIG_PATH.is_file() else dict(DEFAULTS)
        for key, value in patch.items():
            if key in DEFAULTS:
                current[key] = value
        cfg = _normalize(current)
        unknown = {k: v for k, v in stored.items() if k not in DEFAULTS and k != "youtube_api_key"}
        if "lumean" in cfg["tts"]:
            # плоские lumean_* уже перенесены в tts.lumean — не тащим их дальше
            unknown = {k: v for k, v in unknown.items() if not k.startswith("lumean_")}
        _write({**unknown, **cfg})
        return cfg
