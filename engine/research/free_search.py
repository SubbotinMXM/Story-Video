"""Поиск роликов на YouTube без квоты Google: страница поиска через yt-dlp (запасной путь, когда квота search.list кончилась).

Фильтры те же, что у API-поиска: длительность (4–20 мин / от 20 мин), порядок (релевантность / просмотры), за последний
год. Точную дату и все цифры потом даёт дешёвый videos.list (1 единица на 50 роликов) — как и при обычном поиске.
"""

from __future__ import annotations

import base64
import urllib.parse

from engine.core.logutil import log

# параметр sp страницы поиска YouTube (protobuf): порядок, дата, тип, длительность
_ORDER = {"relevance": 0, "viewCount": 3}
_DURATION = {"medium": 3, "long": 2}  # 4–20 мин, от 20 мин
_THIS_YEAR, _VIDEO = 5, 1
LIMIT = 40


def sp_param(order: str, duration: str) -> str:
    filt = bytes([0x08, _THIS_YEAR, 0x10, _VIDEO, 0x18, _DURATION.get(duration, 3)])
    raw = (bytes([0x08, _ORDER[order]]) if _ORDER.get(order) else b"") + bytes([0x12, len(filt)]) + filt
    return urllib.parse.quote(base64.b64encode(raw).decode())


def search_ids(query: str, order: str, duration: str, *, region: str = "", limit: int = LIMIT) -> list[str]:
    """id роликов по запросу. Сбой (YouTube поменял страницу, нет сети) — пусто, а не падение."""
    import yt_dlp

    url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}&sp={sp_param(order, duration)}"
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True, "noprogress": True,
            "playlistend": limit, **({"geo_bypass_country": region} if region else {})}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        log(f"Ниши: бесплатный поиск «{query}» не удался ({str(exc)[:120]})")
        return []
    return [str(e["id"]) for e in (info or {}).get("entries") or [] if e and e.get("id")]
