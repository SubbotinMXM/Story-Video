"""TMDB (The Movie Database): фото людей кино/ТВ, кадры и фоны фильмов и сериалов.

Только картинки: у TMDB видео — ссылки на трейлеры YouTube (чужие, в Content ID), их не берём.
Ключ — «Ключ доступа к API» (длинный токен на eyJ…, идёт заголовком Bearer) или короткий «Ключ API» (параметр api_key).
TMDB просит указывать источник: «This product uses the TMDB API but is not endorsed or certified by TMDB».
Домены TMDB у части провайдеров подменяются на 127.0.0.1 — адрес берётся через DoH (engine/core/dnsfix).
"""

from __future__ import annotations

import hashlib
import re
import time

import httpx

from engine.core import dnsfix
from engine.core.logutil import log
from engine.slides.stocks_base import KeyPool, StockCandidate

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p"
MIN_SIDE = 720  # ниже — не берём (в ролике 1080p)
ATTRIBUTION = "This product uses the TMDB API but is not endorsed or certified by TMDB."
_NAME = re.compile(r"[^\w]+", re.U)


def _h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:14]


def _fold(text: str) -> str:
    return _NAME.sub(" ", (text or "").lower()).strip()


class TmdbClient:
    name = "tmdb"

    def __init__(self, keys: KeyPool) -> None:
        dnsfix.install()
        self.keys = keys
        self._http = httpx.Client(timeout=httpx.Timeout(20.0, connect=8.0), trust_env=False, follow_redirects=True)

    def close(self) -> None:
        self._http.close()

    @property
    def available(self) -> bool:
        return self.keys.available

    # ---------- запросы ----------

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        for attempt in range(3):
            key = self.keys.next()
            if not key:
                return None
            headers = {"accept": "application/json"}
            query = dict(params or {})
            if key.startswith("eyJ"):
                headers["Authorization"] = f"Bearer {key}"
            else:
                query["api_key"] = key
            try:
                r = self._http.get(f"{API}{path}", params=query, headers=headers)
            except httpx.HTTPError as exc:
                if attempt == 2:
                    log(f"TMDB: {str(exc)[:100]}")
                time.sleep(0.6 * (attempt + 1))
                continue
            if r.status_code == 429:
                time.sleep(min(5.0, float(r.headers.get("Retry-After") or 1.0)))
                continue
            if r.status_code == 401:
                self.keys.mark_bad(key)  # ключ недействителен — следующий
                continue
            if r.status_code != 200:
                return None
            try:
                return r.json()
            except ValueError:
                return None
        return None

    # ---------- кандидаты ----------

    def _cand(self, item: dict, query: str, title: str, *, role: str) -> StockCandidate | None:
        path = str(item.get("file_path") or "")
        w, h = int(item.get("width") or 0), int(item.get("height") or 0)
        if not path or min(w, h) < MIN_SIDE:
            return None
        return StockCandidate(
            provider="tmdb", kind="photo", id=f"t_{_h(path)}", query=query,
            preview_url=f"{IMG}/w500{path}", download_url=f"{IMG}/original{path}", width=w, height=h,
            meta={"title": title, "role": role, "vote": float(item.get("vote_average") or 0)},
        )

    @staticmethod
    def _best(items: list[dict], limit: int) -> list[dict]:
        return sorted(items, key=lambda i: -(float(i.get("vote_average") or 0) + float(i.get("vote_count") or 0) * 0.01))[:limit]

    def person_photos(self, name: str, *, limit: int = 40) -> list[StockCandidate]:
        """Фото человека: его портреты и кадры из фильмов, где он отмечен. Тёзки: берётся самый известный с точным именем."""
        data = self._get("/search/person", {"query": name, "include_adult": "false"})
        results = [r for r in (data or {}).get("results") or [] if isinstance(r, dict)]
        exact = [r for r in results if _fold(str(r.get("name") or "")) == _fold(name)]
        person = max(exact or results[:1], key=lambda r: float(r.get("popularity") or 0), default=None)
        if not person or (not exact and _fold(name).split()[-1:] != _fold(str(person.get("name") or "")).split()[-1:]):
            return []
        pid, label = person["id"], str(person.get("name") or name)
        out: list[StockCandidate] = []
        imgs = self._get(f"/person/{pid}/images") or {}
        out += [c for c in (self._cand(i, name, label, role="profile") for i in self._best(imgs.get("profiles") or [], limit)) if c]
        tagged = self._get(f"/person/{pid}/tagged_images") or {}
        out += [c for c in (self._cand(i, name, label, role="still") for i in self._best(tagged.get("results") or [], limit)) if c]
        return out[:limit]

    def title_images(self, kind: str, tid: int, query: str, title: str, *, limit: int) -> list[StockCandidate]:
        """Фоны (кадры) фильма или сериала."""
        data = self._get(f"/{kind}/{tid}/images", {"include_image_language": "en,null"}) or {}
        return [c for c in (self._cand(i, query, title, role="backdrop") for i in self._best(data.get("backdrops") or [], limit)) if c]

    def search(self, query: str, *, prefer_video: bool = False, per_page: int = 10) -> list[StockCandidate]:
        """Общий интерфейс источников. Видео нет. query — имя человека или название фильма/сериала."""
        q = " ".join((query or "").split())
        if prefer_video or not q or not self.keys.available:
            return []
        data = self._get("/search/multi", {"query": q, "include_adult": "false"})
        results = [r for r in (data or {}).get("results") or [] if isinstance(r, dict) and r.get("media_type") in ("person", "movie", "tv")]
        results.sort(key=lambda r: -float(r.get("popularity") or 0))
        out: list[StockCandidate] = []
        for r in results[:2]:
            kind = r["media_type"]
            title = str(r.get("title") or r.get("name") or "")
            if kind == "person":
                out += self.person_photos(title, limit=per_page)
            else:
                out += self.title_images(kind, int(r["id"]), q, title, limit=per_page)
        seen: set[str] = set()
        uniq = [c for c in out if not (c.id in seen or seen.add(c.id))]
        return uniq[: max(per_page, 6)]
