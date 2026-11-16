"""Фоновая музыка для стоковых роликов: каталог assets/music — 5 нейтральных треков из Фонотеки YouTube
(YouTube Audio Library: на них не бывает претензий Content ID; раньше был FreePD — на «Cold Journey» пришла претензия).

Треки заранее подготовлены tools/prepare_music.py: одна громкость (−16 LUFS), ровная
по всей длине, и бесшовный стык для повтора по кругу.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from engine.core.config import PROJECT_ROOT

MUSIC_DIR = PROJECT_ROOT / "assets" / "music"
# громкость музыки под голосом: ~−24 дБ — тихий фон, чтец всегда на первом плане (было −20 дБ — громковато)
MUSIC_VOLUME = 0.063


@lru_cache(maxsize=1)
def catalog() -> dict:
    path = MUSIC_DIR / "catalog.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"groups": {}, "tracks": []}
    tracks = [t for t in raw.get("tracks") or [] if (MUSIC_DIR / str(t.get("file") or "")).is_file()]
    return {"groups": raw.get("groups") or {}, "tracks": tracks}


def track(track_id: str) -> dict | None:
    tid = (track_id or "").strip()
    return next((t for t in catalog()["tracks"] if t["id"] == tid), None) if tid else None


def track_path(track_id: str) -> Path | None:
    t = track(track_id)
    return MUSIC_DIR / t["file"] if t else None


def credit_line(track_id: str) -> str:
    t = track(track_id)
    if not t:
        return ""
    if "YouTube" in str(t.get("source") or ""):
        artist = f" — {t['artist']}" if t.get("artist") else ""
        return (f"Музыка: «{t['title']}»{artist} — Фонотека YouTube (YouTube Audio Library), "
                "без претензий Content ID, указывать автора не обязательно.")
    return f"Музыка: «{t['title']}» — FreePD.com, общественное достояние (CC0), указывать автора не обязательно."


# ---------- претензии YouTube (Content ID) ----------
# Бесплатную музыку (даже CC0) иногда регистрируют в Content ID сторонние «правообладатели» — YouTube тогда
# ставит ролику претензию «Melody or lyrics». Такой трек программа больше не использует: при выкладке она
# читает страницу претензий ролика в Studio и, если там трек из нашей библиотеки, вносит его сюда.
CLAIMS_FILE = "music_claims.json"


def claimed() -> dict[str, dict]:
    from engine.publish.store import read_json

    data = read_json(CLAIMS_FILE, {})
    return data if isinstance(data, dict) else {}


def mark_claimed(track_id: str, *, video: str = "", reason: str = "") -> None:
    import time

    from engine.publish.store import LOCK, read_json, write_json

    t = track(track_id)
    if not t:
        return
    with LOCK:
        data = read_json(CLAIMS_FILE, {})
        data[t["id"]] = {"title": t["title"], "video": video, "reason": reason, "when": time.time()}
        write_json(CLAIMS_FILE, data)


def claims_in(text: str, *, video: str = "") -> list[str]:
    """Текст страницы претензий ролика → треки нашей библиотеки, которые там упомянуты (вносим в список)."""
    low = " ".join((text or "").lower().split())
    found = []
    for t in catalog()["tracks"]:
        if t["title"].lower() in low:
            mark_claimed(t["id"], video=video, reason="претензия YouTube (Content ID)")
            found.append(t["title"])
    return found


def usable(track_id: str) -> tuple[str, str]:
    """Трек для ролика: выбранный, а если на нём претензия YouTube — другой из той же группы (иначе любой
    чистый). → (id трека или «», пояснение для итогов)."""
    t = track(track_id)
    if not t:
        if not (track_id or "").strip():
            return "", ""  # «Без музыки»
        # выбранного трека больше нет в библиотеке (библиотеку заменили) — первый чистый, а не тишина
        clean = [x for x in catalog()["tracks"] if x["id"] not in claimed()]
        if not clean:
            return "", f"трека «{track_id}» нет в библиотеке — ролик без музыки"
        return clean[0]["id"], f"трека «{track_id}» больше нет в библиотеке — взял «{clean[0]['title']}» (выбери в карточке «Музыка»)"
    bad = claimed()
    if t["id"] not in bad:
        return t["id"], ""
    clean = [x for x in catalog()["tracks"] if x["id"] not in bad]
    alt = next((x for x in clean if x["group"] == t["group"]), clean[0] if clean else None)
    if alt is None:
        return "", f"музыка «{t['title']}» получила претензию YouTube, чистых треков не осталось — ролик без музыки"
    return alt["id"], (f"музыка «{t['title']}» получила претензию YouTube — взял «{alt['title']}» "
                       "(выбери трек в карточке «Музыка»)")
