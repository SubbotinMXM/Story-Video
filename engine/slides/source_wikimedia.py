"""Wikimedia Commons — фото без API key."""

from __future__ import annotations

import httpx

from engine.core.logutil import log
from engine.slides.stocks_base import StockCandidate

API = "https://commons.wikimedia.org/w/api.php"


class WikimediaClient:
    name = "wikimedia"

    def __init__(self) -> None:
        self._http = httpx.Client(
            timeout=60.0,
            trust_env=False,
            follow_redirects=True,
            headers={"User-Agent": "youtube-video-factory/0.1 (media search)"},
        )

    def close(self) -> None:
        self._http.close()

    @property
    def available(self) -> bool:
        return True

    def search(
        self,
        query: str,
        *,
        prefer_video: bool = False,  # noqa: ARG002
        per_page: int = 10,
    ) -> list[StockCandidate]:
        q = (query or "").strip()
        if not q:
            return []
        params = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": f"filetype:bitmap|drawing {q}",
            "gsrlimit": str(per_page),
            "gsrnamespace": "6",  # File:
            "prop": "imageinfo",
            "iiprop": "url|size|mime",
            "iiurlwidth": "640",
        }
        try:
            resp = self._http.get(API, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log(f"Wikimedia search fail: {exc}")
            return []

        pages = ((data or {}).get("query") or {}).get("pages") or {}
        out: list[StockCandidate] = []
        for page in pages.values():
            if not isinstance(page, dict):
                continue
            info = (page.get("imageinfo") or [{}])[0]
            if not isinstance(info, dict):
                continue
            mime = str(info.get("mime") or "")
            if not mime.startswith("image/"):
                continue
            preview = str(info.get("thumburl") or info.get("url") or "")
            download = str(info.get("url") or "")
            if not preview or not download:
                continue
            pid = str(page.get("pageid") or page.get("title") or download)
            out.append(
                StockCandidate(
                    provider=self.name,
                    kind="photo",
                    id=pid.replace("/", "_")[:80],
                    query=q,
                    preview_url=preview,
                    download_url=download,
                    width=int(info.get("width") or 0),
                    height=int(info.get("height") or 0),
                )
            )
        return out
