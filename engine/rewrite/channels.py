"""Рерайт «по каналам конкурентов»: самые залетевшие ролики канала — по очереди, без повторов.

Список роликов канала — через YouTube Data API (ключи из настроек; ~40 единиц квоты даже на тысячу роликов),
нет ключей / API не ответил — запасной путь через yt-dlp (без дат публикации — тогда только по отрыву от уровня
канала). Отсев: Shorts, трансляции и премьеры, короче минимума раздела (из короткого не выйдет нужный объём).

«Залётность» = скорость (просмотры в день с публикации) × отрыв от обычного уровня канала (просмотры ÷
медиана канала) — для всех роликов, и свежих, и старых: возраст старше WINDOW_DAYS дней считается мягче
(старый ролик набрал основное в первые недели — средняя скорость за всю жизнь его занижала бы). Каналы-доноры
сравниваются между собой: со всех берутся ролики с самой большой залётностью (не поровну с каждого).
Рейтинг каждый раз считается заново: новый залетевший ролик пойдёт первым.

Экономия запросов: список роликов канала кэшируется (CACHE_FRESH_SEC — берётся как есть); позже — дочитываются
только новые ролики, а просмотры обновляются у новых и недавних (младше REFRESH_DAYS), у старых — раз в
CACHE_FULL_SEC (их цифры почти не меняются).

Журнал переписанного (общий на всю программу, оба раздела, и ссылки руками тоже): ролик, который уже
взят, с канала второй раз не берётся. Взят = поставлен в очередь; задачу удалили или отменили до конца
рерайта — ролик возвращается в пул.
"""

from __future__ import annotations

import re
import statistics
import time
from dataclasses import asdict, dataclass

import httpx

from engine.core.logutil import log
from engine.publish.store import LOCK, read_json, write_json

FILE = "rewrite_ledger.json"
WINDOW_DAYS = 30
MIN_SEC = {"broll": 10 * 60, "slides": 8 * 60}  # короче — не хватит исходника на нужный объём
MAX_VIDEOS = 1500  # столько последних роликов канала смотрим (страниц API — MAX_VIDEOS / 50)
OLD_AGE_POWER = 0.8  # возраст сверх WINDOW_DAYS — в этой степени (смягчение для старых роликов)
CACHE_FILE = "rewrite_channel_cache.json"
CACHE_FRESH_SEC = 6 * 3600  # список канала моложе — без запросов
CACHE_FULL_SEC = 7 * 86400  # просмотры старых роликов обновляем не чаще
REFRESH_DAYS = 90  # у роликов младше — просмотры обновляем при каждом дочитывании
API = "https://www.googleapis.com/youtube/v3"
_VID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([\w-]{11})")


class ChannelError(RuntimeError):
    pass


class KeysExhausted(ChannelError):
    """Ни один ключ YouTube API не может ответить (квота кончилась / доступ запрещён) — не сбой запроса."""


@dataclass
class Video:
    id: str
    title: str
    views: int = 0
    published: float = 0.0  # unix; 0 — неизвестно (запасной путь)
    duration: float = 0.0  # секунд; 0 — неизвестно
    live: bool = False
    score: float = 0.0
    recent: bool = False

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.id}"


def video_id(url: str) -> str:
    m = _VID.search(url or "")
    return m.group(1) if m else ""


# ---------- журнал ----------


def ledger() -> dict:
    return read_json(FILE, {}).get("videos") or {}


def _alive(rec: dict, jobs: dict | None) -> bool:
    """Запись держит ролик: переписан, или стоит в очереди и задача жива (не удалена и не отменена)."""
    if rec.get("status") == "done":
        return True
    if jobs is None:
        return True
    st = jobs.get(str(rec.get("job") or ""))
    return st is not None and st != "cancelled"


def taken(jobs: dict | None = None) -> dict[str, dict]:
    """Ролики, которые уже взяты (id → запись). jobs — {id задачи: статус} из очереди: записи удалённых и
    отменённых до конца рерайта задач — не держат ролик."""
    return {vid: rec for vid, rec in ledger().items() if _alive(rec, jobs)}


def reserve(vid: str, *, title: str, channel: str, section: str, job: str) -> None:
    with LOCK:
        data = read_json(FILE, {})
        videos = data.setdefault("videos", {})
        old = videos.get(vid) or {}
        videos[vid] = {**old, "title": title, "channel": channel or old.get("channel", ""), "section": section,
                       "job": job, "status": old.get("status") if old.get("status") == "done" else "queued",
                       "at": time.time(), "times": int(old.get("times") or 0) + (1 if old.get("status") == "done" else 0)}
        write_json(FILE, data)


def mark_done(vid: str, *, title: str = "") -> None:
    if not vid:
        return
    with LOCK:
        data = read_json(FILE, {})
        rec = data.setdefault("videos", {}).setdefault(vid, {})
        rec.update(status="done", done_at=time.time(), **({"title": title} if title and not rec.get("title") else {}))
        write_json(FILE, data)


def duplicates(urls: list[str], jobs: dict | None = None) -> list[dict]:
    """Ссылки на ролики, которые уже переписывались (или стоят в очереди) — для предупреждения."""
    held = taken(jobs)
    out = []
    for url in urls:
        vid = video_id(url)
        if vid and vid in held:
            rec = held[vid]
            out.append({"url": url, "id": vid, "title": rec.get("title") or vid,
                        "status": "переписан" if rec.get("status") == "done" else "уже в очереди"})
    return out


def channel_stats() -> dict:
    return read_json(FILE, {}).get("channels") or {}


def _save_channel(cid: str, **values) -> None:
    with LOCK:
        data = read_json(FILE, {})
        data.setdefault("channels", {}).setdefault(cid, {}).update(values)
        write_json(FILE, data)


# ---------- список роликов канала ----------


def _iso_duration(raw: str) -> float:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", raw or "")
    if not m:
        return 0.0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return float(d * 86400 + h * 3600 + mi * 60 + s)


def _iso_time(raw: str) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


class _Api:
    def __init__(self, keys: list[str]) -> None:
        from engine.slides.stocks_base import KeyPool

        # у поиска свой суточный лимит: исчерпан он — ключ всё ещё годен для channels / videos / playlistItems,
        # поэтому «негодные» ключи помним отдельно для поиска и для остальных запросов
        self.pools = {"search": KeyPool(keys), "other": KeyPool(keys)}
        self.http = httpx.Client(timeout=30.0, trust_env=False)

    def close(self) -> None:
        self.http.close()

    def get(self, path: str, params: dict) -> dict:
        from engine.slides import youtube_cache, youtube_errors as yerr

        cacheable = path == "search"  # поиск стоит 100 единиц — повторный такой же запрос берём из кэша
        if cacheable and (hit := youtube_cache.get(params)) is not None:
            return hit
        pool = self.pools["search" if cacheable else "other"]
        problems: dict[str, str] = {}  # ключ → почему не работает (для понятного сообщения)
        while True:
            key = pool.next()
            if not key:
                raise KeysExhausted(yerr.exhausted_message(problems))
            resp = self.http.get(f"{API}/{path}", params={**params, "key": key})
            if resp.status_code in (403, 429):
                kind, why = yerr.classify(resp)
                if kind == "quota":
                    yerr.note_quota_spent(key)
                problems[key] = why
                pool.mark_bad(key)
                continue
            if resp.status_code >= 400:
                raise ChannelError(f"YouTube API {resp.status_code}: {resp.text[:150]}")
            data = resp.json()
            if cacheable:
                youtube_cache.put(params, data)
            return data


def _channel_query(url: str) -> dict:
    u = (url or "").strip()
    m = re.search(r"(UC[\w-]{22})", u)
    if m:
        return {"id": m.group(1)}
    m = re.search(r"(?:youtube\.com/)?(@[^/?#\s]+)", u)
    if m:
        from urllib.parse import unquote

        return {"forHandle": unquote(m.group(1))}
    m = re.search(r"youtube\.com/(?:c|user)/([^/?#\s]+)", u)
    if m:
        return {"forUsername": m.group(1)}
    raise ChannelError(f"не понял ссылку на канал: {u} (нужно youtube.com/@имя или youtube.com/channel/UC…)")


def _list_api(url: str, keys: list[str], cached: dict | None = None) -> tuple[str, str, list[Video]]:
    """cached — прошлый список канала из кэша ({"videos": [...], "full_at": …}): тогда дочитываем только новые
    ролики (лента загрузок идёт от новых к старым — до первого знакомого), а просмотры обновляем у новых и недавних;
    у старых — только если полному обновлению больше CACHE_FULL_SEC."""
    api = _Api(keys)
    try:
        data = api.get("channels", {"part": "snippet,contentDetails", **_channel_query(url)})
        items = data.get("items") or []
        if not items:
            raise ChannelError(f"канал не найден: {url}")
        ch = items[0]
        cid, title = ch["id"], ch["snippet"]["title"]
        uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
        known = {v["id"]: Video(**v) for v in (cached or {}).get("videos") or []} if cached else {}
        full = not known or time.time() - float((cached or {}).get("full_at") or 0) > CACHE_FULL_SEC
        ids: list[str] = []
        token = ""
        while len(ids) < MAX_VIDEOS:
            page = api.get("playlistItems", {"part": "contentDetails", "playlistId": uploads, "maxResults": 50,
                                             **({"pageToken": token} if token else {})})
            got = [i["contentDetails"]["videoId"] for i in page.get("items") or []]
            ids += got
            token = page.get("nextPageToken") or ""
            if not token or (known and any(i in known for i in got)):
                break  # дальше — уже знакомые ролики
        if known:  # знакомые, до которых не листали, — из кэша (в том же порядке: от новых к старым)
            ids += [i for i in known if i not in set(ids)][:max(0, MAX_VIDEOS - len(ids))]
        now = time.time()
        stale = [i for i in ids if full or i not in known
                 or not known[i].published or now - known[i].published < REFRESH_DAYS * 86400]
        fresh: dict[str, Video] = {}
        for k in range(0, len(stale), 50):
            batch = api.get("videos", {"part": "snippet,contentDetails,statistics,liveStreamingDetails",
                                       "id": ",".join(stale[k:k + 50])})
            for v in batch.get("items") or []:
                sn = v.get("snippet") or {}
                fresh[v["id"]] = Video(
                    id=v["id"], title=sn.get("title") or "", views=int((v.get("statistics") or {}).get("viewCount") or 0),
                    published=_iso_time(sn.get("publishedAt") or ""),
                    duration=_iso_duration((v.get("contentDetails") or {}).get("duration") or ""),
                    live=bool(v.get("liveStreamingDetails")) or sn.get("liveBroadcastContent") in ("live", "upcoming"))
        # ролик, который спросили, но API не вернул, — удалён или скрыт: в список не попадает
        videos = [fresh[i] if i in fresh else known[i] for i in ids if i in fresh or (i in known and i not in stale)]
        log(f"Каналы: «{title}» — запросов статистики {(len(stale) + 49) // 50}"
            + (f" (из кэша {len(ids) - len(stale)} роликов)" if known else ""))
        return cid, title, videos
    finally:
        api.close()


def _list_ytdlp(url: str) -> tuple[str, str, list[Video]]:
    """Запасной путь без ключей: вкладка «Видео» канала (Shorts и трансляции там отдельно)."""
    import yt_dlp

    base = url.strip().rstrip("/")
    base = re.sub(r"/(videos|shorts|streams|featured)$", "", base)
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
                           "playlistend": MAX_VIDEOS}) as ydl:
        info = ydl.extract_info(base + "/videos", download=False)
    cid = str(info.get("channel_id") or info.get("id") or "")
    title = str(info.get("channel") or info.get("title") or "").removesuffix(" - Videos")
    videos = [Video(id=e["id"], title=str(e.get("title") or ""), views=int(e.get("view_count") or 0),
                    published=float(e.get("timestamp") or 0), duration=float(e.get("duration") or 0),
                    live=e.get("live_status") in ("is_live", "is_upcoming", "was_live"))
              for e in info.get("entries") or [] if e and e.get("id")]
    return cid, title, videos


def _cache_key(url: str) -> str:
    return re.sub(r"/(videos|shorts|streams|featured)/?$", "", (url or "").strip().rstrip("/")).lower()


def list_channel(url: str, keys: list[str]) -> tuple[str, str, list[Video]]:
    """→ (ID канала, название, ролики). Список кэшируется: моложе CACHE_FRESH_SEC — без запросов вовсе,
    старше — дочитываются только новые ролики (см. _list_api)."""
    key = _cache_key(url)
    with LOCK:
        cached = (read_json(CACHE_FILE, {}) or {}).get(key)
    if cached and time.time() - float(cached.get("at") or 0) < CACHE_FRESH_SEC:
        log(f"Каналы: «{cached.get('title')}» — список роликов из кэша (без запросов)")
        return cached["cid"], cached["title"], [Video(**v) for v in cached.get("videos") or []]
    full_at = time.time()
    if keys:
        try:
            cid, title, videos = _list_api(url, keys, cached if cached and cached.get("api") else None)
            full = not (cached and cached.get("api")) or time.time() - float(cached.get("full_at") or 0) > CACHE_FULL_SEC
            _remember(key, cid, title, videos, api=True,
                      full_at=full_at if full else float(cached.get("full_at") or full_at))
            return cid, title, videos
        except ChannelError as exc:
            if "не понял" in str(exc) or "не найден" in str(exc):
                raise
            log(f"Каналы: YouTube API не помог ({exc}) — беру список через yt-dlp")
        except httpx.HTTPError as exc:
            log(f"Каналы: YouTube API не отвечает ({exc}) — беру список через yt-dlp")
    try:
        cid, title, videos = _list_ytdlp(url)
    except Exception as exc:
        raise ChannelError(f"список роликов канала не получить: {str(exc)[:200]}") from None
    _remember(key, cid, title, videos, api=False, full_at=full_at)
    return cid, title, videos


def _remember(key: str, cid: str, title: str, videos: list[Video], *, api: bool, full_at: float) -> None:
    try:
        with LOCK:
            data = read_json(CACHE_FILE, {}) or {}
            data[key] = {"at": time.time(), "full_at": full_at, "api": api, "cid": cid, "title": title,
                         "videos": [{k: v for k, v in asdict(x).items() if k not in ("score", "recent")} for x in videos]}
            write_json(CACHE_FILE, data)
    except OSError as exc:  # кэш — только экономия: не записался — работаем дальше
        log(f"Каналы: кэш списка не записан ({exc})")


# ---------- рейтинг ----------


def effective_age(days: float, window_days: int = WINDOW_DAYS) -> float:
    """Возраст для скорости: до window_days — как есть, дальше — мягче (степень OLD_AGE_POWER)."""
    days = max(1.0, days)
    if days <= window_days:
        return days
    return window_days + (days - window_days) ** OLD_AGE_POWER


def rank(videos: list[Video], *, min_sec: float, now: float | None = None,
         window_days: int = WINDOW_DAYS) -> list[Video]:
    """Подходящие ролики по «залётности» (скорость × отрыв от уровня канала) — свежие и старые вместе.
    Ролики без даты (запасной путь без ключей) — после датированных, по отрыву."""
    now = now or time.time()
    ok = [v for v in videos if not v.live and (v.duration == 0 or v.duration >= min_sec) and v.views > 0]
    if not ok:
        return []
    median = statistics.median(v.views for v in ok) or 1
    for v in ok:
        ratio = v.views / median
        if v.published:
            age = max(1.0, (now - v.published) / 86400)
            v.recent = age <= window_days
            v.score = (v.views / effective_age(age, window_days)) * ratio
        else:
            v.recent, v.score = False, ratio
    return sorted(ok, key=lambda v: (not v.published, -v.score))


def pick(url: str, count: int, *, section: str, keys: list[str], jobs: dict | None = None,
         exclude: set[str] | None = None) -> dict:
    """count следующих залетевших роликов канала, которых ещё не брали. → {"channel", "id", "videos": [Video],
    "left": сколько подходящих ещё останется, "skipped_short": …}."""
    cid, title, videos = list_channel(url, keys)
    ranked = rank(videos, min_sec=MIN_SEC.get(section, 8 * 60))
    held = set(taken(jobs)) | set(exclude or ())  # exclude — уже выбранные в этом же запуске (другой ссылкой)
    fresh = [v for v in ranked if v.id not in held]
    chosen = fresh[:max(0, int(count))]
    left = len(fresh) - len(chosen)
    _save_channel(cid, title=title, url=url, total=len(ranked), left=left,
                  taken=len([v for v in ranked if v.id in held]) + len(chosen), checked_at=time.time())
    log(f"Каналы: «{title}» — подходящих {len(ranked)}, уже взято {len(ranked) - len(fresh)}, беру {len(chosen)}, "
        f"останется {left}")
    return {"channel": title, "id": cid, "videos": chosen, "left": left, "total": len(ranked),
            "all": len(videos), "taken_before": len(ranked) - len(fresh)}


def spread(entries: list[dict], count: int) -> list[tuple[dict, Video]]:
    """Ровно count роликов из каналов-доноров (или сколько есть): каналы сравниваются между собой — берутся ролики
    с самой большой залётностью отовсюду (с сильного канала может уйти больше, со слабого — ничего).
    В entries — результаты pick(): у каждого «videos» (кандидаты по рейтингу). Заполняет entry["taken"], обновляет entry["left"]."""
    for e in entries:
        e["taken"] = 0
    # равная залётность (или нет данных — без ключей) — по очереди: место в своём канале, потом реже бравшийся донор
    pool = [(e, v, i) for e in entries for i, v in enumerate(e["videos"])]
    pool.sort(key=lambda evi: (not getattr(evi[1], "published", 0), -float(getattr(evi[1], "score", 0) or 0),
                               evi[2], int(evi[0].get("taken_before") or 0)))
    out: list[tuple[dict, Video]] = []
    seen: set[str] = set()
    for e, v, _i in pool:
        if len(out) >= max(0, int(count)):
            break
        if v.id in seen:
            continue
        seen.add(v.id)
        out.append((e, v))
        e["taken"] += 1
    for e in entries:
        e["videos"].sort(key=lambda v: v.id not in seen)  # взятые — первыми (для отчёта «что взято»)
        e["left"] = int(e.get("left") or 0) + len(e["videos"]) - e["taken"]  # кандидаты, которые не понадобились
        if e.get("id") and e.get("url"):  # pick записал в учёт «взято» по кандидатам — исправляем на реально взятые
            _save_channel(e["id"], title=e.get("channel") or "", url=e["url"], total=int(e.get("total") or 0),
                          left=e["left"], taken=int(e.get("taken_before") or 0) + e["taken"], checked_at=time.time())
    return out


def as_dict(v: Video) -> dict:
    return {**asdict(v), "url": v.url}
