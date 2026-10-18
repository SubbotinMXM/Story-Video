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
    # YouTube Data API: по ключу на строку; кончилась суточная квота — берётся следующий
    "youtube_api_keys": "",
    # общие опции ролика
    "subtitles": False,
    "subscribe": False,
    "subscribe_path": "",
    "speed_copies": False,
    "speed_copies_count": 1,
    # футажи
    "overlay_text": "",
    "broll_dir": "",
    "head_dir": "",
    "intro_dir": "",
    "outro_dir": "",
    # слайды
    # подбор кадров: theme — по смыслу; strict_3min — строго первые 3 мин; strict_full — строго весь ролик
    "media_mode": "theme",
    "slides_intro_dir": "",
    "slides_outro_dir": "",
    # фоновая музыка стоковых роликов: id трека из assets/music ("" — без музыки)
    "music_track": "",
    # AI-кадры (RoyalTechno AI): генерация фото/видео для кусков без хорошего живого кадра
    "royaltechno_api_key": "",
    "ai_gen_enabled": False,
    "ai_image_model": "",
    "ai_video_model": "",
    # шаблоны оформления: [{"id", "name", "kind": broll|slides, "values": {...}}] (см. TEMPLATE_KEYS)
    "templates": [],
    # какой шаблон сейчас выбран в разделе ("" — настраиваю вручную)
    "active_template": {"broll": "", "slides": ""},
}

# что запоминает шаблон: всё, что настраивается под ролик (ключи API — нет)
_TEMPLATE_COMMON = ("subtitles", "subscribe", "subscribe_path", "speed_copies", "speed_copies_count", "tts_provider", "voice")
TEMPLATE_KEYS: dict[str, tuple[str, ...]] = {
    "broll": _TEMPLATE_COMMON + ("broll_dir", "head_dir", "overlay_text", "intro_dir", "outro_dir"),
    "slides": _TEMPLATE_COMMON
    + ("media_mode", "slides_intro_dir", "slides_outro_dir", "music_track", "ai_gen_enabled", "ai_image_model", "ai_video_model"),
}

# модели, которые сервис убрал: сохранённый выбор сбрасывается на модель по умолчанию
_DEAD_MODELS = {"gpt-5.4", "gpt-5.4-mini", "gpt-5.5", "gpt-5.6-sol"}
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


MEDIA_MODES = ("theme", "strict_3min", "strict_full")


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    raw = _migrate_lumean(raw)
    if not str(raw.get("youtube_api_keys") or "").strip() and str(raw.get("youtube_api_key") or "").strip():
        # раньше был один ключ YouTube — переносим в список
        raw = {**raw, "youtube_api_keys": str(raw["youtube_api_key"]).strip()}
    if raw.get("media_mode") not in MEDIA_MODES:
        # старый чекбокс «строгое соответствие»
        raw = {**raw, "media_mode": "strict_3min" if raw.get("strict_sync") else "theme"}
    cfg = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in raw and raw[key] is not None:
            cfg[key] = raw[key]
    if str(cfg.get("llm_model") or "").strip() in _DEAD_MODELS:
        cfg["llm_model"] = ""
    from engine.tts import PROVIDERS  # здесь, а не наверху: engine.tts тянет клиенты сервисов

    if cfg.get("tts_provider") not in PROVIDERS:
        # выбран всегда ровно один сервис озвучки
        cfg["tts_provider"] = DEFAULTS["tts_provider"]
    cfg["templates"] = _clean_templates(cfg.get("templates"))
    ids = {t["id"]: t["kind"] for t in cfg["templates"]}
    active = cfg.get("active_template") if isinstance(cfg.get("active_template"), dict) else {}
    cfg["active_template"] = {
        kind: (str(active.get(kind) or "") if ids.get(str(active.get(kind) or "")) == kind else "")
        for kind in TEMPLATE_KEYS
    }
    return cfg


def _clean_templates(raw: object) -> list[dict[str, Any]]:
    """Шаблоны из файла/интерфейса: без мусора, без чужих ключей, id и имя обязательны."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()[:80]
        kind = str(item.get("kind") or "")
        if not tid or not name or kind not in TEMPLATE_KEYS or tid in seen:
            continue
        seen.add(tid)
        values = item.get("values") if isinstance(item.get("values"), dict) else {}
        out.append(
            {
                "id": tid,
                "name": name,
                "kind": kind,
                "values": {k: values[k] for k in TEMPLATE_KEYS[kind] if k in values and values[k] is not None},
            }
        )
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
