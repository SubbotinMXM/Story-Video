"""Ролик про конкретных людей — один герой или несколько (актёры, певцы, политики…). Работает в любом
режиме подбора («По смыслу», «Строго первые 2 минуты», «Строго весь ролик»): если ролик про людей,
кадры подбираются так, а не по фразам.

1. LLM читает текст и делит ролик на БЛОКИ — непрерывные куски по героям: «Никулин», «Миронов»,
   «Рыбников и Румянцева» (если кусок про их отношения). Вступление и заключение — единственные
   «общие» куски; посреди ролика общих кусков нет. Блоков немного (до 12 вместе со вступлением и
   заключением), каждый не короче ~1,5 минуты — иначе соседние склеиваются (shape_parts).
2. Названия блоков = главы в описании: «4:10 — Юрий Никулин».
3. На каждого героя — много его фото (Google/Bing, Wikimedia, кадры из его фильмов) и видео (YouTube CC),
   проверка по лицу: на кадре именно он. Коллажи, водяные знаки и крупный текст отсекает зрячая LLM.
4. В блоке героя — только его кадры, без повторов во всём ролике (повтор — только если материала
   не хватило, и тогда медленнее смена кадров, а не те же снимки подряд). Видео расставлены ровно.
   Если собственных видео мало — между блоками атмосферные стоковые видео без людей.
"""

from __future__ import annotations

import json
import math
import random
import re
import shutil
import threading
import time
from concurrent.futures import as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from engine.core.jsonish import json_dict
from engine.core.cancel import CancelledError as CancelledErrorBase
from engine.core.cancel import CancelToken
from engine.core.chapters import _clock, timed_sentences
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
CAST_VERSION = 2  # формат разметки; старые stars.json (по одной фразе на героя) пересчитываются
IMAGE_HOLD = 4.5  # секунд на фото (если снимков много)
IMAGE_HOLD_TARGET = 6.0  # на сколько секунд фото рассчитан запас при подборе
IMAGE_HOLD_MAX = 8.0  # снимков мало — держим дольше, но не больше
VIDEO_MAX = 6.0
BROLL_BUDGET_SEC = 240  # на атмосферные видео — не дольше (долгие загрузки не должны держать ролик)
MAX_CHAPTERS_TOTAL = 12  # блоков вместе со вступлением и заключением
EDGE_MIN_SEC = 15.0  # вступление/заключение короче — присоединяются к соседнему блоку
MIN_BLOCK_RANGE = (45.0, 100.0)  # самый короткий блок героя: длина ролика / 12, но в этих пределах
JUDGE_THREADS = 18
BROLL_WORKERS = 3
STAR_WORKERS = 5  # героев ищем одновременно (поиск картинок через браузер: больше — Bing рвёт соединение)
_MODEL_TRIES = 3
# слова в причине отказа LLM, из-за которых фото звезды не берём (лицо — проверено по лицу)
_BAD_LOOK = ("watermark", "logo", "text", "caption", "subtitle", "collage", "split", "meme", "screenshot", "poster")

Part = tuple[int, tuple[int, ...]]  # (номер фразы, где блок начинается; id героев блока — пусто = общий кусок)
Span = tuple[float, float, tuple[int, ...]]  # (начало, конец, id героев)

_SYSTEM = """\
Ты редактор документальных роликов о знаменитостях. По тексту ролика с пронумерованными фразами
определяешь, кто его герои, и делишь ролик на блоки по героям. Отвечаешь только JSON."""


def min_block_sec(duration: float) -> float:
    lo, hi = MIN_BLOCK_RANGE
    return max(lo, min(hi, duration / 12))


def _prompt(sents: list[tuple[float, str]], *, duration: float, hint: str = "") -> str:
    lines = [f"[{i}] {_clock(t)} {s[:170]}" for i, (t, s) in enumerate(sents)]
    min_sec = round(min_block_sec(duration))
    return (
        "Ролик о знаменитостях (актёры, певцы, политики, спортсмены…). Надо понять, кто его главные герои, "
        "и разделить ролик на БЛОКИ — по одному на героя. К каждому блоку потом подбираются фото ТОЛЬКО его героя, "
        "а названия блоков станут главами в описании видео.\n\n"
        "1. Герои — люди, которым посвящены отдельные большие куски рассказа (не те, кого упомянули мимоходом). "
        "Ролик про одного человека — один герой. Если ролик не о людях как личностях (их жизнь, карьера, отношения, "
        "скандалы), а о событии, войне, стране, открытии, природе — people=false и пустой список stars.\n"
        "2. Блок — НЕПРЕРЫВНЫЙ кусок текста, где рассказ именно про этого героя. Всё, что в нём сказано про других людей, "
        "остаётся внутри блока (они — фон, их фото не нужны). Если кусок целиком про отношения двух-трёх людей "
        "(дуэт, вражда, дружба, общая работа) и разделить по героям нельзя — это один блок со списком их id.\n"
        "   • Блоки идут по порядку текста и вместе покрывают весь ролик без дыр: каждая фраза — в каком-то блоке.\n"
        "   • «Общих» кусков (stars: []) посреди ролика НЕТ. Пока рассказ идёт про героя (переходы между историями, "
        "рассуждения, призыв подписаться) — это его блок. Пустой stars — только вступление в самом начале (тема ролика, "
        "до рассказа о первом герое) и заключение в самом конце.\n"
        f"   • Блоков НЕ БОЛЬШЕ {MAX_CHAPTERS_TOTAL} вместе с вступлением и заключением. Каждый блок героя — не короче "
        f"~{min_sec} секунд ролика (смотри метки времени): короткие эпизоды присоединяй к соседнему блоку, не дроби.\n"
        "   • Герои не «прыгают»: если рассказ про А на пару фраз упомянул Б и вернулся к А — всё это один блок А.\n"
        "   • Героев много (например 8) — блоков примерно столько же, по одному на героя. Герой один — блоки по этапам "
        "его жизни или сюжета (не больше 8), в каждом stars: [его id].\n"
        "3. Для каждого героя:\n"
        "- name — имя и фамилия в именительном падеже на языке текста («Юрий Никулин»);\n"
        "- latin — имя латиницей, как его пишут в мире («Yuri Nikulin»);\n"
        "- search — как его чаще всего ищут в поиске картинок (обычно полное имя на языке его страны; "
        "если так зовут и других известных людей — как ищут именно этого: «Баба Ванга»);\n"
        "- kind — photo, если есть его настоящие фотографии (жил после ~1840), иначе pre_photo;\n"
        "- years — годы жизни или эпоха («1921–1997»);\n"
        "- who — кто это, коротко: профессия и страна («советский актёр театра и кино», «болгарская ясновидящая»). "
        "По этим словам ищутся его фото, чтобы не попал тёзка — бери то, что следует из текста ролика;\n"
        "- works — до 5 фильмов, ролей, песен, спектаклей или событий, которые в ролике связаны именно с ним "
        "(по ним ищутся кадры из фильмов и со съёмок; названия как их знают зрители: «Д'Артаньян и три мушкетёра»). "
        "Только то, что действительно названо в тексте;\n"
        "- doubt — пусто; заполняй, только если даже по тексту непонятно, какой из двух реальных людей с таким "
        "именем имеется в виду (перечисли обоих коротко).\n"
        "Тёзки и однофамильцы: ролик обычно однороден — если большинство героев советские артисты, то и этот герой "
        "советский артист той же эпохи, а не современный тёзка. Уточнения пользователя (если есть) — главнее всего.\n"
        + (f"{hint}\n" if hint else "")
        + '\nОтвет: {"people": true, "stars": [{"id": 1, "name": "...", "latin": "...", "search": "...", "kind": "photo", '
        '"years": "...", "who": "...", "works": ["..."], "doubt": ""}], '
        '"parts": [{"i": номер фразы, где блок начинается, "stars": [id героев блока; [] — вступление или заключение]}]}\n'
        "Блоки идут по порядку, первый — с фразы [0].\n\n"
        f"Фразы ролика (длительность {_clock(duration)}):\n" + "\n".join(lines)
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
    works: list[str] = field(default_factory=list)  # фильмы/роли/песни из рассказа — по ним ищутся кадры


@dataclass
class Cast:
    stars: list[Star]
    parts: list[Part]
    people: bool = True  # ролик о людях как личностях (для тем «история»/«другое» это условие включения)


def doubt_message(doubts: list[str], where: str) -> str:
    return ("Не уверен, о ком именно речь — есть тёзки или однофамильцы:\n" + "\n".join(f"• {d}" for d in doubts)
            + f"\nУточни {where}, например «Андрей Миронов (советский актёр)», и запусти снова.")


class CastDoubt(MediaError):
    """Непонятно, какой из двух реальных людей имеется в виду — спрашиваем пользователя."""


def plan_cast(llm: tuple[str, str, str], sents: list[tuple[float, str]], *, duration: float = 0.0, hint: str = "") -> Cast:
    """Герои и блоки ролика. Не вышло — MediaError."""
    from engine.slides import llm_models
    from engine.slides.llm_client import LLMClient

    duration = duration or (sents[-1][0] + 20.0 if sents else 0.0)
    key, base, model = llm
    notes = stars_notes()
    user = _prompt(sents, duration=duration, hint=hint) + (
        f"\n\nУТОЧНЕНИЯ ПОЛЬЗОВАТЕЛЯ О ГЕРОЯХ (главнее всего):\n{notes[:3000]}" if notes else "")
    last = ""
    for use in list(dict.fromkeys([m for m in [model, *llm_models.chain(base, key, "smart")] if m]))[:_MODEL_TRIES]:
        client = None
        try:
            client = LLMClient(key, use, base)
            raw = client.complete(label="stars_cast", system=_SYSTEM, user=user)
            cast = _parse_cast(raw, len(sents))
            if cast.stars and cast.parts:
                doubts = [f"{s.name}: {s.doubt}" for s in cast.stars if s.doubt]
                if doubts:
                    # спрашиваем, а не берём наугад: уточнение — в поле «Кто эти люди», потом «Продолжить»
                    raise CastDoubt(doubt_message(doubts, "в поле «Кто эти люди» (Стоковые слайды → «Подбор кадров»), "
                                                          "потом нажми «Продолжить»"))
                cast.parts = shape_parts(cast.parts, sents, duration)
                used = {i for _p, ids in cast.parts for i in ids}
                cast.stars = [s for s in cast.stars if s.id in used]
                log("Люди: " + "; ".join(f"{s.name} — {s.who or '?'} ({s.years})" for s in cast.stars))
                return cast
            if re.search(r'"stars"\s*:\s*\[\s*\]', raw or ""):
                log("Люди: в тексте нет героев, которым посвящены части ролика")
                return Cast([], [], people=False)  # ответ «героев нет» — тоже ответ, другую модель не спрашиваем
            last = "LLM не нашёл героев в тексте"
        except MediaError:
            raise
        except Exception as exc:
            last = str(exc)[:200]
            log(f"Люди: модель {use} не разметила текст ({last})")
        finally:
            if client is not None:
                client.close()
    raise MediaError(f"Не удалось разобрать, о ком ролик: {last}")


def _ids_of(raw, known: set[int]) -> tuple[int, ...]:
    out: list[int] = []
    for x in raw if isinstance(raw, list) else [raw]:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if i in known and i not in out:
            out.append(i)
    return tuple(out)


def _parse_cast(raw: str, n_sents: int) -> Cast:
    data = json_dict(raw)
    stars: list[Star] = []
    for s in data.get("stars") or []:
        if not isinstance(s, dict):
            continue
        try:
            sid = int(s.get("id"))
        except (TypeError, ValueError):
            continue
        name = str(s.get("name") or "").strip()
        if sid <= 0 or not name or any(x.id == sid for x in stars):
            continue
        kind = "pre_photo" if str(s.get("kind") or "").strip().lower() == "pre_photo" else "photo"
        works = [" ".join(str(w).split())[:80] for w in (s.get("works") or []) if str(w).strip()] if isinstance(s.get("works"), list) else []
        stars.append(Star(sid, name, str(s.get("latin") or name).strip(), str(s.get("search") or name).strip(),
                          kind, str(s.get("years") or "").strip(), str(s.get("who") or "").strip(),
                          str(s.get("doubt") or "").strip(), works[:5]))
    known = {s.id for s in stars}
    parts: list[Part] = []
    for p in data.get("parts") or []:
        if not isinstance(p, dict):
            continue
        try:
            i = int(p.get("i"))
        except (TypeError, ValueError):
            continue
        if not 0 <= i < n_sents:
            continue
        ids = _ids_of(p["stars"], known) if "stars" in p else _ids_of(p.get("star") or [], known)  # старый формат — один герой
        parts.append((i, ids))
    parts.sort(key=lambda x: x[0])
    if parts and parts[0][0] != 0:
        parts.insert(0, (0, ()))
    # одна и та же фраза — последний ответ; подряд один и тот же состав — один блок
    merged: list[Part] = []
    for i, ids in parts:
        if merged and merged[-1][0] == i:
            merged[-1] = (i, ids)
        elif not merged or merged[-1][1] != ids:
            merged.append((i, ids))
    used = {sid for _i, ids in merged for sid in ids}
    people = data.get("people")
    return Cast([s for s in stars if s.id in used], merged, people=bool(stars) if people is None else bool(people))


# ---------- блоки: сколько их и какой длины ----------


def part_times(parts: list[Part], sents: list[tuple[float, str]], end: float) -> list[Span]:
    """Блоки по фразам → отрезки ролика (начало, конец, id героев)."""
    out: list[Span] = []
    for k, (i, ids) in enumerate(parts):
        t0 = 0.0 if k == 0 else sents[i][0]
        t1 = sents[parts[k + 1][0]][0] if k + 1 < len(parts) else end
        if t1 - t0 > 0.3:
            out.append((round(t0, 3), round(t1, 3), ids))
    return out


def _union(first: list[int], second: list[int]) -> list[int]:
    return [*first, *[x for x in second if x not in first]]


def shape_parts(parts: list[Part], sents: list[tuple[float, str]], end: float, *,
                max_total: int = MAX_CHAPTERS_TOTAL) -> list[Part]:
    """Блоки как их разметила LLM → блоки, пригодные для глав и подбора кадров:
    общих кусков посреди ролика нет (отходят предыдущему герою), совсем короткие блоки склеены с соседними
    (если рассказ про пару героев — это один блок на двоих), всего блоков не больше max_total."""
    if not parts or not sents:
        return parts
    min_sec = min_block_sec(end)
    # [номер фразы начала, [id], начало, конец]
    blocks: list[list] = []
    for k, (i, ids) in enumerate(parts):
        t0 = 0.0 if k == 0 else sents[i][0]
        t1 = sents[parts[k + 1][0]][0] if k + 1 < len(parts) else end
        blocks.append([i, list(ids), t0, max(t0, t1)])

    def same_ids(a, b) -> bool:
        return a[1] == b[1]

    def merge_equal() -> None:
        k = 1
        while k < len(blocks):
            if same_ids(blocks[k - 1], blocks[k]):
                blocks[k - 1][3] = blocks[k][3]
                del blocks[k]
            else:
                k += 1

    def absorb(k: int, into: int) -> None:
        """Блок k целиком присоединяется к соседнему into (k±1): состав героев — по порядку, границы — внешние."""
        a, b = (into, k) if into < k else (k, into)
        blocks[a][1] = _union(blocks[a][1], blocks[b][1])
        blocks[a][3] = blocks[b][3]
        del blocks[b]

    merge_equal()
    # 1) общие куски посреди ролика — часть предыдущего героя
    k = 1
    while k < len(blocks) - 1:
        if not blocks[k][1]:
            blocks[k - 1][3] = blocks[k][3]
            del blocks[k]
        else:
            k += 1
    merge_equal()
    # 2) слишком короткие вступление и заключение — к соседнему блоку
    if len(blocks) > 1 and not blocks[0][1] and blocks[0][3] - blocks[0][2] < EDGE_MIN_SEC:
        blocks[1][0], blocks[1][2] = blocks[0][0], blocks[0][2]
        del blocks[0]
    if len(blocks) > 1 and not blocks[-1][1] and blocks[-1][3] - blocks[-1][2] < EDGE_MIN_SEC:
        blocks[-2][3] = blocks[-1][3]
        del blocks[-1]

    def hero_idx() -> list[int]:
        return [k for k, b in enumerate(blocks) if b[1]]

    def merge_shortest(only_short: bool) -> bool:
        idx = hero_idx()
        if len(idx) < 2:
            return False
        k = min(idx, key=lambda j: blocks[j][3] - blocks[j][2])
        if only_short and blocks[k][3] - blocks[k][2] >= min_sec:
            return False
        near = [j for j in (k - 1, k + 1) if j in idx]
        if not near:
            return False
        # сосед, с которым есть общий герой, иначе более короткий — длины выравниваются
        into = max(near, key=lambda j: (len(set(blocks[j][1]) & set(blocks[k][1])), -(blocks[j][3] - blocks[j][2])))
        absorb(k, into)
        return True

    while merge_shortest(True):
        merge_equal()
    while len(blocks) > max_total and merge_shortest(False):
        merge_equal()
    blocks[0][0] = 0
    return [(int(b[0]), tuple(b[1])) for b in blocks]


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


_RU_LOOKS = ("в молодости", "кадр из фильма", "на съёмках", "на сцене", "интервью", "с коллегами", "в зрелые годы", "портрет")
_EN_LOOKS = ("young", "film still", "on set", "on stage", "interview", "with colleagues", "older", "portrait")


def variant_queries(star: Star, name: str) -> list[str]:
    """Разные снимки одного героя: молодым и зрелым, на съёмках, на сцене, кадры из его фильмов.
    Фильмы и роли — из рассказа (works): «Боярский в фильме «Д'Артаньян…»» — это и есть «по смыслу»."""
    cyr = bool(re.search("[а-яё]", name, re.I))
    out: list[str] = []
    for w in star.works[:5]:
        out += [f"{name} {w}", f"{name} в фильме {w}" if cyr else f"{name} {w} film still"]
    out += [f"{name} {x}" for x in (_RU_LOOKS if cyr else _EN_LOOKS)]
    return list(dict.fromkeys(out))


_RU_TOPUP = ("", "фото", "актёр", "молодой", "в роли", "с женой", "с коллегами", "фотосессия", "архивное фото", "портрет",
             "на съёмочной площадке", "в театре", "старые фото", "редкие фото", "кадры из фильмов", "1960-е", "1970-е", "1980-е",
             "1990-е", "2000-е", "в зрелом возрасте")
_EN_TOPUP = ("", "photo", "actor", "young", "in role", "with wife", "with colleagues", "photoshoot", "archive photo", "portrait",
             "on set", "on stage", "rare photos", "film stills", "1960s", "1970s", "1980s", "1990s", "2000s", "older")


def topup_queries(star: Star, name: str) -> list[str]:
    """Добор фото, когда первого прохода не хватило: те же герои, другие слова — другие снимки."""
    cyr = bool(re.search("[а-яё]", name, re.I))
    out = [f"{name} {x}".strip() for x in (_RU_TOPUP if cyr else _EN_TOPUP)]
    for w in star.works[:5]:
        out += [f"{name} {w} фото" if cyr else f"{name} {w} photo"]
    return list(dict.fromkeys(out))


def _star_plan(plan: ThemePlan, star: Star, name: str, *, extra: bool = False) -> ThemePlan:
    """Запросы на этого героя: его имя, его эпоха (не всего ролика); с уточнением «кто это» — первыми,
    чтобы в выдаче не преобладал тёзка."""
    ctx = StoryContext(era=star.years or plan.context.era, place="", characters=[name],
                       subject="person", person_era=star.kind)
    hinted = anchor_queries(star, name)
    more = topup_queries(star, name) if extra else variant_queries(star, name)
    return ThemePlan(context=ctx, summary=plan.summary, video_queries=hinted[:1], image_queries=[*hinted, *more])


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
    """Фото и видео одного героя (только с его лицом) → (videos, images), пути от run_dir.
    Мало фото — добор: имя латиницей, потом те же поиски другими словами."""
    target = photo_targets(seconds, heroes)[0]
    videos, images = _collect_by_name(http, clients, plan, star, star.search, seconds=seconds, run_dir=run_dir,
                                      cancel=cancel, heroes=heroes)
    if len(images) < target // 2 and star.latin and star.latin.lower() != star.search.lower():
        log(f"Люди: {star.name} — по «{star.search}» фото мало ({len(images)}), добираю по «{star.latin}»")
        v2, i2 = _collect_by_name(http, clients, plan, star, star.latin, seconds=seconds, run_dir=run_dir,
                                  cancel=cancel, heroes=heroes, subdir=f"s{star.id:02d}b")
        videos, images = [*videos, *v2], [*images, *i2]
    log(f"Люди: {star.name} — фото {len(images)}, видео {len(videos)} (на {seconds:.0f}с)")
    return videos, images


def top_up_star(http, clients, plan: ThemePlan, star: Star, *, seconds: float, run_dir: Path, cancel: CancelToken | None,
                heroes: int, round_no: int, missing: int) -> list[dict]:
    """Добор фото героя другими запросами (без кавычек, десятилетия, «с коллегами»…) → новые фото."""
    _v, images = _collect_by_name(http, clients, plan, star, star.search, seconds=seconds, run_dir=run_dir, cancel=cancel,
                                  heroes=heroes, subdir=f"s{star.id:02d}t{round_no}", extra=True, image_need=max(12, missing))
    return images


def photo_targets(seconds: float, heroes: int) -> tuple[int, int]:
    """Сколько фото и видео искать на героя. Фото — на всё время героя с запасом (меньше повторов): один герой —
    до 100, много героев — до 60 на каждого. Видео: на ролик в целом должно быть 10–15."""
    cap = 100 if heroes <= 1 else (70 if heroes <= 3 else 60)
    image_need = max(14, min(cap, math.ceil(seconds / IMAGE_HOLD_TARGET)))
    if seconds < 90:
        video_need = 0
    elif heroes <= 1:
        video_need = min(12, max(2, round(seconds / 120)))
    else:
        video_need = min(4, 1 + int(seconds // 180))
    return image_need, video_need


def _collect_by_name(http, clients, plan: ThemePlan, star: Star, name: str, *, seconds: float, run_dir: Path,
                     cancel: CancelToken | None, heroes: int = 2, subdir: str = "", extra: bool = False,
                     image_need: int | None = None) -> tuple[list[dict], list[dict]]:
    from engine.slides.person_media import collect_person_pool

    pool = run_dir / "pool" / (subdir or f"s{star.id:02d}")
    trials, frames = pool / "_trials", pool / "_frames"
    shutil.rmtree(pool, ignore_errors=True)
    trials.mkdir(parents=True, exist_ok=True)
    frames.mkdir(parents=True, exist_ok=True)
    want, video_need = photo_targets(seconds, heroes)
    if extra:
        video_need = 0  # добор — только фото
    image_need = math.ceil((image_need or want) * 1.3)  # запас: коллажи и фото с надписями потом уберёт проверка LLM
    try:
        videos, images, _rv, _ri = collect_person_pool(
            http, clients, _star_plan(plan, star, name, extra=extra), name=name, anchors=anchor_queries(star, name),
            kind="photo" if star.kind == "photo" else "ancient",
            rng=random.Random(star.id), pool=pool, trials=trials, frames=frames, cancel=cancel,
            progress=lambda _p, m: log(f"{star.name}: {m}"), pct_lo=0.0, pct_hi=1.0,
            image_need=image_need, video_need=video_need,
            max_images_queries=26, max_cands=min(260, max(140, image_need * 3)), only_plan=extra,
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
# атмосферные стоковые видео без людей — между блоками, если у героев своих видео мало
_BROLL_QUERIES = ("film camera lens close up", "cinema projector light beam", "vintage film reel rolling",
                  "theater stage curtain opening", "old television static screen", "film clapperboard",
                  "stage spotlight empty theatre", "vintage microphone close up", "old movie theater seats",
                  "camera crane on film set", "camera flash photographers lights", "retro cassette tape recorder")


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
                        "id": cand.id, "query": q, "star": 0})
            break
    log(f"Люди: нейтральных кадров без людей — {len(out)}")
    return out


def broll_videos(http: httpx.Client, clients: dict, run_dir: Path, need: int, *, cancel: CancelToken | None = None,
                 queries: list[str] | None = None, stop: threading.Event | None = None) -> list[dict]:
    """Короткие стоковые видео без лиц — «дыхание» между блоками героев. Сначала по теме ролика (место, эпоха,
    предметы из рассказа — queries), потом про кино/сцену/съёмки вообще."""
    if need <= 0:
        return []
    from engine.slides import faces
    from engine.slides.media_pipeline import _download, _search_source
    from engine.slides.ffprobe_util import is_landscape, video_height_ok
    from engine.slides.source_mix import MIN_HEIGHT
    from engine.slides.theme_media import _extract_frames

    dest = run_dir / "pool" / "broll"
    frames = dest / "_f"
    dest.mkdir(parents=True, exist_ok=True)
    frames.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    seen: set[str] = set()
    lock = threading.Lock()
    started = time.monotonic()
    taken = [0]  # номера файлов

    def one_query(q: str) -> None:
        if cancel:
            cancel.check()
        with lock:
            if len(out) >= need or time.monotonic() - started > BROLL_BUDGET_SEC or (stop is not None and stop.is_set()):
                return
        for cand in _search_source(clients, "stocks", q, prefer_video=True, rng=random.Random(len(q)))[:4]:
            with lock:
                if (len(out) >= need or cand.kind != "video" or cand.id in seen or time.monotonic() - started > BROLL_BUDGET_SEC
                        or (stop is not None and stop.is_set())):
                    return
                seen.add(cand.id)
                n = taken[0]
                taken[0] += 1
            path = dest / f"b{n:02d}.mp4"
            if not _download(http, cand.download_url, path, retries=1, max_seconds=40.0, max_bytes=45_000_000, connect_timeout=6.0):
                path.unlink(missing_ok=True)
                continue
            dur = probe_duration(path)
            if dur < 2.0 or not is_landscape(path) or not video_height_ok(path, MIN_HEIGHT):
                path.unlink(missing_ok=True)
                continue
            shots = _extract_frames(path, frames, min(dur, 10.0))
            has_face = any(faces.face_embeddings(s) for s in shots)
            for s_ in shots:
                s_.unlink(missing_ok=True)
            if has_face:
                path.unlink(missing_ok=True)  # в кадре человек — это мог бы быть «чужой» герой
                continue
            with lock:
                if len(out) >= need:
                    path.unlink(missing_ok=True)
                    return
                out.append({"path": str(path.relative_to(run_dir)), "kind": "video", "provider": cand.provider,
                            "id": cand.id, "query": q, "duration": round(dur, 3), "star": 0})
            return

    try:
        # запросы параллельно: раньше по одному, и это было 2–3 минуты простоя
        with ThreadPoolExecutor(max_workers=BROLL_WORKERS) as ex:
            list(ex.map(one_query, [*(queries or []), *_BROLL_QUERIES]))
    finally:
        shutil.rmtree(frames, ignore_errors=True)
    if time.monotonic() - started > BROLL_BUDGET_SEC:
        log(f"Люди: атмосферные видео — вышло {BROLL_BUDGET_SEC // 60} мин, беру что есть ({len(out)})")
    out.sort(key=lambda it: it["path"])
    log(f"Люди: атмосферных видео без людей — {len(out)} (нужно {need})")
    return out


def _stop_broll(bg: ThreadPoolExecutor | None, stop: threading.Event, wait_sec: float = 20.0) -> None:
    """Остановить фоновый поиск атмосферных видео и дождаться конца текущей загрузки (не дольше wait_sec)."""
    stop.set()
    if bg is None:
        return
    bg.shutdown(wait=False, cancel_futures=True)
    t0 = time.monotonic()
    while any(t.is_alive() for t in getattr(bg, "_threads", ())) and time.monotonic() - t0 < wait_sec:
        time.sleep(0.2)


def _clean_by_judge(judge, star: Star, items: list[dict], run_dir: Path) -> list[dict]:
    """Фото с крупным текстом, коллажи, водяные знаки — вон (лицо уже проверено по лицу).
    Что нейросеть уже проверяла в прошлых роликах (тот же файл) — не спрашиваем снова: токены и время."""
    if judge is None or not judge.enabled:
        return items
    from engine.slides import judge_cache

    def is_bad(why: str) -> bool:
        return bool(why) and any(w in why.lower() for w in _BAD_LOOK)

    keys: dict[int, str] = {}
    known: dict[int, str] = {}
    for n, it in enumerate(items):
        try:
            keys[n] = judge_cache.fingerprint(run_dir / it["path"])
        except OSError:
            continue
        cached = judge_cache.get(keys[n])
        if cached is not None:
            known[n] = cached

    def one(n: int) -> tuple[int, str]:
        it = items[n]
        try:
            v = judge.pool().rate(run_dir / it["path"], star.search)
        except CancelledErrorBase:
            raise
        except Exception as exc:  # проверка не ответила — фото остаётся (лицо уже сверено), а не теряем героя целиком
            log(f"Люди: {star.name} — проверка кадра не ответила ({str(exc)[:100]}), оставляю")
            return n, ""
        why = (v.reject or "") if v is not None else ""
        if v is not None and n in keys:  # ответ получен — запоминаем (только «плохой вид», не зависящий от героя)
            judge_cache.put(keys[n], why if is_bad(why) else "")
        return n, why

    verdict = dict(known)
    asked = [n for n in range(len(items)) if n not in known]
    # потоков много: листы по 6 кадров уходят в LLM параллельно (при 8 потоках в полёте был один запрос — ~70 с на героя)
    with ThreadPoolExecutor(max_workers=JUDGE_THREADS) as ex:
        for n, why in ex.map(one, asked):
            verdict[n] = why
    judge_cache.flush()
    if known:
        log(f"Люди: {star.name} — из памяти {len(known)} из {len(items)} кадров, нейросети отправлено {len(asked)}")

    keep: list[dict] = []
    for n, it in enumerate(items):
        why = verdict.get(n, "")
        if is_bad(why):
            log(f"Люди: {star.name} — убрал фото ({why})")
            (run_dir / it["path"]).unlink(missing_ok=True)
            continue
        keep.append(it)
    return keep


def dedupe_media(media: dict[int, list[dict]], run_dir: Path, order: list[int]) -> int:
    """Одна и та же фотография не должна попасть двум героям (групповой снимок, дубли с разных сайтов):
    остаётся у того, кто раньше в ролике. → сколько убрано."""
    from engine.slides.look import DUP_BITS, hamming, image_hash

    kept: list[int] = []
    dropped = 0
    for sid in order:
        fresh: list[dict] = []
        for it in media.get(sid, []):
            if it.get("kind") == "video":
                fresh.append(it)
                continue
            h = image_hash(run_dir / it["path"])
            if h is not None and any(hamming(h, k) <= DUP_BITS for k in kept):
                (run_dir / it["path"]).unlink(missing_ok=True)
                dropped += 1
                continue
            if h is not None:
                kept.append(h)
            fresh.append(it)
        media[sid] = fresh
    return dropped


# ---------- ряд кадров ----------


def _slot(it: dict, t: float, dur: float, names: dict[int, str]) -> dict:
    return {
        "kind": it.get("kind", "image"), "path": it["path"], "start": round(t, 3), "end": round(t + dur, 3),
        "duration": round(dur, 3), "query": it.get("query", ""), "text": "", "provider": it.get("provider", ""),
        "id": it.get("id", ""), "source_title": it.get("source_title", ""),
        "source_author": it.get("source_author", ""), "fit": 10, "score": 1.0, "weak": False,
        "fallback": False, "phase": "stars", "star": names.get(it.get("star"), ""),
    }


def _round_robin(lists: list[list[dict]]) -> list[dict]:
    out: list[dict] = []
    for k in range(max((len(x) for x in lists), default=0)):
        out += [x[k] for x in lists if k < len(x)]
    return out


def _interleave(photos: list[dict], videos: list[dict]) -> list[dict]:
    """Видео — ровно по ряду фото, а не кучкой."""
    out = list(photos)
    if not videos:
        return out
    step = len(out) / (len(videos) + 1)
    for j, v in enumerate(videos, 1):
        out.insert(min(len(out), int(round(j * step)) + (j - 1)), v)
    return out


def _vid_len(it: dict) -> float:
    return min(VIDEO_MAX, float(it.get("duration") or VIDEO_MAX))


def _block_plan(photos: list[dict], videos: list[dict], seconds: float) -> tuple[list[dict], float]:
    """Какие кадры взять на seconds секунд и сколько держать фото. Видео — не больше трети времени,
    фото — столько, чтобы без повторов хватило при выдержке IMAGE_HOLD_TARGET…IMAGE_HOLD_MAX."""
    vids: list[dict] = []
    spent = 0.0
    for v in videos:
        if spent + _vid_len(v) > seconds * 0.34:
            break
        vids.append(v)
        spent += _vid_len(v)
    room = max(IMAGE_HOLD, seconds - spent)
    want = max(1, math.ceil(room / IMAGE_HOLD_TARGET))
    take = photos[:want]
    hold = room / max(1, len(take))
    return _interleave(take, vids), max(IMAGE_HOLD, min(IMAGE_HOLD_MAX, hold))


def _lay(seq: list[dict], t0: float, t1: float, hold: float, names: dict[int, str], rng: random.Random,
         recycle: list[dict]) -> list[dict]:
    """Кадры по порядку на отрезок [t0, t1]. Кадров не хватило — докладываем из recycle (повтор последним средством)."""
    slots: list[dict] = []
    t = t0
    queue = list(seq)
    extra = list(recycle)
    while t < t1 - 0.05:
        if not queue:
            if not extra:
                break
            queue = spread(extra, key=lambda it: it["path"], rng=rng, avoid_first=slots[-1]["path"] if slots else None)
        it = queue.pop(0)
        dur = _vid_len(it) if it.get("kind") == "video" else hold
        dur = min(dur, t1 - t)
        if t1 - (t + dur) < 1.5:
            dur = t1 - t  # хвостик — дотягиваем этот кадр до конца блока
        slots.append(_slot(it, t, dur, names))
        t += dur
    return slots


def build_slots(
    parts: list[Span],
    media: dict[int, list[dict]],
    stars: list[Star],
    *,
    rng: random.Random,
    neutral: list[dict] | None = None,
    broll: list[dict] | None = None,
) -> list[dict]:
    """Блоки героев — только их кадры, ни один не повторяется во всём ролике (если хватило материала);
    вступление и заключение — из того, что осталось. Герой без своих кадров — нейтральные кадры без людей.
    Между блоками — атмосферные видео без людей, пока они есть."""
    names = {s.id: s.name for s in stars}
    used: set[str] = set()
    slots_by_part: dict[int, list[dict]] = {}
    # атмосферные видео — поровну по времени между блоками (длинным больше), каждое — один раз
    alloc: dict[int, list[dict]] = {}
    pool_b = list(broll or [])
    longs = [k for k, (t0, t1, _i) in enumerate(parts) if t1 - t0 >= 40]
    total = sum(parts[k][1] - parts[k][0] for k in longs) or 1.0
    for k in sorted(longs, key=lambda j: -(parts[j][1] - parts[j][0])):
        take = max(1, round(len(broll or []) * (parts[k][1] - parts[k][0]) / total))
        alloc[k], pool_b = pool_b[:take], pool_b[take:]
    for k, extra_b in zip(longs, pool_b):  # остаток — по одному в блоки по очереди
        alloc.setdefault(k, []).append(extra_b)

    def unused(sid: int) -> tuple[list[dict], list[dict]]:
        items = [it for it in media.get(sid, []) if it["path"] not in used]
        ph = [it for it in items if it.get("kind") != "video"]
        vd = [it for it in items if it.get("kind") == "video"]
        rng.shuffle(ph)
        rng.shuffle(vd)
        return ph, vd

    def recycle_pool(ids: tuple[int, ...]) -> list[dict]:
        out = [it for sid in (ids or tuple(media)) for it in media.get(sid, [])]
        return out or list(neutral or [])

    # сначала блоки героев (в порядке ролика), потом вступление/заключение из остатка
    order = sorted(range(len(parts)), key=lambda k: (not parts[k][2], k))
    for k in order:
        t0, t1, ids = parts[k]
        seconds = t1 - t0
        if ids:
            lists = [unused(sid) for sid in ids]
            photos = _round_robin([p for p, _v in lists])
            videos = _round_robin([v for _p, v in lists])
            if not photos and not videos:
                log(f"Люди: у «{', '.join(names.get(i, str(i)) for i in ids)}» нет своих фото — ставлю нейтральные кадры без людей")
                photos = [it for it in (neutral or []) if it["path"] not in used] or list(neutral or [])
        else:
            lists = [unused(s.id) for s in stars if media.get(s.id)]
            photos = _round_robin([p for p, _v in lists])
            videos = _round_robin([v for _p, v in lists])
        seq, hold = _block_plan(photos, [*videos, *alloc.get(k, [])], seconds)
        lay = _lay(seq, t0, t1, hold, names, rng, recycle_pool(ids))
        for sl in lay:
            used.add(sl["path"])
        slots_by_part[k] = lay
    return [sl for k in range(len(parts)) for sl in slots_by_part.get(k, [])]


# ---------- стадия целиком ----------


def _cover_hint(run_dir: Path, req: MediaRequest) -> str:
    """Кто изображён на обложке оригинала — подсказка, о скольких и каких людях ролик. Нет ссылки/обложки — пусто."""
    from engine.slides.paths import SOURCE_NAME

    f = run_dir / SOURCE_NAME
    try:
        url = str(json.loads(f.read_text(encoding="utf-8")).get("url") or "").strip()
    except (OSError, ValueError, AttributeError):
        return ""
    if not url:
        return ""
    cache = run_dir / "cover_people.json"
    try:
        names = json.loads(cache.read_text(encoding="utf-8")).get("names")
        if isinstance(names, list):
            return _cover_line(names)
    except (OSError, ValueError, AttributeError):
        pass
    try:
        from engine.publish.cover import _ask_image, youtube_original

        title, image, _lang = youtube_original(url)
        if not image:
            return ""
        data = _ask_image(
            (req.llm_api_key, req.llm_base_url or "", req.llm_model or ""), "stars_cover",
            "Ты опознаёшь известных людей на обложке видео. Отвечаешь только JSON.",
            f"Название ролика: «{title}».\nКто из известных людей изображён на обложке? Называй только тех, "
            'кого уверенно узнаёшь по лицу или кто подписан на обложке. Ответ: {"names": ["Имя Фамилия", …]}',
            image)
        names = [" ".join(str(n).split())[:60] for n in (data.get("names") or []) if str(n).strip()][:15]
        cache.write_text(json.dumps({"names": names}, ensure_ascii=False), encoding="utf-8")
        return _cover_line(names)
    except Exception as exc:  # подсказка — не повод ронять подбор
        log(f"Люди: обложку оригинала не разобрал ({str(exc)[:120]})")
        return ""


def _cover_line(names: list) -> str:
    names = [str(n) for n in names if str(n).strip()]
    if not names:
        return ""
    return (f"ПОДСКАЗКА: на обложке оригинального ролика изображены {len(names)} чел.: {', '.join(names)}. "
            "Обычно это и есть главные герои ролика — сверь с текстом и сделай блоки по ним.")


def collect_people_timeline(
    req: MediaRequest,
    *,
    cancel: CancelToken | None = None,
    on_progress=None,
) -> MediaStageResult | None:
    """Ролик про конкретных людей (любой режим подбора) — на блок про каждого только его фото.
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
    subject = subject_of(ctx.subject, visual_style=ctx.visual_style, historical=ctx.historical)
    named = [c for c in ctx.characters if str(c).strip()]
    # «человек» — основная тема; но ролик о съёмках/скандалах знаменитостей LLM нередко метит «история» —
    # если в нём несколько людей, разметку по героям всё равно запрашиваем (решит people и число героев)
    if subject != "person" and not (subject in ("history", "other") and len(named) >= 2):
        return None  # ролик не про людей — разметка по героям не нужна (и LLM не спрашиваем)

    cast_file = run_dir / STARS_NAME
    cached = _load_cast(cast_file, len(sents))
    if cached is not None:
        cast = cached
    else:
        progress(0.06, "Ролик про людей: кто герои и о ком каждый блок текста…")
        hint = " ".join(x for x in (_cover_hint(run_dir, req), (
            "По теме ролика возможные герои: " + ", ".join(named[:12]) + "." if named else "")) if x)
        try:
            cast = plan_cast(llm, sents, duration=end, hint=hint)
        except CastDoubt:
            raise
        except MediaError as exc:
            if subject == "person":
                # ролик точно про людей: молча подобрать «по теме» — это и есть плохой ролик (чужие лица, мало фото).
                # Останавливаемся с понятной причиной; «Продолжить» повторит разметку.
                raise MediaError(f"Ролик про людей, но не удалось разметить героев ({exc}). Нажми «Продолжить» — "
                                 "разметка повторится; ролик по теме без героев не собираю.") from exc
            log(f"Люди: разметка по героям не вышла ({exc}) — подбираю по выбранному режиму")
            return None
        if subject != "person" and (not cast.people or sum(s.kind == "photo" for s in cast.stars) < 2):
            log("Люди: ролик не про личностей нескольких людей — подбираю по выбранному режиму")
            cast = Cast([], [], people=False)
        cast_file.write_text(json.dumps(
            {"v": CAST_VERSION, "stars": [asdict(s) for s in cast.stars], "parts": [[i, list(ids)] for i, ids in cast.parts],
             "n": len(sents), "people": cast.people}, ensure_ascii=False, indent=1), encoding="utf-8")
    stars, parts = cast.stars, cast.parts
    if not stars:
        return None
    times = part_times(parts, sents, end)
    seconds: dict[int, float] = {}
    general = 0.0
    for t0, t1, ids in times:
        if not ids:
            general += t1 - t0
        for sid in ids:
            seconds[sid] = seconds.get(sid, 0.0) + (t1 - t0) / len(ids)
    log("Люди: " + "; ".join(f"{s.name} — {seconds.get(s.id, 0):.0f}с" for s in stars)
        + f"; вступление/заключение — {general:.0f}с; блоков {len(times)}")
    progress(0.1, f"Люди: героев {len(stars)}, блоков {len(times)} — ищу фото каждого")

    from engine.slides.judge import make_judge

    judge = make_judge(req.llm_api_key, req.llm_model or "", req.llm_base_url or "", plan)
    if judge is not None and cancel:
        cancel.register(judge.client)
    clients = _build_clients(req.keys)
    http = httpx.Client(timeout=120.0, trust_env=False, follow_redirects=True)
    media: dict[int, list[dict]] = {}
    broll: list[dict] = []
    bg: ThreadPoolExecutor | None = None
    stop_broll = threading.Event()
    try:
        # атмосферные видео ищутся одновременно с фото героев (раньше — после, и это было 2–3 минуты простоя)
        want_videos = video_floor(end)
        hero_words = [w.lower() for s in stars for w in (s.name.split()[-1:] or [])]
        themed = [q for q in plan.video_queries if not any(w in q.lower() for w in hero_words)][:10]
        bg = ThreadPoolExecutor(max_workers=1)
        broll_fut = bg.submit(broll_videos, http, clients, run_dir, min(len(times) + 4, want_videos + 2),
                              cancel=cancel, queries=themed, stop=stop_broll)

        def work(s: Star) -> tuple[list[dict], list[dict]]:
            """Один герой целиком: поиск, проверка по лицу и проверка нейросетью (коллажи, надписи) — в своём потоке."""
            videos, images = collect_star(
                http, clients, plan, s, seconds=max(30.0, end if len(stars) == 1 else seconds.get(s.id, 0.0) + general / len(stars)),
                run_dir=run_dir, cancel=cancel, heroes=len(stars))
            return videos, _clean_by_judge(judge, s, images, run_dir)

        with ThreadPoolExecutor(max_workers=STAR_WORKERS) as ex:
            # ролик про одного героя — его фото и на вступление с заключением; про многих — доля общих кусков
            futs = {ex.submit(work, s): s for s in stars}
            done = 0
            for fut in as_completed(futs):
                if cancel:
                    cancel.check()
                star = futs[fut]
                try:
                    videos, images = fut.result()
                except CancelledErrorBase:
                    raise
                except Exception as exc:  # сбой одного героя не должен ронять сборку: добор и нейтральные кадры выручат
                    log(f"Люди: {star.name} — поиск фото не удался ({str(exc)[:160]}), попробую добрать")
                    videos, images = [], []
                media[star.id] = [*({**v, "kind": "video"} for v in videos), *({**i, "kind": "image"} for i in images)]
                done += 1
                progress(0.1 + 0.75 * done / len(stars), f"Люди: {star.name} — {len(images)} фото, {len(videos)} видео ({done}/{len(stars)})")

        if not any(media.values()):
            log("Люди: не нашёл фото ни одного героя — подбираю по выбранному режиму")
            _stop_broll(bg, stop_broll)
            shutil.rmtree(run_dir / "pool", ignore_errors=True)
            return None
        order = [sid for _t0, _t1, ids in times for sid in ids]
        dropped = dedupe_media(media, run_dir, list(dict.fromkeys(order or [s.id for s in stars])))
        if dropped:
            log(f"Люди: одинаковых снимков у разных героев убрано {dropped}")
        need_total = photo_floor(end)
        for round_no in (1, 2):
            have = sum(1 for v in media.values() for it in v if it.get("kind") != "video")
            if have >= need_total:
                break
            # героям с наименьшей долей от нужного добираем в первую очередь
            weak = sorted((s for s in stars if seconds.get(s.id)),
                          key=lambda s: len([1 for it in media.get(s.id, []) if it.get("kind") != "video"])
                          / max(1, photo_targets(max(30.0, seconds[s.id]), len(stars))[0]))[:6]
            progress(0.86, f"Фото мало ({have} из {need_total}) — добор, круг {round_no}: {', '.join(s.name for s in weak)}")
            with ThreadPoolExecutor(max_workers=STAR_WORKERS) as ex:
                futs2 = {}
                for s in weak:
                    cur = len([1 for it in media.get(s.id, []) if it.get("kind") != "video"])
                    sec = max(30.0, end if len(stars) == 1 else seconds[s.id])
                    miss = max(12, photo_targets(sec, len(stars))[0] - cur)
                    futs2[ex.submit(top_up_star, http, clients, plan, s, seconds=sec, run_dir=run_dir, cancel=cancel,
                                    heroes=len(stars), round_no=round_no, missing=miss)] = s
                for fut in as_completed(futs2):
                    if cancel:
                        cancel.check()
                    s = futs2[fut]
                    try:
                        more = _clean_by_judge(judge, s, fut.result(), run_dir)
                    except CancelledErrorBase:
                        raise
                    except Exception as exc:
                        log(f"Люди: добор {s.name} не вышел ({str(exc)[:120]})")
                        continue
                    media.setdefault(s.id, []).extend({**i, "kind": "image"} for i in more)
                    log(f"Люди: добор {s.name} — ещё {len(more)} фото")
            dropped = dedupe_media(media, run_dir, list(dict.fromkeys(order or [s.id for s in stars])))
        have_photos = sum(1 for v in media.values() for it in v if it.get("kind") != "video")
        if have_photos < MIN_PHOTOS_PER_VIDEO * min(1.0, end / 1200.0) // 2:
            # героев мало в сети (инженер, учёный): ролик из 4 фото на 40 минут — брак. Режим «люди» не годится
            log(f"Люди: фото героев всего {have_photos} (нужно не меньше {round(MIN_PHOTOS_PER_VIDEO * min(1.0, end / 1200.0) / 2)}) — "
                "ролик не про знаменитостей, подбираю по теме, как обычно")
            _stop_broll(bg, stop_broll)
            shutil.rmtree(run_dir / "pool", ignore_errors=True)
            cast_file.write_text(json.dumps(
                {"v": CAST_VERSION, "stars": [], "parts": [], "n": len(sents), "people": False}, ensure_ascii=False),
                encoding="utf-8")
            return None
        missing = [s.name for s in stars if not media.get(s.id) and seconds.get(s.id)]
        neutral = neutral_media(http, clients, run_dir) if missing else []
        if missing:
            log(f"Люди: не нашёл фото — {', '.join(missing)}")
        own_videos = sum(1 for v in media.values() for it in v if it.get("kind") == "video")
        progress(0.92, f"Видео с героями: {own_videos}; атмосферные кадры без людей — дожидаюсь")
        try:
            broll = broll_fut.result()
        except CancelledErrorBase:
            raise
        except Exception as exc:
            log(f"Люди: атмосферные видео не вышли ({str(exc)[:120]})")
            broll = []
        finally:
            _stop_broll(bg, stop_broll)
        keep_b = max(0, want_videos - own_videos + 2)  # своих видео хватает — лишние атмосферные не нужны
        for extra_b in broll[keep_b:]:
            (run_dir / extra_b["path"]).unlink(missing_ok=True)
        broll = broll[:keep_b]
    finally:
        _stop_broll(bg, stop_broll)  # ошибка или «Стоп» по пути — фоновый поиск видео не должен жить дальше
        http.close()
        for c in clients.values():
            try:
                c.close()
            except Exception:
                pass
        if judge is not None:
            judge.close()

    unique = (sum(1 for v in media.values() for it in v) + len(broll) + len(neutral))
    need_unique = round(MIN_PHOTOS_PER_VIDEO * min(1.0, end / 1200.0))
    if unique < need_unique:
        log(f"Люди: разных кадров всего {unique} (нужно не меньше {need_unique}) — "
            "подбираю по теме, как обычно")
        shutil.rmtree(run_dir / "pool", ignore_errors=True)
        cast_file.write_text(json.dumps(
            {"v": CAST_VERSION, "stars": [], "parts": [], "n": len(sents), "people": False}, ensure_ascii=False),
            encoding="utf-8")
        return None
    slots = build_slots(times, media, stars, rng=random.Random(req.seed), neutral=neutral, broll=broll)
    wrong = foreign_slots(slots, times, stars)
    if wrong:  # так быть не должно: лучше остановиться, чем показать Никулина на рассказе про Миронова
        raise MediaError(f"Внутренняя ошибка раскладки: {len(wrong)} кадров не того героя в блоке ({wrong[0]['star']})")
    report = _report(stars, media, slots, times, end)
    (run_dir / "stars_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    for w in report["warnings"]:
        log(f"Люди: ВНИМАНИЕ — {w}")

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
        "parts": [{"start": t0, "end": t1, "star": ids[0] if ids else 0, "stars": list(ids)} for t0, t1, ids in times],
    }
    timeline = run_dir / TIMELINE_NAME
    timeline.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    preview = run_dir / "timeline_preview.txt"
    names = {s.id: s.name for s in stars}
    preview.write_text(
        "\n".join(f"{t0:7.1f}-{t1:7.1f}  {', '.join(names.get(i, '?') for i in ids) or 'вступление/заключение'}"
                  for t0, t1, ids in times) + "\n", encoding="utf-8")
    chk = cp.load(run_dir)
    cp.mark_media_done(
        run_dir, language=chk.language, voice_id=chk.voice_id, text_chars=chk.text_chars, audio_bytes=chk.audio_bytes,
        desktop_audio=chk.desktop_audio, word_count=chk.word_count, duration=chk.duration,
        desktop_preview=chk.desktop_preview, chunk_count=chk.chunk_count, desktop_chunks=chk.desktop_chunks,
        selected_media=len(slots), desktop_timeline="",
    )
    progress(1.0, f"Ролик про людей: героев {len(stars)}, блоков {len(times)}, уникальных фото {report['photos_unique']}, "
                  f"видео {report['videos_unique']}")
    notes = [f"Люди: героев {len(stars)}, глав {len(times)}, уникальных фото {report['photos_unique']}, видео {report['videos_unique']}",
             *[f"Люди — мало материала: {w}" for w in report["warnings"]]]
    return MediaStageResult(run_dir=run_dir, timeline_file=timeline, preview_file=preview, desktop_preview=None,
                            selected=len(slots), skipped=0, weak=0, notes=notes)


# сколько уникальных кадров на ролик считаем достаточным (требование к качеству): 80–100 фото и 10–15 видео
MIN_PHOTOS_PER_VIDEO = 80
WANT_PHOTOS_PER_VIDEO = 100
MIN_VIDEOS_PER_VIDEO = 10
WANT_VIDEOS_PER_VIDEO = 12


def photo_floor(end: float) -> int:
    """Сколько фото на весь ролик набрать (до этого — добор). Короткий ролик столько не требует."""
    return max(30, round(WANT_PHOTOS_PER_VIDEO * min(1.0, end / 1200.0)))


def video_floor(end: float) -> int:
    return max(3, round(WANT_VIDEOS_PER_VIDEO * min(1.0, end / 900.0)))


def foreign_slots(slots: list[dict], times: list[Span], stars: list[Star]) -> list[dict]:
    """Кадры, чей герой не из этого блока (в блоках героев — только свои; вступление/заключение, атмосферные
    видео и нейтральные кадры — без героя — не в счёт)."""
    names = {s.id: s.name for s in stars}
    bad: list[dict] = []
    for t0, t1, ids in times:
        if not ids:
            continue
        own = {names.get(i) for i in ids}
        bad += [sl for sl in slots if t0 <= sl["start"] < t1 - 1e-6 and sl.get("star") and sl["star"] not in own]
    return bad


def _report(stars: list[Star], media: dict[int, list[dict]], slots: list[dict], times: list[Span], end: float) -> dict:
    """Итог подбора — сколько чего и где недобрали. Видно в логе и в stars_report.json."""
    shown_p = {s["path"] for s in slots if s["kind"] == "image"}
    shown_v = {s["path"] for s in slots if s["kind"] == "video"}
    own_v = {it["path"] for v in media.values() for it in v if it.get("kind") == "video"}
    per = {s.name: {"photos": sum(1 for it in media.get(s.id, []) if it.get("kind") != "video"),
                    "videos": sum(1 for it in media.get(s.id, []) if it.get("kind") == "video")} for s in stars}
    warnings: list[str] = []
    scale = min(1.0, end / 1200.0)  # короткий ролик столько не требует
    if len(shown_p) < MIN_PHOTOS_PER_VIDEO * scale:
        warnings.append(f"уникальных фото {len(shown_p)} (нужно не меньше {round(MIN_PHOTOS_PER_VIDEO * scale)})")
    if len(shown_v) < MIN_VIDEOS_PER_VIDEO * scale:
        warnings.append(f"видео {len(shown_v)} (нужно не меньше {round(MIN_VIDEOS_PER_VIDEO * scale)})")
    for name, c in per.items():
        if c["photos"] < 8:
            warnings.append(f"у «{name}» мало фото: {c['photos']}")
    repeats = len([s for s in slots]) - len(shown_p) - len(shown_v)
    return {"heroes": per, "blocks": len(times), "photos_unique": len(shown_p), "videos_unique": len(shown_v),
            "own_videos": len(shown_v & own_v), "repeated_slots": max(0, repeats), "slots": len(slots),
            "warnings": warnings}


def _load_cast(path: Path, n_sents: int) -> Cast | None:
    """Разметка уже есть (продолжение после сбоя) — LLM второй раз не спрашиваем.
    Записанное «героев нет» — тоже ответ: Cast([], [])."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("n") != n_sents or raw.get("v") != CAST_VERSION:
            return None
        stars = [Star(**s) for s in raw["stars"]]
        parts = [(int(i), tuple(int(x) for x in ids)) for i, ids in raw["parts"]]
        if not stars:
            return Cast([], [], people=False)
        return Cast(stars, parts, people=bool(raw.get("people", True))) if parts else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


# ---------- главы по героям ----------

_INTRO = {"ru": "Вступление", "uk": "Вступ", "en": "Introduction", "es": "Introducción", "de": "Einleitung",
          "fr": "Introduction", "pl": "Wstęp", "it": "Introduzione", "ro": "Introducere"}
_OUTRO = {"ru": "Заключение", "uk": "Висновок", "en": "Conclusion", "es": "Conclusión", "de": "Fazit",
          "fr": "Conclusion", "pl": "Zakończenie", "it": "Conclusione", "ro": "Încheiere"}
_AND = {"ru": "и", "uk": "і", "en": "and", "es": "y", "de": "und", "fr": "et", "pl": "i", "it": "e", "ro": "și"}
_OTHERS = {"ru": "и другие", "uk": "та інші", "en": "and others", "es": "y otros", "de": "und andere", "fr": "et autres",
           "pl": "i inni", "it": "e altri", "ro": "și alții"}


def _block_title(names: list[str], code: str) -> str:
    """Один герой — полное имя; двое — «А и Б»; трое — по фамилиям; больше — «А, Б и другие»."""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} {_AND.get(code, _AND['en'])} {names[1]}"
    sur = [n.split()[-1] for n in names]
    if len(names) == 3:
        return f"{sur[0]}, {sur[1]} {_AND.get(code, _AND['en'])} {sur[2]}"
    return f"{sur[0]}, {sur[1]} {_OTHERS.get(code, _OTHERS['en'])}"


def star_chapters(timeline: dict, language: str = "") -> list[tuple[float, str]]:
    """Главы по блокам: «4:10 — Юрий Никулин», «9:30 — Рыбников и Румянцева». Общие части: в начале — «Вступление»,
    в конце — «Заключение»."""
    code = (language or "ru").split("-")[0].lower()
    latin = code not in ("ru", "uk", "be", "")
    stars = {int(s["id"]): (s.get("latin") if latin else s.get("name")) or s.get("name") for s in timeline.get("stars") or []}
    parts = timeline.get("parts") or []
    out: list[tuple[float, str]] = []
    for k, p in enumerate(parts):
        ids = [int(x) for x in (p.get("stars") if p.get("stars") is not None else [p.get("star")] if p.get("star") else [])]
        t0 = float(p.get("start") or 0)
        names = [stars[i] for i in ids if stars.get(i)]
        if names:
            title = _block_title(names, code)
        elif not out:
            title = _INTRO.get(code, _INTRO["en"])
        elif k == len(parts) - 1:
            title = _OUTRO.get(code, _OUTRO["en"])
        else:
            continue  # общая вставка посередине — часть текущей главы
        if title and (not out or out[-1][1] != title):
            out.append((t0, title))
    return out
