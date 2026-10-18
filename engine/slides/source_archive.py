"""Archive.org — изображения/короткие видео без API key."""

from __future__ import annotations

from urllib.parse import quote

import httpx

from engine.core.logutil import log
from engine.slides.stocks_base import StockCandidate


class ArchiveOrgClient:
    name = "archive"

    def __init__(self) -> None:
        self._http = httpx.Client(
            timeout=60.0,
            trust_env=False,
            follow_redirects=True,
            headers={"User-Agent": "youtube-video-factory/0.1"},
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
        prefer_video: bool = True,
        per_page: int = 8,
    ) -> list[StockCandidate]:
        q = (query or "").strip()
        if not q:
            return []
        out: list[StockCandidate] = []
        if prefer_video:
            out.extend(self._search_media(q, mediatype="movies", per_page=per_page))
        if len(out) < 3:
            out.extend(self._search_media(q, mediatype="image", per_page=per_page))
        return out[:per_page]

    def _search_media(
        self, query: str, *, mediatype: str, per_page: int
    ) -> list[StockCandidate]:
        q = (
            f"({query}) AND mediatype:{mediatype} "
            f"AND (format:(JPEG OR PNG OR GIF OR MPEG4 OR h.264 OR MP4))"
        )
        try:
            resp = self._http.get(
                "https://archive.org/advancedsearch.php",
                params=[
                    ("q", q),
                    ("fl[]", "identifier"),
                    ("fl[]", "title"),
                    ("rows", str(per_page)),
                    ("page", "1"),
                    ("output", "json"),
                    ("sort[]", "downloads desc"),
                ],
            )
            resp.raise_for_status()
            docs = ((resp.json() or {}).get("response") or {}).get("docs") or []
        except Exception as exc:
            log(f"Archive search fail: {exc}")
            return []

        out: list[StockCandidate] = []
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            ident = str(doc.get("identifier") or "").strip()
            if not ident:
                continue
            cand = self._candidate_from_item(ident, query, mediatype)
            if cand:
                out.append(cand)
        return out

    def _candidate_from_item(
        self, ident: str, query: str, mediatype: str
    ) -> StockCandidate | None:
        try:
            meta = self._http.get(f"https://archive.org/metadata/{ident}").json()
        except Exception:
            return None
        files = meta.get("files") or []
        preview = ""
        download = ""
        kind = "photo"
        w = h = 0
        if mediatype == "movies":
            kind = "video"
            for f in files:
                name = str(f.get("name") or "")
                low = name.lower()
                if low.endswith((".mp4", ".m4v")) and int(f.get("size") or 0) < 80_000_000:
                    download = f"https://archive.org/download/{ident}/{quote(name)}"
                    break
            # thumbnail
            for f in files:
                name = str(f.get("name") or "")
                if "__ia_thumb.jpg" in name or name.endswith(".thumb.jpg"):
                    preview = f"https://archive.org/download/{ident}/{quote(name)}"
                    break
            if not preview and download:
                preview = f"https://archive.org/services/img/{ident}"
        else:
            for f in files:
                name = str(f.get("name") or "")
                low = name.lower()
                if low.endswith((".jpg", ".jpeg", ".png")) and int(f.get("size") or 0) > 20_000:
                    download = f"https://archive.org/download/{ident}/{quote(name)}"
                    preview = download
                    break
            if not preview:
                preview = f"https://archive.org/services/img/{ident}"
                download = preview

        if not download or not preview:
            return None
        return StockCandidate(
            provider=self.name,
            kind=kind,  # type: ignore[arg-type]
            id=ident[:80],
            query=query,
            preview_url=preview,
            download_url=download,
            width=w,
            height=h,
            meta={"archive_id": ident},
        )
