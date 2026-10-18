"""Unsplash photos only."""

from __future__ import annotations

import httpx

from engine.core.logutil import log
from engine.slides.stocks_base import KeyPool, StockCandidate, get_with_keys


class UnsplashClient:
    name = "unsplash"
    SEARCH_URL = "https://api.unsplash.com/search/photos"

    def __init__(self, keys: KeyPool) -> None:
        self.keys = keys
        self._http = httpx.Client(timeout=60.0, trust_env=False, follow_redirects=True)

    def close(self) -> None:
        self._http.close()

    def search(
        self,
        query: str,
        *,
        prefer_video: bool = True,  # noqa: ARG002 — API только фото
        per_page: int = 10,
    ) -> list[StockCandidate]:
        q = (query or "").strip()
        if not q or not self.keys.available:
            return []
        data = get_with_keys(
            self._http,
            self.keys,
            "Unsplash",
            self.SEARCH_URL,
            lambda key: (
                {"query": q, "per_page": per_page},
                {"Authorization": f"Client-ID {key}", "Accept-Version": "v1"},
            ),
        )
        if data is None:
            return []
        out: list[StockCandidate] = []
        for item in (data or {}).get("results") or []:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("id") or "")
            urls = item.get("urls") or {}
            preview = str(urls.get("small") or urls.get("thumb") or "")
            download = str(urls.get("regular") or urls.get("full") or "")
            if not preview or not download:
                continue
            out.append(
                StockCandidate(
                    provider=self.name,
                    kind="photo",
                    id=pid,
                    query=q,
                    preview_url=preview,
                    download_url=download,
                    width=int(item.get("width") or 0),
                    height=int(item.get("height") or 0),
                )
            )
        return out
