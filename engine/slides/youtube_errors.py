"""Ответы YouTube Data API с отказом: понятная причина и память «поиск на этом ключе сегодня исчерпан».

У Google два разных отказа с одним и тем же кодом 403/429: кончилась суточная квота (вернётся после полуночи
по Тихоокеанскому времени) и «доступ запрещён» (в проекте ключа не включён YouTube Data API v3 или ключ ограничен —
сам не пройдёт). Раньше оба выглядели как «ключи кончились или не подходят», а проверка ключа в настройках
показывала «OK» у ключа, поиск на котором уже исчерпан (проверка стоит 1 единицу, поиск — 100 и считается отдельно).
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

_LOCK = threading.Lock()
QUOTA_REASONS = ("quotaexceeded", "ratelimitexceeded", "dailylimitexceeded", "userratelimitexceeded")


def _file() -> str:
    from engine.core import config as cfg_mod

    return os.environ.get("STORY_VIDEO_YT_QUOTA_FILE") or str(cfg_mod.DATA_DIR / "youtube_quota.json")


def reset_at(now: float | None = None) -> float:
    """Когда обновится суточная квота: ближайшая полночь по Тихоокеанскому времени."""
    tz = ZoneInfo("America/Los_Angeles")
    cur = datetime.fromtimestamp(time.time() if now is None else now, tz)
    nxt = (cur + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.timestamp()


def reset_text(now: float | None = None) -> str:
    """«завтра в 11:00» / «сегодня в 11:00» — по часам этого компьютера."""
    at = datetime.fromtimestamp(reset_at(now))
    cur = datetime.fromtimestamp(time.time() if now is None else now)
    return f"{'сегодня' if at.date() == cur.date() else 'завтра'} около {at:%H:%M}"


def classify(resp: httpx.Response) -> tuple[str, str]:
    """(вид, пояснение): вид — «quota» (кончилась квота) / «denied» (доступ запрещён) / «other»."""
    reason, message = "", ""
    try:
        err = (resp.json() or {}).get("error") or {}
        reason = str((err.get("errors") or [{}])[0].get("reason") or "")
        message = str(err.get("message") or "")
    except Exception:  # noqa: BLE001 — тело не JSON
        message = resp.text[:200]
    low = f"{reason} {message}".lower()
    if resp.status_code == 429 or any(r in low for r in QUOTA_REASONS) or "quota" in low:
        return "quota", "суточная квота исчерпана"
    if resp.status_code in (400, 401, 403):
        why = "ключ недействителен" if "not valid" in low or "api_key_invalid" in low else (
            "доступ запрещён: в проекте этого ключа не включён YouTube Data API v3 или ключ ограничен")
        return "denied", why
    return "other", f"HTTP {resp.status_code}"


def note_quota_spent(key: str) -> None:
    """Запомнить: на этом ключе квота исчерпана (до ближайшего обновления)."""
    if not key:
        return
    with _LOCK:
        data = _read()
        data[key[-6:]] = time.time()
        _write(data)


def spent_since(key: str, now: float | None = None) -> float:
    """Когда на ключе исчерпалась квота (0 — не исчерпана или уже обновилась)."""
    now = time.time() if now is None else now
    with _LOCK:
        t = float(_read().get(key[-6:]) or 0)
    return t if t and now < reset_at(t) else 0.0


def clear(key: str) -> None:
    with _LOCK:
        data = _read()
        if data.pop(key[-6:], None) is not None:
            _write(data)


def _read() -> dict:
    try:
        with open(_file(), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write(data: dict) -> None:
    path = _file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def exhausted_message(problems: dict[str, str]) -> str:
    """Итоговая фраза, когда не осталось рабочих ключей: что случилось с каждым и что делать."""
    if not problems:
        return "ключи YouTube API не заданы"
    parts = [f"ключ …{k[-4:]}: {why}" for k, why in problems.items()]
    quota = [k for k, w in problems.items() if "квота" in w]
    tail = ""
    if quota:
        tail += f" Квота обновится {reset_text()}."
    if len(quota) < len(problems):
        tail += " Ключ с запретом сам не заработает: включи YouTube Data API v3 в его проекте или замени ключ."
    if quota:
        tail += " Можно добавить ключ из другого проекта Google Cloud."
    return "ключи YouTube API не работают — " + "; ".join(parts) + "." + tail
