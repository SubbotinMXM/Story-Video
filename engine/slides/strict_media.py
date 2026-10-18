"""Строгий ряд — кадр под фразу.

- «Строго первые 2 минуты»: 0…HEAD_SEC — кадр под каждую фразу, смена каждые 3–5 с,
  фото и видео чередуются (видео — примерно половина). Дальше — блок из пула по теме.
- «Строго весь ролик»: так же первые 2 минуты; дальше текст режется на куски 8–10 с,
  и на кусок — 2 разных кадра (≈4–5 с каждый) по его смыслу: кадр меняется часто,
  а запросов и поисков — как при одном кадре на кусок.
- Призыв подписаться/поставить лайк — не буквальный кадр, а видео по теме ролика.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
import json
import random
import shutil
import threading
from concurrent.futures import as_completed
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from engine.core.spread import spread
from engine.core.threads import ThreadPoolExecutor
from engine.slides import checkpoint as cp
from engine.core.cancel import CancelToken, CancelledError
from engine.slides.clip_ranker import get_clip
from engine.slides.ffprobe_util import (
    candidate_height_ok,
    candidate_is_landscape,
    is_landscape,
    probe_duration,
    probe_video_size,
)
from engine.slides.source_mix import (
    MIN_HEIGHT,
    TARGET_HEIGHT,
    Budget,
    SourceStats,
    allot,
    base_weights,
    subject_of,
)
from engine.slides.llm_client import LLMClient
from engine.core.logutil import log
from engine.slides.media_pipeline import (
    MediaError,
    MediaRequest,
    MediaStageResult,
    _build_clients,
    _download,
    _search_source,
)
from engine.slides.paths import AUDIO_NAME, SCRIPT_NAME, TIMELINE_NAME, TIMINGS_NAME, desktop_copy_name
from engine.slides.stocks_base import StockCandidate
from engine.slides.subtitles import ORIGIN_NAME, align_script_words
from engine.slides.theme_media import (
    FADE_SEC,
    IMAGE_FIT_MIN,
    _extract_frames,
    _ext_from_url,
    _frame_ok,
    _llm,
    _middle_first,
)
from engine.slides.look import fingerprint, seen
from engine.slides.theme_plan import _main_name, ThemePlan, ensure_theme_plan
from engine.slides.vision_ranker import score_fit
from engine.slides.whisper_timings import WordTiming

# начало ролика: кадр под каждую фразу, смена каждые 3–5 с
BEAT_MIN = 3.0
BEAT_MAX = 5.0
# «весь ролик» после начала: куски текста 8–10 с, на каждый — FULL_SHOTS кадров
FULL_BEAT_MIN = 8.0
FULL_BEAT_MAX = 10.0
FULL_SHOTS = 2
TAIL_MERGE = 2.0
# длительность «начала» (строго под фразы в обоих строгих режимах)
HEAD_SEC = 120.0
VIDEO_PHASE_SEC = HEAD_SEC  # старое имя: так называлось «начало», когда оно было только из видео
# пометка запроса: кусок — призыв подписаться (кадр по теме ролика, не буквальный)
CTA_MARK = "CTA|"
QUERY_BATCH = 16
IMAGE_TRIES = 3
VIDEO_TRIES = 3
CANDIDATE_CAP = 6
WEAK_FIT = 3  # локальный CLIP мягче — берём best даже с низким fit
# куски независимы — число потоков на выбор кадра не влияет, только на скорость
# кадры независимы: больше потоков — быстрее (ждём в основном скачивание, не процессор)
STRICT_FETCH_WORKERS = 10

_QUERY_SYSTEM = """\
You write one English search query (for a stock photo or video) per narration beat.
Each query must depict what THAT beat says as filmable B-roll.
Return ONLY JSON: {"queries": ["...", "..."]} with the same length as the beats.
If a beat is a call to action — subscribe, like, comment, ring the bell, support the channel —
return exactly "CTA" for it (that moment gets a general shot about the film's topic).

Rules:
- 4 to 10 English words, concrete things a camera can film
- Prefer real places, actions, people, landmarks matching the beat AND story context
- If the story is about China / a city / a country, keep that place in the query when relevant
- If the beat is about a named person, name them in a filmable way.
  A person of the photography age (20th–21st century actor, singer, politician…):
  "<name> photo", "<name> on stage", "<name> interview", "<name> 1970s" —
  never statues, busts, paintings or engravings of them
- historical=true: no cars, smartphones, jeans, offices, modern cities
"""


@dataclass(frozen=True)
class Beat:
    start: float
    end: float
    text: str
    # номер кадра внутри куска текста («весь ролик»: 0 и 1 — два кадра на один кусок)
    shot: int = 0

    @property
    def duration(self) -> float:
        return max(0.2, self.end - self.start)

    @property
    def video_phase(self) -> bool:
        """Начало ролика (первые HEAD_SEC): кадр строго под эту фразу."""
        return self.start < HEAD_SEC


PROGRESS_NAME = "strict_progress.json"
LATE_POOL_NAME = "late_pool.json"
# «Первые 2 мин»: пул для остального ролика
LATE_IMAGES = (80, 100)
LATE_VIDEOS = (10, 15)
LATE_VIDEO_SEARCH_SEC = 150.0
# фото — 3–5 с, видео — не дольше 6 с (блок ~6–7 мин при 80–100 фото)
LATE_IMAGE_HOLD = 4.0
LATE_VIDEO_MAX = 6.0


def _late_pool(
    run_dir: Path, req, plan, *, http, clients, pool: Path, cancel, progress, exclude: list[dict] | None = None
) -> tuple[list[dict], list[dict]]:
    """Пул по теме для всего, что после первых 2 минут. Готовый пул (после сбоя) — с диска."""
    from engine.slides.theme_media import gather_pool

    cache = run_dir / LATE_POOL_NAME
    if cache.is_file():
        try:
            raw = json.loads(cache.read_text(encoding="utf-8"))
            keep = lambda items: [x for x in items if (run_dir / str(x.get("path") or "")).is_file()]
            videos, images = keep(raw.get("videos") or []), keep(raw.get("images") or [])
            if images or videos:
                log(f"Пул после 2 минут: уже собран ({len(images)} картинок, {len(videos)} видео)")
                return videos, images
        except (OSError, json.JSONDecodeError):
            pass
    rng = random.Random(req.seed)
    trials, frames = pool / "_late_trials", pool / "_late_frames"
    trials.mkdir(parents=True, exist_ok=True)
    frames.mkdir(parents=True, exist_ok=True)
    try:
        videos, images, _rv, _ri = gather_pool(
            http, clients, plan, rng=rng, pool=pool, trials=trials, frames=frames, req=req,
            cancel=cancel, progress=progress, pct_lo=0.68, pct_hi=0.92,
            image_need=rng.randint(*LATE_IMAGES), video_need=rng.randint(*LATE_VIDEOS),
            video_search_sec=LATE_VIDEO_SEARCH_SEC,
            # кадры начала (и похожие на них) в блок не берём — иначе тот же клип дважды за ролик
            exclude=exclude,
        )
    finally:
        shutil.rmtree(trials, ignore_errors=True)
        shutil.rmtree(frames, ignore_errors=True)
    cache.write_text(json.dumps({"videos": videos, "images": images}, ensure_ascii=False), encoding="utf-8")
    return videos, images


def _late_slots(
    videos: list[dict],
    images: list[dict],
    start: float,
    duration: float,
    *,
    avoid_first: str | None,
    rng: random.Random,
) -> list[dict]:
    """Блок для хвоста ролика: каждый кадр пула ровно один раз, одинаковые не подряд,
    первый кадр — не тот, что последний в первых 2 минутах. Рендер повторяет блок
    склейкой до конца озвучки (стык блока с самим собой — разные кадры).
    Хвост короче блока — блок обрезается по нему."""
    items: list[tuple[str, dict, float]] = [("image", it, LATE_IMAGE_HOLD) for it in images]
    items += [("video", it, max(1.0, min(float(it.get("duration") or LATE_VIDEO_MAX), LATE_VIDEO_MAX))) for it in videos]
    if not items or duration <= 0.05:
        return []
    order = spread(
        list(range(len(items))),
        key=lambda i: str(items[i][1].get("path")),
        rng=rng,
        avoid_first=avoid_first,
    )
    end = start + duration
    out: list[dict] = []
    t = start
    for i in order:
        if t >= end - 0.05:
            break
        kind, item, dur = items[i]
        dur = min(dur, end - t)
        if dur < 1.0 and out:  # хвостик в долю секунды — дотянуть предыдущий кадр
            out[-1]["end"] = round(end, 3)
            out[-1]["duration"] = round(out[-1]["end"] - out[-1]["start"], 3)
            break
        out.append(
            {
                "kind": kind,
                "path": item.get("path"),
                "start": round(t, 3),
                "end": round(t + dur, 3),
                "duration": round(dur, 3),
                "query": item.get("query", ""),
                "text": "",
                "provider": item.get("provider", ""),
                "id": item.get("id", ""),
                "source_title": item.get("source_title", ""),
                "source_author": item.get("source_author", ""),
                "fit": int(round(float(item.get("score") or 0) * 10)),
                "weak": False,
                "fallback": False,
                "phase": "pool",
            }
        )
        t += dur
    return out


def _beats_sig(beats: list[Beat], variant: str) -> str:
    raw = variant + "#" + "|".join(f"{b.start:.2f}:{b.end:.2f}:{b.text}" for b in beats)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _save_progress(
    run_dir: Path, beats: list[Beat], queries: list[str], slots: list[dict | None], variant: str
) -> None:
    data = {
        "sig": _beats_sig(beats, variant),
        "queries": queries,
        "slots": {str(i): s for i, s in enumerate(slots) if s is not None},
    }
    tmp = run_dir / (PROGRESS_NAME + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(run_dir / PROGRESS_NAME)


def _load_progress(run_dir: Path, beats: list[Beat], variant: str) -> dict | None:
    """Прошлый незавершённый подбор: запросы + готовые куски, чьи файлы целы."""
    path = run_dir / PROGRESS_NAME
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    queries = raw.get("queries")
    if raw.get("sig") != _beats_sig(beats, variant) or not isinstance(queries, list) or len(queries) != len(beats):
        return None
    slots: dict[int, dict] = {}
    for key, slot in (raw.get("slots") or {}).items():
        try:
            i = int(key)
        except ValueError:
            continue
        if 0 <= i < len(beats) and isinstance(slot, dict) and (run_dir / str(slot.get("path") or "")).is_file():
            slots[i] = slot
    return {"queries": queries, "slots": slots}


def with_shots(beats: list[Beat], shots: int = FULL_SHOTS) -> list[Beat]:
    """После начала каждый кусок текста → shots кадров поровну (тот же текст, разные кадры)."""
    out: list[Beat] = []
    for beat in beats:
        if beat.video_phase or shots < 2:
            out.append(beat)
            continue
        step = (beat.end - beat.start) / shots
        for k in range(shots):
            end = beat.end if k == shots - 1 else beat.start + step * (k + 1)
            out.append(Beat(beat.start + step * k, end, beat.text, shot=k))
    return out


# однозначные призывы (на языках роликов)
_CTA_STRONG = re.compile(
    r"subscri|suscr[ií]b|aboneaz|abonea|abonați|abonati|subskryb|abonnier|abonnez|iscrivet|iscriviti|"
    r"колокольч|лайк|hit the bell|like (?:the|this) video|campanita|campanella|clopo[țt]el|dzwonek|glocke|cloche|dale like",
    re.I,
)
# «подпишитесь…» — только рядом с «канал/видео» (иначе «подписал договор» в истории)
_CTA_SIGN = re.compile(r"подпи[сш]", re.I)
_CTA_CHANNEL = re.compile(r"канал|видео|channel|ролик", re.I)


def is_cta(text: str) -> bool:
    """Кусок — призыв подписаться / лайк / колокольчик (на любом из языков роликов)."""
    t = text or ""
    return bool(_CTA_STRONG.search(t) or (_CTA_SIGN.search(t) and _CTA_CHANNEL.search(t)))


def split_beats(
    words: list[WordTiming],
    voice_end: float,
    *,
    late_min: float = BEAT_MIN,
    late_max: float = BEAT_MAX,
) -> list[Beat]:
    """Режет тайминги на окна 5–10с, по границе фразы, если она уже длиннее 5с.

    late_min/late_max — длина окон после видео-фазы (в режиме «весь ролик» 10–15с).
    """
    clean = [w for w in words if (w.word or "").strip() and w.end > w.start]
    if not clean:
        return []
    voice_end = max(float(voice_end), clean[-1].end)
    beats: list[Beat] = []
    buf: list[WordTiming] = []
    beat_start = 0.0

    def close(end: float) -> None:
        nonlocal buf, beat_start
        text = " ".join(w.word.strip() for w in buf).strip()
        end = max(end, beat_start + 0.2)
        if text:
            beats.append(Beat(beat_start, end, text))
            beat_start = end
        buf = []

    for i, word in enumerate(clean):
        buf.append(word)
        end = word.end
        dur = end - beat_start
        last = i == len(clean) - 1
        if last:
            close(max(end, voice_end))
            break
        lo, hi = (BEAT_MIN, BEAT_MAX) if beat_start < VIDEO_PHASE_SEC else (late_min, late_max)
        if dur >= hi or (dur >= lo and _sentence_end(word.word)):
            close(end)

    if len(beats) >= 2:
        tail = beats[-1]
        prev = beats[-2]
        tail_dur = tail.end - tail.start
        if tail_dur < TAIL_MERGE and (tail.end - prev.start) <= (max(BEAT_MAX, late_max) + 2.0):
            beats[-2] = Beat(prev.start, tail.end, f"{prev.text} {tail.text}".strip())
            beats.pop()
    return beats


def collect_strict_timeline(
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
        raise MediaError("Нужен LLM key: и запросы, и проверка кадра идут через него")

    run_dir = req.run_dir
    script_path = run_dir / SCRIPT_NAME
    if not script_path.is_file():
        raise MediaError(f"Нет {SCRIPT_NAME}")
    if not (run_dir / TIMINGS_NAME).is_file():
        raise MediaError(f"Нет {TIMINGS_NAME}: строгий ряд режется по таймингам слов")

    words = _load_words(run_dir)
    voice = run_dir / AUDIO_NAME
    voice_end = probe_duration(voice) if voice.is_file() else (words[-1].end if words else 0.0)
    full = bool(req.strict_full)
    variant = "full" if full else "3min"
    if full:
        beats = with_shots(split_beats(words, voice_end, late_min=FULL_BEAT_MIN, late_max=FULL_BEAT_MAX))
    else:
        # «Первые 2 минуты»: по фразам — только начало. Остальное — один пул по теме
        # (80–100 фото + 10–15 видео), разложенный без одинаковых подряд.
        beats = [b for b in split_beats(words, voice_end) if b.video_phase]
    if not beats:
        raise MediaError("Не из чего резать ряд: в таймингах нет слов")
    n_head = sum(1 for b in beats if b.video_phase)
    n_late = len(beats) - n_head
    late_start = beats[-1].end
    late_dur = 0.0 if full else max(0.0, voice_end - late_start)
    yt = "да" if (req.keys.youtube_api_key or "").strip() else "нет"
    late = (
        f"дальше {n_late} кадров: куски текста {FULL_BEAT_MIN:.0f}–{FULL_BEAT_MAX:.0f}с по {FULL_SHOTS} кадра"
        if full
        else f"дальше {late_dur:.0f}с — пул по теме ({LATE_IMAGES[0]}–{LATE_IMAGES[1]} фото + видео) без повторов подряд"
    )
    log(
        f"Строгий ряд ({'весь ролик' if full else 'первые 2 мин'}): {len(beats)} кадров, "
        f"озвучка {voice_end:.0f}с → первые {HEAD_SEC:.0f}с: {n_head} кадров по {BEAT_MIN:.0f}–{BEAT_MAX:.0f}с "
        f"(фото и видео вперемешку), {late}, YouTube={yt}"
    )

    progress(0.02, "Тема + запросы под каждый кусок текста…")
    client = _llm(req)
    if cancel:
        cancel.register(client)
    saved = _load_progress(run_dir, beats, variant)
    try:
        plan = ensure_theme_plan(run_dir, script_path.read_text(encoding="utf-8"), client)
        from engine.slides.person_media import person_mode

        if person_mode(plan) is not None:
            # Ролик про конкретного человека: по фразам искать нечего — нужны только его
            # фото и видео. Пул с героем подходит и к строгим режимам.
            log("Строгий ряд: ролик про человека — беру только его фото и видео")
            client.close()
            from engine.slides.theme_media import collect_theme_pool

            return collect_theme_pool(req, cancel=cancel, on_progress=on_progress)
        if saved is not None:
            queries = saved["queries"]
        else:
            queries = _plan_queries(client, plan, beats, cancel, all_beats=full)
    finally:
        client.close()

    clients = _build_clients(req.keys)
    pool = run_dir / "pool"
    if saved is None and pool.exists():
        shutil.rmtree(pool, ignore_errors=True)
    trials = pool / "_trials"
    frames = pool / "_frames"
    trials.mkdir(parents=True, exist_ok=True)
    frames.mkdir(parents=True, exist_ok=True)
    http = httpx.Client(timeout=120.0, trust_env=False, follow_redirects=True)
    workers = STRICT_FETCH_WORKERS
    progress(0.10, "CLIP: гружу локальную модель…")
    get_clip()
    ctx = plan.context
    mix = MixState(
        subject=subject_of(ctx.subject, visual_style=ctx.visual_style, historical=ctx.historical),
        stats=SourceStats(),
        budget=Budget.for_beats(len(beats)),
        cancel=cancel,
    )
    log(
        f"Подбор: тема «{mix.subject}», видео {base_weights(mix.subject, video=True)}, "
        f"картинки {base_weights(mix.subject, video=False)}, цель {TARGET_HEIGHT}p, не ниже {MIN_HEIGHT}p"
    )

    slots: list[dict | None] = [None] * len(beats)
    if saved is not None:
        for i, slot in saved["slots"].items():
            slots[i] = slot
        log(f"Строгий ряд: продолжаю, уже есть {len(saved['slots'])}/{len(beats)} кусков")
    for slot in slots:  # уже найденные клипы заняты — недостающие куски их не возьмут
        if slot:
            mix.taken.add((str(slot.get("provider") or ""), str(slot.get("id") or "")))
            mix.add_look(fingerprint(run_dir / str(slot.get("path") or "")))
    _save_progress(run_dir, beats, queries, slots, variant)
    rejected = 0
    weak = 0
    lock = threading.Lock()
    done_n = sum(1 for s in slots if s is not None)

    def _job(index: int) -> tuple[int, dict | None, int, bool]:
        if cancel and cancel.is_cancelled():
            raise CancelledError("Остановлено пользователем")
        return (
            index,
            *_pick_beat(
                http,
                clients,
                plan,
                beats[index],
                queries[index],
                index=index,
                trials=trials,
                frames=frames,
                pool=pool,
                mix=mix,
                full=full,
            ),
        )

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_job, i) for i in range(len(beats)) if slots[i] is None]
            if cancel:
                # «Стоп» не ждёт очереди из сотен кусков: ещё не начатые снимаем сразу
                cancel.register(_FutureCanceller(futs))
            for fut in as_completed(futs):
                if cancel:
                    cancel.check()
                index, slot, rej, was_weak = fut.result()
                with lock:
                    slots[index] = slot
                    rejected += rej
                    weak += int(was_weak)
                    done_n += 1
                    now = done_n
                    # каждый готовый кусок сразу на диск — при падении не теряем
                    _save_progress(run_dir, beats, queries, slots, variant)
                progress(
                    0.12 + (0.8 if full else 0.55) * (now / len(beats)),
                    f"Строгий ряд: {now}/{len(beats)}, отбраковано {rejected}",
                )
        head_idx = [i for i, b in enumerate(beats) if b.video_phase]
        if head_idx and not any(slots[i] and slots[i].get("kind") == "video" for i in head_idx):
            # видео в начале обязательно: для нескольких кадров ищем видео по теме ролика
            log("В первых 2 минутах нет видео — подбираю видео по теме ролика для нескольких кадров")
            for i in head_idx[:: max(1, len(head_idx) // 3)][:3]:
                hit, _rej = _try_kind(
                    http, clients, plan, beats[i], _theme_video_query(plan, i, fallback=queries[i]),
                    index=i, trials=trials, frames=frames, pool=pool, prefer_video=True, mix=mix,
                    tries=VIDEO_TRIES, allow_weak=True, keep_any=True, thematic=True,
                )
                if hit is not None:
                    if slots[i]:
                        _drop_slot(pool, slots[i], mix)
                    slots[i] = hit
            _save_progress(run_dir, beats, queries, slots, variant)
        late_pool = (
            _late_pool(
                run_dir, req, plan, http=http, clients=clients, pool=pool, cancel=cancel, progress=progress,
                exclude=[x for x in slots if x],
            )
            if late_dur > 0.5
            else ([], [])
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

    log(f"Источники в ролике: {mix.stats.summary()}")
    filled = _fill_holes(beats, slots)
    if not filled:
        raise MediaError("Не нашёл ни одного кадра под текст")
    head_slots = [x for x in filled if float(x.get("start") or 0) < HEAD_SEC]
    if head_slots and not any(x.get("kind") == "video" for x in head_slots):
        log("В первых 2 минутах так и нет видео (не нашлось ни одного подходящего клипа) — только фото")
    holes = sum(1 for item in filled if item.get("fallback"))
    if holes:
        log(f"Дыр без своего кадра: {holes}, закрыты другими найденными кадрами (не соседними)")
    if late_dur > 0.5:
        videos, images = late_pool
        if not videos and not images:
            log("Пул для остального ролика пуст — повторяю кадры первых 2 минут вразбивку")
            videos = [dict(x) for x in filled if x.get("kind") == "video" and not x.get("fallback")]
            images = [dict(x) for x in filled if x.get("kind") == "image" and not x.get("fallback")]
        loop_block = _late_slots(
            videos, images, late_start, late_dur,
            avoid_first=str(filled[-1].get("path")) if filled else None,
            rng=random.Random(req.seed),
        )
        if not loop_block:
            raise MediaError("Не нашёл кадров для ролика после первых 2 минут")
        block_dur = loop_block[-1]["end"] - late_start
        log(
            f"После 2 минут: блок {len(loop_block)} кадров ({block_dur:.0f}с) из пула "
            f"{len(images)} картинок + {len(videos)} видео, повтор до конца ролика ×{late_dur / block_dur:.1f}"
        )
    else:
        loop_block = []

    payload = {
        "mode": "strict_sync",
        "variant": variant,
        "summary": plan.summary,
        "fade_sec": FADE_SEC,
        "beat_min": BEAT_MIN,
        "beat_max": BEAT_MAX,
        "video_phase_sec": HEAD_SEC,
        "story_context": plan.context.to_dict(),
        "slots": filled,
        # «первые 2 мин»: блок пула рендерится один раз и повторяется до конца озвучки
        "loop_block": loop_block,
        "loop_start": round(late_start, 3),
    }
    timeline_file = run_dir / TIMELINE_NAME
    timeline_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / PROGRESS_NAME).unlink(missing_ok=True)
    (run_dir / LATE_POOL_NAME).unlink(missing_ok=True)
    preview_file = run_dir / "timeline_preview.txt"
    lines = [
        plan.summary,
        (
            f"strict beats={len(filled)} video_phase<{VIDEO_PHASE_SEC:.0f}s "
            f"rejected={rejected} weak={weak} holes={holes}"
        ),
        "",
    ]
    for item in filled:
        phase = "VID" if float(item.get("start") or 0) < VIDEO_PHASE_SEC else "IMG"
        lines.append(
            f"{item['start']:.1f}-{item['end']:.1f} [{phase}] {item['kind'].upper()} "
            f"fit={item.get('fit')}  {item.get('query')}  {item.get('text')}"
        )
    if loop_block:
        lines.append("")
        lines.append(f"ДАЛЬШЕ С {late_start:.0f}с — БЛОК ПУЛА ({len(loop_block)} кадров), ПОВТОР ДО КОНЦА:")
        for item in loop_block:
            lines.append(f"  {item['kind'].upper()} {item.get('provider')}  {item.get('query')}")
    preview_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    desktop = None
    if req.copy_preview_to_desktop:
        dest = desktop_copy_name(f"timeline_{run_dir.name}", ".txt")
        shutil.copy2(preview_file, dest)
        desktop = dest

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
        selected_media=len(filled),
        desktop_timeline=str(desktop or ""),
    )
    progress(
        1.0,
        f"Строгий ряд: {sum(1 for x in filled if x.get('kind') == 'video')} видео + "
        f"{sum(1 for x in filled if x.get('kind') != 'video')} картинок на {voice_end:.0f}с",
    )
    return MediaStageResult(
        run_dir=run_dir,
        timeline_file=timeline_file,
        preview_file=preview_file,
        desktop_preview=desktop,
        selected=len(filled),
        skipped=rejected,
        weak=weak,
    )


def _pick_beat(
    http: httpx.Client,
    clients: dict,
    plan: ThemePlan,
    beat: Beat,
    query: str,
    *,
    index: int,
    trials: Path,
    frames: Path,
    pool: Path,
    mix: MixState,
    full: bool = False,
) -> tuple[dict | None, int, bool]:
    if query.startswith(CTA_MARK):
        # призыв подписаться: видео по общей теме ролика, без буквального соответствия
        hit, rej = _try_kind(
            http, clients, plan, beat, query[len(CTA_MARK):],
            index=index, trials=trials, frames=frames, pool=pool, prefer_video=True, mix=mix,
            tries=VIDEO_TRIES, allow_weak=True, keep_any=True, thematic=True,
        )
        if hit is not None:
            return hit, rej, bool(hit.get("weak"))
        query = query[len(CTA_MARK):]

    # Начало (оба строгих режима) и весь «весь ролик»: кадр строго под этот кусок,
    # фото и видео чередуются (чётный кусок — сначала видео, нечётный — фото).
    if beat.video_phase or full:
        return _pick_mixed(
            http, clients, plan, beat,
            video_query=query, image_query=query, thematic=False,
            index=index, trials=trials, frames=frames, pool=pool, mix=mix,
        )
    video_q = _theme_video_query(plan, index, fallback=query)
    image_q = _theme_image_query(plan, index, fallback=query)
    return _pick_mixed(
        http, clients, plan, beat,
        video_query=video_q, image_query=image_q, thematic=True,
        index=index, trials=trials, frames=frames, pool=pool, mix=mix,
    )


def _pick_mixed(
    http: httpx.Client,
    clients: dict,
    plan: ThemePlan,
    beat: Beat,
    *,
    video_query: str,
    image_query: str,
    thematic: bool,
    index: int,
    trials: Path,
    frames: Path,
    pool: Path,
    mix: MixState,
) -> tuple[dict | None, int, bool]:
    """После видео-фазы: видео или фото, чередуя по кускам.

    Чётный кусок начинает с видео, нечётный — с фото: так по всему ролику идут
    вперемешку оба вида. Нашлось только слабое (или ничего) — пробуем второй
    вид и берём лучший из двух. Видео нет совсем — остаются фото.
    """
    video_first = index % 2 == 0
    order = (True, False) if video_first else (False, True)
    best: dict | None = None
    rejected = 0
    for prefer_video in order:
        if best is not None and not best.get("weak"):
            break
        if best is not None and beat.shot > 0:
            break  # второй кадр куска: слабый, но найденный — берём, второй вид не ищем (скорость)
        if best is not None and mix.budget.phase == "hard":
            break  # время вышло — второй тип не ищем
        hit, rej = _try_kind(
            http,
            clients,
            plan,
            beat,
            video_query if prefer_video else image_query,
            index=index,
            trials=trials,
            frames=frames,
            pool=pool,
            prefer_video=prefer_video,
            mix=mix,
            tries=VIDEO_TRIES if prefer_video else IMAGE_TRIES,
            allow_weak=True,
            keep_any=True,
            thematic=thematic,
        )
        rejected += rej
        if hit is None:
            continue
        if best is None or int(hit.get("fit") or 0) > int(best.get("fit") or 0):
            if best is not None:
                _drop_slot(pool, best, mix)
            best = hit
        else:
            _drop_slot(pool, hit, mix)
    return best, rejected, bool(best and best.get("weak"))


def _drop_slot(pool: Path, slot: dict, mix: MixState) -> None:
    """Проигравший кадр: удалить файл и не считать его в доле источника."""
    (pool.parent / str(slot.get("path") or "")).unlink(missing_ok=True)
    mix.stats.unpick(str(slot.get("provider") or ""))
    mix.release_slot(slot)


def _theme_video_query(plan: ThemePlan, index: int, *, fallback: str) -> str:
    qs = [q for q in plan.video_queries if (q or "").strip()]
    return qs[index % len(qs)] if qs else _theme_image_query(plan, index, fallback=fallback)


def _theme_image_query(plan: ThemePlan, index: int, *, fallback: str) -> str:
    qs = [q for q in plan.image_queries if (q or "").strip()]
    if qs:
        return qs[index % len(qs)]
    bits = [plan.summary, plan.context.place, plan.context.era]
    joined = " ".join(b for b in bits if b)
    return joined.strip() or fallback


def _try_kind(
    http: httpx.Client,
    clients: dict,
    plan: ThemePlan,
    beat: Beat,
    query: str,
    *,
    index: int,
    trials: Path,
    frames: Path,
    pool: Path,
    prefer_video: bool,
    mix: MixState,
    tries: int,
    allow_weak: bool,
    keep_any: bool,
    thematic: bool,
) -> tuple[dict | None, int]:
    found = _candidates(clients, query, plan, prefer_video=prefer_video, index=index, mix=mix)
    rejected = 0
    best: tuple[tuple[int, int], int, Path, StockCandidate, int | None] | None = None
    used = 0
    claimed: list[StockCandidate] = []
    kept: StockCandidate | None = None
    try:
        for cand in found:
            if mix.cancel is not None:
                mix.cancel.check()
            if used >= tries and best is not None and not keep_any:
                break
            if used >= max(tries, CANDIDATE_CAP):
                break
            if prefer_video and not candidate_is_landscape(cand.width, cand.height):
                rejected += 1
                continue
            if not mix.claim(cand):
                continue  # этот клип уже взял другой кусок — не повторяем
            claimed.append(cand)
            used += 1
            ext = ".mp4" if prefer_video else _ext_from_url(cand.download_url)
            trial = trials / f"b{index:04d}_{cand.provider}_{cand.id}{ext}"
            if not _fetch_candidate(
                http, clients, cand, trial, prefer_video=prefer_video, index=index
            ):
                trial.unlink(missing_ok=True)
                continue
            if not trial.is_file() or trial.stat().st_size < 1000:
                trial.unlink(missing_ok=True)
                continue
            if prefer_video and not is_landscape(trial):
                log(f"skip portrait video {trial.name}")
                rejected += 1
                trial.unlink(missing_ok=True)
                continue
            height = _media_height(trial, video=prefer_video)
            if height < MIN_HEIGHT:
                log(f"skip low-res {trial.name} ({height}p < {MIN_HEIGHT}p)")
                rejected += 1
                trial.unlink(missing_ok=True)
                continue
            fp = fingerprint(trial)
            if mix.look_taken(fp):
                log(f"Кусок {index + 1}: такой кадр уже стоит в ролике — беру другой")
                rejected += 1
                trial.unlink(missing_ok=True)
                continue
            fit = _score_file(
                trial,
                plan,
                beat,
                query,
                frames=frames,
                prefer_video=prefer_video,
                thematic=thematic,
            )
            if fit is None:
                rejected += 1
                trial.unlink(missing_ok=True)
                continue
            mix.stats.observe(cand.provider, fit)
            phase = mix.budget.phase
            # картинке 1080 не обязателен; видео 1080p предпочитаем, пока есть время
            sharp = not prefer_video or height >= TARGET_HEIGHT
            overused = mix.stats.overused(cand.provider)
            # превью платного фотостока (водяной знак) — только если чистого не нашлось
            marked = bool((cand.meta or {}).get("watermark"))
            if fit >= IMAGE_FIT_MIN and not overused and not marked and (sharp or phase != "normal"):
                if best is not None:
                    best[2].unlink(missing_ok=True)
                mix.stats.picked(cand.provider)
                mix.add_look(fp)
                kept = cand
                return (
                    _keep(
                        trial, cand, fit, beat, query, index, pool, prefer_video, weak=False
                    ),
                    rejected,
                )
            # лучший из проверенных: оценка (минус 1 за перекос источника), при равенстве — 1080p
            key = (fit - (1 if overused else 0) - (2 if marked else 0), 1 if sharp else 0)
            if (allow_weak or keep_any or fit >= IMAGE_FIT_MIN) and (best is None or key > best[0]):
                if best is not None:
                    best[2].unlink(missing_ok=True)
                best = (key, fit, trial, cand, fp)
            else:
                rejected += 1
                trial.unlink(missing_ok=True)
            if mix.budget.phase == "hard":
                break  # время вышло — берём лучшее из найденного
        if best is None:
            return None, rejected
        _key, fit, trial, cand, fp = best
        mix.stats.picked(cand.provider)
        mix.add_look(fp)
        kept = cand
        log(f"Кусок {index + 1}: best fit={fit} {cand.provider} ({'video' if prefer_video else 'image'})")
        return (
            _keep(trial, cand, fit, beat, query, index, pool, prefer_video, weak=fit < IMAGE_FIT_MIN),
            rejected,
        )
    finally:
        # отбракованные кандидаты снова свободны для других кусков
        for c in claimed:
            if c is not kept:
                mix.release(c)


def _media_height(path: Path, *, video: bool) -> int:
    """Видео — высота кадра; картинка — короткая сторона (портрет тоже должен быть чётким)."""
    if video:
        size = probe_video_size(path)
        return size[1] if size else 0
    try:
        from PIL import Image

        with Image.open(path) as img:
            return min(img.size)
    except Exception:
        return 0


def _fetch_candidate(
    http: httpx.Client,
    clients: dict,
    cand: StockCandidate,
    trial: Path,
    *,
    prefer_video: bool,
    index: int,
) -> bool:
    if cand.provider == "youtube":
        yt = clients.get("youtube")
        if yt is None:
            return False
        rng = random.Random(f"{cand.id}:{index}")
        return bool(
            yt.download_clip(
                cand,
                trial,
                clip_sec=min(BEAT_MAX, max(BEAT_MIN, CLIP_FROM_YT)),
                rng=rng,
            )
        )
    return _download(http, cand.download_url, trial, retries=3)


CLIP_FROM_YT = 10.0


def _keep(
    trial: Path,
    cand: StockCandidate,
    fit: int,
    beat: Beat,
    query: str,
    index: int,
    pool: Path,
    prefer_video: bool,
    *,
    weak: bool,
) -> dict:
    ext = trial.suffix or ".jpg"
    kind = "video" if prefer_video else "image"
    dest = pool / f"beat_{index:04d}_{cand.provider}{ext}"
    trial.replace(dest)
    return {
        "kind": kind,
        "path": str(dest.relative_to(pool.parent)),
        "start": round(beat.start, 3),
        "end": round(beat.end, 3),
        "duration": round(beat.duration, 3),
        "query": query,
        "text": beat.text[:240],
        "provider": cand.provider,
        "source_title": str((cand.meta or {}).get("title") or ""),
        "source_author": str((cand.meta or {}).get("channel") or ""),
        "source_url": cand.download_url if cand.provider == "youtube" else (cand.preview_url or cand.download_url),
        "id": cand.id,
        "fit": fit,
        "score": round(fit / 10.0, 3),
        "weak": weak,
        "fallback": False,
        "phase": "video" if beat.video_phase else "image",
    }


def _score_file(
    trial: Path,
    plan: ThemePlan,
    beat: Beat,
    query: str,
    *,
    frames: Path,
    prefer_video: bool,
    thematic: bool,
) -> int | None:
    if prefer_video:
        try:
            src = probe_duration(trial)
        except Exception:
            src = 0.0
        if src < 2.0:
            return None
        extracted = _extract_frames(trial, frames, min(BEAT_MAX, src))
        samples = _middle_first(extracted)[:1]
        for extra in extracted:
            if extra not in samples:
                extra.unlink(missing_ok=True)
    else:
        samples = [trial]
    if not samples:
        return None
    narration = plan.summary if thematic else beat.text
    sample = samples[0]
    try:
        try:
            fit, reason = score_fit(
                None,
                sample,
                narration=narration,
                query=query,
                era=plan.context.era,
                place=plan.context.place,
                historical=plan.context.historical,
            )
        except Exception as exc:
            log(f"Кадр {sample.name}: CLIP fail ({exc}), skip")
            return None
        log(f"Кадр {sample.name}: fit={fit} ({reason[:80]})")
        if prefer_video:
            return int(fit)
        if not _frame_ok(fit, reason, strict=False) and fit < WEAK_FIT:
            return None
        return int(fit)
    finally:
        if sample != trial:
            sample.unlink(missing_ok=True)


class _FutureCanceller:
    """Для CancelToken.register: при «Стоп» снять ещё не начатые куски."""

    def __init__(self, futs) -> None:
        self.futs = futs

    def close(self) -> None:
        for f in self.futs:
            f.cancel()


@dataclass
class MixState:
    """Общее на все куски ролика: тема, статистика источников, таймер подбора."""

    subject: str
    stats: SourceStats
    budget: Budget
    # клипы, уже взятые кусками ролика: один клип — один кусок
    taken: set = field(default_factory=set)
    # «Стоп»: проверяется на каждом кандидате, а не только между кусками
    cancel: CancelToken | None = None
    # как выглядят уже взятые кадры: то же фото с другого сайта или тот же клип — не берём
    looks: list = field(default_factory=list)
    # выдача поиска по (источник, запрос, видео?): второй кадр того же куска не ищет заново
    searches: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def look_taken(self, fp: int | None) -> bool:
        with self._lock:
            return seen(fp, self.looks)

    def add_look(self, fp: int | None) -> None:
        if fp is not None:
            with self._lock:
                self.looks.append(fp)

    def claim(self, cand: StockCandidate) -> bool:
        key = (cand.provider, cand.id)
        with self._lock:
            if key in self.taken:
                return False
            self.taken.add(key)
            return True

    def release(self, cand: StockCandidate) -> None:
        with self._lock:
            self.taken.discard((cand.provider, cand.id))

    def release_slot(self, slot: dict) -> None:
        with self._lock:
            self.taken.discard((str(slot.get("provider") or ""), str(slot.get("id") or "")))


def _available_groups(clients: dict) -> list[str]:
    out = [g for g in ("youtube", "google", "wikimedia", "archive") if g in clients]
    if any(n in clients for n in ("pexels", "pixabay", "unsplash")):
        out.append("stocks")
    return out


def _candidates(
    clients: dict,
    query: str,
    plan: ThemePlan,
    *,
    prefer_video: bool,
    index: int,
    mix: MixState,
) -> list[StockCandidate]:
    """Кандидаты со всех источников сразу: места делятся по долям темы и статистике."""
    weights = mix.stats.adjusted(base_weights(mix.subject, video=prefer_video))
    groups_on = _available_groups(clients)
    hero = _main_name(plan.context.characters).lower()
    if hero and hero in query.lower():
        # конкретного человека на Pexels/Pixabay/Unsplash нет — его места отдаём Google/YouTube/Wikimedia
        groups_on = [g for g in groups_on if g != "stocks"]
    quotas = allot(weights, groups_on, CANDIDATE_CAP)
    if not quotas:
        return []
    rng = random.Random(index + 1)

    def search(group: str) -> list[StockCandidate]:
        good: list[StockCandidate] = []
        key = (group, query, prefer_video)
        with mix._lock:
            cached = mix.searches.get(key)
        if cached is None:
            cached = _search_source(clients, group, query, prefer_video=prefer_video, rng=random.Random(rng.random()))
            with mix._lock:
                mix.searches[key] = cached
        for cand in cached:
            if prefer_video != (cand.kind == "video"):
                continue
            if prefer_video and not candidate_is_landscape(cand.width, cand.height):
                continue
            # размер известен заранее — мелкое отсекаем до скачивания
            short = min(cand.width, cand.height) if not prefer_video else cand.height
            if not candidate_height_ok(cand.width, short, MIN_HEIGHT):
                continue
            cand.query = query
            good.append(cand)
        return good

    groups = list(quotas)
    with ThreadPoolExecutor(max_workers=len(groups)) as ex:
        found = dict(zip(groups, ex.map(search, groups)))

    # по кругу: самый «весомый» источник первым, каждому — его квота
    order = sorted(groups, key=lambda g: -weights.get(g, 0))
    out: list[StockCandidate] = []
    seen: set[tuple[str, str]] = set()
    taken = {g: 0 for g in groups}

    def take(g: str) -> bool:
        while found[g]:
            cand = found[g].pop(0)
            key = (cand.provider, cand.id)
            if key not in seen:
                seen.add(key)
                out.append(cand)
                taken[g] += 1
                return True
        return False

    progress_made = True
    while len(out) < CANDIDATE_CAP and progress_made:
        progress_made = False
        for g in order:
            if len(out) >= CANDIDATE_CAP:
                break
            if taken[g] < quotas[g] and take(g):
                progress_made = True
    # у кого-то не хватило результатов — добираем из остальных
    for g in order:
        while len(out) < CANDIDATE_CAP and take(g):
            pass
    return out


def _plan_queries(
    client: LLMClient,
    plan: ThemePlan,
    beats: list[Beat],
    cancel: CancelToken | None,
    *,
    all_beats: bool = False,
) -> list[str]:
    queries = [""] * len(beats)
    # Запрос на каждый кусок: видео-фаза всегда; «весь ролик» — и все остальные куски.
    # Иначе после видео-фазы подставятся тематические запросы.
    # второй кадр того же куска текста запрос не просит — берёт запрос первого
    video_beats = [(i, b) for i, b in enumerate(beats) if (b.video_phase or all_beats) and b.shot == 0]
    ctx = json.dumps(plan.context.to_dict(), ensure_ascii=False)

    def ask(start: int) -> tuple[list[tuple[int, Beat]], list[str]]:
        if cancel:
            cancel.check()
        chunk = video_beats[start : start + QUERY_BATCH]
        lines = [f"{j}. {beat.text}" for j, (_i, beat) in enumerate(chunk, start=1)]
        user = (
            "STORY CONTEXT:\n"
            + ctx
            + "\n\nBEATS:\n"
            + "\n".join(lines)
            + "\n\nWrite one VIDEO search query per beat."
        )
        raw = client.complete(
            label=f"beat_queries_{start}", system=_QUERY_SYSTEM, user=user
        )
        return chunk, _parse_queries(raw)

    # пачки независимы — спрашиваем LLM одновременно, ответы те же
    starts = list(range(0, len(video_beats), QUERY_BATCH))
    with ThreadPoolExecutor(max_workers=max(1, len(starts))) as ex:
        answers = list(ex.map(ask, starts))
    for chunk, got in answers:
        for offset, query in enumerate(got):
            if offset >= len(chunk):
                break
            idx = chunk[offset][0]
            if query:
                queries[idx] = query
    fallback = plan.summary or plan.context.place or plan.context.era or "documentary b-roll"
    for i, beat in enumerate(beats):
        if beat.shot > 0 and i > 0:
            queries[i] = queries[i - 1]
        if queries[i].strip().upper() == "CTA" or is_cta(beat.text):
            # призыв подписаться — не буквально, а видео по теме всего ролика
            queries[i] = CTA_MARK + _theme_video_query(plan, i, fallback=fallback)
            log(f"Кусок {i + 1}: призыв подписаться — беру видео по теме ролика")
            continue
        if queries[i]:
            continue
        if beat.video_phase:
            queries[i] = fallback
            log(f"Кусок {i + 1}: video-запрос не пришёл, беру тему")
        else:
            queries[i] = _theme_image_query(plan, i, fallback=fallback)
    return queries


def _parse_queries(raw: str) -> list[str]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return []
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return []
    raw_list = data.get("queries") if isinstance(data, dict) else data
    if not isinstance(raw_list, list):
        return []
    return [str(item or "").strip() for item in raw_list]


def _fill_holes(beats: list[Beat], slots: list[dict | None]) -> list[dict]:
    """Дыры (кусок без своего кадра) закрываем уже найденным кадром.

    Никогда не соседом: одинаковое подряд бросается в глаза. Берём кадр, который
    стоит дальше всего от дыры и использован реже всех. Вид: в видео-фазе только
    видео; после неё сначала картинка, нет подходящей — видео. Ни один кусок не
    выпадает (иначе всё после дыры съехало бы раньше своего текста). Соседний
    повтор — только если других кадров нужного вида нет вовсе.
    """
    resolved: list[dict | None] = list(slots)
    originals = [slot for slot in slots if slot]
    uses = Counter(str(slot.get("path")) for slot in originals)

    def media(slot: dict | None) -> str | None:
        return str(slot.get("path")) if slot else None

    def kinds_for(beat: Beat) -> list[str]:
        return ["video", "image"] if beat.video_phase else ["image", "video"]

    def distance(path: str, i: int) -> int:
        pos = [j for j, slot in enumerate(resolved) if media(slot) == path]
        return min((abs(j - i) for j in pos), default=len(beats))

    for i, beat in enumerate(beats):
        if resolved[i] is not None:
            continue
        neighbours = {media(resolved[i - 1]) if i > 0 else None, media(resolved[i + 1]) if i + 1 < len(beats) else None}
        donor: dict | None = None
        for allow_neighbour in (False, True):
            for kind in kinds_for(beat):
                pool: dict[str, dict] = {}
                for slot in originals:
                    if slot.get("kind") == kind and (allow_neighbour or media(slot) not in neighbours):
                        pool.setdefault(str(slot.get("path")), slot)
                if pool:
                    best = max(pool, key=lambda path: (distance(path, i), -uses[path]))
                    donor = pool[best]
                    break
            if donor is not None:
                break
        if donor is not None:
            resolved[i] = _clone_slot(donor, beat)
            uses[str(donor.get("path"))] += 1

    return [slot for slot in resolved if slot is not None]


def _clone_slot(src: dict, beat: Beat) -> dict:
    out = dict(src)
    out["start"] = round(beat.start, 3)
    out["end"] = round(beat.end, 3)
    out["duration"] = round(beat.duration, 3)
    out["text"] = beat.text[:240]
    out["fallback"] = True
    out["weak"] = True
    out["phase"] = "video" if beat.video_phase else "image"
    return out


def _load_words(run_dir: Path) -> list[WordTiming]:
    raw = json.loads((run_dir / TIMINGS_NAME).read_text(encoding="utf-8"))
    words = raw.get("words") if isinstance(raw, dict) else None
    if not isinstance(words, list):
        return []
    marker = run_dir / ORIGIN_NAME
    script = run_dir / SCRIPT_NAME
    origin = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
    if origin == "user" and script.is_file():
        words = align_script_words(script.read_text(encoding="utf-8"), words)
    out: list[WordTiming] = []
    for item in words:
        if not isinstance(item, dict):
            continue
        text = str(item.get("word") or "").strip()
        if not text:
            continue
        try:
            start = float(item.get("start") or 0.0)
            end = float(item.get("end") or start)
        except (TypeError, ValueError):
            continue
        if end <= start:
            end = start + 0.18
        out.append(WordTiming(text, start, end))
    return out


def _sentence_end(word: str) -> bool:
    return word.rstrip("\"'»”").endswith((".", "!", "?", "…", "。"))
