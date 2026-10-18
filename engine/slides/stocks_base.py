"""Общие типы стоков и ротация API-ключей."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import httpx

from engine.core.logutil import log

MediaKind = Literal["video", "photo"]


@dataclass
class StockCandidate:
    provider: str
    kind: MediaKind
    id: str
    query: str
    preview_url: str
    download_url: str
    width: int = 0
    height: int = 0
    duration: float | None = None
    score: float = 0.0
    meta: dict = field(default_factory=dict)


class KeyPool:
    """Один ключ, пока живой. На лимите — следующий, после последнего снова первый."""

    def __init__(self, keys: list[str]) -> None:
        cleaned = []
        seen: set[str] = set()
        for k in keys:
            k = (k or "").strip()
            if k and k not in seen:
                seen.add(k)
                cleaned.append(k)
        self._keys = cleaned
        self._dead: set[str] = set()
        self._i = 0
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return any(k not in self._dead for k in self._keys)

    def next(self) -> str | None:
        with self._lock:
            live = [k for k in self._keys if k not in self._dead]
            if not live:
                return None
            self._i %= len(live)
            return live[self._i]

    def rotate(self, key: str) -> None:
        """Лимит на ключе: берём следующий по кругу, этот не выбрасываем."""
        with self._lock:
            live = [k for k in self._keys if k not in self._dead]
            if not live:
                return
            if key in live:
                self._i = (live.index(key) + 1) % len(live)
            else:
                self._i = (self._i + 1) % len(live)

    @property
    def size(self) -> int:
        return len(self._keys)

    def mark_bad(self, key: str) -> None:
        """Ключ недействителен, больше не берём."""
        if key:
            with self._lock:
                self._dead.add(key)


_SPLIT = re.compile(r"[\n,;]+")


def parse_keys(raw: object) -> list[str]:
    """Строка/список ключей → уникальный список."""
    if raw is None:
        return []
    if isinstance(raw, list):
        parts = [str(x) for x in raw]
    else:
        parts = _SPLIT.split(str(raw))
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        k = p.strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def height_penalty(h: int) -> int:
    """Меньше — лучше. Порядок: 1080p → 1440p → 720p → 4K (тяжёлый) → ниже 720p."""
    if h <= 0:
        return 100_000
    if h >= 1080:
        return (h - 1080) * 4
    return (1080 - h) * 10


def get_with_keys(
    http: httpx.Client,
    pool: KeyPool,
    name: str,
    url: str,
    request_for: Callable[[str], tuple[dict, dict]],
) -> dict | None:
    """GET с перебором ключей. Ключ упёрся в лимит (403/429) или недействителен (401) —
    тот же запрос сразу со следующим ключом, а не пустой результат.

    request_for(key) -> (params, headers) для этого ключа.
    """
    for _ in range(max(1, pool.size)):
        key = pool.next()
        if not key:
            return None
        params, headers = request_for(key)
        try:
            resp = http.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            log(f"{name} network: {exc}")
            return None
        if resp.status_code == 401:
            log(f"{name} 401 — ключ недействителен, беру следующий")
            pool.mark_bad(key)
            continue
        if resp.status_code in (403, 429):
            log(f"{name} {resp.status_code} — лимит ключа, беру следующий")
            pool.rotate(key)
            continue
        if resp.status_code >= 400:
            log(f"{name} ERR {resp.status_code}: {resp.text[:200]}")
            return None
        try:
            data = resp.json()
        except Exception:
            return None
        return data if isinstance(data, dict) else None
    return None
