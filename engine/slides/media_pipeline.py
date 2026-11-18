"""Клиенты лицензионных источников и скачивание файлов."""

from __future__ import annotations

from typing import TYPE_CHECKING

import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from engine.core.cancel import CancelToken
from engine.core.logutil import log
from engine.slides.source_mix import MIN_HEIGHT
from engine.slides.source_archive import ArchiveOrgClient
from engine.slides.source_wikimedia import WikimediaClient
from engine.slides.stocks_base import KeyPool, StockCandidate, parse_keys
from engine.slides.stocks_pexels import PexelsClient
from engine.slides.stocks_pixabay import PixabayClient
from engine.slides.stocks_unsplash import UnsplashClient

ProgressCb = Callable[[float, str], None]

SEARCH_PER_SOURCE = 12
_DOWNLOAD_TIMEOUT = httpx.Timeout(45.0, connect=10.0, read=25.0)
_MAX_BYTES = 80_000_000


if TYPE_CHECKING:
    from engine.slides.ai_gen import AiConfig


@dataclass
class MediaSourcesConfig:
    pexels: list[str] = field(default_factory=list)
    pixabay: list[str] = field(default_factory=list)
    unsplash: list[str] = field(default_factory=list)
    google: bool = True
    youtube: list[str] = field(default_factory=list)

    @property
    def youtube_api_key(self) -> str:
        """Первый ключ YouTube (для проверок «есть ли YouTube»)."""
        return self.youtube[0] if self.youtube else ""


StockKeys = MediaSourcesConfig


@dataclass
class MediaRequest:
    run_dir: Path
    keys: MediaSourcesConfig
    copy_preview_to_desktop: bool = True
    seed: int | None = None
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    strict_sync: bool = False
    strict_full: bool = False
    # ролик про конкретных людей — на часть текста про каждого его фото (engine/slides/stars_media);
    # False — уже проверено (людей нет / фото не нашлось), подбор по режиму как обычно
    people: bool = True
    # свои доли источников {ai, stocks, youtube, google, wikimedia, archive}, %; None — по теме ролика
    source_mix: dict | None = None
    # AI-кадры (RoyalTechno): None — выключены, ролик только из живых кадров
    ai: "AiConfig | None" = None


@dataclass
class MediaStageResult:
    run_dir: Path
    timeline_file: Path
    preview_file: Path
    desktop_preview: Path | None
    selected: int
    skipped: int
    weak: int


class MediaError(RuntimeError):
    pass


def run_media_stage(
    req: MediaRequest,
    *,
    cancel: CancelToken | None = None,
    on_progress: ProgressCb | None = None,
) -> MediaStageResult:
    if req.people:
        # в любом режиме: ролик про людей — кадры по героям, а не по фразам и не вперемешку
        from dataclasses import replace

        from engine.slides.stars_media import collect_people_timeline

        done = collect_people_timeline(req, cancel=cancel, on_progress=on_progress)
        if done is not None:
            return done
        req = replace(req, people=False)
    if req.strict_sync:
        from engine.slides.strict_media import collect_strict_timeline

        return collect_strict_timeline(req, cancel=cancel, on_progress=on_progress)
    from engine.slides.theme_media import collect_theme_pool

    return collect_theme_pool(req, cancel=cancel, on_progress=on_progress)


def stock_keys_from_config(cfg: dict) -> MediaSourcesConfig:
    return MediaSourcesConfig(
        pexels=parse_keys(cfg.get("pexels_api_keys") or cfg.get("pexels_api_key")),
        pixabay=parse_keys(cfg.get("pixabay_api_keys") or cfg.get("pixabay_api_key")),
        unsplash=parse_keys(
            cfg.get("unsplash_api_keys") or cfg.get("unsplash_api_key")
        ),
        google=True,  # Google/Bing ищем всегда, доля зависит от темы (source_mix)
        youtube=parse_keys(cfg.get("youtube_api_keys") or cfg.get("youtube_api_key")),
    )



def _build_clients(cfg: MediaSourcesConfig) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if cfg.pexels:
        out["pexels"] = PexelsClient(KeyPool(cfg.pexels))
    if cfg.pixabay:
        out["pixabay"] = PixabayClient(KeyPool(cfg.pixabay))
    if cfg.unsplash:
        out["unsplash"] = UnsplashClient(KeyPool(cfg.unsplash))
    out["wikimedia"] = WikimediaClient()
    out["archive"] = ArchiveOrgClient()
    if cfg.google:
        try:
            from engine.slides.source_google import GoogleImagesClient

            out["google"] = GoogleImagesClient()
        except Exception as exc:
            log(f"Google Images недоступен: {exc}")
    if cfg.youtube:
        try:
            from engine.slides.source_youtube import YouTubeClient

            out["youtube"] = YouTubeClient(cfg.youtube, min_height=MIN_HEIGHT)
        except Exception as exc:
            log(f"YouTube недоступен: {exc}")
    return out


def _search_source(
    clients: dict[str, Any],
    source: str,
    query: str,
    *,
    prefer_video: bool,
    rng: random.Random,
) -> list[StockCandidate]:
    if source == "stocks":
        names = [n for n in ("pexels", "pixabay", "unsplash") if n in clients]
        if not names:
            return []
        rng.shuffle(names)
        out: list[StockCandidate] = []
        seen: set[tuple[str, str]] = set()
        for name in names:
            found = _safe_search(clients[name], query, prefer_video=prefer_video)
            for cand in found:
                # Unsplash отдаёт только фото: при поиске видео они не должны
                # занять все места и не пустить к Pexels/Pixabay
                if prefer_video != (cand.kind == "video"):
                    continue
                key = (cand.provider, cand.id)
                if key in seen:
                    continue
                seen.add(key)
                out.append(cand)
            if len(out) >= SEARCH_PER_SOURCE:
                break
        return out[:SEARCH_PER_SOURCE]

    client = clients.get(source)
    if client is None:
        return []
    return _safe_search(client, query, prefer_video=prefer_video)[:SEARCH_PER_SOURCE]


def _safe_search(client: Any, query: str, *, prefer_video: bool) -> list[StockCandidate]:
    try:
        return list(
            client.search(query, prefer_video=prefer_video, per_page=SEARCH_PER_SOURCE)
            or []
        )
    except Exception as exc:
        log(f"{getattr(client, 'name', client)} search fail: {exc}")
        return []


def _download(
    http: httpx.Client,
    url: str,
    dest: Path,
    *,
    require_image: bool = False,
    retries: int = 3,
) -> bool:
    if not url:
        return False
    last_exc: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Новое соединение на каждую попытку: после SSL EOF старый клиент мёртвый.
            with httpx.Client(
                timeout=_DOWNLOAD_TIMEOUT,
                trust_env=False,
                follow_redirects=True,
            ) as fresh:
                with fresh.stream("GET", url) as resp:
                    if resp.status_code >= 400:
                        return False
                    expected = resp.headers.get("content-length")
                    try:
                        if expected and int(expected) > _MAX_BYTES:
                            log(f"download skip, файл > {_MAX_BYTES // 1_000_000}MB")
                            return False
                    except ValueError:
                        pass
                    got = 0
                    with dest.open("wb") as f:
                        for chunk in resp.iter_bytes(65536):
                            f.write(chunk)
                            got += len(chunk)
                            if got > _MAX_BYTES:
                                raise OSError("file too large")
                    if expected:
                        try:
                            exp_n = int(expected)
                            if exp_n > 0 and got < exp_n * 0.95:
                                raise OSError(
                                    f"incomplete download {got}/{exp_n} (try {attempt})"
                                )
                        except ValueError:
                            pass
            if not dest.is_file() or dest.stat().st_size < 32:
                raise OSError("empty file")
            if require_image and not _looks_like_image(dest):
                dest.unlink(missing_ok=True)
                return False
            return True
        except Exception as exc:
            last_exc = exc
            if dest.exists():
                dest.unlink(missing_ok=True)
            if attempt < retries:
                time.sleep(0.4 * attempt)
                continue
    log(f"download fail: {last_exc}")
    return False


def _looks_like_image(path: Path) -> bool:
    try:
        head = path.read_bytes()[:32]
    except OSError:
        return False
    if head.startswith(b"\xff\xd8\xff"):
        return True
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return True
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    return False
