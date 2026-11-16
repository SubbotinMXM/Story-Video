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
import time
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
from engine.slides.ffmpeg_util import find_ffmpeg
from engine.slides.hook import HOOK_SEC as HOOK_SEC_COMMON
from engine.slides.hook import NO_AI_SEC, add_beauty, head_rank, hook_first
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
# «Крючок»: первые HOOK_SEC ролика — самые красивые кадры (пролёты, виды, золотой час).
# Под эти куски перебираем больше кандидатов и выбираем по смыслу + красоте, а не первый годный.
HOOK_SEC = HOOK_SEC_COMMON
HOOK_BEAUTY_OK = 0.03  # кадр уже «цепляет» — дальше не ищем
HOOK_BEAUTY_WEIGHT = 50  # красота ±0.08 → ±4 балла к оценке смысла (0–10)
# пометка запроса: кусок — призыв подписаться (кадр по теме ролика, не буквальный)
CTA_MARK = "CTA|"
# пометка запроса: кусок про цифры/графики/статистику — живой кадр к нему почти не найти,
# при включённых AI-кадрах сюда обязательно ставится сгенерированная инфографика
DATA_MARK = "DATA|"
# кусков на один запрос: длинная инструкция и контекст уходят реже (экономия ключа LLM)
QUERY_BATCH = 32
IMAGE_TRIES = 3
VIDEO_TRIES = 3
CANDIDATE_CAP = 6
WEAK_FIT = 3  # локальный CLIP мягче — берём best даже с низким fit
# куски независимы — число потоков на выбор кадра не влияет, только на скорость
# кадры независимы: больше потоков — быстрее (ждём в основном скачивание, не процессор)
STRICT_FETCH_WORKERS = 10
STRICT_JUDGE_WORKERS = 16  # куски начала ждут LLM — остальные тем временем подбираются

_QUERY_SYSTEM = """\
You write one English search query (for a stock photo or video) per narration beat.
Each query must depict what THAT beat says as filmable B-roll.
Return ONLY JSON: {"queries": ["...", "..."]} with the same length as the beats.
If a beat is a call to action — subscribe, like, comment, ring the bell, support the channel —
return exactly "CTA" for it (that moment gets a general shot about the film's topic).
Beats ending with ★ open the film and must hook the viewer. For them spectacle beats literal meaning:
always a visually stunning REAL shot of the story's place, people or nature — aerial/drone flyover, sweeping
landscape or cityscape, golden hour, timelapse, cinematic close-up (e.g. "aerial drone flyover Mongolian steppe
golden hour"). An abstract ★ beat (myths, contradictions, numbers, opinions, history in general) gets the most
stunning establishing shot of the place. Never DATA, charts, maps, flags, documents, museums or archives for ★.
Beats ending with ★★ are the first 30 seconds — the most important moment of the film: the single most spectacular
shot possible — iconic aerial/drone views of the place, breathtaking landscapes; and if the film is about the
people or women of a country, alternate them with "beautiful young <nationality> woman smiling cinematic slow motion
<place>" style shots (attractive, photogenic, in local or summer clothing).
If a beat is mainly about numbers, statistics, percentages, money, prices, growth or decline,
rankings, comparisons, a chart/graph/map or an abstract idea that cannot be filmed, return
"DATA: <short English description of a chart or infographic that shows it>",
e.g. "DATA: bar chart of Mongolia GDP growth 2010-2020".

Rules:
- 4 to 10 English words, concrete things a camera can film
- Prefer real places, actions, people, landmarks matching the beat AND story context
- ALWAYS anchor the query in the film's world from STORY CONTEXT: its country/region/city and era.
  "village" in a film about Japan → "Japanese mountain village Gifu" or "Hokkaido snowy village", never just
  "winter village". Only a beat explicitly about another place gets that other place.
- Use the visual_markers of the story when they help (architecture, signage, clothing, landscape)
- Prefer queries that find professional, cinematic footage: for places, cities, nature and scenery add
  words like "aerial", "drone", "cinematic", "4K", "timelapse", "golden hour" when natural; for people and
  street life — "cinematic", "slow motion", "close-up". Never words that bring amateur vlogs or news.
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
    run_dir: Path, req, plan, *, http, clients, pool: Path, cancel, progress, exclude: list[dict] | None = None,
    judge=None,
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
            judge=judge,
        )
    finally:
        shutil.rmtree(trials, ignore_errors=True)
        shutil.rmtree(frames, ignore_errors=True)
    cache.write_text(json.dumps({"videos": videos, "images": images}, ensure_ascii=False), encoding="utf-8")
    return videos, images


def _pregenerate_data(side, req, plan: ThemePlan, beats: list[Beat], queries: list[str], *, pool: Path, judge,
                      cancel: CancelToken | None):
    """Куски про цифры известны сразу после запросов — их инфографика генерируется параллельно
    с подбором живых кадров (раньше — после него: +10–15 мин на ролик)."""
    if req.ai is None or not req.ai.active:
        return None
    slots = [
        {"kind": "image", "start": round(b.start, 3), "end": round(b.end, 3), "duration": round(b.duration, 3),
         "text": b.text[:240], "query": q, "data": q[len(DATA_MARK):], "fit": 0, "path": ""}
        for b, q in zip(beats, queries) if q.startswith(DATA_MARK) and b.start >= NO_AI_SEC
    ]
    if not slots:
        return None
    from engine.slides.ai_fill import fill_slots

    log(f"AI-кадры: {len(slots)} кусков про цифры — инфографика генерируется параллельно с подбором")
    return side.submit(fill_slots, slots, plan, req.ai, pool=pool, cancel=cancel, tag="beat", judge=judge, total=len(beats))


def _make_judge(req, plan: ThemePlan, cancel: CancelToken | None):
    """Проверка кадров зрячей LLM на весь подбор (свой клиент: запросы идут параллельно)."""
    from engine.slides.judge import make_judge

    judge = make_judge(req.llm_api_key, req.llm_model or "", req.llm_base_url or "", plan)
    if judge is not None and cancel:
        cancel.register(judge.client)
    return judge


def _late_moments(words: list[WordTiming], start: float, limit: int = 60) -> list[str]:
    """Фразы рассказа после начала ролика — под них пишутся AI-кадры пула (а не под общие темы)."""
    text = " ".join(w.word for w in words if w.start >= start)
    sents = [x.strip() for x in re.split(r"(?<=[.!?…])\s+", text) if len(x.strip()) > 25]
    if len(sents) <= limit:
        return sents
    step = len(sents) / limit
    return [sents[int(k * step)] for k in range(limit)]


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
    # «крючок»: если блок начинается раньше конца первых HOOK_SEC — сначала красивейшие кадры пула
    head = [i for i in hook_first(
        [head_rank(it) for _k, it, _d in items], [d for _k, _it, d in items], HOOK_SEC - start,
        video=[k == "video" for k, _it, _d in items],
        allowed=[it.get("provider") != "ai" for _k, it, _d in items],
    ) if str(items[i][1].get("path")) != avoid_first]
    rest = spread(
        [i for i in range(len(items)) if i not in set(head)],
        key=lambda i: str(items[i][1].get("path")),
        rng=rng,
        avoid_first=str(items[head[-1]][1].get("path")) if head else avoid_first,
    )
    order = head + rest
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
    r"колокольч|лайк|hit the bell|like (?:the|this) video|campanita|campanella|clopo[țt]el|dzwonek|glocke|cloche|dale like|"
    # финальный призыв: «три ссылки на экране, плейлист, поделитесь мнением в комментариях»
    r"три ссылки|ссылк\w* на экране|плейлист|playlist|links? on (?:the )?screen|link-uri|enlaces en (?:la )?pantalla|"
    r"liens? à l'écran|link sullo schermo|linki na ekranie|poleć|поделитесь (?:своим )?мнением|напишите в комментари",
    re.I,
)
# «подпишитесь…» — только рядом с «канал/видео» (иначе «подписал договор» в истории)
_CTA_SIGN = re.compile(r"подпи[сш]", re.I)
_CTA_CHANNEL = re.compile(r"канал|видео|channel|ролик", re.I)


_DATA_NUM = re.compile(r"\d")
_DATA_WORDS = re.compile(
    r"%|процент|миллион|миллиард|тысяч|трлн|млрд|млн|ВВП|доход|зарплат|рубл|доллар|евро|"
    r"percent|million|billion|trillion|thousand|GDP|income|salary|dollar|euro|"
    r"por ciento|millón|millones|mil millones|PIB|"
    r"procent|milion|miliard|PKB|"
    r"prozent|millionen|milliarde|BIP|"
    r"pour cent|millions|milliards|"
    r"per cento|milioni|miliardi|"
    r"lei\b|PIB|[$€₽£¥]",
    re.I,
)


def is_data(text: str) -> bool:
    """Кусок про цифры: число + проценты/миллионы/деньги (годы сами по себе не считаются)."""
    t = text or ""
    return bool(_DATA_NUM.search(t) and _DATA_WORDS.search(t))


def is_cta(text: str) -> bool:
    """Кусок — призыв подписаться / лайк / колокольчик (на любом из языков роликов)."""
    t = text or ""
    return bool(_CTA_STRONG.search(t) or (_CTA_SIGN.search(t) and _CTA_CHANNEL.search(t)))


def cta_spans(words: list[WordTiming]) -> list[tuple[float, float]]:
    """Где в озвучке призывы (по целым предложениям): кусок текста может разрезать призыв
    пополам, и половина без «подпишитесь» иначе получила бы кадр «под слова» (колокольчик, ссылки)."""
    spans: list[tuple[float, float]] = []
    sent: list[WordTiming] = []
    for w in words:
        sent.append(w)
        if _sentence_end(w.word) or w is words[-1]:
            text = " ".join(x.word for x in sent)
            if is_cta(text):
                if spans and sent[0].start - spans[-1][1] < 1.5:
                    spans[-1] = (spans[-1][0], sent[-1].end)  # призыв из нескольких предложений — один
                else:
                    spans.append((sent[0].start, sent[-1].end))
            sent = []
    return spans


def in_cta(beat: "Beat", spans: list[tuple[float, float]]) -> bool:
    """Кусок хотя бы наполовину приходится на призыв."""
    dur = max(0.1, beat.end - beat.start)
    covered = sum(max(0.0, min(beat.end, b) - max(beat.start, a)) for a, b in spans)
    return covered / dur >= 0.5


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
    (run_dir / "ai_report.json").unlink(missing_ok=True)  # отчёт AI-кадров — заново на этот подбор
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
            queries = _plan_queries(client, plan, beats, cancel, all_beats=full, cta=cta_spans(words))
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
    progress(0.10, "CLIP: гружу локальную модель…")
    get_clip()
    ctx = plan.context
    judge = _make_judge(req, plan, cancel)
    mix = MixState(
        subject=subject_of(ctx.subject, visual_style=ctx.visual_style, historical=ctx.historical),
        stats=SourceStats(),
        # LLM смотрит только начало ролика — время подбора остального как раньше
        budget=Budget.for_beats(len(beats)),
        cancel=cancel,
        judge=judge,
        custom=req.source_mix,
    )
    # проверка LLM отвечает 15–50 с — кусков в работе вдвое больше, иначе подбор растянется
    workers = STRICT_JUDGE_WORKERS if judge is not None else STRICT_FETCH_WORKERS
    log(
        f"Подбор: тема «{mix.subject}», видео {mix.weights(video=True)}, картинки {mix.weights(video=False)}"
        + (" (свои доли)" if mix.custom else " (по теме ролика)")
        + f", цель {TARGET_HEIGHT}p, не ниже {MIN_HEIGHT}p"
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

    side = ThreadPoolExecutor(max_workers=2)
    late_fut = None
    data_fut = _pregenerate_data(side, req, plan, beats, queries, pool=pool, judge=judge, cancel=cancel)
    try:
        if late_dur > 0.5:
            # пул на остаток ролика собирается одновременно с началом (раньше — после него: +20 мин)
            late_fut = side.submit(
                _late_pool, run_dir, req, plan, http=http, clients=clients, pool=pool, cancel=cancel,
                progress=lambda _p, m: log(f"Пул после 2 минут: {m}"), exclude=[x for x in slots if x], judge=judge,
            )
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
        if late_fut is not None:
            if not late_fut.done():
                progress(0.68, "Пул для остального ролика: дособираю…")
            late_pool = _drop_like_head(*late_fut.result(), slots, run_dir)
            _fill_head_from_pool(beats, slots, *late_pool, run_dir)
            _upgrade_head(beats, slots, late_pool[0], run_dir)
        else:
            late_pool = ([], [])
            _fill_head_holes_by_theme(
                http, clients, plan, beats, slots, queries, trials=trials, frames=frames, pool=pool, mix=mix,
            )
        _save_progress(run_dir, beats, queries, slots, variant)
    except BaseException:
        if late_fut is not None:
            late_fut.cancel()
        if judge is not None:
            judge.close()
        raise
    finally:
        side.shutdown(wait=False, cancel_futures=True)
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
    by_start = {round(b.start, 3): q for b, q in zip(beats, queries)}
    for slot in filled:
        q = by_start.get(round(float(slot.get("start") or 0), 3), "")
        if q.startswith(DATA_MARK):
            slot["data"] = q[len(DATA_MARK):]
    if data_fut is not None:
        try:
            data_fut.result()  # инфографика, заказанная параллельно с подбором
        except Exception as exc:
            log(f"AI-кадры: инфографика заранее не получилась ({exc}) — закажу вместе с остальными")
    if req.ai is not None and req.ai.active:
        # цифры/графики — в первую очередь; дальше дыры и слабые кадры (≤ ~12–15% ролика)
        from engine.slides.ai_fill import fill_slots

        progress(0.9, "AI-кадры: генерирую для кусков без хорошего живого кадра…")
        filled = fill_slots(filled, plan, req.ai, pool=pool, cancel=cancel, tag="beat",
                            progress=lambda p, m: progress(0.9 + 0.04 * p, m), judge=judge, total=len(beats))
    holes = sum(1 for item in filled if item.get("fallback"))
    if holes:
        log(f"Дыр без своего кадра: {holes}, закрыты другими найденными кадрами (не соседними)")
    if late_dur > 0.5:
        videos, images = late_pool
        if req.ai is not None and req.ai.active:
            from engine.slides.ai_fill import fill_pool

            videos, images = fill_pool(videos, images, plan, req.ai, pool=pool, image_target=len(images),
                                       video_target=len(videos), cancel=cancel, tag="late", judge=judge,
                                       moments=_late_moments(words, late_start))
        if not videos and not images:
            log("Пул для остального ролика пуст — повторяю кадры первых 2 минут вразбивку")
            videos = [dict(x) for x in filled if x.get("kind") == "video" and not x.get("fallback")]
            images = [dict(x) for x in filled if x.get("kind") == "image" and not x.get("fallback")]
        add_beauty([*videos, *images], run_dir)
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

    if judge is not None:
        log(f"Проверка кадров LLM: {judge.calls} листов" + ("" if judge.enabled else " (выключилась по ходу — дальше был CLIP)"))
        judge.close()
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
            + (f"  [LLM: {item['judge']}]" if item.get("judge") else "")
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

    if query.startswith(DATA_MARK):
        # цифры/график: живой кадр всё равно ищем (на случай выключенных AI-кадров),
        # а пометку несём дальше — AI-кадры поставят сюда инфографику в первую очередь
        desc = query[len(DATA_MARK):]
        hit, rej, weak = _pick_beat(http, clients, plan, beat, desc, index=index, trials=trials,
                                    frames=frames, pool=pool, mix=mix, full=full)
        if hit is not None:
            hit["data"] = desc
        return hit, rej, weak

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
    # первые 2 минуты — видео в приоритете (пролёты, виды), фото — только если видео не нашлось
    head = beat.start < NO_AI_SEC
    video_first = head or index % 2 == 0
    order = (True, False) if video_first else (False, True)
    best: dict | None = None
    rejected = 0
    for prefer_video in order:
        if best is not None and not best.get("weak"):
            break
        if best is not None and beat.shot > 0:
            break  # второй кадр куска: слабый, но найденный — берём, второй вид не ищем (скорость)
        if best is not None and head and best.get("kind") == "video":
            break  # начало ролика: видео есть (пусть не идеальное) — фото не нужно
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
    hook = beat.start < HOOK_SEC
    # Глазами LLM смотрим только начало ролика (там решается, останется ли зритель): её ответ
    # 20–45 с на кадр, а кадров в часовом ролике сотни. Дальше — быстрый подбор по запросам,
    # привязанным к стране и эпохе (+ CLIP). AI-кадры проверяются всегда (ai_quality).
    if hook and mix.judge is not None and mix.judge.enabled:
        return _try_kind_judged(
            http, clients, plan, beat, query, index=index, trials=trials, frames=frames, pool=pool,
            prefer_video=prefer_video, mix=mix, tries=tries, thematic=thematic,
        )
    found = _candidates(clients, query, plan, prefer_video=prefer_video, index=index, mix=mix, hook=hook)
    rejected = 0
    best: tuple[tuple[int, int], int, Path, StockCandidate, int | None] | None = None
    best_beauty = 0.0
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
            beauty = _beauty_of(trial, prefer_video) if hook else 0.0
            # начало ролика: берём сразу, только если кадр и по смыслу, и красивый, и чёткий
            pretty = not hook or (beauty >= HOOK_BEAUTY_OK and sharp)
            if fit >= IMAGE_FIT_MIN and not overused and not marked and (sharp or phase != "normal") and pretty:
                if best is not None:
                    best[2].unlink(missing_ok=True)
                mix.stats.picked(cand.provider)
                mix.add_look(fp)
                kept = cand
                slot = _keep(trial, cand, fit, beat, query, index, pool, prefer_video, weak=False)
                if hook:
                    slot["beauty"] = round(beauty, 4)
                return slot, rejected
            # лучший из проверенных: оценка (минус 1 за перекос источника; в начале ролика — плюс
            # красота кадра), при равенстве — 1080p
            bonus = round(beauty * HOOK_BEAUTY_WEIGHT) if hook else 0
            key = (fit - (1 if overused else 0) - (2 if marked else 0) + bonus, 1 if sharp else 0)
            if (allow_weak or keep_any or fit >= IMAGE_FIT_MIN) and (best is None or key > best[0]):
                if best is not None:
                    best[2].unlink(missing_ok=True)
                best = (key, fit, trial, cand, fp)
                best_beauty = beauty
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
        log(f"Кусок {index + 1}: best fit={fit} {cand.provider} ({'video' if prefer_video else 'image'})"
            + (f", красота {best_beauty:+.3f}" if hook else ""))
        slot = _keep(trial, cand, fit, beat, query, index, pool, prefer_video, weak=fit < IMAGE_FIT_MIN)
        if hook:
            slot["beauty"] = round(best_beauty, 4)
        return slot, rejected
    finally:
        # отбракованные кандидаты снова свободны для других кусков
        for c in claimed:
            if c is not kept:
                mix.release(c)


# проверка зрячей LLM: кандидатов на один лист (начало ролика — больше выбор)
JUDGE_BATCH = 2
JUDGE_BATCH_HOOK = 3
# итоговая оценка (0–10) кадра, после которой второй лист не смотрим
JUDGE_GOOD = 6.5
# первые 30 секунд (PRIME_SEC): ищем дольше и строже — до кадра уровня «вау»
PRIME_SEC = 30.0
JUDGE_GOOD_PRIME = 8.0
HEAD_QUALITY_MIN_PRIME = 8
JUDGE_BEAT_CAP_PRIME = 240.0
CANDIDATE_CAP_PRIME = 12
# начало ролика: красота CLIP (±0.08) → ±1.2 балла к оценке LLM (она и так смотрит на качество)
JUDGE_BEAUTY_WEIGHT = 15
# потолок времени на кусок (с учётом ожидания LLM): дальше — лучшее из проверенного
JUDGE_BEAT_CAP_HOOK = 150.0
# первые минуты: съёмка не ниже этого (0–10), иначе место закрывает лучший кадр пула
HEAD_QUALITY_MIN = 7
# кадр пула, которым можно заменить слабый кадр начала
POOL_QUALITY_FOR_HEAD = 8
JUDGE_BEAT_CAP = 150.0


@dataclass
class _Trial:
    cand: StockCandidate
    path: Path
    clip_fit: int
    fp: int | None
    sharp: bool
    beauty: float = 0.0
    shake: float | None = None


def _try_kind_judged(
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
    thematic: bool,
) -> tuple[dict | None, int]:
    """Кандидаты листами по 2–3: CLIP отсеивает совсем мимо, лучший выбирает LLM по смыслу
    ролика, словам момента и качеству съёмки. Всё мимо (чужая страна, эпоха, брак) — следующий лист;
    не нашлось — None (кусок добьют другим видом кадра, AI-кадром или повтором)."""
    from engine.slides.judge import Verdict
    from engine.slides.shake import SHAKY, shakiness

    judge = mix.judge
    hook = beat.start < HOOK_SEC
    prime = beat.start < PRIME_SEC
    found = _candidates(clients, query, plan, prefer_video=prefer_video, index=index, mix=mix, hook=hook)
    batch_n = JUDGE_BATCH_HOOK if hook else JUDGE_BATCH
    rejected = 0
    used = 0
    claimed: list[StockCandidate] = []
    kept: StockCandidate | None = None
    best: tuple[float, _Trial, Verdict] | None = None
    queue = iter(found)
    t0 = time.monotonic()
    cap_sec = JUDGE_BEAT_CAP_PRIME if prime else JUDGE_BEAT_CAP_HOOK if hook else JUDGE_BEAT_CAP
    cap_n = CANDIDATE_CAP_PRIME if prime else CANDIDATE_CAP

    def take(k: int) -> list[StockCandidate]:
        """Следующие k кандидатов (занятые этим куском)."""
        nonlocal used, rejected
        out: list[StockCandidate] = []
        for cand in queue:
            if used >= max(tries, cap_n):
                break
            if prefer_video and not candidate_is_landscape(cand.width, cand.height):
                rejected += 1
                continue
            if not mix.claim(cand):
                continue
            claimed.append(cand)
            used += 1
            out.append(cand)
            if len(out) >= k:
                break
        return out

    def prepare(cand: StockCandidate) -> _Trial | None:
        """Скачать и проверить файл (размер, повтор, CLIP). None — кандидат не годится."""
        if mix.cancel is not None:
            mix.cancel.check()
        ext = ".mp4" if prefer_video else _ext_from_url(cand.download_url)
        trial = trials / f"b{index:04d}_{cand.provider}_{cand.id}{ext}"
        if not _fetch_candidate(http, clients, cand, trial, prefer_video=prefer_video, index=index) \
                or not trial.is_file() or trial.stat().st_size < 1000:
            trial.unlink(missing_ok=True)
            return None
        if prefer_video and not is_landscape(trial):
            trial.unlink(missing_ok=True)
            return None
        height = _media_height(trial, video=prefer_video)
        if height < MIN_HEIGHT:
            log(f"skip low-res {trial.name} ({height}p < {MIN_HEIGHT}p)")
            trial.unlink(missing_ok=True)
            return None
        fp = fingerprint(trial)
        if mix.look_taken(fp):
            trial.unlink(missing_ok=True)
            return None
        fit = _score_file(trial, plan, beat, query, frames=frames, prefer_video=prefer_video, thematic=thematic)
        if fit is None:
            trial.unlink(missing_ok=True)
            return None
        mix.stats.observe(cand.provider, fit)
        t = _Trial(cand, trial, int(fit), fp, sharp=not prefer_video or height >= TARGET_HEIGHT)
        if hook:
            t.beauty = _beauty_of(trial, prefer_video)
        if prefer_video:
            t.shake = shakiness(trial)
        return t

    def next_batch() -> list[_Trial]:
        """Лист кандидатов: скачиваются одновременно, а не по одному."""
        nonlocal rejected
        batch: list[_Trial] = []
        while len(batch) < batch_n:
            cands = take(batch_n - len(batch))
            if not cands:
                break
            with ThreadPoolExecutor(max_workers=len(cands)) as ex:
                for t in ex.map(prepare, cands):
                    if t is None:
                        rejected += 1
                    else:
                        batch.append(t)
        return batch

    try:
        while True:
            batch = next_batch()
            if not batch:
                break
            verdicts = judge.rate([t.path for t in batch], moment=beat.text, query=query, thematic=thematic,
                                  label=f"judge_{index + 1}", opening=hook, prime=prime)
            by_clip = verdicts is None  # проверка недоступна — как раньше, по CLIP
            for k, t in enumerate(batch):
                v = Verdict(t.clip_fit, t.clip_fit, t.clip_fit) if by_clip else verdicts[k]
                if not by_clip:
                    log(f"Кусок {index + 1}: {t.cand.provider} {'видео' if prefer_video else 'фото'} — {v.note()}"
                        + (f", тряска {t.shake:.1f}" if t.shake is not None and t.shake > SHAKY else ""))
                head_min = HEAD_QUALITY_MIN_PRIME if prime else HEAD_QUALITY_MIN
                if not by_clip and (not v.ok or (hook and v.quality < head_min)):
                    # начало ролика: только сильная съёмка — слабое место закроет лучший кадр пула
                    rejected += 1
                    t.path.unlink(missing_ok=True)
                    continue
                # начало ролика: зрелищность важнее буквального смысла фразы
                key = (0.3 * v.context + 0.15 * v.beat + 0.55 * v.quality) if hook and not by_clip else v.score
                key -= 1 if mix.stats.overused(t.cand.provider) else 0
                key -= 2 if (t.cand.meta or {}).get("watermark") else 0
                key += 0.5 if t.sharp else 0
                key += t.beauty * JUDGE_BEAUTY_WEIGHT if hook else 0
                if t.shake is not None and t.shake > SHAKY:
                    key -= 2 if t.shake > 2 * SHAKY else 1  # съёмка с рук — только если лучше нет
                if best is None or key > best[0]:
                    if best is not None:
                        best[1].path.unlink(missing_ok=True)
                    best = (key, t, v)
                else:
                    t.path.unlink(missing_ok=True)
            good = JUDGE_GOOD_PRIME if prime else JUDGE_GOOD
            if best is not None and ((best[2].quality if prime else best[2].score) >= good or by_clip):
                break
            if mix.budget.phase == "hard":
                break
            if time.monotonic() - t0 > cap_sec:
                # на кусок ушло слишком много: берём лучшее из проверенного (или дыру закроет пул)
                log(f"Кусок {index + 1}: {cap_sec / 60:.0f} мин на поиск вышли — дальше не ищу")
                break
        if best is None:
            return None, rejected
        _key, t, v = best
        fit = int(round(v.score))
        mix.stats.picked(t.cand.provider)
        mix.add_look(t.fp)
        kept = t.cand
        slot = _keep(t.path, t.cand, fit, beat, query, index, pool, prefer_video, weak=fit < IMAGE_FIT_MIN)
        slot["judge"] = v.note()
        if not by_clip:
            slot["quality"] = v.quality
        if hook:
            slot["beauty"] = round(t.beauty, 4)
        return slot, rejected
    finally:
        for c in claimed:
            if c is not kept:
                mix.release(c)


def _beauty_of(path: Path, video: bool) -> float:
    """Красота кадра (CLIP): для видео — по кадру из середины клипа."""
    from engine.slides.clip_ranker import beauty_scores

    if not video:
        return beauty_scores([path]).get(path, 0.0)
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        frame = Path(tmp) / "f.jpg"
        subprocess.run([find_ffmpeg(), "-v", "error", "-y", "-ss", "2", "-i", str(path), "-frames:v", "1", "-q:v", "3", str(frame)],
                       capture_output=True, timeout=60)
        if not frame.is_file():
            subprocess.run([find_ffmpeg(), "-v", "error", "-y", "-i", str(path), "-frames:v", "1", "-q:v", "3", str(frame)],
                           capture_output=True, timeout=60)
        return beauty_scores([frame]).get(frame, 0.0) if frame.is_file() else 0.0


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
    # проверка кадров зрячей LLM (None — только CLIP)
    judge: object = None
    # свои доли источников, % (None — по теме ролика)
    custom: dict | None = None

    def weights(self, *, video: bool) -> dict[str, int]:
        from engine.slides.source_mix import custom_weights

        return custom_weights(self.custom, video=video) if self.custom else base_weights(self.subject, video=video)
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
    hook: bool = False,
) -> list[StockCandidate]:
    """Кандидаты со всех источников сразу: места делятся по долям темы и статистике."""
    base = mix.weights(video=prefer_video)
    if hook and prefer_video and mix.subject != "person" and "stocks" in base:
        # начало ролика: стоковые видео — профессиональная съёмка (дроны, таймлапсы), YouTube CC — чаще любительская
        base["stocks"] = base["stocks"] + 2
    weights = mix.stats.adjusted(base)
    groups_on = _available_groups(clients)
    if hook and mix.subject != "person":
        # начало ролика: Google Images приносит флаги, графики, афиши, скриншоты — только стоки и YouTube
        groups_on = [g for g in groups_on if g != "google"] or groups_on
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
    cta: list[tuple[float, float]] | None = None,
) -> list[str]:
    queries = [""] * len(beats)
    # Запрос на каждый кусок: видео-фаза всегда; «весь ролик» — и все остальные куски.
    # Иначе после видео-фазы подставятся тематические запросы.
    # второй кадр того же куска текста запрос не просит — берёт запрос первого
    video_beats = [(i, b) for i, b in enumerate(beats) if (b.video_phase or all_beats) and b.shot == 0]
    ctx = json.dumps(plan.context.to_dict(), ensure_ascii=False)

    def ask_chunk(chunk: list[tuple[int, Beat]], start: int) -> list[str]:
        if cancel:
            cancel.check()
        lines = [f"{j}. {beat.text}{' ★★' if beat.start < PRIME_SEC else ' ★' if beat.start < HOOK_SEC else ''}"
                 for j, (_i, beat) in enumerate(chunk, start=1)]
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
        return _parse_queries(raw)

    def ask(start: int) -> tuple[list[tuple[int, Beat]], list[str]]:
        chunk = video_beats[start : start + QUERY_BATCH]
        got = ask_chunk(chunk, start)
        if len(got) != len(chunk) and len(chunk) > 4:
            # ответов не столько, сколько кусков — запросы сдвинулись бы на чужие куски; пополам заново
            log(f"Запросы кадров: на {len(chunk)} кусков пришло {len(got)} ответов — переспрашиваю половинами")
            mid = len(chunk) // 2
            a, b = ask_chunk(chunk[:mid], start), ask_chunk(chunk[mid:], start + mid)
            got = [*a[:mid], *[""] * (mid - len(a[:mid])), *b]
        return chunk, got

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
        if queries[i].strip().upper() == "CTA" or is_cta(beat.text) or in_cta(beat, cta or []):
            # призыв подписаться — не буквально, а видео по теме всего ролика
            queries[i] = CTA_MARK + _theme_video_query(plan, i, fallback=fallback)
            log(f"Кусок {i + 1}: призыв подписаться — беру видео по теме ролика")
            continue
        q = queries[i].strip()
        if beat.start < HOOK_SEC and (q.upper().startswith("DATA:") or q.startswith(DATA_MARK)):
            # начало ролика: вместо графика — зрелищный общий план места
            queries[i] = _theme_video_query(plan, i, fallback=fallback)
            continue
        if q.upper().startswith("DATA:") or q.startswith(DATA_MARK):
            desc = q.split(":", 1)[1].strip() if q.upper().startswith("DATA:") else q[len(DATA_MARK):]
            queries[i] = DATA_MARK + (desc or fallback)
            continue
        if q and is_data(beat.text) and beat.start >= HOOK_SEC:
            # начало ролика — без инфографики: AI-кадров там нет, а графики не цепляют зрителя
            queries[i] = DATA_MARK + q
            continue
        if queries[i]:
            continue
        if beat.start < HOOK_SEC:
            queries[i] = _theme_video_query(plan, i, fallback=fallback)
        elif beat.video_phase:
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


def _drop_like_head(videos: list[dict], images: list[dict], slots: list[dict | None], run_dir: Path) -> tuple[list[dict], list[dict]]:
    """Пул собирался одновременно с началом: его кадры, совпавшие с кадрами начала
    (тот же клип или та же картинка с другого сайта), убираем — иначе повтор в первые минуты."""
    head = [x for x in slots if x]
    keys = {(str(x.get("provider") or ""), str(x.get("id") or "")) for x in head}
    looks = [fingerprint(run_dir / str(x.get("path") or "")) for x in head]
    used = {str(x.get("path") or "") for x in head}

    def keep(items: list[dict]) -> list[dict]:
        out = []
        for it in items:
            path = run_dir / str(it.get("path") or "")
            if str(it.get("path") or "") in used:
                continue  # этот файл уже стоит в начале (продолжение после сбоя) — не удалять
            if (str(it.get("provider") or ""), str(it.get("id") or "")) in keys or seen(fingerprint(path), looks):
                path.unlink(missing_ok=True)
                continue
            out.append(it)
        return out

    v, i = keep(videos), keep(images)
    if len(v) + len(i) < len(videos) + len(images):
        log(f"Пул: убрал {len(videos) + len(images) - len(v) - len(i)} кадров, уже стоящих в начале ролика")
    return v, i


def _fill_head_from_pool(beats: list[Beat], slots: list[dict | None], videos: list[dict], images: list[dict], run_dir: Path) -> None:
    """Куски начала без своего кадра — лучшими кадрами пула (видео вперёд, по красоте), а не повтором
    кадров начала. Взятые убираются из пула: в блоке после 2 минут они не повторятся."""
    holes = [i for i, b in enumerate(beats) if slots[i] is None and b.start < HOOK_SEC]
    if not holes or not (videos or images):
        return
    add_beauty([*videos, *images], run_dir)

    def rank(it: dict) -> tuple[float, float]:
        return (head_rank(it), float(it.get("score") or 0))

    order = [("video", it) for it in sorted(videos, key=rank, reverse=True)]
    order += [("image", it) for it in sorted(images, key=rank, reverse=True)]
    n = 0
    for i, (kind, it) in zip(holes, order):
        beat = beats[i]
        slots[i] = {
            "kind": kind, "path": it.get("path"), "start": round(beat.start, 3), "end": round(beat.end, 3),
            "duration": round(beat.duration, 3), "query": it.get("query", ""), "text": beat.text[:240],
            "provider": it.get("provider", ""), "id": it.get("id", ""),
            "source_title": it.get("source_title", ""), "source_author": it.get("source_author", ""),
            "fit": int(round(float(it.get("score") or 0) * 10)), "score": float(it.get("score") or 0),
            "weak": False, "fallback": False, "phase": "video", "beauty": it.get("beauty", 0.0), "from_pool": True,
            "quality": it.get("quality"),
        }
        (videos if kind == "video" else images).remove(it)
        n += 1
    log(f"Начало ролика: {n} кусков без своего кадра — лучшие кадры пула вместо повторов")


def _head_weak(slot: dict | None) -> bool:
    """Кадр начала, который стоит заменить: фото или слабая по оценке LLM съёмка."""
    if slot is None:
        return True
    q = slot.get("quality")
    return slot.get("kind") != "video" or (isinstance(q, (int, float)) and q < HEAD_QUALITY_MIN)


def _upgrade_head(beats: list[Beat], slots: list[dict | None], videos: list[dict], run_dir: Path) -> None:
    """Страховка первых минут: фото и слабые по съёмке кадры начала — на лучшие видео пула
    (съёмка от POOL_QUALITY_FOR_HEAD). Взятые видео уходят из пула: после 2 минут не повторятся."""
    weak = [i for i, b in enumerate(beats) if b.start < HEAD_SEC and slots[i] is not None and _head_weak(slots[i])]
    strong = sorted(
        (v for v in videos if isinstance(v.get("quality"), (int, float)) and v["quality"] >= POOL_QUALITY_FOR_HEAD),
        key=head_rank, reverse=True,
    )
    if not weak or not strong:
        return
    # сначала самые слабые места начала
    weak.sort(key=lambda i: (slots[i].get("kind") == "video", float(slots[i].get("quality") or 0)))
    n = 0
    for i, it in zip(weak, strong):
        old = slots[i]
        beat = beats[i]
        slots[i] = {
            **old, "kind": "video", "path": it.get("path"), "query": it.get("query", ""), "provider": it.get("provider", ""),
            "id": it.get("id", ""), "source_title": it.get("source_title", ""), "source_author": it.get("source_author", ""),
            "fit": int(round(float(it.get("score") or 0) * 10)), "score": float(it.get("score") or 0),
            "quality": it.get("quality"), "beauty": it.get("beauty", 0.0), "from_pool": True, "judge": "",
            "start": round(beat.start, 3), "end": round(beat.end, 3), "duration": round(beat.duration, 3),
        }
        videos.remove(it)
        n += 1
    log(f"Начало ролика: {n} фото/слабых кадров заменил лучшими видео пула (съёмка ≥ {POOL_QUALITY_FOR_HEAD})")


def _fill_head_holes_by_theme(http, clients, plan: ThemePlan, beats: list[Beat], slots: list[dict | None],
                              queries: list[str], *, trials: Path, frames: Path, pool: Path, mix: MixState) -> None:
    """«Весь ролик» (пула нет): куски начала без своего кадра — видео по теме ролика, а не повтор."""
    holes = [i for i, b in enumerate(beats) if b.start < HEAD_SEC and _head_weak(slots[i])]
    if not holes:
        return
    log(f"Начало ролика: {len(holes)} кусков без своего кадра или со слабым кадром — ищу видео по теме ролика")

    def one(i: int) -> tuple[int, dict | None]:
        hit, _rej = _try_kind(
            http, clients, plan, beats[i], _theme_video_query(plan, i, fallback=queries[i]),
            index=i, trials=trials, frames=frames, pool=pool, prefer_video=True, mix=mix,
            tries=VIDEO_TRIES, allow_weak=True, keep_any=True, thematic=True,
        )
        return i, hit

    # параллельно: ждём в основном ответа LLM, а не процессор
    with ThreadPoolExecutor(max_workers=min(16, len(holes))) as ex:
        for i, hit in ex.map(one, holes):
            if hit is None:
                continue
            if slots[i] is not None:
                if _head_weak(hit) and float(hit.get("quality") or 0) <= float(slots[i].get("quality") or 0):
                    _drop_slot(pool, hit, mix)  # новое не лучше прежнего
                    continue
                _drop_slot(pool, slots[i], mix)
            slots[i] = hit


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
        # начало ролика: повтор кадра оттуда же бросается в глаза — сначала берём кадры из дальних частей
        late_only = beat.start < HOOK_SEC and any(float(x.get("start") or 0) >= HOOK_SEC for x in originals)
        for far, allow_neighbour in ((late_only, False), (False, False), (False, True)):
            for kind in kinds_for(beat):
                pool: dict[str, dict] = {}
                for slot in originals:
                    if far and float(slot.get("start") or 0) < HOOK_SEC:
                        continue
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
