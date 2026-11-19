"""Пул по теме: картинки по смыслу, видео-вставки если успели найти."""

from __future__ import annotations

import re

import json
import random
import shutil
import threading
import time
from concurrent.futures import FIRST_COMPLETED, as_completed, wait
from pathlib import Path

import httpx

from engine.core.threads import ThreadPoolExecutor
from engine.slides import checkpoint as cp
from engine.core.cancel import CancelToken, CancelledError
from engine.slides.clip_ranker import get_clip
from engine.slides.ffprobe_util import (
    grab_frame,
    candidate_height_ok,
    candidate_is_landscape,
    is_landscape,
    probe_duration,
    video_height_ok,
)
from engine.slides.llm_client import DEFAULT_BASE_URL, DEFAULT_MODEL, LLMClient
from engine.core.logutil import log
from engine.slides.media_pipeline import (
    MediaError,
    MediaRequest,
    MediaStageResult,
    _build_clients,
    _download,
    _search_source,
)
from engine.slides.paths import SCRIPT_NAME, TIMELINE_NAME, desktop_copy_name
from engine.slides.stocks_base import StockCandidate
from engine.slides.theme_plan import (
    ThemePlan,
    ensure_theme_plan,
    _main_name,
)
from engine.slides.look import fingerprint, seen
from engine.slides.source_mix import GOOGLE_SHARE, MIN_HEIGHT, subject_of
from engine.slides.vision_ranker import score_fit

VIDEO_BLOCK_SEC = 60.0
VIDEO_SEARCH_SEC = 210.0
CLIP_SEC = 10.0
IMAGE_FIT_MIN = 6  # локальный CLIP мягче LLM-judge
IMAGE_NEED_MIN = 80
IMAGE_NEED_MAX = 100
GOOGLE_NEED_MIN = 1
VIDEO_NEED_MIN = 10
VIDEO_NEED_MAX = 15
HERO_IMAGE_CAP = 22
HERO_VIDEO_CAP = 3
# смена кадра: фото 3–5 с (держим 4 с), видео — не дольше 6 с
IMAGE_HOLD_SEC = 4.0
VIDEO_HOLD_SEC = 6.0
FADE_SEC = 0.5
VISION_MIN = 4
SEARCH_WORKERS = 8
FETCH_WORKERS = 6
MAX_VIDEO_TRIALS = 40
MAX_IMAGE_TRIALS = 80


def collect_theme_pool(
    req: MediaRequest,
    *,
    cancel: CancelToken | None = None,
    on_progress=None,
) -> MediaStageResult:
    def progress(pct: float, msg: str) -> None:
        log(msg)
        if on_progress:
            on_progress(pct, msg)

    if not (req.llm_api_key or "").strip():
        raise MediaError("Нужен LLM key: план темы / запросы")

    run_dir = req.run_dir
    script_path = run_dir / SCRIPT_NAME
    if not script_path.is_file():
        raise MediaError(f"Нет {SCRIPT_NAME}")
    script = script_path.read_text(encoding="utf-8")
    (run_dir / "ai_report.json").unlink(missing_ok=True)  # отчёт AI-кадров — заново на этот подбор

    progress(0.02, "Тема: читаю весь текст…")
    client = _llm(req)
    if cancel:
        cancel.register(client)
    try:
        plan = ensure_theme_plan(run_dir, script, client)
    finally:
        client.close()
    progress(
        0.08,
        f"Тема: {plan.summary} | video q={len(plan.video_queries)} "
        f"image q={len(plan.image_queries)}",
    )

    clients = _build_clients(req.keys)
    from engine.slides.judge import make_judge

    judge = make_judge(req.llm_api_key, req.llm_model or "", req.llm_base_url or "", plan)
    if judge is not None and cancel:
        cancel.register(judge.client)
    rng = random.Random(req.seed)
    image_need, video_need = pool_targets(rng)
    log(f"На этот ролик: картинок {image_need}, видео {video_need}")
    pool = run_dir / "pool"
    if pool.exists():
        shutil.rmtree(pool, ignore_errors=True)
    trials = pool / "_trials"
    frames = pool / "_frames"
    trials.mkdir(parents=True, exist_ok=True)
    frames.mkdir(parents=True, exist_ok=True)
    http = httpx.Client(timeout=120.0, trust_env=False, follow_redirects=True)

    from engine.slides.person_media import collect_person_pool, person_mode

    # people=False — подбор по героям уже пробовали (stars_media): повторять те же поиски незачем
    hero = person_mode(plan) if req.people else None
    try:
        videos: list[dict] = []
        images: list[dict] = []
        rejected_v = rejected_i = 0
        if hero is not None:
            # ролик про конкретного человека — только кадры с ним
            videos, images, rejected_v, rejected_i = collect_person_pool(
                http, clients, plan, name=hero[0], kind=hero[1], rng=rng, pool=pool, trials=trials,
                frames=frames, cancel=cancel, progress=progress, pct_lo=0.12, pct_hi=0.92,
                image_need=image_need, video_need=video_need,
            )
            if not images:
                log(f"Фото «{hero[0]}» не нашлось — подбираю по теме, как обычно")
                hero = None
        if hero is None:
            videos, images, rejected_v, rejected_i = gather_pool(
                http,
                clients,
                plan,
                rng=rng,
                pool=pool,
                trials=trials,
                frames=frames,
                req=req,
                cancel=cancel,
                progress=progress,
                pct_lo=0.12,
                pct_hi=0.92,
                image_need=image_need,
                video_need=video_need,
                video_search_sec=VIDEO_SEARCH_SEC,
                # 80–100 картинок всего: Google по доле темы + стоки до цели (не сверху — дольше)
                stocks_on_top_of_google=False,
                judge=judge,
            )
    finally:
        http.close()
        for c in clients.values():
            try:
                c.close()
            except Exception:
                pass
        shutil.rmtree(trials, ignore_errors=True)
        shutil.rmtree(frames, ignore_errors=True)

    if req.ai is not None and req.ai.active and hero is None:
        # живые кадры важнее: AI — только нехватка и слабые, не больше ~12% пула
        from engine.slides.ai_fill import fill_pool

        progress(0.92, "AI-кадры: генерирую для слабых и недостающих кадров…")
        videos, images = fill_pool(
            videos, images, plan, req.ai, pool=pool, image_target=image_need, video_target=video_need,
            cancel=cancel, progress=lambda p, m: progress(0.92 + 0.06 * p, m), judge=judge,
            moments=_story_moments(script),
        )
    if judge is not None:
        log(f"Проверка кадров LLM: {judge.calls} проверок" + ("" if judge.enabled else " (выключилась по ходу — дальше был CLIP)"))
        judge.close()

    from engine.slides.hook import add_beauty

    add_beauty([*videos, *images], run_dir)  # первые минуты ролика — красивейшие кадры
    video_sec = sum(float(v.get("duration") or CLIP_SEC) for v in videos)
    if not videos:
        log("Подходящих видео нет. Ролик будет из картинок.")
    elif len(videos) < video_need:
        log(f"Видео {len(videos)} из {video_need}. Добирать не буду.")
    if not images:
        raise MediaError("Не нашёл ни одной картинки по теме (после проверки файла)")

    payload = {
        "mode": "theme_pool",
        "summary": plan.summary,
        "video_block_sec": round(video_sec, 3),
        "clip_sec": CLIP_SEC,
        "image_hold_sec": IMAGE_HOLD_SEC,
        "video_hold_sec": VIDEO_HOLD_SEC,
        "fade_sec": FADE_SEC,
        "story_context": plan.context.to_dict(),
        "videos": videos,
        "images": images,
        # ролик про человека: пул только с ним (подходит к любому режиму подбора)
        "person": hero[0] if hero is not None else "",
    }
    timeline_file = run_dir / TIMELINE_NAME
    timeline_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    preview_file = run_dir / "timeline_preview.txt"
    lines = [
        plan.summary,
        f"videos={len(videos)} уникальных, {video_sec:.0f}с, отбраковано {rejected_v}",
        f"images={len(images)} (цель {image_need}, дальше повтор блока), "
        f"отбраковано {rejected_i}",
        "",
    ]
    for item in videos:
        lines.append(
            f"VIDEO {item.get('score')} {item.get('duration')}s  "
            f"{item.get('query')}  {item.get('path')}"
        )
    for item in images:
        lines.append(f"IMAGE {item.get('score')}  {item.get('query')}  {item.get('path')}")
    preview_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    desktop = None
    if req.copy_preview_to_desktop:
        dest = desktop_copy_name(f"timeline_{run_dir.name}", ".txt")
        shutil.copy2(preview_file, dest)
        desktop = dest

    selected = len(videos) + len(images)
    chk = cp.load(run_dir)
    cp.mark_media_done(
        run_dir,
        language=chk.language,
        voice_id=chk.voice_id,
        text_chars=chk.text_chars,
        audio_bytes=chk.audio_bytes,
        desktop_audio=chk.desktop_audio,
        word_count=chk.word_count,
        duration=chk.duration,
        desktop_preview=chk.desktop_preview,
        chunk_count=chk.chunk_count,
        desktop_chunks=chk.desktop_chunks,
        selected_media=selected,
        desktop_timeline=str(desktop or ""),
    )
    progress(
        1.0,
        f"Пул: видео {len(videos)}/{video_need} ({video_sec:.0f}с), картинки {len(images)}/{image_need}",
    )
    return MediaStageResult(
        run_dir=run_dir,
        timeline_file=timeline_file,
        preview_file=preview_file,
        desktop_preview=desktop,
        selected=selected,
        skipped=rejected_v + rejected_i,
        weak=0,
    )


def gather_pool(
    http: httpx.Client,
    clients: dict,
    plan: ThemePlan,
    *,
    rng: random.Random,
    pool: Path,
    trials: Path,
    frames: Path,
    req: MediaRequest,
    cancel: CancelToken | None,
    progress,
    pct_lo: float,
    pct_hi: float,
    image_need: int,
    video_need: int,
    video_search_sec: float,
    stocks_on_top_of_google: bool = False,
    exclude: list[dict] | None = None,
    judge=None,
) -> tuple[list[dict], list[dict], int, int]:
    """Пул по теме: видео (до video_search_sec), потом картинки — доля Google по теме,
    остальное стоки/Wikimedia/Archive. Возвращает (videos, images, отбраковано видео, картинок).

    stocks_on_top_of_google: стоки добирают ещё image_need сверх Google (режим «по смыслу»);
    иначе всего картинок ~image_need.
    exclude: уже стоящие в ролике кадры (начало строгого режима) — их и похожие на них не берём.
    """
    span = pct_hi - pct_lo

    def at(frac: float) -> float:
        return pct_lo + span * frac

    progress(at(0.0), f"Видео: ищу до {video_search_sec / 60:.0f} мин, потом картинки")
    videos, rejected_v = _fill_kind(
        http, clients, plan, rng=rng, pool=pool, trials=trials, frames=frames,
        kind="video", queries=plan.video_queries, req=req, cancel=cancel, progress=progress,
        pct_lo=at(0.0), pct_hi=at(0.4), deadline=time.monotonic() + video_search_sec, need=video_need,
        exclude=exclude, judge=judge,
    )
    images: list[dict] = []
    rejected_g = 0
    custom = getattr(req, "source_mix", None)
    google_need = _google_need(plan, image_need, custom)
    if "google" in clients and google_need > 0:
        progress(at(0.45), "Google Images: браузерный поиск…")
        images, rejected_g = _fill_kind(
            http, clients, plan, rng=rng, pool=pool, trials=trials, frames=frames,
            kind="photo", queries=plan.image_queries, req=req, cancel=cancel, progress=progress,
            pct_lo=at(0.45), pct_hi=at(0.7), need=google_need, sources=["google"], exclude=exclude, judge=judge,
        )
        progress(at(0.7), f"Google: {sum(1 for x in images if x.get('provider') == 'google')}/{google_need}")
    progress(at(0.72), "Стоки/wikimedia: добираю картинки…")
    total = len(images) + image_need if stocks_on_top_of_google else image_need
    if _subject(plan) == "person" and len(images) >= image_need * GOOGLE_SHARE["person"] * 0.6:
        # ролик про человека: Google нашёл его фото — стоки добавляют лишь немного фона
        total = max(len(images) + round(image_need * 0.3), image_need)
    images, rejected_i = _fill_kind(
        http, clients, plan, rng=rng, pool=pool, trials=trials, frames=frames,
        kind="photo", queries=plan.image_queries, req=req, cancel=cancel, progress=progress,
        pct_lo=at(0.72), pct_hi=at(1.0), already=images, need=max(total, len(images)),
        exclude=exclude, judge=judge,
        # современная тема — сначала свежие качественные стоки, старые архивы — в запас
        sources=["wikimedia", "archive", "stocks"] if plan.context.historical else ["stocks", "wikimedia", "archive"],
    )
    return videos, images, rejected_v, rejected_i + rejected_g


def _fill_kind(
    http: httpx.Client,
    clients: dict,
    plan: ThemePlan,
    *,
    rng: random.Random,
    pool: Path,
    trials: Path,
    frames: Path,
    kind: str,
    queries: list[str],
    req: MediaRequest,
    cancel: CancelToken | None,
    progress,
    pct_lo: float,
    pct_hi: float,
    already: list[dict] | None = None,
    deadline: float | None = None,
    need: int = IMAGE_NEED_MIN,
    sources: list[str] | None = None,
    exclude: list[dict] | None = None,
    judge=None,
) -> tuple[list[dict], int]:
    prefer_video = kind == "video"
    custom = getattr(req, "source_mix", None)
    if sources is None and custom:
        from engine.slides.source_mix import ordered_sources

        sources = ordered_sources(custom, video=prefer_video) or ["stocks"]
    elif sources is not None and custom and sources != ["google"]:
        # свои доли: источник с 0% не ищем, остальные — по убыванию доли
        from engine.slides.source_mix import ordered_sources

        allowed = ordered_sources(custom, video=prefer_video)
        sources = [s for s in allowed if s in sources] or sources
    if sources is None:
        if prefer_video and plan.context.historical:
            sources = ["youtube", "archive", "stocks"]
        elif prefer_video:
            sources = ["stocks", "youtube", "archive"]
        else:
            sources = ["google", "wikimedia", "archive", "stocks"]
    # Порядок проверки кандидатов (берутся первыми, пока не наберётся нужное):
    # источники в порядке sources (для современной темы — свежие стоки впереди архивов),
    # внутри источника запросы вперемешку (разнообразие); потом герой ролика вперёд (_hero_first);
    # превью платных фотостоков (водяной знак) — в самый конец, только если чистых не хватило.
    rank = {name: i for i, name in enumerate(sources)}

    def _order(c: StockCandidate) -> int:
        group = "stocks" if c.provider in ("pexels", "pixabay", "unsplash") else c.provider
        return rank.get(group, len(rank))

    gathered = _gather(
        clients, queries, sources, prefer_video=prefer_video, rng=rng, cancel=cancel,
        hero_name=_main_name(plan.context.characters),
    )
    gathered.sort(key=_order)
    found = _hero_first(gathered, plan, prefer_video=prefer_video)
    found.sort(key=lambda c: bool((c.meta or {}).get("watermark")))
    progress(pct_lo, f"{kind}: кандидатов {len(found)}, качаю и смотрю файл")
    saved: list[dict] = list(already or [])
    start_n = len(saved)
    have = {
        (str(item.get("provider") or ""), str(item.get("id") or ""))
        for item in [*saved, *(exclude or [])]
    }
    # как выглядят уже взятые кадры: одинаковое фото с другого сайта или тот же клип — не берём
    looks = [fingerprint(pool.parent / str(item.get("path") or "")) for item in [*saved, *(exclude or [])]]
    unique_sec = sum(float(v.get("duration") or 0) for v in saved) if prefer_video else 0.0
    rejected = 0
    trials_left = max(need * 3, MAX_VIDEO_TRIALS if prefer_video else MAX_IMAGE_TRIALS)
    # Для Google почти все запросы — про героя; иначе hero_cap режет пул раньше need.
    if sources == ["google"]:
        hero_cap = need
    elif prefer_video:
        hero_cap = HERO_VIDEO_CAP
    else:
        hero_cap = max(1, round(need * HERO_IMAGE_CAP / IMAGE_NEED_MIN))
    get_clip()
    pending: dict = {}
    cand_iter = iter(found)
    exhausted = False

    def _full() -> bool:
        return len(saved) >= need

    def _over_time() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    def _saturated() -> bool:
        if _over_time() or _full() or trials_left <= 0:
            return True
        return len(saved) + len(pending) >= need

    def _job(cand: StockCandidate, hero: bool) -> dict | None:
        if cancel and cancel.is_cancelled():
            raise CancelledError("Остановлено пользователем")
        return _evaluate_candidate(
            http,
            cand,
            plan,
            youtube=clients.get("youtube"),
            trials=trials,
            frames=frames,
            kind=kind,
            prefer_video=prefer_video,
            hero=hero,
            min_height=MIN_HEIGHT,
            judge=judge,
        )

    # проверка LLM отвечает 15–50 с — кандидатов в работе больше, иначе пул собирается вдвое дольше
    workers = FETCH_WORKERS * 2 if judge is not None and judge.enabled else FETCH_WORKERS
    with ThreadPoolExecutor(max_workers=workers) as pool_ex:
        while not exhausted or pending:
            if cancel:
                cancel.check()
            if _over_time() and not pending:
                log(f"{kind}: прошло {VIDEO_SEARCH_SEC / 60:.0f} мин, поиск стоп")
                break
            while not _saturated() and not exhausted and len(pending) < workers:
                cand = next(cand_iter, None)
                if cand is None:
                    exhausted = True
                    break
                if (cand.provider, cand.id) in have:
                    continue
                if prefer_video and not candidate_is_landscape(
                    cand.width, cand.height
                ):
                    continue
                if prefer_video and not candidate_height_ok(
                    cand.width, cand.height, MIN_HEIGHT
                ):
                    continue
                hero = _is_hero(cand.query or "", plan)
                hero_n = sum(1 for item in saved if item.get("hero"))
                if hero and hero_n >= hero_cap:
                    continue
                if trials_left <= 0:
                    exhausted = True
                    log(f"{kind}: лимит скачиваний, стоп")
                    break
                trials_left -= 1
                have.add((cand.provider, cand.id))
                fut = pool_ex.submit(_job, cand, hero)
                pending[fut] = hero
            if not pending:
                break
            done, _ = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for fut in done:
                pending.pop(fut)
                try:
                    hit = fut.result()
                except CancelledError:
                    raise
                except Exception as exc:
                    log(f"{kind}: ошибка кандидата: {exc}")
                    rejected += 1
                    continue
                if not hit or not hit.get("ok"):
                    if hit and hit.get("rejected"):
                        rejected += 1
                    continue
                if _full():
                    Path(hit["trial"]).unlink(missing_ok=True)
                    continue
                hero_n = sum(1 for item in saved if item.get("hero"))
                if hit["hero"] and hero_n >= hero_cap:
                    Path(hit["trial"]).unlink(missing_ok=True)
                    continue
                trial = Path(hit["trial"])
                fp = fingerprint(trial)
                if seen(fp, looks):
                    log(f"{kind}: такой кадр уже есть (другой сайт / тот же клип) — пропускаю")
                    trial.unlink(missing_ok=True)
                    rejected += 1
                    continue
                looks.append(fp)
                ext = trial.suffix or ".jpg"
                dest = pool / f"{kind}_{len(saved):02d}_{hit['provider']}{ext}"
                trial.replace(dest)
                use_sec = float(hit["duration"])
                if prefer_video:
                    unique_sec += use_sec
                saved.append(
                    {
                        "path": str(dest.relative_to(pool.parent)),
                        "score": round(float(hit["score"]), 3),
                        "query": hit["query"],
                        "provider": hit["provider"],
                        "id": hit["id"],
                        "duration": round(use_sec, 3),
                        "hero": hit["hero"],
                        "source_title": hit.get("source_title", ""),
                        "source_author": hit.get("source_author", ""),
                        # качество съёмки по LLM (0–10): первые минуты ролика — из лучших
                        "quality": hit.get("quality"),
                    }
                )
                progress(
                    pct_lo
                    + (pct_hi - pct_lo)
                    * min(
                        1.0,
                        len(saved) / max(1, need),
                    ),
                    f"{kind}: взял {len(saved) - start_n}, отбраковал {rejected}"
                    + (f", {unique_sec:.0f}с" if prefer_video else ""),
                )
    return saved, rejected


def _evaluate_candidate(
    http: httpx.Client,
    cand: StockCandidate,
    plan: ThemePlan,
    *,
    trials: Path,
    frames: Path,
    kind: str,
    prefer_video: bool,
    hero: bool,
    min_height: int,
    youtube=None,
    judge=None,
) -> dict | None:
    ext = ".mp4" if prefer_video else _ext_from_url(cand.download_url)
    trial = trials / f"{kind}_{cand.provider}_{cand.id}{ext}"
    if cand.provider == "youtube":
        # нарезка CLIP_SEC из CC-видео (только видеопоток, не ниже 720p)
        ok = youtube is not None and youtube.download_clip(
            cand, trial, clip_sec=CLIP_SEC, rng=random.Random(cand.id)
        )
    else:
        ok = _download(http, cand.download_url, trial, retries=3)
    if not ok:
        trial.unlink(missing_ok=True)
        return None
    if not trial.is_file() or trial.stat().st_size < 1000:
        trial.unlink(missing_ok=True)
        return None
    use_sec = CLIP_SEC
    if prefer_video:
        if not is_landscape(trial):
            log(f"skip portrait video {trial.name}")
            trial.unlink(missing_ok=True)
            return {"ok": False, "rejected": True}
        if not video_height_ok(trial, min_height):
            log(f"skip low-res video {trial.name} (< {min_height}p)")
            trial.unlink(missing_ok=True)
            return {"ok": False, "rejected": True}
        try:
            use_sec = min(CLIP_SEC, max(2.0, probe_duration(trial)))
        except Exception:
            use_sec = CLIP_SEC
        samples = _extract_frames(trial, frames, use_sec)
        samples = _middle_first(samples)[:1]
    else:
        if _image_short_side(trial) < MIN_HEIGHT:
            log(f"skip low-res image {trial.name} (< {MIN_HEIGHT}px)")
            trial.unlink(missing_ok=True)
            return {"ok": False, "rejected": True}
        samples = [trial]
    if not samples:
        log(f"Не вырезал кадры, выкинул {trial.name}")
        trial.unlink(missing_ok=True)
        return {"ok": False, "rejected": True}
    score = _accept_samples(
        _middle_first(samples) if not prefer_video else samples,
        plan,
        cand.query or "",
        strict=not prefer_video,
    )
    for sample in samples:
        if sample != trial:
            sample.unlink(missing_ok=True)
    if score is None:
        log(f"Не по смыслу, меняю: {cand.provider} {cand.id} ({cand.query})")
        trial.unlink(missing_ok=True)
        return {"ok": False, "rejected": True}
    quality = None
    if judge is not None and judge.enabled:
        judged = _judge_pool_item(judge, trial, cand, prefer_video=prefer_video)
        if judged is None:
            trial.unlink(missing_ok=True)
            return {"ok": False, "rejected": True}
        if judged != _CLIP_KEEP:
            score, quality = judged
    return {
        "ok": True,
        "trial": str(trial),
        "score": score,
        "duration": use_sec,
        "query": cand.query,
        "provider": cand.provider,
        "id": cand.id,
        "hero": hero,
        "source_title": str((cand.meta or {}).get("title") or ""),
        "source_author": str((cand.meta or {}).get("channel") or ""),
        "quality": quality,
    }


def _judge_pool_item(judge, trial: Path, cand: StockCandidate, *, prefer_video: bool):
    """Кадр пула глазами LLM: подходит ли ролику (страна, эпоха, тема) и хороша ли съёмка.
    → (оценка 0–1, качество съёмки 0–10); None — не берём; _CLIP_KEEP — проверки не было."""
    from engine.slides.shake import SHAKY, shakiness

    shake = shakiness(trial) if prefer_video else None
    if shake is not None and shake > 2 * SHAKY:
        log(f"Пул: {cand.provider} {cand.id} — сильная тряска ({shake:.1f}), не беру")
        return None
    v = judge.pool().rate(trial, cand.query or "")
    if v is None:
        return _CLIP_KEEP
    log(f"Пул: {cand.provider} {'видео' if prefer_video else 'фото'} ({cand.query}) — {v.note()}")
    if not v.ok:
        return None
    score = v.score - (1 if shake is not None and shake > SHAKY else 0)
    quality = v.quality - (2 if shake is not None and shake > SHAKY else 0)
    return round(max(0.0, score) / 10, 3), max(0, quality)


_CLIP_KEEP = -1.0  # метка «проверки не было»: оценку CLIP не трогаем


def _gather(
    clients: dict,
    queries: list[str],
    sources: list[str],
    *,
    prefer_video: bool,
    rng: random.Random,
    cancel: CancelToken | None,
    hero_name: str = "",
) -> list[StockCandidate]:
    jobs: list[tuple[str, str, int]] = []
    hero = hero_name.strip().lower()
    seed_base = rng.randrange(1 << 30)
    n = 0
    for q in queries:
        for source in sources:
            if source not in clients and source != "stocks":
                continue
            if source == "stocks" and not any(
                name in clients for name in ("pexels", "pixabay", "unsplash")
            ):
                continue
            # на Pexels/Pixabay/Unsplash конкретного человека нет — по его имени они дают
            # случайные статуи и портреты незнакомцев; там ищем только эпоху и место
            if source == "stocks" and hero and hero in q.lower():
                continue
            jobs.append((q, source, seed_base + n))
            n += 1
    found: list[StockCandidate] = []
    seen: set[tuple[str, str]] = set()
    lock = threading.Lock()

    def _one(job: tuple[str, str, int]) -> None:
        if cancel and cancel.is_cancelled():
            raise CancelledError("Остановлено пользователем")
        query, source, seed = job
        batch = _search_source(
            clients,
            source,
            query,
            prefer_video=prefer_video,
            rng=random.Random(seed),
        )
        picked: list[StockCandidate] = []
        for cand in batch:
            if prefer_video and cand.kind != "video":
                continue
            if not prefer_video and cand.kind == "video":
                continue
            if prefer_video and not candidate_is_landscape(cand.width, cand.height):
                continue
            if not prefer_video and cand.width and cand.height and min(cand.width, cand.height) < MIN_HEIGHT:
                continue
            cand.query = query
            picked.append(cand)
        with lock:
            for cand in picked:
                key = (cand.provider, cand.id)
                if key in seen:
                    continue
                seen.add(key)
                found.append(cand)

    if jobs:
        with ThreadPoolExecutor(max_workers=min(SEARCH_WORKERS, len(jobs))) as ex:
            futs = [ex.submit(_one, job) for job in jobs]
            for fut in as_completed(futs):
                if cancel:
                    cancel.check()
                fut.result()
    rng.shuffle(found)
    return found


def pool_targets(rng: random.Random) -> tuple[int, int]:
    """Сколько картинок и роликов целимся взять на этот фильм."""
    return (
        rng.randint(IMAGE_NEED_MIN, IMAGE_NEED_MAX),
        rng.randint(VIDEO_NEED_MIN, VIDEO_NEED_MAX),
    )


def _image_short_side(path: Path) -> int:
    try:
        from PIL import Image

        with Image.open(path) as img:
            return min(img.size)
    except Exception:
        return 0


def _subject(plan: ThemePlan) -> str:
    ctx = plan.context
    return subject_of(ctx.subject, visual_style=ctx.visual_style, historical=ctx.historical)


def _google_need(plan: ThemePlan, image_need: int, custom: dict | None = None) -> int:
    """Сколько картинок пула брать из Google: про человека — больше, про места — меньше
    (свои доли источников — по ним)."""
    if custom:
        from engine.slides.source_mix import google_share

        return round(image_need * google_share(custom))
    ctx = plan.context
    subject = subject_of(ctx.subject, visual_style=ctx.visual_style, historical=ctx.historical)
    return max(GOOGLE_NEED_MIN, round(image_need * GOOGLE_SHARE[subject]))


def _is_hero(query: str, plan: ThemePlan) -> bool:
    name = _main_name(plan.context.characters).lower()
    return bool(name) and name in (query or "").lower()


def _hero_first(
    found: list[StockCandidate], plan: ThemePlan, *, prefer_video: bool
) -> list[StockCandidate]:
    hero = [c for c in found if _is_hero(c.query or "", plan)]
    rest = [c for c in found if not _is_hero(c.query or "", plan)]
    if not hero:
        return found
    front = 8 if prefer_video else 40
    return hero[:front] + rest + hero[front:]


def _accept_samples(
    samples: list[Path],
    plan: ThemePlan,
    query: str,
    *,
    strict: bool = False,
) -> float | None:
    """CLIP-отсев: берём min по кадрам (обычно один средний кадр)."""
    pos = plan.clip_text()
    fits: list[int] = []
    for path in samples:
        try:
            fit, reason = score_fit(
                None,
                path,
                narration=plan.summary,
                query=query or pos,
                era=plan.context.era,
                place=plan.context.place,
                historical=plan.context.historical,
            )
        except Exception as exc:
            log(f"CLIP ошибка {path.name}: {exc}")
            return None
        log(f"Кадр {path.name}: fit={fit} ({reason[:80]})")
        if not _frame_ok(fit, reason, strict=strict):
            return None
        fits.append(fit)
    if not fits:
        return None
    return min(fits) / 10.0


def _frame_ok(fit: int, reason: str, *, strict: bool = False) -> bool:
    _ = reason
    if strict:
        return fit >= IMAGE_FIT_MIN
    if fit >= 6:
        return True
    if fit < VISION_MIN:
        return False
    return True


def _middle_first(samples: list[Path]) -> list[Path]:
    if len(samples) < 2:
        return samples
    mid = len(samples) // 2
    return [samples[mid], *samples[:mid], *samples[mid + 1 :]]


def _extract_frames(video: Path, dest_dir: Path, window_sec: float) -> list[Path]:
    window = max(0.4, min(window_sec, CLIP_SEC))
    stamps = [max(0.05, window * frac) for frac in (0.12, 0.5, 0.82)]
    out: list[Path] = []
    for i, t in enumerate(stamps):
        dest = dest_dir / f"{video.stem}_f{i}.jpg"
        if grab_frame(video, dest, at=t):
            out.append(dest)
        else:
            dest.unlink(missing_ok=True)
    return out


def _story_moments(script: str, limit: int = 60) -> list[str]:
    """Фразы рассказа равномерно по всему тексту — под них пишутся AI-кадры пула."""
    sents = [x.strip() for x in re.split(r"(?<=[.!?…])\s+", script or "") if len(x.strip()) > 25]
    if len(sents) <= limit:
        return sents
    step = len(sents) / limit
    return [sents[int(k * step)] for k in range(limit)]


def _llm(req: MediaRequest) -> LLMClient:
    return LLMClient(
        req.llm_api_key,
        model=req.llm_model or DEFAULT_MODEL,
        base_url=req.llm_base_url or DEFAULT_BASE_URL,
    )


def _ext_from_url(url: str) -> str:
    low = (url or "").lower().split("?")[0]
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        if low.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"
