"""Шаблоны запуска: сохранённый набор настроек раздела («Рассказы» или «Стоковые слайды») под конкретные каналы.

Шаблон = «что делаем» (каналы-конкуренты, промпт рерайта, озвучка, режим подбора, AI, обложки/названия…) +
«куда выкладываем» (каналы, соавторы, шаг между роликами, время). Запуск шаблона — «сделай N роликов»: программа
сама берёт N следующих ещё не переписанных роликов конкурентов, ставит рерайт → ролик → выкладку по настройкам шаблона.

Шаблон хранит только свои настройки (ключи конфига) — это наложение поверх текущих настроек на момент запуска:
глобальные настройки не меняются, ключи сервисов остаются живыми, уже запущенные задачи не ломаются при правке
шаблона (у каждой задачи свой замороженный снимок). «Изменить» — загрузить шаблон в настройки, поправить на привычных
экранах и «Сохранить в шаблон».
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid
from typing import Any

from engine.core import config as cfg_mod
from server.runners import snapshot_settings

SECTIONS = ("broll", "slides")
_LOCK = threading.Lock()

# настройки приложения, а не запуска: в шаблон не входят (библиотеки промптов, аналитика, версия окна «Что нового»)
APP_KEYS = frozenset({"analytics_groups", "analytics_view", "analytics_auto", "last_seen_version", "rewrite_prompts",
                      "cover_prompts", "adspower_url", "adspower_api_key"})
# у каждого раздела свой набор: в шаблон «Рассказов» не попадают настройки слайдов и наоборот
BROLL_ONLY = frozenset({"broll_dir", "head_dir", "intro_dir", "outro_dir", "overlay_text"})
SLIDES_ONLY = frozenset({"media_mode", "music_track", "source_mix", "transition", "ai_gen_enabled", "ai_image_model",
                         "ai_video_model", "ai_budget_usd"})
# словари «по разделам»: в шаблон — только запись своего раздела, при загрузке — вливается в общий словарь
PER_SECTION = ("rewrite_channels", "rewrite_prompt")
_TTS_SECRET = "api_key"


def _path():
    return cfg_mod.DATA_DIR / "templates.json"


def _read() -> list[dict[str, Any]]:
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [t for t in raw if isinstance(t, dict) and t.get("id") and t.get("section") in SECTIONS] if isinstance(raw, list) else []


def _write(items: list[dict[str, Any]]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def keys_for(section: str) -> set[str]:
    """Ключи конфига, которые входят в шаблон раздела."""
    other = "slides" if section == "broll" else "broll"
    skip = set(APP_KEYS) | (SLIDES_ONLY if section == "broll" else BROLL_ONLY)
    return {k for k in cfg_mod.DEFAULTS
            if k not in skip and not k.startswith(f"{other}_") and k != f"{section}_publish_dates"}


def capture(cfg: dict[str, Any], section: str) -> dict[str, Any]:
    """Текущие настройки раздела → наложение шаблона (без ключей сервисов и без календарных дат)."""
    snap = snapshot_settings(cfg)
    out: dict[str, Any] = {k: copy.deepcopy(snap[k]) for k in keys_for(section) if k in snap}
    for k in PER_SECTION:
        if isinstance(out.get(k), dict):
            out[k] = {section: copy.deepcopy(out[k].get(section))} if section in out[k] else {}
    return out


def patch_for(cfg: dict[str, Any], tpl: dict[str, Any]) -> dict[str, Any]:
    """Что записать в конфиг, чтобы наложить шаблон: словари «по разделам» вливаются, у озвучки остаются секреты."""
    overlay = copy.deepcopy(tpl.get("settings") or {})
    for k in PER_SECTION:
        if k in overlay:
            overlay[k] = {**(cfg.get(k) or {}), **overlay[k]}
    if "tts" in overlay:
        live = cfg.get("tts") or {}
        tts = dict(live)
        for pid, opts in (overlay["tts"] or {}).items():
            secret = (live.get(pid) or {}).get(_TTS_SECRET)
            tts[pid] = {**(opts or {}), **({_TTS_SECRET: secret} if secret is not None else {})}
        overlay["tts"] = tts
    return overlay


def merged(cfg: dict[str, Any], tpl: dict[str, Any]) -> dict[str, Any]:
    """Полные настройки для запуска: текущие, поверх них шаблон."""
    return {**cfg, **patch_for(cfg, tpl)}


# ---------- хранилище ----------


def _clean_name(name: str) -> str:
    name = " ".join(str(name or "").split())
    if not name:
        raise ValueError("Назови шаблон")
    return name[:80]


def _check_free(items: list[dict], name: str, section: str, *, skip: str = "") -> None:
    if any(t["id"] != skip and t["section"] == section and t["name"].lower() == name.lower() for t in items):
        raise ValueError(f"Шаблон «{name}» уже есть — выбери другое название")


def listing() -> list[dict[str, Any]]:
    with _LOCK:
        return sorted(_read(), key=lambda t: (t["section"], t.get("name", "").lower()))


def get(tid: str) -> dict[str, Any] | None:
    with _LOCK:
        return next((t for t in _read() if t["id"] == tid), None)


def create(name: str, section: str, cfg: dict[str, Any]) -> dict[str, Any]:
    if section not in SECTIONS:
        raise ValueError("Раздел шаблона — «Рассказы» или «Стоковые слайды»")
    name = _clean_name(name)
    with _LOCK:
        items = _read()
        _check_free(items, name, section)
        now = time.time()
        tpl = {"id": uuid.uuid4().hex[:10], "name": name, "section": section, "settings": capture(cfg, section),
               "created": now, "updated": now}
        items.append(tpl)
        _write(items)
        return tpl


def _mutate(tid: str, fn) -> dict[str, Any]:
    with _LOCK:
        items = _read()
        tpl = next((t for t in items if t["id"] == tid), None)
        if tpl is None:
            raise KeyError(tid)
        fn(tpl, items)
        tpl["updated"] = time.time()
        _write(items)
        return tpl


def rename(tid: str, name: str) -> dict[str, Any]:
    name = _clean_name(name)

    def go(tpl, items):
        _check_free(items, name, tpl["section"], skip=tid)
        tpl["name"] = name

    return _mutate(tid, go)


def recapture(tid: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """«Сохранить в шаблон»: взять текущие настройки раздела как новое содержимое шаблона."""
    return _mutate(tid, lambda tpl, _items: tpl.__setitem__("settings", capture(cfg, tpl["section"])))


def set_setting(tid: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Поменять в шаблоне отдельные настройки (например, каналы выкладки из окна запуска)."""
    return _mutate(tid, lambda tpl, _items: tpl["settings"].update(copy.deepcopy(patch)))


def copy_of(tid: str) -> dict[str, Any]:
    with _LOCK:
        items = _read()
        src = next((t for t in items if t["id"] == tid), None)
        if src is None:
            raise KeyError(tid)
        base, n = src["name"], 2
        name = f"{base} (копия)"
        while any(t["section"] == src["section"] and t["name"].lower() == name.lower() for t in items):
            name = f"{base} (копия {n})"
            n += 1
        now = time.time()
        tpl = {**copy.deepcopy(src), "id": uuid.uuid4().hex[:10], "name": name, "created": now, "updated": now}
        items.append(tpl)
        _write(items)
        return tpl


def delete(tid: str) -> bool:
    with _LOCK:
        items = _read()
        left = [t for t in items if t["id"] != tid]
        if len(left) == len(items):
            return False
        _write(left)
        return True
