"""Яндекс.Картинки — обычный HTTP, без браузера и ключа.

Для знаменитостей (особенно русскоязычных) выдача богатая: по запросу «имя фамилия» — десятки настоящих
фото с исходными размерами. Google просит капчу, Bing рвёт соединения под нагрузкой — этот источник их страхует.
Капча/блокировка Яндекса: источник отключается на время, поиск идёт по остальным.
"""

from __future__ import annotations

import hashlib
import html
import re
import threading
import time

import httpx

from engine.core.logutil import log
from engine.slides.stocks_base import StockCandidate

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/122.0.0.0 Safari/537.36")
PAGE_SIZE = 30
MAX_PAGES = 3
MIN_SIDE = 400
_PAUSE_SEC = 15 * 60  # после капчи не мучаем Яндекс
_GAP_SEC = 0.5  # между запросами — не быстрее, иначе капча
_ORIGIN = re.compile(r'"origin":\{"w":(\d+),"h":(\d+),"url":"(https?:[^"]+)"')
_HREF = re.compile(r'"img_href":"(https?:[^"]+)"')


class YandexImagesClient:
    name = "yandex"

    def __init__(self) -> None:
        self._http = httpx.Client(timeout=25.0, trust_env=False, follow_redirects=True,
                                  headers={"User-Agent": _UA, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5"})
        self._lock = threading.Lock()
        self._last = 0.0
        self._off_until = 0.0
        self._captchas = 0

    def close(self) -> None:
        self._http.close()

    @property
    def available(self) -> bool:
        return time.monotonic() >= self._off_until

    def _get(self, query: str, page: int) -> str | None:
        with self._lock:  # запросы по одному и не чаще раза в полсекунды
            wait = self._last + _GAP_SEC - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
        try:
            resp = self._http.get("https://yandex.ru/images/search", params={"text": query, "p": page, "isize": "large"})
        except Exception as exc:
            log(f"Яндекс.Картинки: {str(exc)[:100]}")
            return None
        text = resp.text
        if resp.status_code in (403, 429) or "showcaptcha" in str(resp.url) or "captcha" in text[:4000].lower():
            self._captchas += 1
            if self._captchas >= 2:
                self._off_until = time.monotonic() + _PAUSE_SEC
                self._captchas = 0
                log(f"Яндекс.Картинки: капча — {_PAUSE_SEC // 60} мин ищу в других источниках")
            return None
        self._captchas = 0
        return text if resp.status_code == 200 else None

    def search(self, query: str, *, prefer_video: bool = False, per_page: int = 30) -> list[StockCandidate]:
        q = (query or "").strip()
        if prefer_video or not q or not self.available:
            return []
        from engine.slides.source_google import is_watermarked

        out: list[StockCandidate] = []
        seen: set[str] = set()
        for page in range(min(MAX_PAGES, max(1, -(-per_page // PAGE_SIZE)))):
            text = self._get(q, page)
            if not text:
                break
            body = html.unescape(text).replace("\\/", "/")
            found = [(int(w), int(h), u) for w, h, u in _ORIGIN.findall(body)]
            if not found:  # другой разметки нет — берём просто ссылки без размеров
                found = [(0, 0, u) for u in _HREF.findall(body)]
            if not found:
                break
            for w, h, url in found:
                if url in seen or "avatars.yandex" in url or "yandex.net/get" in url:
                    continue
                if w and h and min(w, h) < MIN_SIDE:
                    continue
                seen.add(url)
                out.append(StockCandidate(
                    provider=self.name, kind="photo", id="y" + hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()[:14], query=q, preview_url=url,
                    download_url=url, width=w, height=h, meta={"watermark": True} if is_watermarked(url) else {}))
            if len(out) >= per_page:
                break
        return out[:per_page]
