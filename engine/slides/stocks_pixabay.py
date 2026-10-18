"""Pixabay photos + videos."""

from __future__ import annotations

import httpx

from engine.core.logutil import log
from engine.slides.stocks_base import KeyPool, StockCandidate, get_with_keys, height_penalty


class PixabayClient:
    name = "pixabay"
    PHOTO_URL = "https://pixabay.com/api/"
    VIDEO_URL = "https://pixabay.com/api/videos/"

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
            {"q": query, "per_page": per_page, "orientation": "horizontal"},
        )
        if not data:
            return []
        out: list[StockCandidate] = []
        for item in data.get("hits") or []:
            if not isinstance(item, dict):
                continue
            vid = str(item.get("id") or "")
            videos = item.get("videos") or {}
            # ближе к 1080p: large обычно 1920x1080, medium 1280x720, small 960x540
            blocks = [
                b
                for b in (videos.get(q) for q in ("large", "medium", "small", "tiny"))
                if isinstance(b, dict) and b.get("url")
            ]
            chosen = min(blocks, key=lambda b: height_penalty(int(b.get("height") or 0)), default=None)
            if not chosen:
                continue
            preview = str(item.get("userImageURL") or "")
            pic_id = str(item.get("picture_id") or "")
            if pic_id:
                preview = f"https://i.vimeocdn.com/video/{pic_id}_640x360.jpg"
            download = str(chosen.get("url") or "")
            if not download:
                continue
            # нельзя подсовывать mp4 как preview — CLIP падает
            if not preview or not preview.lower().split("?")[0].endswith(
                (".jpg", ".jpeg", ".png", ".webp")
            ):
                if not pic_id:
                    continue
            out.append(
                StockCandidate(
                    provider=self.name,
                    kind="video",
                    id=vid,
                    query=query,
                    preview_url=preview,
                    download_url=download,
                    width=int(chosen.get("width") or 0),
                    height=int(chosen.get("height") or 0),
                    duration=float(item.get("duration") or 0) or None,
                )
            )
        return out

    def _search_photos(self, query: str, *, per_page: int) -> list[StockCandidate]:
        data = self._get(
            self.PHOTO_URL,
            {
                "q": query,
                "image_type": "photo",
                "per_page": per_page,
                "orientation": "horizontal",
            },
        )
        if not data:
            return []
        out: list[StockCandidate] = []
        for item in data.get("hits") or []:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("id") or "")
            preview = str(item.get("previewURL") or item.get("webformatURL") or "")
            download = str(
                item.get("largeImageURL")
                or item.get("fullHDURL")
                or item.get("webformatURL")
                or ""
            )
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
                    width=int(item.get("imageWidth") or 0),
                    height=int(item.get("imageHeight") or 0),
                )
            )
        return out

    def _get(self, url: str, params: dict) -> dict | None:
        return get_with_keys(self._http, self.keys, "Pixabay", url, lambda key: ({**params, "key": key}, {}))

