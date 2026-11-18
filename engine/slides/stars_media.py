"""Ролик про конкретных людей — один герой или 5–15 (актёры, певцы, политики…). Работает в любом
режиме подбора («По смыслу», «Строго первые 2 минуты», «Строго весь ролик»): если по теме ролик про
людей, кадры подбираются так, а не по фразам.

1. LLM читает текст: список героев и на каждую фразу — о ком она (или «общее»:
   вступление, призыв подписаться, заключение).
2. По таймингам слов — отрезки ролика: «0:40–4:10 — Никулин, 4:10–7:30 — Миронов…».
3. На каждого героя — его фото (Google, Wikimedia) и немного видео (YouTube CC), проверка
   по лицу: на кадре именно он (faces), чужие лица отсекаются. Кадры с крупным текстом,
   коллажи и водяные знаки отсекает зрячая LLM (если она доступна).
4. На отрезке героя — только его кадры, без одинаковых подряд. Общие части — фото всех
   героев по очереди. AI-кадров в этом режиме нет: только настоящие фото людей.
Превью YouTube с портретами героев делается отдельно — вкладка «Превью» (engine/slides/thumbnail).
"""

from __future__ import annotations

import json
import math
import random
import re
import shutil
from concurrent.futures import as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from engine.core.cancel import CancelToken
from engine.core.chapters import timed_sentences
from engine.core.logutil import log
from engine.core.spread import spread
from engine.core.threads import ThreadPoolExecutor
from engine.slides import checkpoint as cp
from engine.slides.ffprobe_util import probe_duration
from engine.slides.media_pipeline import MediaError, MediaRequest, MediaStageResult, _build_clients
from engine.slides.paths import AUDIO_NAME, SCRIPT_NAME, TIMELINE_NAME, TIMINGS_NAME
from engine.slides.story_context import StoryContext
from engine.slides.theme_plan import ThemePlan, ensure_theme_plan

STARS_NAME = "stars.json"
GENERAL = 0  # часть текста не про одного героя
IMAGE_HOLD = 4.5  # секунд на фото
VIDEO_MAX = 6.0
STAR_WORKERS = 4  # героев ищем одновременно (Google упрётся в капчу — сам перейдёт на Bing)
_MODEL_TRIES = 3
# слова в причине отказа LLM, из-за которых фото звезды не берём (лицо — проверено по лицу)
_BAD_LOOK = ("watermark", "logo", "text", "caption", "subtitle", "collage", "split", "meme", "screenshot", "poster")

_SYSTEM = """\
Ты редактор документальных роликов о знаменитостях. По тексту ролика с пронумерованными фразами
определяешь, о каких людях ролик и о ком каждая часть текста. Отвечаешь только JSON."""


def _prompt(sents: list[tuple[float, str]]) -> str:
    lines = [f"[{i}] {s[:180]}" for i, (_t, s) in enumerate(sents)]
    return (
        "1. Составь список героев ролика — людей, которым посвящены отдельные части текста "
        "(не тех, кто упомянут мимоходом). Ролик про одного человека — один герой. Героев нет "
        "(ролик о событии, стране, природе) — пустой список stars.\n"
        "2. Разбей текст на части по героям: часть начинается с фразы, где рассказ переходит к другому человеку.\n"
        "Часть про героя — вся его история, даже если в ней упоминаются другие люди. "
        "star=0 — часть не про одного героя: вступление, призыв подписаться, заключение, рассказ сразу о многих.\n"
        "Для каждого героя:\n"
        "- name — имя и фамилия в именительном падеже на языке текста («Юрий Никулин»);\n"
        "- latin — имя латиницей, как его пишут в мире («Yuri Nikulin»);\n"
        "- search — как его чаще всего ищут в поиске картинок (обычно полное имя на языке его страны; "
        "если так зовут и других известных людей — как ищут именно этого: «Баба Ванга»);\n"
        "- kind — photo, если есть его настоящие фотографии (жил после ~1840), иначе pre_photo;\n"
        "- years — годы жизни или эпоха («1921–1997»);\n"
        "- who — кто это, коротко: профессия и страна («советский актёр театра и кино», «болгарская ясновидящая»). "
        "По этим словам ищутся его фото, чтобы не попал тёзка — бери то, что следует из текста ролика;\n"
        "- doubt — пусто; заполняй, только если даже по тексту непонятно, какой из двух реальных людей с таким "
        "именем имеется в виду (перечисли обоих коротко).\n"
        "Тёзки и однофамильцы: ролик обычно однороден — если большинство героев советские артисты, то и этот герой "
        "советский артист той же эпохи, а не современный тёзка. Уточнения пользователя (если есть) — главнее всего.\n"
        'Ответ: {"stars": [{"id": 1, "name": "...", "latin": "...", "search": "...", "kind": "photo", "years": "...", '
        '"who": "...", "doubt": ""}], '
        '"parts": [{"i": номер фразы, где начинается часть, "star": id героя или 0}]}\n'
        "Части идут по порядку, первая — с фразы [0].\n\n"
        "Фразы ролика:\n" + "\n".join(lines)
    )


@dataclass
class Star:
    id: int
    name: str
    latin: str
    search: str
    kind: str = "photo"
    years: str = ""
    who: str = ""  # «советский актёр театра и кино» — поиск фото с уточнением (не тёзка)
    doubt: str = ""


class CastDoubt(MediaError):
    """Непонятно, какой из двух реальных людей имеется в виду — спрашиваем пользователя."""


def plan_cast(llm: tuple[str, str, str], sents: list[tuple[float, str]]) -> tuple[list[Star], list[tuple[int, int]]]:
    """→ (герои, части [(номер фразы, id героя или 0)]). Не вышло — MediaError."""
    from engine.slides import llm_models
    from engine.slides.llm_client import LLMClient

    key, base, model = llm
    notes = stars_notes()
    user = _prompt(sents) + (f"\n\nУТОЧНЕНИЯ ПОЛЬЗОВАТЕЛЯ О ГЕРОЯХ (главнее всего):\n{notes[:3000]}" if notes else "")
    last = ""
    for use in list(dict.fromkeys([m for m in [model, *llm_models.chain(base, key, "smart")] if m]))[:_MODEL_TRIES]:
        client = LLMClient(key, use, base)
        try:
            raw = client.complete(label="stars_cast", system=_SYSTEM, user=user)
            stars, parts = _parse_cast(raw, len(sents))
            if stars and parts:
                doubts = [f"{s.name}: {s.doubt}" for s in stars if s.doubt]
                if doubts:
                    from engine.slides.identities import doubt_message

                    # спрашиваем, а не берём наугад: уточнение — в поле «Кто эти люди», потом «Продолжить»
                    raise CastDoubt(doubt_message(doubts, "в поле «Кто эти люди» (Стоковые слайды → «Подбор кадров»), "
                                                          "потом нажми «Продолжить»"))
                log("Люди: " + "; ".join(f"{s.name} — {s.who or '?'} ({s.years})" for s in stars))
                return stars, parts
            if re.search(r'"stars"\s*:\s*\[\s*\]', raw or ""):
                log("Люди: в тексте нет героев, которым посвящены части ролика")
                return [], []  # ответ «героев нет» — тоже ответ, другую модель не спрашиваем
            last = "LLM не нашёл героев в тексте"
        except MediaError:
            raise
        except Exception as exc:
            last = str(exc)[:200]
            log(f"Люди: модель {use} не разметила текст ({last})")
        finally:
            client.close()
    raise MediaError(f"Не удалось разобрать, о ком ролик: {last}")


def _parse_cast(raw: str, n_sents: int) -> tuple[list[Star], list[tuple[int, int]]]:
    m = re.search(r"\{.*\}", raw or "", re.S)
    data = json.loads(m.group(0)) if m else {}
    stars: list[Star] = []
    for s in data.get("stars") or []:
        try:
            sid = int(s.get("id"))
        except (TypeError, ValueError):
            continue
        name = str(s.get("name") or "").strip()
        if sid <= 0 or not name or any(x.id == sid for x in stars):
            continue
        kind = "pre_photo" if str(s.get("kind") or "").strip().lower() == "pre_photo" else "photo"
        stars.append(Star(sid, name, str(s.get("latin") or name).strip(), str(s.get("search") or name).strip(),
                          kind, str(s.get("years") or "").strip(), str(s.get("who") or "").strip(),
                          str(s.get("doubt") or "").strip()))
    ids = {s.id for s in stars}
    parts: list[tuple[int, int]] = []
    for p in data.get("parts") or []:
        try:
            i, sid = int(p.get("i")), int(p.get("star") or 0)
        except (TypeError, ValueError):
            continue
        if 0 <= i < n_sents:
            parts.append((i, sid if sid in ids else GENERAL))
    parts.sort()
    if parts and parts[0][0] != 0:
        parts.insert(0, (0, GENERAL))
    # подряд одно и то же — одна часть
    merged: list[tuple[int, int]] = []
    for i, sid in parts:
        if merged and (merged[-1][1] == sid or merged[-1][0] == i):
            if merged[-1][0] == i:
                merged[-1] = (i, sid)
            continue
        merged.append((i, sid))
    used = {sid for _i, sid in merged}
    return [s for s in stars if s.id in used], merged


def part_times(parts: list[tuple[int, int]], sents: list[tuple[float, str]], end: float) -> list[tuple[float, float, int]]:
    """Части по фразам → отрезки ролика (начало, конец, id героя)."""
    out: list[tuple[float, float, int]] = []
    for k, (i, sid) in enumerate(parts):
        t0 = 0.0 if k == 0 else sents[i][0]
        t1 = sents[parts[k + 1][0]][0] if k + 1 < len(parts) else end
        if t1 - t0 > 0.3:
            out.append((round(t0, 3), round(t1, 3), sid))
    return out


# ---------- кадры героев ----------


def stars_notes() -> str:
    """Уточнения пользователя о героях («Андрей Миронов — советский актёр»): поле в разделе слайдов.
    Читаются свежими (не из снимка задачи): вписал уточнение — «Продолжить» сразу его учтёт."""
    from engine.core.config import load_config

    return str(load_config().get("stars_notes") or "").strip()


def anchor_queries(star: Star, name: str) -> list[str]:
    """Точные запросы: имя + кто это + годы жизни. По ним выдача — почти только нужный человек,
    тёзка или однофамилец (актёр вместо певца, мужчина вместо Ванги) туда не попадает."""
    out: list[str] = []
    if star.who:
        out += [f"{name} {star.who}", f"{name} {star.who} фото"]
    if star.years:
        out.append(f"{name} {star.years}")
    if star.who and star.years:
        out.append(f"{name} {star.who} {star.years}")
    return list(dict.fromkeys(out))


def _star_plan(plan: ThemePlan, star: Star, name: str) -> ThemePlan:
    """Запросы на этого героя: его имя, его эпоха (не всего ролика); с уточнением «кто это» — первыми,
    чтобы в выдаче не преобладал тёзка."""
    ctx = StoryContext(era=star.years or plan.context.era, place="", characters=[name],
                       subject="person", person_era=star.kind)
    hinted = anchor_queries(star, name)
    return ThemePlan(context=ctx, summary=plan.summary, video_queries=hinted[:1], image_queries=hinted)


def collect_star(
    http: httpx.Client,
    clients: dict,
    plan: ThemePlan,
    star: Star,
    *,
    seconds: float,
    run_dir: Path,
    cancel: CancelToken | None,
    heroes: int = 2,
) -> tuple[list[dict], list[dict]]:
    """Фото и видео одного героя (только с его лицом) → (videos, images), пути от run_dir."""
    videos, images = _collect_by_name(http, clients, plan, star, star.search, seconds=seconds, run_dir=run_dir,
                                      cancel=cancel, heroes=heroes)
    if not images and star.latin and star.latin.lower() != star.search.lower():
        log(f"Люди: {star.name} — по «{star.search}» фото нет, ищу по «{star.latin}»")
        videos, images = _collect_by_name(http, clients, plan, star, star.latin, seconds=seconds, run_dir=run_dir,
                                          cancel=cancel, heroes=heroes)
    log(f"Люди: {star.name} — фото {len(images)}, видео {len(videos)} (на {seconds:.0f}с)")
    return videos, images


def photo_targets(seconds: float, heroes: int) -> tuple[int, int]:
    """Сколько фото и видео искать на героя. Ролик про одного — фото на весь ролик, как в обычном пуле
    (меньше повторов); героев много — у каждого свой короткий отрезок, хватает меньшего."""
    cap = 80 if heroes <= 1 else (50 if heroes <= 3 else 30)
    image_need = max(8, min(cap, round(seconds / IMAGE_HOLD / 1.5)))
    if seconds < 90:
        video_need = 0
    elif heroes <= 1:
        video_need = min(6, max(1, round(seconds / 150)))
    else:
        video_need = 1 if seconds < 240 else 2
    return image_need, video_need


def _collect_by_name(http, clients, plan: ThemePlan, star: Star, name: str, *, seconds: float, run_dir: Path,
                     cancel: CancelToken | None, heroes: int = 2) -> tuple[list[dict], list[dict]]:
    from engine.slides.person_media import collect_person_pool

    pool = run_dir / "pool" / f"s{star.id:02d}"
    trials, frames = pool / "_trials", pool / "_frames"
    shutil.rmtree(pool, ignore_errors=True)
    trials.mkdir(parents=True, exist_ok=True)
    frames.mkdir(parents=True, exist_ok=True)
    image_need, video_need = photo_targets(seconds, heroes)
    image_need = math.ceil(image_need * 1.25)  # запас: коллажи и фото с надписями потом уберёт проверка LLM
    try:
        videos, images, _rv, _ri = collect_person_pool(
            http, clients, _star_plan(plan, star, name), name=name, anchors=anchor_queries(star, name),
            kind="photo" if star.kind == "photo" else "ancient",
            rng=random.Random(star.id), pool=pool, trials=trials, frames=frames, cancel=cancel,
            progress=lambda _p, m: log(f"{star.name}: {m}"), pct_lo=0.0, pct_hi=1.0,
            image_need=image_need, video_need=video_need,
        )
    finally:
        shutil.rmtree(trials, ignore_errors=True)
        shutil.rmtree(frames, ignore_errors=True)
    for it in [*videos, *images]:
        # collect_person_pool пишет путь от run_dir/pool — делаем от run_dir
        it["path"] = str((pool.parent / it["path"]).relative_to(run_dir))
        it["star"] = star.id
    return videos, images


# герой без единого своего фото: кадры без людей по теме «знаменитости», а не чужое лицо
_NEUTRAL_QUERIES = ("theater stage curtain spotlight", "vintage film camera", "old film reel", "empty cinema hall",
                    "stage lights concert", "microphone on stage", "film clapperboard", "red carpet event lights")


def neutral_media(http: httpx.Client, clients: dict, run_dir: Path, need: int = 8) -> list[dict]:
    """Нейтральные кадры со стоков, на которых нет лиц (проверка по лицу)."""
    from engine.slides import faces
    from engine.slides.media_pipeline import _download, _search_source

    dest = run_dir / "pool" / "neutral"
    dest.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    for q in _NEUTRAL_QUERIES:
        if len(out) >= need:
            break
        for cand in _search_source(clients, "stocks", q, prefer_video=False, rng=random.Random(len(out)))[:3]:
            path = dest / f"n{len(out):02d}.jpg"
            if not _download(http, cand.download_url, path, retries=2):
                continue
            if faces.face_embeddings(path):
                path.unlink(missing_ok=True)  # на кадре человек — это может быть кто угодно
                continue
            out.append({"path": str(path.relative_to(run_dir)), "kind": "image", "provider": cand.provider,
                        "id": cand.id, "query": q, "star": GENERAL})
            break
    log(f"Люди: нейтральных кадров без людей — {len(out)}")
    return out


def _clean_by_judge(judge, star: Star, items: list[dict], run_dir: Path) -> list[dict]:
    """Фото с крупным текстом, коллажи, водяные знаки — вон (лицо уже проверено по лицу)."""
    if judge is None or not judge.enabled:
        return items

    def one(it: dict) -> tuple[dict, str]:
        v = judge.pool().rate(run_dir / it["path"], star.search)
        why = (v.reject or "") if v is not None else ""
        return it, why

    keep: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for it, why in ex.map(one, items):
            if why and any(w in why.lower() for w in _BAD_LOOK):
                log(f"Люди: {star.name} — убрал фото ({why})")
                (run_dir / it["path"]).unlink(missing_ok=True)
                continue
            keep.append(it)
    return keep


# ---------- ряд кадров ----------


def build_slots(
    parts: list[tuple[float, float, int]],
    media: dict[int, list[dict]],
    stars: list[Star],
    *,
    rng: random.Random,
    neutral: list[dict] | None = None,
) -> list[dict]:
    """На отрезке героя — его кадры (без одинаковых подряд); общие части — все герои по очереди.
    Герой без своих кадров — нейтральные кадры без людей (не чужое лицо)."""
    names = {s.id: s.name for s in stars}
    slots: list[dict] = []
    general_turn = 0
    for t0, t1, sid in parts:
        if sid != GENERAL and media.get(sid):
            seq = _star_sequence(media[sid], t1 - t0, rng, avoid=slots[-1]["path"] if slots else None)
        elif sid != GENERAL:
            log(f"Люди: у «{names.get(sid, sid)}» нет своих фото — ставлю нейтральные кадры без людей")
            seq = _star_sequence(neutral or [], t1 - t0, rng, avoid=slots[-1]["path"] if slots else None)
            if not seq:
                seq, general_turn = _general_sequence(media, stars, t1 - t0, general_turn)
        else:
            seq, general_turn = _general_sequence(media, stars, t1 - t0, general_turn)
        t = t0
        for it in seq:
            if t >= t1 - 0.05:
                break
            dur = min(VIDEO_MAX, float(it.get("duration") or VIDEO_MAX)) if it.get("kind") == "video" else IMAGE_HOLD
            dur = min(dur, t1 - t)
            if t1 - (t + dur) < 1.5:
                dur = t1 - t  # хвостик — дотягиваем этот кадр до конца части
            slots.append({
                "kind": it.get("kind", "image"), "path": it["path"], "start": round(t, 3), "end": round(t + dur, 3),
                "duration": round(dur, 3), "query": it.get("query", ""), "text": "", "provider": it.get("provider", ""),
                "id": it.get("id", ""), "source_title": it.get("source_title", ""),
                "source_author": it.get("source_author", ""), "fit": 10, "score": 1.0, "weak": False,
                "fallback": False, "phase": "stars", "star": names.get(it.get("star"), ""),
            })
            t += dur
    return slots


def _star_sequence(items: list[dict], seconds: float, rng: random.Random, *, avoid: str | None) -> list[dict]:
    need = max(1, math.ceil(seconds / IMAGE_HOLD) + 1)
    pool = list(items)
    rng.shuffle(pool)
    seq = (pool * math.ceil(need / len(pool)))[:need] if pool else []
    return spread(seq, key=lambda it: it["path"], rng=rng, avoid_first=avoid)


def _general_sequence(media: dict[int, list[dict]], stars: list[Star], seconds: float, turn: int) -> tuple[list[dict], int]:
    """Общая часть: фото героев по очереди (каждый раз — следующий герой и следующее его фото)."""
    have = [s.id for s in stars if media.get(s.id)]
    if not have:
        return [], turn
    need = max(1, math.ceil(seconds / IMAGE_HOLD) + 1)
    seq: list[dict] = []
    for k in range(need):
        sid = have[(turn + k) % len(have)]
        photos = [it for it in media[sid] if it.get("kind") != "video"] or media[sid]
        seq.append(photos[((turn + k) // len(have)) % len(photos)])
    return seq, turn + need


# ---------- стадия целиком ----------


def collect_people_timeline(
    req: MediaRequest,
    *,
    cancel: CancelToken | None = None,
    on_progress=None,
) -> MediaStageResult | None:
    """Ролик про конкретных людей (любой режим подбора) — на часть текста про каждого только его фото.
    None — ролик не про людей, героев в тексте нет или ни одного их фото не нашлось: подбор идёт
    по выбранному режиму как обычно. Не ясно, кто из тёзок, — CastDoubt (спрашиваем пользователя)."""

    def progress(pct: float, msg: str) -> None:
        log(msg)
        if on_progress:
            on_progress(pct, msg)

    run_dir = req.run_dir
    script = (run_dir / SCRIPT_NAME).read_text(encoding="utf-8") if (run_dir / SCRIPT_NAME).is_file() else ""
    if not (req.llm_api_key or "").strip() or not script.strip() or not (run_dir / TIMINGS_NAME).is_file():
        return None
    from engine.core.chapters import read_words
    from engine.slides.source_mix import subject_of
    from engine.slides.theme_media import _llm

    voice = run_dir / AUDIO_NAME
    words = read_words(run_dir / TIMINGS_NAME)
    end = probe_duration(voice) if voice.is_file() else float((words or [{}])[-1].get("end") or 0)
    sents = timed_sentences(script, words, end)
    llm = (req.llm_api_key, req.llm_base_url or "", req.llm_model or "")

    progress(0.03, "Тема ролика…")
    client = _llm(req)
    if cancel:
        cancel.register(client)
    try:
        plan = ensure_theme_plan(run_dir, script, client)
    finally:
        client.close()
    ctx = plan.context
    if subject_of(ctx.subject, visual_style=ctx.visual_style, historical=ctx.historical) != "person":
        return None  # ролик не про людей — разметка по героям не нужна (и LLM не спрашиваем)

    cast_file = run_dir / STARS_NAME
    cached = _load_cast(cast_file, len(sents))
    if cached is not None:
        stars, parts = cached
    else:
        progress(0.06, "Ролик про людей: кто герои и о ком каждая часть текста…")
        try:
            stars, parts = plan_cast(llm, sents)
        except CastDoubt:
            raise
        except MediaError as exc:
            log(f"Люди: разметка по героям не вышла ({exc}) — подбираю по выбранному режиму")
            return None
        cast_file.write_text(json.dumps({"stars": [asdict(s) for s in stars], "parts": parts, "n": len(sents)},
                                        ensure_ascii=False, indent=1), encoding="utf-8")
    if not stars:
        return None
    times = part_times(parts, sents, end)
    seconds: dict[int, float] = {}
    for t0, t1, sid in times:
        seconds[sid] = seconds.get(sid, 0.0) + (t1 - t0)
    log("Люди: " + "; ".join(f"{s.name} — {seconds.get(s.id, 0):.0f}с" for s in stars)
        + f"; общие части — {seconds.get(GENERAL, 0):.0f}с")
    progress(0.1, f"Люди: героев {len(stars)} — ищу фото каждого")

    from engine.slides.judge import make_judge

    judge = make_judge(req.llm_api_key, req.llm_model or "", req.llm_base_url or "", plan)
    if judge is not None and cancel:
        cancel.register(judge.client)
    clients = _build_clients(req.keys)
    http = httpx.Client(timeout=120.0, trust_env=False, follow_redirects=True)
    media: dict[int, list[dict]] = {}
    try:
        with ThreadPoolExecutor(max_workers=STAR_WORKERS) as ex:
            # ролик про одного героя — его фото и на вступление с заключением
            futs = {ex.submit(collect_star, http, clients, plan, s,
                              seconds=max(30.0, end if len(stars) == 1 else seconds.get(s.id, 0.0)),
                              run_dir=run_dir, cancel=cancel, heroes=len(stars)): s for s in stars}
            done = 0
            for fut in as_completed(futs):
                if cancel:
                    cancel.check()
                star = futs[fut]
                videos, images = fut.result()
                images = _clean_by_judge(judge, star, images, run_dir)
                media[star.id] = [*({**v, "kind": "video"} for v in videos), *({**i, "kind": "image"} for i in images)]
                done += 1
                progress(0.1 + 0.8 * done / len(stars), f"Люди: {star.name} — {len(images)} фото, {len(videos)} видео ({done}/{len(stars)})")

        if not any(media.values()):
            log("Люди: не нашёл фото ни одного героя — подбираю по выбранному режиму")
            shutil.rmtree(run_dir / "pool", ignore_errors=True)
            return None
        missing = [s.name for s in stars if not media.get(s.id) and seconds.get(s.id)]
        neutral = neutral_media(http, clients, run_dir) if missing else []
        if missing:
            log(f"Люди: не нашёл фото — {', '.join(missing)}")
    finally:
        http.close()
        for c in clients.values():
            try:
                c.close()
            except Exception:
                pass
        if judge is not None:
            judge.close()

    slots = build_slots(times, media, stars, rng=random.Random(req.seed), neutral=neutral)

    payload = {
        "mode": "strict_sync",
        "variant": "stars",
        "summary": plan.summary,
        "fade_sec": 0.5,
        "story_context": plan.context.to_dict(),
        "slots": slots,
        "loop_block": [],
        "stars": [asdict(s) for s in stars],
        "people": True,
        "parts": [{"start": t0, "end": t1, "star": sid} for t0, t1, sid in times],
    }
    timeline = run_dir / TIMELINE_NAME
    timeline.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    preview = run_dir / "timeline_preview.txt"
    names = {s.id: s.name for s in stars}
    preview.write_text(
        "\n".join(f"{t0:7.1f}-{t1:7.1f}  {names.get(sid, 'общее (все герои)')}" for t0, t1, sid in times) + "\n",
        encoding="utf-8",
    )
    chk = cp.load(run_dir)
    cp.mark_media_done(
        run_dir, language=chk.language, voice_id=chk.voice_id, text_chars=chk.text_chars, audio_bytes=chk.audio_bytes,
        desktop_audio=chk.desktop_audio, word_count=chk.word_count, duration=chk.duration,
        desktop_preview=chk.desktop_preview, chunk_count=chk.chunk_count, desktop_chunks=chk.desktop_chunks,
        selected_media=len(slots), desktop_timeline="",
    )
    progress(1.0, f"Ролик про людей: героев {len(stars)}, кадров {len(slots)}")
    return MediaStageResult(run_dir=run_dir, timeline_file=timeline, preview_file=preview, desktop_preview=None,
                            selected=len(slots), skipped=0, weak=0)


def _load_cast(path: Path, n_sents: int) -> tuple[list[Star], list[tuple[int, int]]] | None:
    """Разметка уже есть (продолжение после сбоя) — LLM второй раз не спрашиваем.
    Записанное «героев нет» — тоже ответ: ([], [])."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("n") != n_sents:
            return None
        stars = [Star(**s) for s in raw["stars"]]
        parts = [(int(i), int(sid)) for i, sid in raw["parts"]]
        if not stars:
            return [], []
        return (stars, parts) if parts else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


# ---------- главы по героям ----------

_INTRO = {"ru": "Вступление", "uk": "Вступ", "en": "Introduction", "es": "Introducción", "de": "Einleitung",
          "fr": "Introduction", "pl": "Wstęp", "it": "Introduzione", "ro": "Introducere"}
_OUTRO = {"ru": "Заключение", "uk": "Висновок", "en": "Conclusion", "es": "Conclusión", "de": "Fazit",
          "fr": "Conclusion", "pl": "Zakończenie", "it": "Conclusione", "ro": "Încheiere"}


def star_chapters(timeline: dict, language: str = "") -> list[tuple[float, str]]:
    """Главы по героям: «4:10 — Юрий Никулин». Общие части: в начале — «Вступление», в конце — «Заключение»."""
    code = (language or "ru").split("-")[0].lower()
    latin = code not in ("ru", "uk", "be", "")
    stars = {int(s["id"]): (s.get("latin") if latin else s.get("name")) or s.get("name") for s in timeline.get("stars") or []}
    parts = timeline.get("parts") or []
    out: list[tuple[float, str]] = []
    for k, p in enumerate(parts):
        sid, t0 = int(p.get("star") or 0), float(p.get("start") or 0)
        if sid:
            title = stars.get(sid, "")
        elif not out:
            title = _INTRO.get(code, _INTRO["en"])
        elif k == len(parts) - 1:
            title = _OUTRO.get(code, _OUTRO["en"])
        else:
            continue  # общая вставка посередине (призыв подписаться) — часть текущей главы
        if title and (not out or out[-1][1] != title):
            out.append((t0, title))
    return out
