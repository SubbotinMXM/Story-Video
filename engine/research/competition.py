"""Конкуренция подниши по языкам: на каком языке выгоднее делать тему канала-образца.

Человек даёт ссылку на канал (или ролик) и отмечает языки. Выгодный язык — где одновременно:
  1. маленькие каналы залетают на этой теме (хотя бы два): ролик подниши у канала до ~10 тыс. подписчиков набрал
     от 20 тыс. просмотров и в 10+ раз больше, чем у канала подписчиков;
  2. серьёзных конкурентов мало — как видно глазами на YouTube, ориентир 2–3 канала. Конкурент — не любой канал,
     у которого мелькнул ролик, а состоявшийся канал (больше ~11,5 тыс. подписчиков), который снимает эту поднишу:
     2+ ролика подниши за полгода с заметными просмотрами, или крупный канал (100 тыс.+) с хитом на эту тему —
     его глазами видно в поиске сразу. Разовый ролик среднего канала — не конкурент; маленькие каналы — тоже,
     их успехи — признак открытой ниши (пункт 1).

Как считается:
1. Подниша. Нейросеть читает образец (описание, лучшие и свежие ролики) и определяет УЗКУЮ тему — не
   «исторические личности», а «философы и их учения», плюс признаки её роликов и соседние темы, которые не в счёт.
   Человек может задать поднишу сам. Подниша и запросы запоминаются для канала: повторная проверка сравнима
   с прошлой и почти не тратит квоту (поиск YouTube в кэше).
2. Поиск: длинные ролики (от 5 мин) по запросам на каждом языке — так, как ищут носители: по релевантности
   за полгода и, как ищут ниши вручную, «за последний месяц по популярности» — что выстрелило недавно.
3. Отбор: ролики, где автор указал другой язык, — не в счёт; из остальных нейросеть по названиям оставляет
   только ролики подниши И на нужном языке. Сам образец — не конкурент.
4. По роликам подниши — залёты маленьких каналов, серьёзные конкуренты, спрос (сколько набирает сильный ролик).
5. Свежесть: что залетало год назад, не факт, что зайдёт сейчас. «Выгодно» — только если тема жива сейчас:
   за последние 2 месяца есть свежий залёт маленького канала или ролик подниши набрал 100 тыс.+ за месяц.
6. Вывод по языку: выгодно / можно попробовать / тяжело / мало данных → рейтинг языков.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from engine.core.cancel import CancelToken
from engine.core.jsonish import json_dict
from engine.core.logutil import log
from engine.publish.store import LOCK, read_json, write_json
from engine.research import niches

RESULTS_FILE = "competition.json"
SUBNICHE_FILE = "competition_subniche.json"  # подниша и запросы по каналу-образцу (повторные проверки сравнимы)
SUBNICHE_DAYS = 30  # столько подниша канала помнится
HISTORY = 20
# поиск по каждому языку: запросы × проходы × 100 единиц квоты (5 × 2 = 1000 на язык; повтор — из кэша)
QUERIES_PER_LANG = 5
PASSES = (("relevance", "medium"), ("relevance", "long"))  # за полгода
FRESH_PASSES = (("viewCount", "any"),)  # «за последний месяц, по популярности» — что выстрелило недавно
FRESH_DAYS = 30
QUOTA_PER_LANG = QUERIES_PER_LANG * (len(PASSES) + len(FRESH_PASSES)) * 100
CLASSIFY_BATCH = 60  # столько названий за один вопрос нейросети

# 1. залёт маленького канала
SMALL_SUBS = int(niches.MAX_SUBS * (1 + niches.SUBS_SLACK))  # до ~11,5 тыс. подписчиков — «маленький»
WIN_VIEWS = 20_000  # ролик подниши набрал хотя бы столько…
WIN_RATIO = 10.0  # …и в столько раз больше подписчиков канала
MIN_WINS = 2  # «выгодно» — хотя бы столько маленьких каналов залетели (один — может быть случайность)
# 2. серьёзный конкурент — состоявшийся канал (больше SMALL_SUBS), который снимает поднишу
RIVAL_VIDEOS = 2  # роликов подниши за полгода — хотя бы столько…
RIVAL_VIEWS = 10_000  # …и лучший из них набрал хотя бы столько
RIVAL_HIT = 100_000  # или крупный канал (BIG_SUBS+) с роликом подниши на столько просмотров
FEW_RIVALS = 3  # ориентир «мало конкурентов» (как видно глазами на YouTube)
SOME_RIVALS = 8
SMALL_SAMPLE = 15  # меньше роликов подниши — вывод по горстке роликов, стоит проверить глазами
BIG_SUBS = 100_000  # «крупный канал»
# 3. тема жива сейчас
FRESH_HIT = 100_000  # ролик подниши набрал столько за последний месяц — тема на подъёме
RECENT_WIN_DAYS = 60  # залёт маленького канала не старше стольких дней — «свежий»
# данных достаточно
MIN_VIDEOS = 5  # меньше роликов подниши — вывод ненадёжен
TOP_DEMAND = 20  # спрос — медиана стольких самых просматриваемых роликов подниши
MIN_DEMAND = 10_000  # сильные ролики подниши набирают меньше — зрителей почти нет
LIST_MAX = 10

VERDICTS = ("best", "ok", "hard", "empty")  # порядок в рейтинге

LANG_NAMES = {"ru": "русский", "en": "английский", "es": "испанский", "pt": "португальский", "de": "немецкий",
              "fr": "французский", "it": "итальянский", "pl": "польский", "uk": "украинский", "tr": "турецкий",
              "ro": "румынский", "nl": "нидерландский"}

_SUBNICHE = """Ты аналитик YouTube. Дан канал-образец: описание, ключевые слова и названия роликов (лучшие и свежие).
{task}
Ответ — JSON:
- subniche — подниша, коротко, на русском;
- broader — более общая ниша, в которую она входит (чтобы НЕ путать с ней), на русском;
- signs — 3–6 признаков ролика ИМЕННО этой подниши (о чём он, кто/что в центре), на русском;
- not_this — 3–6 соседних тем, похожих, но НЕ относящихся к поднише, на русском;
- language — язык аудитории образца, код (ru, en, es, pt, de, fr, it, pl, uk, tr, ro, nl);
- queries — объект: для КАЖДОГО языка из списка — {n} коротких (2–5 слов) поисковых запросов, которыми зритель
  ищет ролики именно этой подниши, СТРОГО на этом языке, так, как пишут носители (не дословный перевод);
  разные по смыслу (герои, предметы, типичные формулировки названий); узко по поднише, не по общей нише;
  не называй сам канал.
Формат: {"subniche": "…", "broader": "…", "signs": ["…"], "not_this": ["…"], "language": "ru",
"queries": {"en": ["…"], "de": ["…"]}}"""

_FIND = """Определи его УЗКУЮ поднишу — максимально конкретную тему, которую реально снимает этот канал, а не общую нишу.
Пример: канал про Сократа, Ницше и стоиков — подниша «философы и их учения», а НЕ «исторические личности»;
канал про танки и самолёты Второй мировой — «военная техника Второй мировой», а не «история» и не «война»."""

_GIVEN = """Поднишу ЗАДАЛ человек: «{subniche}». Возьми её как есть (subniche — дословно), остальное опиши под неё."""

_MORE_QUERIES = """Ты аналитик YouTube. Подниша: {subniche} (общая ниша «{broader}» — не она).
Признаки роликов подниши: {signs}.
Для КАЖДОГО языка из списка напиши {n} коротких (2–5 слов) поисковых запросов, которыми зритель ищет ролики
именно этой подниши, СТРОГО на этом языке, так, как пишут носители (не дословный перевод), разные по смыслу.
Формат: {"queries": {"en": ["…"], "de": ["…"]}}"""

_CLASSIFY = """Ты отбираешь ролики YouTube по узкой поднише.
Подниша: {subniche}
Признаки ролика подниши: {signs}
НЕ подниша (соседние темы — не брать): {not_this}; и вообще вся общая ниша «{broader}» сверх подниши.
Дан список названий роликов (номер: название). Нужна аудитория языка «{lang}». Верни номера ТОЛЬКО тех роликов,
которые явно про эту поднишу И название которых написано на языке «{lang}» (ролики на других языках — не брать,
даже если тема та же). Сомневаешься или ролик про общую нишу, а не про поднишу — не бери.
Формат: {"yes": [1, 4, 7]}"""


class CompetitionError(ValueError):
    """Сообщение показывается пользователю как есть."""


@dataclass
class LangResult:
    language: str
    region: str = ""
    queries: list[str] = field(default_factory=list)
    found: int = 0  # длинных роликов на этом языке нашлось по запросам
    on_topic: int = 0  # из них — именно подниша
    channels: int = 0  # всех каналов с роликами подниши (включая разовые ролики)
    winners: int = 0  # 1. маленьких каналов с залётом
    rivals: int = 0  # 2. серьёзных конкурентов (состоявшиеся каналы, которые снимают поднишу)
    small_channels: int = 0  # маленьких каналов с роликами подниши (до SMALL_SUBS)
    big_rivals: int = 0  # из них крупных (от BIG_SUBS)
    strong_views: int = 0  # медиана TOP_DEMAND самых просматриваемых роликов подниши — спрос
    median_views: int = 0  # медиана всех роликов подниши (мелкие каналы её занижают)
    big_share: float = 0.0  # доля просмотров подниши у крупных каналов
    fresh: int = 0  # роликов подниши за последние FRESH_DAYS
    fresh_hits: int = 0  # из них набрали FRESH_HIT+ — тема на подъёме сейчас
    recent_wins: int = 0  # маленьких каналов со свежим залётом (не старше RECENT_WIN_DAYS)
    minutes: float = 0.0  # типичная длина ролика подниши (медиана), мин
    verdict: str = ""  # best / ok / hard / empty
    reason: str = ""  # почему такой вывод — одной фразой
    notes: list[str] = field(default_factory=list)
    winner_list: list[dict] = field(default_factory=list)
    rival_list: list[dict] = field(default_factory=list)
    examples: list[dict] = field(default_factory=list)
    fresh_examples: list[dict] = field(default_factory=list)
    error: str = ""


# ---------- 1. подниша и запросы ----------


def _sample_text(channel: dict, videos: list[dict]) -> str:
    sn = channel.get("snippet") or {}
    keywords = ((channel.get("brandingSettings") or {}).get("channel") or {}).get("keywords") or ""
    top = sorted(videos, key=lambda v: -v["views"])
    fresh = sorted(videos, key=lambda v: -v["ts"])
    titles = list(dict.fromkeys(v["title"] for v in [*top[:10], *fresh[:10]] if v["title"]))[:18]
    return (f"Канал: {sn.get('title') or ''}\nОписание: {(sn.get('description') or '')[:700]}\n"
            f"Ключевые слова канала: {keywords[:300]}\nНазвания роликов:\n" + "\n".join(f"- {t}" for t in titles))


def _langs_line(languages: list[str]) -> str:
    return ", ".join(f"{code} ({LANG_NAMES.get(code, code)})" for code in languages)


def _queries(qmap: Any, languages: list[str]) -> dict[str, list[str]]:
    qmap = qmap if isinstance(qmap, dict) else {}
    return {code: list(dict.fromkeys(str(q).strip() for q in qmap.get(code) or [] if str(q).strip()))[:QUERIES_PER_LANG]
            for code in languages}


def _ask(system: str, user: str, llm, cancel) -> dict:
    from engine.publish.polls import ask

    try:
        return json_dict(ask(system, user, llm, cancel, "niche_competition"))
    except Exception as exc:
        if cancel is not None:
            cancel.check()
        raise CompetitionError(f"Нейросеть не ответила ({str(exc)[:160]}). Без неё узкую поднишу не определить — "
                               "проверь Codex или ключ LLM (Настройки).") from None


def describe_subniche(channel: dict, videos: list[dict], languages: list[str], llm, cancel, given: str = "") -> dict:
    """Подниша образца (или заданная человеком) + запросы на каждом языке — один вопрос нейросети."""
    task = _GIVEN.replace("{subniche}", given) if given else _FIND
    data = _ask(_SUBNICHE.replace("{task}", task).replace("{n}", str(QUERIES_PER_LANG)),
                f"{_sample_text(channel, videos)}\nЯзыки для запросов: {_langs_line(languages)}", llm, cancel)
    subniche = given or str(data.get("subniche") or "").strip()[:160]
    if not subniche:
        raise CompetitionError("Нейросеть не смогла определить тему канала — впиши поднишу сам или дай ссылку на "
                               "другой канал или ролик")
    return {"subniche": subniche, "broader": str(data.get("broader") or "").strip()[:160],
            "signs": [str(x).strip() for x in data.get("signs") or [] if str(x).strip()][:6],
            "not_this": [str(x).strip() for x in data.get("not_this") or [] if str(x).strip()][:6],
            "language": str(data.get("language") or "").strip().lower()[:2],
            "queries": _queries(data.get("queries"), languages), "given": bool(given)}


def more_queries(sub: dict, languages: list[str], llm, cancel) -> dict[str, list[str]]:
    """Запросы на новых языках для уже известной подниши (сама подниша не меняется)."""
    prompt = (_MORE_QUERIES.replace("{subniche}", sub["subniche"]).replace("{broader}", sub.get("broader") or "—")
              .replace("{signs}", "; ".join(sub.get("signs") or []) or "—").replace("{n}", str(QUERIES_PER_LANG)))
    return _queries(_ask(prompt, f"Языки: {_langs_line(languages)}", llm, cancel).get("queries"), languages)


def remembered(channel_id: str) -> dict | None:
    """Подниша канала из прошлой проверки (не старше SUBNICHE_DAYS)."""
    data = read_json(SUBNICHE_FILE, {})
    rec = data.get(channel_id) if isinstance(data, dict) else None
    if not isinstance(rec, dict) or not rec.get("subniche") or time.time() - float(rec.get("at") or 0) > SUBNICHE_DAYS * 86400:
        return None
    return rec


def remember(channel_id: str, sub: dict, at: float = 0.0) -> None:
    with LOCK:
        data = read_json(SUBNICHE_FILE, {})
        data = data if isinstance(data, dict) else {}
        data[channel_id] = {**sub, "at": at or time.time()}
        write_json(SUBNICHE_FILE, data)


def subniche_for(channel: dict, videos: list[dict], languages: list[str], llm, cancel, given: str = "",
                 fresh: bool = False) -> dict:
    """Подниша и запросы: из памяти по каналу (дозапрашиваем только новые языки) или заново.
    given — подниша от человека (другая, чем в памяти, — определяем заново); fresh — не брать из памяти."""
    old = None if fresh else remembered(channel["id"])
    if old and given and old.get("subniche") != given:
        old = None
    if old:
        keep = {k: v for k, v in old.items() if k not in ("at", "reused_at")}
        sub = {**keep, "queries": dict(old.get("queries") or {})}
        missing = [code for code in languages if not sub["queries"].get(code)]
        if missing:
            sub["queries"].update(more_queries(sub, missing, llm, cancel))
            remember(channel["id"], sub, float(old.get("at") or 0))  # срок памяти — от определения подниши
        sub["reused_at"] = float(old.get("at") or 0)
        return sub
    sub = describe_subniche(channel, videos, languages, llm, cancel, given)
    remember(channel["id"], sub)
    return sub


# ---------- 3. отбор роликов подниши ----------


def same_language(audio: str, code: str) -> bool:
    """Язык ролика (как указал автор) подходит: не указан — решит нейросеть по названию."""
    return not audio or audio.split("-")[0] == code


def classify(videos: list[dict], sub: dict, language: str, llm, cancel) -> tuple[set[str], int]:
    """(id роликов именно про поднишу и на нужном языке, сколько пачек названий не разобрано).
    Пачка не получилась — пробуем ещё раз; не вышло ни одной — ошибка языка (а не «роликов нет»)."""
    from engine.publish.polls import ask

    keep: set[str] = set()
    prompt = (_CLASSIFY.replace("{subniche}", sub["subniche"]).replace("{signs}", "; ".join(sub.get("signs") or []) or "—")
              .replace("{not_this}", "; ".join(sub.get("not_this") or []) or "—")
              .replace("{broader}", sub.get("broader") or "—").replace("{lang}", LANG_NAMES.get(language, language)))
    failed = 0
    batches = [videos[k:k + CLASSIFY_BATCH] for k in range(0, len(videos), CLASSIFY_BATCH)]
    for batch in batches:
        listing = "\n".join(f"{n}: {v['title']}" for n, v in enumerate(batch, 1))
        data = None
        for attempt in range(2):
            try:
                data = json_dict(ask(prompt, listing, llm, cancel, "niche_competition"))
                break
            except Exception as exc:
                if cancel is not None:
                    cancel.check()
                if attempt:
                    log(f"Конкуренция [{language}]: нейросеть не разобрала пачку названий ({str(exc)[:120]})")
        if data is None:
            failed += 1
            continue
        for n in data.get("yes") or []:
            try:
                i = int(n) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(batch):
                keep.add(batch[i]["id"])
    if batches and failed == len(batches):
        raise CompetitionError("нейросеть не смогла отобрать ролики подниши — запусти проверку ещё раз")
    return keep, failed


# ---------- 4–5. цифры и вывод ----------


def is_win(views: int, subs: int, hidden: bool = False) -> bool:
    """Залёт маленького канала: ролик подниши у канала до SMALL_SUBS набрал WIN_VIEWS+ и в WIN_RATIO+ раз больше подписчиков."""
    return not hidden and subs <= SMALL_SUBS and views >= WIN_VIEWS and views >= WIN_RATIO * max(1, subs)


def is_rival(videos: int, best: int, subs: int, hidden: bool = False) -> bool:
    """Серьёзный конкурент: состоявшийся канал (больше SMALL_SUBS; скрытые подписчики — считаем состоявшимся),
    который снимает поднишу (2+ ролика с заметными просмотрами), или крупный канал с хитом на эту тему.
    Разовый хит среднего канала — не конкурент (его успех ближе к залёту, чем к занятой нише)."""
    if not hidden and subs <= SMALL_SUBS:
        return False
    return (videos >= RIVAL_VIDEOS and best >= RIVAL_VIEWS) or (not hidden and subs >= BIG_SUBS and best >= RIVAL_HIT)


def measure(res: LangResult, videos: list[dict], channels: dict[str, niches.Found], *, now: float | None = None) -> None:
    """Цифры по роликам подниши одного языка и вывод."""
    now = now or time.time()
    res.on_topic = len(videos)
    if not videos:
        res.verdict, res.reason = verdict(res)
        return
    views = sorted((v["views"] for v in videos), reverse=True)
    res.median_views = int(statistics.median(views))
    res.strong_views = int(statistics.median(views[:TOP_DEMAND]))
    res.fresh = sum(1 for v in videos if v.get("ts") and now - v["ts"] <= FRESH_DAYS * 86400)
    res.fresh_hits = sum(1 for v in videos if v.get("ts") and now - v["ts"] <= FRESH_DAYS * 86400
                         and v["views"] >= FRESH_HIT)
    res.minutes = round(statistics.median(v["minutes"] for v in videos), 1)
    by_ch: dict[str, dict] = {}
    for v in videos:
        c = channels.get(v["channel"])
        row = by_ch.setdefault(v["channel"], {
            "id": v["channel"], "title": (c.title if c else "") or v["channel"], "subs": c.subs if c else 0,
            "hidden": bool(c is None or c.hidden), "views": 0, "videos": 0, "best": 0, "best_title": "",
            "best_id": "", "best_ts": 0.0, "best_minutes": 0.0, "win_ts": 0.0,
            "url": f"https://www.youtube.com/channel/{v['channel']}"})
        row["views"] += v["views"]
        row["videos"] += 1
        if v["views"] > row["best"]:
            row.update(best=v["views"], best_title=v["title"], best_id=v["id"], best_ts=v.get("ts") or 0.0,
                       best_minutes=v["minutes"])
        if is_win(v["views"], row["subs"], row["hidden"]):
            row["win_ts"] = max(row["win_ts"], v.get("ts") or 0.0)  # самый свежий залёт канала
    res.channels = len(by_ch)
    total = max(1, sum(views))
    res.big_share = round(sum(r["views"] for r in by_ch.values() if r["subs"] >= BIG_SUBS) / total, 3)
    winners = [r for r in by_ch.values() if is_win(r["best"], r["subs"], r["hidden"])]
    rivals = [r for r in by_ch.values() if is_rival(r["videos"], r["best"], r["subs"], r["hidden"])]
    res.winners, res.rivals = len(winners), len(rivals)
    res.small_channels = sum(1 for r in by_ch.values() if not r["hidden"] and r["subs"] <= SMALL_SUBS)
    res.recent_wins = sum(1 for r in winners if r["win_ts"] and now - r["win_ts"] <= RECENT_WIN_DAYS * 86400)
    res.big_rivals = sum(1 for r in rivals if r["subs"] >= BIG_SUBS)

    def card(r: dict) -> dict:
        return {"id": r["id"], "title": r["title"], "subs": r["subs"], "hidden": r["hidden"], "videos": r["videos"],
                "views": r["views"], "best": r["best"], "best_title": r["best_title"], "url": r["url"],
                "best_url": f"https://www.youtube.com/watch?v={r['best_id']}" if r["best_id"] else "",
                "ratio": 0 if r["hidden"] else round(r["best"] / max(1, r["subs"]), 1),
                "best_minutes": round(r["best_minutes"]), "best_days": int((now - r["best_ts"]) / 86400) if r["best_ts"] else 0}

    res.winner_list = [card(r) for r in sorted(winners, key=lambda r: -r["best"] / max(1, r["subs"]))[:LIST_MAX]]
    res.rival_list = [card(r) for r in sorted(rivals, key=lambda r: -r["views"])[:LIST_MAX]]
    def example(v: dict) -> dict:
        return {"title": v["title"], "views": v["views"], "channel": by_ch[v["channel"]]["title"],
                "subs": by_ch[v["channel"]]["subs"], "minutes": round(v["minutes"]),
                "days": int((now - v["ts"]) / 86400) if v.get("ts") else 0,
                "url": f"https://www.youtube.com/watch?v={v['id']}"}

    res.examples = [example(v) for v in sorted(videos, key=lambda v: -v["views"])[:5]]
    # что выстрелило недавно (как «за последний месяц, по популярности» вручную)
    res.fresh_examples = [example(v) for v in sorted(
        (v for v in videos if v.get("ts") and now - v["ts"] <= FRESH_DAYS * 86400), key=lambda v: -v["views"])[:5]]
    res.verdict, res.reason = verdict(res)
    if res.verdict != "empty" and res.on_topic < SMALL_SAMPLE:
        res.notes.append(f"Вывод по {res.on_topic} роликам подниши — выборка маленькая, перепроверь глазами "
                         "на YouTube (каналы и ролики — в «Подробнее»)")


def _k(n: int) -> str:
    return f"{n / 1000:.0f} тыс." if n >= 10_000 else f"{n:,}".replace(",", " ")


def verdict(r: LangResult) -> tuple[str, str]:
    """Вывод по двум критериям: залёты маленьких каналов и сколько серьёзных конкурентов."""
    if not r.on_topic:
        return "empty", "роликов этой подниши на языке не нашлось — тему почти не снимают или ищут другими словами"
    if r.on_topic < MIN_VIDEOS:
        return "empty", f"роликов подниши всего {r.on_topic} — данных для вывода мало"
    if r.strong_views < MIN_DEMAND and not r.winners:
        return "empty", f"даже сильные ролики подниши набирают около {_k(r.strong_views)} — зрителей почти нет"
    wins = f"маленькие каналы залетают ({r.winners})" if r.winners else "залётов маленьких каналов нет"
    rivals = f"конкурентов {r.rivals}" + (f", из них крупных {r.big_rivals}" if r.big_rivals else "")
    alive = r.recent_wins > 0 or r.fresh_hits > 0  # тема жива сейчас, а не только полгода назад
    if r.winners >= MIN_WINS and r.rivals <= FEW_RIVALS:
        if alive:
            return "best", f"{wins}, {rivals} — ниша свободна, и тема жива сейчас"
        return "ok", (f"{wins}, {rivals}, но за последние {RECENT_WIN_DAYS // 30} мес. свежих залётов нет "
                      "— интерес мог остыть")
    if r.winners == 1 and r.rivals <= FEW_RIVALS:
        return "ok", f"залёт маленького канала один — может быть случайностью, {rivals}"
    if (r.winners and r.rivals <= SOME_RIVALS) or (not r.winners and r.rivals <= FEW_RIVALS):
        return "ok", f"{wins}, {rivals}"
    return "hard", f"{wins}, {rivals} — тему плотно снимают"


def rank(results: list[LangResult]) -> list[LangResult]:
    """Выгоднее — выше: вывод, потом больше залётов, меньше конкурентов, больше спрос; ошибки — в конце."""
    order = {v: i for i, v in enumerate(VERDICTS)}
    return sorted(results, key=lambda r: (1 if r.error else 0, order.get(r.verdict, len(VERDICTS)),
                                          -min(r.winners, 10), r.rivals, -(r.recent_wins + r.fresh_hits),
                                          -r.strong_views))


# ---------- весь анализ ----------


def analyze(url: str, languages: list[str], keys: list[str], llm, *, subniche: str = "", fresh: bool = False,
            cancel: CancelToken | None = None, progress=None) -> dict[str, Any]:
    """Канал-образец + языки → рейтинг языков: где подниша образца выгоднее (сохраняется в историю).
    subniche — подниша, заданная человеком (иначе определит нейросеть или возьмётся из прошлой проверки канала);
    fresh — определить поднишу заново, не из памяти."""
    from engine.rewrite.channels import _Api

    langs = list(dict.fromkeys(code for code in languages if code in niches.LANG_REGION))
    if not langs:
        raise CompetitionError("Отметь хотя бы один язык")
    if not keys:
        raise CompetitionError("Нужен ключ YouTube Data API (Настройки → Ключи → YouTube)")
    given = " ".join((subniche or "").split())[:160]

    def step(p: float, m: str) -> None:
        if progress:
            progress(p, m)

    api = _Api(keys)
    try:
        step(0.02, "Читаю канал-образец…")
        try:
            channel = niches.resolve_channel(api, url)
        except niches.SimilarError as exc:
            raise CompetitionError(str(exc)) from None
        sample = niches.sample_videos(api, channel)
        if not sample:
            raise CompetitionError("У канала нет длинных роликов (от 5 минут) — тему определить не по чему")
        step(0.05, "Определяю поднишу и пишу запросы на каждом языке…")
        sub = subniche_for(channel, sample, langs, llm, cancel, given, fresh)
        log(f"Конкуренция: подниша «{sub['subniche']}» (общая ниша «{sub.get('broader')}»)"
            + (" — из прошлой проверки" if sub.get("reused_at") else "") + f", языки {', '.join(langs)}")
        results: list[LangResult] = []
        for n, code in enumerate(langs):
            if cancel:
                cancel.check()
            lo, hi = 0.08 + 0.9 * n / len(langs), 0.08 + 0.9 * (n + 1) / len(langs)
            name = LANG_NAMES.get(code, code).capitalize()
            res = LangResult(language=code, region=niches.LANG_REGION.get(code, ""),
                             queries=list(sub["queries"].get(code) or []))
            results.append(res)
            if not res.queries:
                res.error = "нейросеть не написала запросы на этом языке — запусти проверку ещё раз"
                continue
            try:
                f = niches.Filters.of({"language": code})
                chans, vids = niches.search(api, res.queries, f, cancel=cancel, passes=PASSES,
                                            progress=lambda p, m, lo=lo, hi=hi, name=name:
                                            step(lo + (hi - lo) * 0.45 * p, f"{name}: {m}"))
                # как ищут ниши вручную: «за последний месяц, по популярности» — что выстрелило недавно
                chans2, vids2 = niches.search(api, res.queries, f, cancel=cancel, passes=FRESH_PASSES,
                                              window_days=FRESH_DAYS,
                                              progress=lambda p, m, lo=lo, hi=hi, name=name:
                                              step(lo + (hi - lo) * (0.45 + 0.2 * p), f"{name}: свежее — {m}"))
                chans = {**chans2, **chans}
                vids = {**vids2, **vids}
                own = channel["id"]
                # поиск YouTube привязывает к языку нестрого: ролик, у которого автор указал другой язык, — не наш
                long_ = [v for v in vids.values() if v["minutes"] >= niches.MIN_MINUTES and v["channel"] != own
                         and same_language(v.get("audio") or "", code)]
                res.found = len(long_)
                step(lo + (hi - lo) * 0.7, f"{name}: отбираю ролики подниши ({len(long_)})…")
                keep, failed = classify(long_, sub, code, llm, cancel) if long_ else (set(), 0)
                measure(res, [v for v in long_ if v["id"] in keep], chans)
                if failed:
                    res.notes.append(f"Нейросеть не разобрала часть найденных роликов ({failed} из "
                                     f"{-(-len(long_) // CLASSIFY_BATCH)} пачек) — цифры могут быть занижены")
                log(f"Конкуренция [{code}]: найдено {res.found}, подниша {res.on_topic}, залётов {res.winners}, "
                    f"конкурентов {res.rivals}, сильный ролик {res.strong_views} → {res.verdict}")
            except Exception as exc:  # язык не получился — остальные считаем дальше
                if cancel is not None:
                    cancel.check()
                res.error = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
                log(f"Конкуренция [{code}]: ошибка — {res.error}")
    finally:
        api.close()
    sn, st = channel.get("snippet") or {}, channel.get("statistics") or {}
    result = {"id": f"{time.time():.6f}", "at": time.time(), "languages": langs,
              "source": {"id": channel["id"], "title": sn.get("title") or channel["id"],
                         "url": f"https://www.youtube.com/channel/{channel['id']}",
                         "subs": 0 if st.get("hiddenSubscriberCount") else niches._int(st.get("subscriberCount")),
                         "language": sub.get("language") or ""},
              "subniche": {"subniche": sub["subniche"], "broader": sub.get("broader") or "",
                           "signs": sub.get("signs") or [], "not_this": sub.get("not_this") or [],
                           "given": bool(sub.get("given")), "reused_at": float(sub.get("reused_at") or 0)},
              "results": [asdict(r) for r in rank(results)]}
    save(result)
    return result


VERDICT_LABELS = {"best": "выгодно", "ok": "можно попробовать", "hard": "тяжело", "empty": "мало данных"}


def summary(result: dict) -> tuple[str, list[str]]:
    """Итог для карточки задачи: (заголовок, строки по языкам)."""
    lines = []
    for n, r in enumerate(result["results"], 1):
        name = LANG_NAMES.get(r["language"], r["language"]).capitalize()
        lines.append(f"{n}. {name}: " + (f"ошибка — {r['error']}" if r["error"] else
                                         f"{VERDICT_LABELS.get(r['verdict'], '?')} — {r['reason']}"))
    ok = [r for r in result["results"] if not r["error"]]
    best = [r for r in ok if r["verdict"] == "best"]
    sub = result["subniche"]["subniche"]
    if best:
        head = f"«{sub}»: выгоднее всего — " + ", ".join(LANG_NAMES.get(r["language"], r["language"]) for r in best)
    elif (top := next((r for r in ok if r["verdict"] in ("ok", "hard")), None)) is not None:
        head = f"«{sub}»: явно выгодного языка нет, лучше других — {LANG_NAMES.get(top['language'], top['language'])}"
    else:
        head = f"«{sub}»: данных мало на всех языках"
    return head, lines


# ---------- история ----------


def save(result: dict) -> None:
    with LOCK:
        data = read_json(RESULTS_FILE, [])
        write_json(RESULTS_FILE, [result, *(data if isinstance(data, list) else [])][:HISTORY])


def runs() -> list[dict]:
    data = read_json(RESULTS_FILE, [])
    return [r for r in data if isinstance(r, dict) and r.get("id")] if isinstance(data, list) else []


def remove(run_id: str) -> bool:
    with LOCK:
        data = runs()
        left = [r for r in data if r["id"] != run_id]
        if len(left) == len(data):
            return False
        write_json(RESULTS_FILE, left)
    return True
