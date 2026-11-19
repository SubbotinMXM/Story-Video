"""Пинг ключей стоков."""

from __future__ import annotations

import time

import httpx

from engine.slides.stocks_base import parse_keys


def ping_stocks(
    *,
    pexels: str = "",
    pixabay: str = "",
    unsplash: str = "",
    youtube: str = "",
) -> str:
    lines: list[str] = []
    any_key = False
    with httpx.Client(timeout=30.0, trust_env=False, follow_redirects=True) as http:
        for name, raw, fn in (
            ("Pexels", pexels, _ping_pexels),
            ("Pixabay", pixabay, _ping_pixabay),
            ("Unsplash", unsplash, _ping_unsplash),
            ("YouTube", youtube, _ping_youtube),
        ):
            keys = parse_keys(raw)
            if not keys:
                lines.append(f"{name}: — (пусто)")
                continue
            any_key = True
            ok_n = 0
            errs: list[str] = []
            for i, key in enumerate(keys, 1):
                try:
                    fn(http, key)
                    ok_n += 1
                except Exception as exc:
                    errs.append(f"#{i}: {exc}")
            if ok_n == len(keys):
                lines.append(f"{name}: OK ({ok_n}/{len(keys)} ключей)")
            else:
                detail = "; ".join(errs[:3])
                lines.append(f"{name}: {ok_n}/{len(keys)} OK. {detail}")
    if not any_key:
        raise RuntimeError("Нет ни одного ключа стоков и YouTube")
    return "\n".join(lines)


def _ping_pexels(http: httpx.Client, key: str) -> None:
    t0 = time.monotonic()
    resp = http.get(
        "https://api.pexels.com/v1/search",
        params={"query": "cat", "per_page": "1"},
        headers={"Authorization": key},
    )
    ms = int((time.monotonic() - t0) * 1000)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} ({ms} ms)")


def _ping_pixabay(http: httpx.Client, key: str) -> None:
    t0 = time.monotonic()
    resp = http.get(
        "https://pixabay.com/api/",
        params={"key": key, "q": "cat", "per_page": "3"},
    )
    ms = int((time.monotonic() - t0) * 1000)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} ({ms} ms)")
    # Pixabay при кривом ключе часто 200 + "ERROR"
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if isinstance(data, dict) and str(data.get("error") or ""):
        raise RuntimeError(f"{data.get('error')} ({ms} ms)")


def _ping_unsplash(http: httpx.Client, key: str) -> None:
    t0 = time.monotonic()
    resp = http.get(
        "https://api.unsplash.com/search/photos",
        params={"query": "cat", "per_page": "1"},
        headers={"Authorization": f"Client-ID {key}"},
    )
    ms = int((time.monotonic() - t0) * 1000)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} ({ms} ms)")


def _ping_youtube(http: httpx.Client, key: str) -> None:
    """Проверка ключа за 1 единицу квоты (поиск стоил бы 100). Поиск считается отдельно: если приложение уже
    видело, что на этом ключе он исчерпан, — так и говорим, а не «OK»."""
    from datetime import datetime

    from engine.slides import youtube_errors as yerr

    t0 = time.monotonic()
    resp = http.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={"part": "id", "id": "dQw4w9WgXcQ", "key": key},
    )
    ms = int((time.monotonic() - t0) * 1000)
    if resp.status_code in (403, 429):
        _kind, why = yerr.classify(resp)
        raise RuntimeError(f"{why} ({ms} ms)")
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} ({ms} ms)")
    since = yerr.spent_since(key)
    if since:
        raise RuntimeError(f"ключ принимается, но поиск на нём исчерпан с {datetime.fromtimestamp(since):%H:%M} "
                           f"(обновится {yerr.reset_text()})")
