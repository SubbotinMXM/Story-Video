"""Google Images через Chromium (Playwright), 3 вкладки параллельно.

Без API-ключа. Поиск → URL картинок → дальше обычный httpx download.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import threading
from concurrent.futures import Future
from urllib.parse import quote_plus, urlparse

from engine.core.logutil import log
from engine.slides.stocks_base import StockCandidate

_TABS = 3
_BAD_HOST = (
    "gstatic.com",
    "google.com",
    "google.ru",
    "googleapis.com",
    "ggpht.com",
    "schema.org",
    "youtube.com",
    "ytimg.com",
)
# Платные фотостоки: бесплатно отдают только превью с водяным знаком. Не выкидываем —
# для редких тем это бывает заметная часть находок, — но ставим в конец очереди:
# берутся, только если чистой подходящей картинки не нашлось.
_WATERMARK_HOST = (
    "alamy.com",
    "shutterstock.com",
    "istockphoto.com",
    "gettyimages.",
    "dreamstime.com",
    "depositphotos.com",
    "123rf.com",
    "ftcdn.net",  # Adobe Stock
    "stock.adobe.com",
    "bigstockphoto.com",
    "canstockphoto.",
    "agefotostock.com",
    "superstock.com",
    "masterfile.com",
    "pond5.com",
    "vectorstock.com",
    "colourbox.com",
    "photodune.net",
    "envato",
    "featurepics.com",
    "stockfresh.com",
    "fotolia.com",
    "mediastorehouse.com",
    "granger.com",
    "bridgemanimages.com",
    "akg-images.",
    "topfoto.co.uk",
    "photoshelter.com",
)


def is_watermarked(url: str) -> bool:
    low = (url or "").lower()
    return any(h in low for h in _WATERMARK_HOST)


_IMG_RE = re.compile(
    r"https://[^\s\"'<>]+?\.(?:jpe?g|png|webp)(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


class GoogleImagesClient:
    name = "google"

    def __init__(self, *, tabs: int = _TABS) -> None:
        self._tabs = max(1, min(8, int(tabs)))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._closed = False
        self._failed = False
        self._fail_reason = ""
        self._sem: asyncio.Semaphore | None = None
        self._browser = None
        self._context = None
        self._start_lock = threading.Lock()

    def close(self) -> None:
        self._closed = True
        loop = self._loop
        if loop is None:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
            fut.result(timeout=15)
        except Exception as exc:
            log(f"Google Images close: {exc}")
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._loop = None
        self._thread = None

    @property
    def available(self) -> bool:
        return not self._closed and not self._failed

    def search(
        self,
        query: str,
        *,
        prefer_video: bool = False,
        per_page: int = 10,
    ) -> list[StockCandidate]:
        if prefer_video:
            return []
        q = (query or "").strip()
        if not q or self._closed:
            return []
        try:
            self._ensure_loop()
        except Exception as exc:
            log(f"Google Images: Playwright не стартовал ({exc})")
            return []
        if self._failed or self._loop is None:
            return []
        fut: Future = asyncio.run_coroutine_threadsafe(
            self._search_async(q, max(1, min(40, int(per_page)))),
            self._loop,
        )
        try:
            return fut.result(timeout=120)
        except Exception as exc:
            log(f"Google Images search fail ({q!r}): {exc}")
            return []

    def _ensure_loop(self) -> None:
        with self._start_lock:
            if self._failed:
                raise RuntimeError(self._fail_reason or "Google Images unavailable")
            if self._loop is not None and self._ready.is_set():
                return
            if self._thread and self._thread.is_alive():
                if not self._ready.wait(timeout=60):
                    raise RuntimeError("Google Images loop timeout")
                if self._failed:
                    raise RuntimeError(self._fail_reason or "Google Images unavailable")
                return
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._run_loop, name="google-images", daemon=True
            )
            self._thread.start()
            if not self._ready.wait(timeout=60):
                self._failed = True
                self._fail_reason = "Google Images loop timeout"
                raise RuntimeError(self._fail_reason)
            if self._failed:
                raise RuntimeError(self._fail_reason or "Google Images unavailable")

    def _run_loop(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self._failed = True
            self._fail_reason = "нет пакета playwright"
            log("Нет playwright. Перезапусти ./run.sh (поставится сам).")
            self._ready.set()
            return

        async def _boot() -> None:
            self._pw = await async_playwright().start()
            launch_kwargs = {
                "headless": True,
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            # Системный Chrome реже ловит /sorry, чем bundled chromium.
            try:
                self._browser = await self._pw.chromium.launch(
                    channel="chrome", **launch_kwargs
                )
                log("Google Images: канал chrome")
            except Exception:
                self._browser = await self._pw.chromium.launch(**launch_kwargs)
                log("Google Images: bundled chromium")
            self._context = await self._browser.new_context(
                user_agent=_UA,
                locale="en-US",
                viewport={"width": 1400, "height": 900},
            )
            self._sem = asyncio.Semaphore(self._tabs)

        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_boot())
            self._ready.set()
            loop.run_forever()
        except Exception as exc:
            self._failed = True
            self._fail_reason = str(exc)
            log(f"Google Images loop crash: {exc}")
            self._ready.set()
        finally:
            try:
                if not loop.is_closed():
                    loop.run_until_complete(self._shutdown())
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None

    async def _shutdown(self) -> None:
        ctx = self._context
        browser = self._browser
        pw = getattr(self, "_pw", None)
        self._context = None
        self._browser = None
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:
                pass
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass

    async def _search_async(self, query: str, per_page: int) -> list[StockCandidate]:
        if self._context is None or self._sem is None:
            return []
        async with self._sem:
            page = await self._context.new_page()
            try:
                urls = await self._scrape(page, query, per_page)
            finally:
                await page.close()
        out: list[StockCandidate] = []
        seen: set[str] = set()
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            out.append(
                StockCandidate(
                    provider=self.name,
                    kind="photo",
                    id=_url_id(url),
                    query=query,
                    preview_url=url,
                    download_url=url,
                    meta={"watermark": True} if is_watermarked(url) else {},
                )
            )
            if len(out) >= per_page:
                break
        return out

    async def _scrape(self, page, query: str, limit: int) -> list[str]:
        urls = await _scrape_google(page, query, limit)
        if len(urls) >= max(3, limit // 3):
            return urls
        log(f"Google Images пусто/капча ({query!r}) → Bing")
        bing = await _scrape_bing(page, query, limit)
        urls = _uniq_good(urls + bing)
        if len(urls) >= max(3, limit // 3):
            return urls
        log(f"Bing мало ({query!r}) → DuckDuckGo")
        ddg = await asyncio.to_thread(_scrape_ddg_http, query, limit)
        return _uniq_good(urls + ddg)[: limit * 2]


async def _scrape_google(page, query: str, limit: int) -> list[str]:
    url = (
        "https://www.google.com/search?"
        f"q={quote_plus(query)}&tbm=isch&hl=en&safe=active"
    )
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    if "sorry" in (page.url or "") or "/sorry/" in (page.url or ""):
        log("Google Images: captcha/sorry")
        return []
    await _dismiss_consent(page)
    for _ in range(6):
        await page.mouse.wheel(0, 2800)
        await page.wait_for_timeout(350)
    html = await page.content()
    if "unusual traffic" in html.lower() or "captcha" in html.lower():
        return []
    urls = _extract_urls(html)
    if len(urls) < limit:
        thumbs = page.locator("div[data-id], a.wXeWr, div.isv-r")
        n = min(await thumbs.count(), max(limit, 12))
        for i in range(n):
            try:
                await thumbs.nth(i).click(timeout=1500)
                await page.wait_for_timeout(250)
                big = page.locator(
                    "img.sFlh5c, img.n3VNCb, img.iPVvYb, img[jsname='kn3ccd']"
                )
                if await big.count():
                    src = await big.first.get_attribute("src")
                    if src and src.startswith("http"):
                        urls.append(src)
            except Exception:
                continue
            if len(_uniq_good(urls)) >= limit:
                break
    return _uniq_good(urls)


async def _scrape_bing(page, query: str, limit: int) -> list[str]:
    url = f"https://www.bing.com/images/search?q={quote_plus(query)}&form=HDRSC2"
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await _dismiss_consent(page)
    for _ in range(5):
        await page.mouse.wheel(0, 2800)
        await page.wait_for_timeout(300)
    html = (await page.content()).replace("&quot;", '"').replace("&#34;", '"')
    urls = _extract_urls(html)
    urls.extend(re.findall(r'"murl"\s*:\s*"(https://[^"]+)"', html))
    urls.extend(re.findall(r'"imgurl"\s*:\s*"(https://[^"]+)"', html))
    # data-murl на плитках
    try:
        murls = await page.eval_on_selector_all(
            "[murl], [data-murl], a.iusc",
            """els => els.map(e => {
                const a = e.getAttribute('murl') || e.getAttribute('data-murl');
                if (a) return a;
                try {
                    const m = JSON.parse(e.getAttribute('m') || '{}');
                    return m.murl || m.imgur || '';
                } catch (err) { return ''; }
            }).filter(Boolean)""",
        )
        urls.extend(murls or [])
    except Exception:
        pass
    return _uniq_good(urls)


def _scrape_ddg_http(query: str, limit: int) -> list[str]:
    """Запасной путь без браузера — i.js DuckDuckGo."""
    try:
        import httpx
    except ImportError:
        return []
    q = (query or "").strip()
    if not q:
        return []
    headers = {
        "User-Agent": _UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    try:
        with httpx.Client(
            timeout=30.0, trust_env=False, follow_redirects=True, headers=headers
        ) as http:
            # актуальный способ: POST на duckduckgo.com/i.js через vqd из html
            home = http.get(
                "https://duckduckgo.com/",
                params={"q": q, "iax": "images", "ia": "images"},
            )
            vqd = _ddg_vqd(home.text)
            if not vqd:
                return []
            resp = http.get(
                "https://duckduckgo.com/i.js",
                params={
                    "l": "us-en",
                    "o": "json",
                    "q": q,
                    "vqd": vqd,
                    "f": ",,,,,",
                    "p": "1",
                },
                headers={**headers, "Referer": "https://duckduckgo.com/"},
            )
            if resp.status_code >= 400:
                log(f"DDG i.js HTTP {resp.status_code}")
                return []
            data = resp.json()
    except Exception as exc:
        log(f"DDG images fail: {exc}")
        return []
    urls: list[str] = []
    for item in (data or {}).get("results") or []:
        if not isinstance(item, dict):
            continue
        image = str(item.get("image") or "")
        if image.startswith("http"):
            urls.append(image)
        if len(urls) >= limit * 2:
            break
    return _uniq_good(urls)


def _ddg_vqd(html: str) -> str:
    for pat in (
        r'vqd="([^"]+)"',
        r"vqd='([^']+)'",
        r"vqd=([\d-]+)&",
        r'"vqd":"([^"]+)"',
    ):
        m = re.search(pat, html or "")
        if m:
            return m.group(1)
    return ""


async def _dismiss_consent(page) -> None:
    for text in (
        "Accept all",
        "I agree",
        "Accept",
        "Принять все",
        "Согласен",
        "Alle akzeptieren",
    ):
        try:
            btn = page.get_by_role("button", name=text)
            if await btn.count():
                await btn.first.click(timeout=1200)
                await page.wait_for_timeout(300)
                return
        except Exception:
            continue
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass


def _extract_urls(html: str) -> list[str]:
    return _IMG_RE.findall(html or "")


def _uniq_good(urls: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        url = (
            str(raw or "")
            .replace("\\u003d", "=")
            .replace("\\u0026", "&")
            .replace("&amp;", "&")
            .rstrip("\\")
        )
        if "&quot;" in url or '"' in url:
            url = url.split("&quot;")[0].split('"')[0]
        if not url.startswith("http"):
            continue
        low = url.lower()
        if any(b in low for b in _BAD_HOST):
            continue
        if "encrypted-tbn" in low or "tse" in low and "bing.net" in low:
            continue
        if len(url) < 20 or len(url) > 2000:
            continue
        host = urlparse(url).netloc.lower()
        if not host or "google" in host:
            continue
        path = urlparse(url).path.lower()
        if path.endswith((".html", ".htm", ".php", ".asp", ".aspx")):
            continue
        if "/listing/" in path:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    # чистые — первыми, с водяным знаком — в конце (сортировка устойчивая)
    return sorted(out, key=is_watermarked)


def _url_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8", errors="replace")).hexdigest()[:16]
