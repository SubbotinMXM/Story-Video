"""YouTube Data API search + yt-dlp clip download."""

from __future__ import annotations

import html
import random
import re
import threading
from pathlib import Path

import httpx

from engine.core.cancel import CancelledError, run_cancellable
from engine.core.logutil import log
from engine.slides.ffmpeg_util import find_ffmpeg
from engine.slides.stocks_base import KeyPool, StockCandidate

CLIP_SEC = 10.0
MAX_HEIGHT = 1080
YT_PARALLEL = 8
_YT_SLOTS = threading.BoundedSemaphore(YT_PARALLEL)


def _format(min_height: int) -> str:
    """Только видеопоток: звук из клипа всё равно выкидываем (-an).

    Совмещённые mp4 (видео+звук) YouTube отдаёт лишь в 360p — их не берём.
    Порядок: h264 до 1080p → любой кодек до 1080p → что угодно не ниже min_height.
    """
    lo = max(0, int(min_height))
    rng = f"[height>={lo}][height<={MAX_HEIGHT}]"
    return f"bv*{rng}[vcodec^=avc1]/bv*{rng}/bv*[height>={lo}]"


class _SilentLogger:
    """yt-dlp пишет ERROR в stderr даже с quiet — ошибки и так ловим исключением."""

    def debug(self, msg: str) -> None:
        pass

    info = warning = error = debug


class YouTubeClient:
    name = "youtube"
    SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
    VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

    def __init__(self, api_keys: list[str] | str, *, min_height: int = 720) -> None:
        keys = [api_keys] if isinstance(api_keys, str) else list(api_keys or [])
        live = [k for k in keys if (k or "").strip()]
        self.keys = KeyPool(live)  # для поиска; у него свой суточный лимит
        self._keys_other = KeyPool(live)  # для проверки найденных видео (1 единица) — не зависит от лимита поиска
        self.api_key = self.keys.next() or ""
        self.min_height = max(0, int(min_height))
        self._http = httpx.Client(timeout=45.0, trust_env=False, follow_redirects=True)
        # длительности из проверки видео — не тратить на них лишний запрос
        self._durations: dict[str, float] = {}

    def close(self) -> None:
        self._http.close()

    def _get(self, url: str, params: dict) -> httpx.Response | None:
        """Запрос к YouTube Data API: квота ключа кончилась (403/429) — следующий ключ."""
        from engine.slides import youtube_cache

        cacheable = url == self.SEARCH_URL  # поиск стоит 100 единиц — повтор берём из кэша
        if cacheable and (hit := youtube_cache.get(params)) is not None:
            return httpx.Response(200, json=hit)
        pool = self.keys if cacheable else self._keys_other
        while True:
            key = pool.next()
            if not key:
                return None
            try:
                resp = self._http.get(url, params={**params, "key": key})
            except Exception as exc:
                log(f"youtube fail: {exc}")
                return None
            if resp.status_code in (403, 429):
                # суточная квота (или ключ недействителен) — до конца этого запуска ключ не трогаем
                from engine.slides import youtube_errors as yerr

                kind, why = yerr.classify(resp)
                if kind == "quota":
                    yerr.note_quota_spent(key)
                left = sum(1 for k in pool._keys if k not in pool._dead) - 1
                log(f"youtube: ключ …{key[-4:]} — HTTP {resp.status_code}: {why}, осталось ключей: {left}")
                pool.mark_bad(key)
                continue
            if resp.status_code >= 400:
                log(f"youtube HTTP {resp.status_code}")
                return None
            self.api_key = key
            if cacheable:
                try:
                    youtube_cache.put(params, resp.json())
                except ValueError:
                    pass
            return resp

    def search(
        self,
        query: str,
        *,
        prefer_video: bool = True,
        per_page: int = 10,
    ) -> list[StockCandidate]:
        q = (query or "").strip()
        if not prefer_video or not q or not self.api_key:
            return []
        resp = self._get(
            self.SEARCH_URL,
            {
                "part": "snippet",
                "type": "video",
                "q": q,
                "maxResults": max(1, min(25, per_page)),
                "videoEmbeddable": "true",
                # только Creative Commons: автор разрешил переиспользование
                # с указанием авторства (список авторов кладём в папку ролика)
                "videoLicense": "creativeCommon",
                # только HD: отсекает старое и любительское низкого качества ещё до скачивания
                "videoDefinition": "high",
                "safeSearch": "moderate",
            },
        )
        if resp is None:
            return []
        try:
            data = resp.json()
        except Exception:
            return []
        out: list[StockCandidate] = []
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            vid = str((item.get("id") or {}).get("videoId") or "").strip()
            snip = item.get("snippet") or {}
            if not vid:
                continue
            thumbs = snip.get("thumbnails") or {}
            preview = str(
                (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get(
                    "url"
                )
                or ""
            )
            # API отдаёт HTML-сущности: &amp; &#39;
            title = html.unescape(str(snip.get("title") or ""))
            channel = html.unescape(str(snip.get("channelTitle") or ""))
            if _risky(title, channel):
                log(f"youtube: пропускаю рискованное ({channel}: {title[:60]})")
                continue
            out.append(
                StockCandidate(
                    provider=self.name,
                    kind="video",
                    id=vid,
                    query=q,
                    preview_url=preview,
                    download_url=f"https://www.youtube.com/watch?v={vid}",
                    meta={"title": title, "channel": channel, "license": "CC BY"},
                )
            )
        return self._verified(out)

    def _verified(self, cands: list[StockCandidate]) -> list[StockCandidate]:
        """Проверка каждого видео (videos.list, 1 ед. квоты на 50 видео): лицензия, Content ID,
        признаки перезалива. Проверить не вышло — не берём ничего (лучше без YouTube, чем со страйком)."""
        if not cands:
            return []
        resp = self._get(
            self.VIDEOS_URL,
            {"part": "snippet,contentDetails,status", "id": ",".join(c.id for c in cands), "maxResults": 50},
        )
        try:
            items = (resp.json() or {}).get("items") if resp is not None else None
        except Exception:
            items = None
        if not isinstance(items, list):
            log("youtube: не смог проверить лицензии найденных видео — пропускаю их")
            return []
        infos = {str(i.get("id")): i for i in items if isinstance(i, dict) and isinstance(i.get("id"), str)}
        out: list[StockCandidate] = []
        for cand in cands:
            info = infos.get(cand.id)
            why = _reupload_signs(info) if info is not None else "видео недоступно"
            if why:
                log(f"youtube: пропускаю {cand.id} ({cand.meta.get('channel')}: {str(cand.meta.get('title'))[:50]}) — {why}")
                continue
            dur = _parse_iso8601(str((info.get("contentDetails") or {}).get("duration") or ""))
            if dur:
                self._durations[cand.id] = dur
            out.append(cand)
        return out

    def download_clip(
        self,
        cand: StockCandidate,
        dest: Path,
        *,
        clip_sec: float = CLIP_SEC,
        rng: random.Random | None = None,
    ) -> bool:
        """Скачивает короткий кусок. Не больше YT_PARALLEL загрузок на всё приложение
        (два ролика параллельно не должны удваивать нагрузку на YouTube)."""
        with _YT_SLOTS:
            return self._download_clip(cand, dest, clip_sec=clip_sec, rng=rng)

    def _download_clip(
        self,
        cand: StockCandidate,
        dest: Path,
        *,
        clip_sec: float,
        rng: random.Random | None,
    ) -> bool:
        url = (cand.download_url or "").strip()
        if not url:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.unlink(missing_ok=True)
        try:
            import yt_dlp
        except ImportError:
            log("youtube: нет yt-dlp — pip install yt-dlp")
            return False

        start = self._pick_start(cand, clip_sec=clip_sec, rng=rng)
        end = start + max(4.0, clip_sec + 1.0)
        tmp = dest.with_suffix(".src.mp4")
        self._cleanup_glob(tmp)
        ff = find_ffmpeg()
        opts: dict = {
            "format": _format(self.min_height),
            "outtmpl": str(tmp.parent / f"{tmp.stem}.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": _SilentLogger(),
            # зависшее соединение не держит поток подбора минутами: 20 с тишины — следующий кандидат
            "socket_timeout": 20,
            "retries": 2,
            "fragment_retries": 2,
            "ffmpeg_location": str(Path(ff).parent),
        }
        try:
            from yt_dlp.utils import download_range_func

            # ranges + один поток (только видео, без merge) — иначе ffmpeg code 8.
            # Без force_keyframes_at_cuts: кусок копируется с ближайшего ключевого
            # кадра, без перекодирования (в 2+ раза быстрее, процессор свободен).
            opts["download_ranges"] = download_range_func(None, [(start, end)])
        except Exception:
            pass
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as exc:
            self._cleanup_glob(tmp)
            if "format is not available" in str(exc):
                log(f"youtube {cand.id}: нет видео от {self.min_height}p, пропускаю")
                return False
            log(f"youtube ydl fail {cand.id}: {exc}")
            return self._download_and_cut(url, dest, start=start, clip_sec=clip_sec)

        raw = self._resolve_out(tmp)
        if raw is None:
            return self._download_and_cut(url, dest, start=start, clip_sec=clip_sec)
        # ranges уже обрезали — только remux/нормализуем
        ok = self._ffmpeg_cut(raw, dest, start=0.0, clip_sec=clip_sec)
        raw.unlink(missing_ok=True)
        if not ok:
            return self._download_and_cut(url, dest, start=start, clip_sec=clip_sec)
        return True

    def _download_and_cut(
        self,
        url: str,
        dest: Path,
        *,
        start: float,
        clip_sec: float,
    ) -> bool:
        """Простой путь: скачать кусок файла → обрезать."""
        try:
            import yt_dlp
        except ImportError:
            return False
        tmp = dest.with_suffix(".full.mp4")
        self._cleanup_glob(tmp)
        ff = find_ffmpeg()
        opts = {
            "format": _format(self.min_height),
            "outtmpl": str(tmp.parent / f"{tmp.stem}.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": _SilentLogger(),
            # зависшее соединение не держит поток подбора минутами: 20 с тишины — следующий кандидат
            "socket_timeout": 20,
            "retries": 2,
            "fragment_retries": 2,
            "merge_output_format": "mp4",
            "ffmpeg_location": str(Path(ff).parent),
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as exc:
            log(f"youtube head fail: {exc}")
            self._cleanup_glob(tmp)
            return False
        raw = self._resolve_out(tmp)
        if raw is None:
            return False
        ok = self._ffmpeg_cut(raw, dest, start=max(0.0, start), clip_sec=clip_sec)
        raw.unlink(missing_ok=True)
        return ok

    def _ffmpeg_cut(
        self, src: Path, dest: Path, *, start: float, clip_sec: float
    ) -> bool:
        """Обрезать до clip_sec. Сначала копией потока (без потерь, мгновенно):
        финальный рендер всё равно перекодирует кадр. Не вышло — перекодируем."""
        ff = find_ffmpeg()
        head = [ff, "-y", "-hide_banner", "-loglevel", "error"]
        if start > 0.05:
            head += ["-ss", f"{start:.2f}"]
        head += ["-i", str(src), "-t", f"{max(3.0, clip_sec):.2f}", "-an"]
        tail = ["-movflags", "+faststart", str(dest)]
        copy = head + ["-c:v", "copy"] + tail
        encode = head + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"] + tail
        for cmd in (copy, encode):
            try:
                if run_cancellable(cmd, timeout=240).returncode != 0:
                    raise RuntimeError("ffmpeg не вырезал кусок")
            except CancelledError:
                raise
            except Exception as exc:
                log(f"youtube cut fail: {exc}")
                dest.unlink(missing_ok=True)
                continue
            if _clip_ok(dest, min_sec=2.0):
                return True
            dest.unlink(missing_ok=True)
        return False

    def _resolve_out(self, tmp: Path) -> Path | None:
        # outtmpl stem.%(ext)s → stem.mp4; ranges иногда пишут stem без расширения
        bare = tmp.with_suffix("")
        for path in (tmp, bare, tmp.with_suffix(".mp4"), bare.with_suffix(".mp4")):
            if path.is_file() and path.stat().st_size > 2000:
                return path
        matches = sorted(
            path
            for path in tmp.parent.glob(tmp.stem + ".*")
            if path.is_file() and path.stat().st_size > 2000
        )
        return matches[0] if matches else None

    def _cleanup_glob(self, tmp: Path) -> None:
        bare = tmp.with_suffix("")
        for path in (tmp, bare, *tmp.parent.glob(tmp.stem + ".*")):
            if path.is_file():
                path.unlink(missing_ok=True)

    def _pick_start(
        self,
        cand: StockCandidate,
        *,
        clip_sec: float,
        rng: random.Random | None,
    ) -> float:
        dur = self._video_duration(cand.id)
        if dur is None or dur <= clip_sec + 2:
            return 0.0
        lo = max(0.0, dur * 0.2)
        hi = max(lo, dur * 0.75 - clip_sec)
        if hi <= lo:
            return lo
        r = rng or random.Random(cand.id)
        return round(r.uniform(lo, hi), 2)

    def _video_duration(self, video_id: str) -> float | None:
        if video_id in self._durations:
            return self._durations[video_id]
        if not self.api_key or not video_id:
            return None
        try:
            resp = self._get(self.VIDEOS_URL, {"part": "contentDetails", "id": video_id})
            if resp is None:
                return None
            items = (resp.json() or {}).get("items") or []
            if not items:
                return None
            iso = str((items[0].get("contentDetails") or {}).get("duration") or "")
            return _parse_iso8601(iso)
        except Exception:
            return None


# Даже среди Creative Commons встречаются чужие перезаливы. Фильмы, сериалы, клипы,
# спорт, игры, документалки крупных студий и телеканалы — основная причина претензий Content ID.
_RISKY_WORDS = (
    "trailer", "трейлер", "full movie", "movie clip", "movie", "film", "фильм", "фильма", "official video",
    "official music", "music video", "клип", "episode", "серия", "season", "сезон", "series", "сериал",
    "tv show", "tv series", "cartoon", "мультфильм", "мультик", "anime", "аниме", "amv",
    "highlights", "full match", "обзор матча", "netflix", "hbo", "disney", "marvel", "pixar", "warner",
    "paramount", "cinemax", "universal pictures", "sony pictures", "cnn", "bbc", "fox news", "nbc", "abc news",
    "nfl", "nba", "uefa", "fifa", "premier league", "ufc", "wwe",
    "national geographic", "nat geo", "natgeo", "discovery channel", "history channel", "smithsonian",
    "documentary", "documentaire", "documental", "dokumentation", "doku", "документальный", "документалка",
    "lyrics", "karaoke", "караоке", "concert", "концерт", "live performance",
    "gameplay", "walkthrough", "let's play", "летсплей",
    "compilation", "подборка", "remastered", "reupload", "re-upload", "перезалив", "excerpt", "отрывок",
    # новостные каналы часто перезаливают чужие съёмки
    "news", "новости", "новостей", "noticias", "wiadomości", "știri", "breaking",
)
_RISKY_RE = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(w) for w in _RISKY_WORDS) + r")(?!\w)")
# внутри слов и без пробелов: «ArtistVEVO», «中国新闻社»
_RISKY_PARTS = ("vevo", "新闻", "电影", "电视剧")
# так пишут в описании те, кто выложил чужое: лицензия CC у такого видео ничего не стоит
_REUPLOAD = re.compile(
    r"i do ?n[o']?t own|no copyright infringement|copyright infringement (?:is )?(?:not )?intended|"
    r"copyright disclaimer|section 107|fair use|all rights reserved|all rights (?:go|belong)|"
    r"rights (?:go|belong) to|credits? (?:go )?to the|re-?upload|не являюсь (?:владельцем|автором)|"
    r"права принадлежат|все права защищены|добросовестн\w+ использовани|перезалив"
)


# категории YouTube: 1 — фильмы и анимация, 10 — музыка, 20 — игры (там почти всё чужое)
_RISKY_CATEGORIES = frozenset({"1", "10", "20"})


def _risky(title: str, channel: str) -> bool:
    text = f" {title} {channel} ".lower()
    return bool(_RISKY_RE.search(text)) or any(part in text for part in _RISKY_PARTS)


def _reupload_signs(info: dict) -> str:
    """Почему видео похоже на чужое (пусто — признаков нет). info — элемент videos.list."""
    status = info.get("status") or {}
    details = info.get("contentDetails") or {}
    snip = info.get("snippet") or {}
    if status.get("license") != "creativeCommon":
        return "лицензия не Creative Commons"
    if details.get("licensedContent"):
        # видео совпало с чужим контентом в Content ID — у него есть правообладатель
        return "в Content ID у видео есть правообладатель"
    text = " ".join([str(snip.get("description") or ""), " ".join(map(str, snip.get("tags") or []))]).lower()
    if _REUPLOAD.search(text):
        return "в описании признаки перезалива («I do not own», «fair use», «all rights reserved»…)"
    if str(snip.get("categoryId") or "") in _RISKY_CATEGORIES:
        return "категория: фильмы/мультфильмы, музыка или игры"
    if _risky(" ".join(map(str, snip.get("tags") or [])), ""):
        return "теги: фильмы, сериалы, игры, ТВ"
    return ""


def _clip_ok(path: Path, *, min_sec: float) -> bool:
    if not path.is_file() or path.stat().st_size <= 2000:
        return False
    try:
        from engine.slides.ffprobe_util import probe_duration

        return probe_duration(path) >= min_sec
    except Exception:
        return False


def _parse_iso8601(raw: str) -> float | None:
    s = (raw or "").strip().upper()
    if not s.startswith("PT"):
        return None
    s = s[2:]
    total = 0.0
    num = ""
    for ch in s:
        if ch.isdigit():
            num += ch
            continue
        if not num:
            continue
        n = float(num)
        num = ""
        if ch == "H":
            total += n * 3600
        elif ch == "M":
            total += n * 60
        elif ch == "S":
            total += n
    return total if total > 0 else None
