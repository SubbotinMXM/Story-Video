"""Фото героя из его статьи в Википедии (и Wikimedia Commons).

Главное фото статьи — почти наверняка сам герой: по нему опознаётся лицо, остальные снимки сверяются с ним
(защита от тёзок). Остальные картинки статьи — тоже кандидаты (лицо проверяется как у всех).
Без ключа. Википедия просит вежливый User-Agent и отвечает 429 при частых запросах — повторяем с паузой.
"""

from __future__ import annotations

import hashlib
import re
import time

import httpx

from engine.core.logutil import log
from engine.slides.stocks_base import StockCandidate

_UA = "youtube-video-factory/0.2 (celebrity photo search; contact: local app)"
_SKIP = re.compile(r"(flag|logo|icon|signature|autograph|map|commons-|wikiquote|wiktionary|ambox|question_book|"
                   r"symbol|coat_of_arms|crystal|edit-|disambig|stub|poster|cover|screenshot|grave|tomb|монет|подпись)", re.I)
_EXT = (".jpg", ".jpeg", ".png", ".webp")


def _h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:14]


class WikipediaPhotosClient:
    name = "wikipedia"

    def __init__(self) -> None:
        self._http = httpx.Client(timeout=30.0, trust_env=False, follow_redirects=True, headers={"User-Agent": _UA})

    def close(self) -> None:
        self._http.close()

    @property
    def available(self) -> bool:
        return True

    def _api(self, lang: str, params: dict) -> dict:
        for attempt in range(3):
            try:
                r = self._http.get(f"https://{lang}.wikipedia.org/w/api.php", params={"format": "json", **params})
                if r.status_code == 429:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as exc:
                if attempt == 2:
                    log(f"Википедия: {str(exc)[:100]}")
                time.sleep(0.8)
        return {}

    def photos(self, name: str, *, limit: int = 40) -> tuple[list[StockCandidate], StockCandidate | None]:
        """→ (фото из статьи, главное фото статьи). Статьи нет — ([], None)."""
        langs = ("ru", "en") if re.search("[а-яё]", name, re.I) else ("en", "ru")
        for lang in langs:
            data = self._api(lang, {"action": "query", "generator": "search", "gsrsearch": name, "gsrlimit": 1,
                                    "prop": "pageimages|images", "piprop": "original", "imlimit": 60})
            pages = ((data.get("query") or {}).get("pages") or {}).values()
            page = next(iter(pages), None)
            if not page:
                continue
            title = str(page.get("title") or "")
            # название статьи должно содержать фамилию героя — иначе поиск вернул что-то другое
            last = name.split()[-1].lower() if name.split() else ""
            if last and last[:5] not in title.lower():
                continue
            lead = None
            orig = (page.get("original") or {}).get("source")
            if orig and str(orig).lower().endswith(_EXT):
                lead = StockCandidate(provider="wikipedia", kind="photo", id="wl_" + _h(orig), query=name,
                                      preview_url=orig, download_url=orig,
                                      width=int((page.get("original") or {}).get("width") or 0),
                                      height=int((page.get("original") or {}).get("height") or 0), meta={"hero_ref": True})
            files = [str(i.get("title") or "") for i in page.get("images") or []]
            files = [f for f in files if f.lower().endswith(_EXT) and not _SKIP.search(f)][:limit]
            out = self._file_urls(lang, files, name)
            if lead is not None:
                out = [c for c in out if c.download_url != lead.download_url]
            if lead is not None or out:
                return out, lead
        return [], None

    def _file_urls(self, lang: str, titles: list[str], query: str) -> list[StockCandidate]:
        out: list[StockCandidate] = []
        for k in range(0, len(titles), 40):
            data = self._api(lang, {"action": "query", "titles": "|".join(titles[k:k + 40]), "prop": "imageinfo",
                                    "iiprop": "url|size|mime"})
            for p in ((data.get("query") or {}).get("pages") or {}).values():
                info = (p.get("imageinfo") or [{}])[0]
                url = str(info.get("url") or "")
                if not url or not str(info.get("mime") or "").startswith("image/") or min(
                        int(info.get("width") or 0), int(info.get("height") or 0)) < 400:
                    continue
                out.append(StockCandidate(provider="wikipedia", kind="photo", id="w_" + _h(url), query=query,
                                          preview_url=url, download_url=url, width=int(info.get("width") or 0),
                                          height=int(info.get("height") or 0)))
        return out

    def search(self, query: str, *, prefer_video: bool = False, per_page: int = 10) -> list[StockCandidate]:
        """Совместимость с общим интерфейсом источников: query — имя героя."""
        if prefer_video:
            return []
        out, lead = self.photos(query, limit=per_page)
        return ([lead] if lead else []) + out
