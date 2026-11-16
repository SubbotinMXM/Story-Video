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

import random
import re
import unicodedata
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
    name = _main_name(ctx.characters)
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


def person_query_set(plan: ThemePlan, name: str, kind: str) -> tuple[list[str], list[str]]:
    """Все запросы — с именем героя в кавычках (точное совпадение в поиске)."""
    images, videos = person_queries(name, plan.context.era, plan.context.place, kind)
    extra_i = [q for q in plan.image_queries if name.lower() in q.lower()]
    extra_v = [q for q in plan.video_queries if name.lower() in q.lower()]
    if kind == "photo":
        extra_i = [q for q in extra_i if not _STATUE_WORDS.search(q)]
        extra_v = [q for q in extra_v if not _STATUE_WORDS.search(q)]

    def quoted(qs: list[str]) -> list[str]:
        out: list[str] = []
        for q in qs:
            q = re.sub(re.escape(name), f'"{name}"', q, flags=re.I) if '"' not in q else q
            if q not in out:
                out.append(q)
        return out

    # запросы плана с уточнением («… советский актёр») — первыми: точнее, тёзка в выдаче реже
    return quoted(extra_i + images)[:16], quoted(extra_v + videos)[:8]


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
) -> tuple[list[dict], list[dict], int, int]:
    """Пул только с героем → (videos, images, отбраковано видео, картинок)."""
    from engine.slides.theme_media import CLIP_SEC, _extract_frames

    span = pct_hi - pct_lo
    img_q, vid_q = person_query_set(plan, name, kind)
    photo = kind == "photo"
    log(f"Герой «{name}» ({kind}): только его кадры. Запросы: {img_q[:6]}…")
    progress(pct_lo, f"Ищу фото: {name}")

    # --- картинки: Google + Wikimedia, скачиваем с запасом ---
    cands = _search(clients, "google", img_q, video=False, cancel=cancel)
    cands += _search(clients, "wikimedia", img_q[:6], video=False, cancel=cancel)
    cands.sort(key=lambda c: bool((c.meta or {}).get("watermark")))  # водяной знак — в конец
    cands = cands[: max(image_need * 4, 120)]
    log(f"Герой: кандидатов-картинок {len(cands)}")

    def fetch(i: int, cand: StockCandidate) -> dict | None:
        if cancel and cancel.is_cancelled():
            raise CancelledError("Остановлено пользователем")
        ext = Path(cand.download_url.split("?")[0]).suffix.lower()
        trial = trials / f"hero_{i:03d}{ext if ext in ('.jpg', '.jpeg', '.png', '.webp') else '.jpg'}"
        if not _download(http, cand.download_url, trial, retries=2):
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

    got: list[dict] = []
    rejected_i = 0
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
        futs = [ex.submit(fetch, i, c) for i, c in enumerate(cands)]
        for n, fut in enumerate(as_completed(futs), 1):
            if cancel:
                cancel.check()
            hit = fut.result()
            if hit is None:
                rejected_i += 1
            else:
                got.append(hit)
            if n % 10 == 0:
                progress(pct_lo + span * 0.55 * n / max(1, len(futs)), f"Фото героя: проверено {n}/{len(futs)}, подходит {len(got)}")

    hero = faces.find_hero([h["faces"] for h in got]) if photo else None
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
