"""Ролик про конкретного человека: кадры только с ним.

- Запросы — только с его именем; источники — те, где он есть: Google, Wikimedia,
  YouTube (Creative Commons). Стоков и «фона эпохи» нет.
- Человек эпохи фотографии: на кадре должно быть ЕГО лицо. Героя опознаём по
  найденным фото (faces.find_hero), остальное — здания, другие люди — отсекается.
- Человек до фотографии (Цезарь): статуи, бюсты, картины, гравюры с ним;
  карты, здания и пейзажи отсекаются.
Картинок берём столько же, сколько в обычном пуле; если чистых меньше — меньше,
ролик повторит их вразбивку, но чужих не подмешает.
"""

from __future__ import annotations

import math
import random
import re
import threading
import unicodedata
from urllib.parse import urlparse
from concurrent.futures import as_completed
from pathlib import Path

import httpx

from engine.core.cancel import CancelledError, CancelToken
from engine.core.logutil import log
from engine.core.threads import ThreadPoolExecutor
from engine.slides import faces
from engine.slides.clip_ranker import score_image_paths
from engine.slides.ffprobe_util import is_landscape, probe_duration, video_height_ok
from engine.slides.media_pipeline import _download
from engine.slides.source_mix import MIN_HEIGHT, subject_of
from engine.slides.stocks_base import StockCandidate
from engine.slides.theme_plan import ThemePlan, _main_name, hero_era, person_queries

PER_QUERY = 30  # Google отдаёт много фото героя — берём шире обычного
IMAGE_MIN_SIDE = 480  # старые фото людей часто меньше 720p; 720+ всё равно идут первыми
VIDEO_TRIALS_PER_NEED = 4
FETCH_WORKERS = 8  # скачивание фото героя упирается в сеть, а не в процессор
_STATUE_WORDS = re.compile(r"\b(statue|bust|sculpture|marble|bronze|engraving|ancient)\b", re.I)


def person_mode(plan: ThemePlan) -> tuple[str, str] | None:
    """(имя, эпоха) если ролик про конкретного человека, иначе None."""
    ctx = plan.context
    name = (ctx.key_people[0]["name"] if ctx.key_people else "") or _main_name(ctx.characters)
    if not name or subject_of(ctx.subject, visual_style=ctx.visual_style, historical=ctx.historical) != "person":
        return None
    return name, hero_era(ctx)


def _fold(text: str) -> str:
    """Без диакритики и регистра: «Călugăreanu» = «Calugareanu»."""
    norm = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in norm if not unicodedata.combining(ch)).lower()


def name_in(text: str, name: str) -> bool:
    """В названии видео есть фамилия героя (или имя целиком)."""
    folded = _fold(text)
    parts = [p for p in _fold(name).split() if len(p) >= 3]
    return bool(parts) and (parts[-1] in folded or _fold(name) in folded)


_RU_VIDEO = ("интервью", "документальный фильм", "вечер памяти", "творческий вечер", "передача")


def person_query_set(plan: ThemePlan, name: str, kind: str, limit: int = 16, *, only_plan: bool = False) -> tuple[list[str], list[str]]:
    """Все запросы — с именем героя в кавычках (точное совпадение в поиске)."""
    images, videos = person_queries(name, plan.context.era, plan.context.place, kind)
    if kind == "photo" and re.search("[а-яё]", name, re.I):
        videos = [*videos, *[f"{name} {x}" for x in _RU_VIDEO]]
    extra_i = [q for q in plan.image_queries if name.lower() in q.lower()]
    extra_v = [q for q in plan.video_queries if name.lower() in q.lower()]
    if kind == "photo":
        extra_i = [q for q in extra_i if not _STATUE_WORDS.search(q)]
        extra_v = [q for q in extra_v if not _STATUE_WORDS.search(q)]

    def quoted(qs: list[str]) -> list[str]:
        out: list[str] = []
        for q in qs:
            q = quote_name(q, name)
            if q not in out:
                out.append(q)
        return out

    # запросы плана с уточнением («… советский актёр») — первыми: точнее, тёзка в выдаче реже
    # only_plan — добор: только запросы плана (стандартные уже были в первом проходе)
    if only_plan:  # добор: без кавычек — шире выдача, чужих отсеет проверка по лицу
        return list(dict.fromkeys(extra_i))[:limit], quoted(extra_v + videos)[:10]
    return quoted(extra_i + images)[:limit], quoted(extra_v + videos)[:10]


def quote_name(query: str, name: str) -> str:
    """Имя героя в кавычках — поиск ищет точное совпадение."""
    return re.sub(re.escape(name), f'"{name}"', query, flags=re.I) if '"' not in query else query


class HostGuard:
    """Сайты, которые не отдают файлы (рукопожатие SSL висит, блокировка): после нескольких подряд неудач без единой
    удачи — больше туда не ходим. Такая попытка стоила до 20 секунд, а таких адресов в выдаче треть."""

    LIMIT = 3

    def __init__(self) -> None:
        self._fails: dict[str, int] = {}
        self._ok: set[str] = set()
        self._lock = threading.Lock()

    @staticmethod
    def host(url: str) -> str:
        return urlparse(url).netloc.lower()

    def blocked(self, url: str) -> bool:
        h = self.host(url)
        with self._lock:
            return h not in self._ok and self._fails.get(h, 0) >= self.LIMIT

    def report(self, url: str, ok: bool) -> None:
        h = self.host(url)
        with self._lock:
            if ok:
                self._ok.add(h)
            else:
                self._fails[h] = self._fails.get(h, 0) + 1


def _merge(lists: list[list[StockCandidate]]) -> list[StockCandidate]:
    """Источники вперемешку (по кругу), без повторов одного адреса: если один отвалился, остальные держат."""
    out: list[StockCandidate] = []
    seen: set[str] = set()
    for rank in range(max((len(x) for x in lists), default=0)):
        for lst in lists:
            if rank < len(lst) and lst[rank].download_url not in seen:
                seen.add(lst[rank].download_url)
                out.append(lst[rank])
    return out


def _search(clients: dict, source: str, queries: list[str], *, video: bool, cancel: CancelToken | None) -> list[StockCandidate]:
    client = clients.get(source)
    if client is None:
        return []
    found: list[StockCandidate] = []
    seen: set[str] = set()

    def one(q: str) -> list[StockCandidate]:
        if cancel and cancel.is_cancelled():
            raise CancelledError("Остановлено пользователем")
        try:
            batch = list(client.search(q, prefer_video=video, per_page=PER_QUERY) or [])
        except Exception as exc:
            log(f"{source}: поиск «{q}» не удался: {exc}")
            return []
        for cand in batch:
            cand.query = q
        return batch

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(queries)))) as ex:
        # по кругу: первая выдача каждого запроса раньше хвостов — больше разных фото
        results = list(ex.map(one, queries))
    for rank in range(PER_QUERY):
        for batch in results:
            if rank < len(batch):
                cand = batch[rank]
                if cand.id in seen or (video != (cand.kind == "video")):
                    continue
                seen.add(cand.id)
                found.append(cand)
    return found


from engine.slides.look import DUP_BITS, image_hash  # noqa: E402  (общий отпечаток кадра)
from engine.slides.look import hamming as _hamming  # noqa: E402


def _is_depiction(path: Path, name: str) -> bool:
    """Древний герой: статуя/бюст/картина/портрет человека, а не здание, карта или пейзаж."""
    person = score_image_paths(f"a statue, bust, painting, engraving or portrait of {name}, a person", [path], threshold=0.0)
    other = score_image_paths("a building, ruins, landscape, map, text document, object, scenery without people", [path], threshold=0.0)
    return bool(person) and bool(other) and person[0][1] > other[0][1]


def collect_person_pool(
    http: httpx.Client,
    clients: dict,
    plan: ThemePlan,
    *,
    name: str,
    kind: str,
    rng: random.Random,
    pool: Path,
    trials: Path,
    frames: Path,
    cancel: CancelToken | None,
    progress,
    pct_lo: float,
    pct_hi: float,
    image_need: int,
    video_need: int,
    anchors: list[str] | None = None,
    max_images_queries: int = 16,
    max_cands: int | None = None,
    only_plan: bool = False,
) -> tuple[list[dict], list[dict], int, int]:
    """Пул только с героем → (videos, images, отбраковано видео, картинок).
    anchors — точные запросы (имя + кто это + годы): лицо героя узнаём по их выдаче, а не по всем
    фото с этим именем — иначе тёзка, которого в выдаче больше, стал бы «героем»."""
    from engine.slides.theme_media import CLIP_SEC, _extract_frames

    span = pct_hi - pct_lo
    img_q, vid_q = person_query_set(plan, name, kind, limit=max_images_queries, only_plan=only_plan)
    if only_plan:  # точные запросы — в кавычках: по ним опознаётся лицо героя (защита от тёзок)
        exact_q = set(anchors or [])
        img_q = [quote_name(q, name) if q in exact_q else q for q in img_q]
    photo = kind == "photo"
    log(f"Герой «{name}» ({kind}): только его кадры. Запросы: {img_q[:6]}…")
    progress(pct_lo, f"Ищу фото: {name}")

    # --- картинки: сначала источники без браузера (статья Википедии, Яндекс, Commons), потом Google/Bing на добор ---
    cap = max_cands or max(image_need * 4, 120)
    lists: list[list[StockCandidate]] = []
    wiki = clients.get("wikipedia")
    if wiki is not None and photo and not only_plan:
        try:
            article, lead = wiki.photos(name)
        except Exception as exc:
            log(f"Википедия: {str(exc)[:100]}")
            article, lead = [], None
        if lead is not None:
            lists.append([lead])  # главное фото статьи — по нему опознаётся лицо героя
        lists.append(article)
    lists.append(_search(clients, "yandex", img_q[:10], video=False, cancel=cancel))
    lists.append(_search(clients, "wikimedia", img_q[:6], video=False, cancel=cancel))
    cands = _merge(lists)
    if len(cands) < cap * 0.8:  # мало — браузерный поиск (медленный и капризный: капча, обрывы)
        log(f"Герой: без браузера набралось {len(cands)} — добавляю Google/Bing")
        lists.append(_search(clients, "google", img_q, video=False, cancel=cancel))
        cands = _merge(lists)
    cands.sort(key=lambda c: bool((c.meta or {}).get("watermark")))  # водяной знак — в конец
    cands = cands[:cap]
    log(f"Герой: кандидатов-картинок {len(cands)}")

    guard = HostGuard()

    def fetch(i: int, cand: StockCandidate) -> dict | None:
        if cancel and cancel.is_cancelled():
            raise CancelledError("Остановлено пользователем")
        ext = Path(cand.download_url.split("?")[0]).suffix.lower()
        trial = trials / f"hero_{i:03d}{ext if ext in ('.jpg', '.jpeg', '.png', '.webp') else '.jpg'}"
        if guard.blocked(cand.download_url):
            return None
        # фото — быстро: рукопожатие дольше 4 с и вторая попытка не нужны (кандидатов с запасом)
        ok = _download(http, cand.download_url, trial, retries=1, connect_timeout=4.0, max_seconds=30.0, max_bytes=30_000_000)
        guard.report(cand.download_url, ok)
        if not ok:
            trial.unlink(missing_ok=True)
            return None
        from engine.slides.theme_media import _image_short_side

        side = _image_short_side(trial)
        if side < IMAGE_MIN_SIDE:
            trial.unlink(missing_ok=True)
            return None
        vecs = faces.face_embeddings(trial) if photo else []
        if photo and not vecs:
            trial.unlink(missing_ok=True)  # нет лица — здание, пейзаж, плакат
            return None
        if not photo and not _is_depiction(trial, name):
            trial.unlink(missing_ok=True)
            return None
        return {"trial": trial, "cand": cand, "side": side, "faces": vecs}

    # Качаем волнами и останавливаемся, как только подходящих (с лицом героя, разных) набралось на ролик:
    # раньше скачивалось и проверялось всё, что нашла выдача (в 3–4 раза больше нужного).
    got: list[dict] = []
    rejected_i = 0
    hero = None
    exact = {quote_name(q, name).lower() for q in anchors} if photo and anchors else set()

    def exact_faces() -> list:
        # лицо героя: фото из точных запросов и главное фото его статьи в Википедии
        return [h["faces"] for h in got if str(h["cand"].query or "").lower() in exact or (h["cand"].meta or {}).get("hero_ref")]

    def enough() -> bool:
        """Подходящих разных фото уже хватает (лицо героя известно) — остальное качать незачем."""
        if photo and hero is None:
            return False
        mine = [h for h in got if not photo or faces.is_hero_shot(h["faces"], hero)]
        hashes: list[int] = []
        for h in mine:
            if "hash" not in h:
                h["hash"] = image_hash(h["trial"])
            if h["hash"] is None or not any(_hamming(h["hash"], k) <= DUP_BITS for k in hashes):
                if h["hash"] is not None:
                    hashes.append(h["hash"])
                else:
                    hashes.append(-1)
        return len(hashes) >= image_need

    pos, done_n = 0, 0
    wave = max(40, math.ceil(image_need * 1.4))
    while pos < len(cands):
        batch = list(enumerate(cands[pos:pos + wave], start=pos))
        pos += wave
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
            futs = [ex.submit(fetch, i, c) for i, c in batch]
            for fut in as_completed(futs):
                if cancel:
                    cancel.check()
                hit = fut.result()
                done_n += 1
                if hit is None:
                    rejected_i += 1
                else:
                    got.append(hit)
                if done_n % 10 == 0:
                    progress(pct_lo + span * 0.55 * min(1.0, done_n / max(1, len(cands))),
                             f"Фото героя: проверено {done_n}/{len(cands)}, подходит {len(got)}")
        if photo and hero is None and len(exact_faces()) >= faces.MIN_HERO_IMAGES:
            hero = faces.find_hero(exact_faces())
            if hero is not None:
                log(f"Герой: лицо «{name}» взято по точным запросам ({len(exact_faces())} фото) — тёзки отсекаются")
        if enough():
            if pos < len(cands):
                log(f"Герой: фото набралось ({len(got)} проверено, остальные {len(cands) - pos} адресов не качаю)")
            break
    log(f"Герой: проверено {done_n} адресов, не отвечали/не подошли {rejected_i}")

    if photo and hero is None and exact:
        sure = exact_faces()
        hero = faces.find_hero(sure) if sure else None
        if hero is not None:
            log(f"Герой: лицо «{name}» взято по точным запросам ({len(sure)} фото) — тёзки отсекаются")
    if photo and hero is None:
        hero = faces.find_hero([h["faces"] for h in got])
    if photo and hero is not None:
        keep = [h for h in got if faces.is_hero_shot(h["faces"], hero)]
        rejected_i += len(got) - len(keep)
        for h in got:
            if h not in keep:
                h["trial"].unlink(missing_ok=True)
        got = keep
    elif photo:
        log("Герой: лицо не опознано — беру фото с лицами по его имени")

    # первыми: без водяного знака; где герой — главное (самое крупное) лицо; крупнее по размеру
    for h in got:
        h["main"] = not photo or hero is None or (bool(h["faces"]) and float(h["faces"][0] @ hero) >= faces.HERO_MATCH)
        h["hash"] = image_hash(h["trial"])
    got.sort(key=lambda h: (bool((h["cand"].meta or {}).get("watermark")), not h["main"], -min(h["side"], 1080)))
    images: list[dict] = []
    kept_hashes: list[int] = []
    dupes = 0
    for h in got:
        # одна и та же фотография с разных сайтов — один раз (оставляем лучшую — она раньше)
        if h["hash"] is not None and any(_hamming(h["hash"], k) <= DUP_BITS for k in kept_hashes):
            h["trial"].unlink(missing_ok=True)
            dupes += 1
            continue
        if len(images) >= image_need:
            h["trial"].unlink(missing_ok=True)
            continue
        if h["hash"] is not None:
            kept_hashes.append(h["hash"])
        dest = pool / f"photo_{len(images):02d}_{h['cand'].provider}{h['trial'].suffix}"
        h["trial"].replace(dest)
        images.append({
            "path": str(dest.relative_to(pool.parent)),
            "score": 1.0,
            "query": h["cand"].query,
            "provider": h["cand"].provider,
            "id": h["cand"].id,
            "duration": 0.0,
            "hero": True,
        })
    log(f"Герой: фото {len(images)} (цель {image_need}), отбраковано {rejected_i}, дублей {dupes}")
    progress(pct_lo + span * 0.6, f"Фото героя: {len(images)}")

    # --- видео: только YouTube CC, в названии — его имя, в кадре — его лицо ---
    videos: list[dict] = []
    rejected_v = 0
    youtube = clients.get("youtube")
    if youtube is not None and video_need > 0:
        vc = [c for c in _search(clients, "youtube", vid_q, video=True, cancel=cancel)
              if name_in(str((c.meta or {}).get("title") or ""), name)]
        log(f"Герой: видео-кандидатов с именем в названии {len(vc)}")
        for i, cand in enumerate(vc[: video_need * VIDEO_TRIALS_PER_NEED]):
            if cancel:
                cancel.check()
            if len(videos) >= video_need:
                break
            trial = trials / f"hero_v{i:03d}.mp4"
            ok = youtube.download_clip(cand, trial, clip_sec=CLIP_SEC, rng=random.Random(cand.id))
            if not ok or not trial.is_file() or not is_landscape(trial) or not video_height_ok(trial, MIN_HEIGHT):
                trial.unlink(missing_ok=True)
                rejected_v += 1
                continue
            use_sec = min(CLIP_SEC, max(2.0, probe_duration(trial)))
            shots = _extract_frames(trial, frames, use_sec)
            if photo:
                vecs = [v for s in shots for v in faces.face_embeddings(s)]
                fine = bool(vecs) and (hero is None or faces.hero_score(vecs, hero) >= faces.HERO_MATCH)
            else:
                fine = bool(shots) and _is_depiction(shots[len(shots) // 2], name)
            for s in shots:
                s.unlink(missing_ok=True)
            if not fine:
                trial.unlink(missing_ok=True)
                rejected_v += 1
                continue
            dest = pool / f"video_{len(videos):02d}_youtube.mp4"
            trial.replace(dest)
            videos.append({
                "path": str(dest.relative_to(pool.parent)),
                "score": 1.0,
                "query": cand.query,
                "provider": "youtube",
                "id": cand.id,
                "duration": round(use_sec, 3),
                "hero": True,
                "source_title": str((cand.meta or {}).get("title") or ""),
                "source_author": str((cand.meta or {}).get("channel") or ""),
            })
            progress(pct_lo + span * (0.6 + 0.4 * len(videos) / max(1, video_need)), f"Видео с героем: {len(videos)}")
        log(f"Герой: видео {len(videos)} (цель {video_need}), отбраковано {rejected_v}")
    return videos, images, rejected_v, rejected_i
