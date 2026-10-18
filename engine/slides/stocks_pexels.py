"""Pexels photos + videos."""

from __future__ import annotations

import httpx

from engine.core.logutil import log
from engine.slides.stocks_base import KeyPool, StockCandidate, get_with_keys, height_penalty


class PexelsClient:
    name = "pexels"
    PHOTO_URL = "https://api.pexels.com/v1/search"
    VIDEO_URL = "https://api.pexels.com/videos/search"

    def __init__(self, keys: KeyPool) -> None:
        self.keys = keys
        self._http = httpx.Client(timeout=60.0, trust_env=False, follow_redirects=True)

    def close(self) -> None:
        self._http.close()

    def search(
        self,
        query: str,
        *,
        prefer_video: bool = True,
        per_page: int = 10,
    ) -> list[StockCandidate]:
        q = (query or "").strip()
        if not q or not self.keys.available:
            return []
        out: list[StockCandidate] = []
        if prefer_video:
            out.extend(self._search_videos(q, per_page=per_page))
        if len(out) < 3:
            out.extend(self._search_photos(q, per_page=per_page))
        return out

    def _search_videos(self, query: str, *, per_page: int) -> list[StockCandidate]:
        data = self._get(
            self.VIDEO_URL,
            {"query": query, "per_page": per_page, "orientation": "landscape"},
        )
        if not data:
            return []
        out: list[StockCandidate] = []
        for item in data.get("videos") or []:
            if not isinstance(item, dict):
                continue
            vid = str(item.get("id") or "")
            preview = str(item.get("image") or "")
            files = item.get("video_files") or []
            download, w, h = _pick_video_file(files)
            if not download or not preview:
                continue
            dur = item.get("duration")
            out.append(
                StockCandidate(
                    provider=self.name,
                    kind="video",
                    id=vid,
                    query=query,
                    preview_url=preview,
                    download_url=download,
                    width=w,
                    height=h,
                    duration=float(dur) if dur is not None else None,
                )
            )
        return out

    def _search_photos(self, query: str, *, per_page: int) -> list[StockCandidate]:
        data = self._get(
            self.PHOTO_URL,
            {"query": query, "per_page": per_page, "orientation": "landscape"},
        )
        if not data:
            return []
        out: list[StockCandidate] = []
        for item in data.get("photos") or []:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("id") or "")
            src = item.get("src") or {}
            preview = str(src.get("medium") or src.get("small") or "")
            download = str(src.get("large2x") or src.get("large") or src.get("original") or "")
            if not preview or not download:
                continue
            out.append(
                StockCandidate(
                    provider=self.name,
                    kind="photo",
                    id=pid,
                    query=query,
                    preview_url=preview,
                    download_url=download,
                    width=int(item.get("width") or 0),
                    height=int(item.get("height") or 0),
                )
            )
        return out

    def _get(self, url: str, params: dict) -> dict | None:
        return get_with_keys(self._http, self.keys, "Pexels", url, lambda key: (params, {"Authorization": key}))

def _pick_video_file(files: list) -> tuple[str, int, int]:
    """Берём файл ближе к 1080p: 4K тяжёлый, 720p и ниже на 1080p-рендере мылит."""
    scored: list[tuple[int, str, int, int]] = []
    for f in files:
        if not isinstance(f, dict):
            continue
        link = str(f.get("link") or "").strip()
        if not link:
            continue
        w = int(f.get("width") or 0)
        h = int(f.get("height") or 0)
        # предпочтение mp4
        if "mp4" not in link.lower() and f.get("file_type") not in (None, "video/mp4"):
            continue
        scored.append((height_penalty(h), link, w, h))
    if not scored:
        return "", 0, 0
    scored.sort(key=lambda x: x[0])
    _, link, w, h = scored[0]
    return link, w, h

