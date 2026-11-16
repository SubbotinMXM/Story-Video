"""Главы для описания YouTube: «ГЛАВЫ:\\n0:00 — …» — от 7 до 15 таймкодов по смыслу рассказа.

Время каждой фразы — по таймингам слов Whisper/перевода (если есть), иначе по доле
текста от длины озвучки. Названия глав и места переходов выбирает только LLM: без него глав нет.
YouTube требует: первая глава 0:00, глав не меньше 3, каждая не короче 10 с.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from engine.core.logutil import log

CHAPTERS_NAME = "главы.txt"
MIN_CHAPTERS, MAX_CHAPTERS = 7, 15
# между главами — не меньше (YouTube требует ≥10 с)
MIN_GAP = 20.0
HEADERS = {
    "ru": "ГЛАВЫ:",
    "en": "CHAPTERS:",
    "es": "CAPÍTULOS:",
    "de": "KAPITEL:",
    "fr": "CHAPITRES :",
    "pl": "ROZDZIAŁY:",
    "it": "CAPITOLI:",
    "ro": "CAPITOLE:",
    "uk": "РОЗДІЛИ:",
}
_SENT = re.compile(r"(?<=[.!?…])[\"»”')\]]*\s+|\n+")
_WORD = re.compile(r"\S+")

Sentence = tuple[float, str]


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(text or "") if s and s.strip()]


def timed_sentences(text: str, words: list[dict] | None, duration: float) -> list[Sentence]:
    """(начало фразы в секундах от начала рассказа, фраза)."""
    sents = split_sentences(text)
    if not sents and words:
        sents = split_sentences(" ".join(str(w.get("word") or "") for w in words))
    if not sents:
        return []
    counts = [len(_WORD.findall(s)) or 1 for s in sents]
    total_words = sum(counts)
    starts = [float(w["start"]) for w in words or [] if isinstance(w, dict) and isinstance(w.get("start"), (int, float))]
    out: list[Sentence] = []
    done = 0
    chars_total = sum(len(s) for s in sents) or 1
    chars_done = 0
    for sent, n in zip(sents, counts):
        if starts:
            # k-е слово текста ≈ то же по доле слово в таймингах (Whisper мог слышать чуть иначе)
            idx = min(len(starts) - 1, round(done * len(starts) / total_words))
            t = starts[idx]
        else:
            t = duration * chars_done / chars_total
        out.append((max(0.0, t), sent))
        done += n
        chars_done += len(sent)
    return out


def _positions(sents: list[Sentence]) -> list[float]:
    """Где начинается каждая фраза — доля текста по словам (0…1): одинакова у оригинала и перевода."""
    counts = [len(_WORD.findall(s)) or 1 for _t, s in sents]
    total = sum(counts) or 1
    out, done = [], 0
    for n in counts:
        out.append(done / total)
        done += n
    return out


# главы основного ролика по тексту: перевод ролика берёт их (переводит названия), а не размечает заново
def _base_dir() -> Path:
    from engine.core.config import DATA_DIR

    return Path(os.environ.get("STORY_VIDEO_CHAPTERS_DIR") or DATA_DIR / "chapters")


def _base_file(text: str) -> Path:
    return _base_dir() / (hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()[:16] + ".json")


def _save_base(text: str, sents: list[Sentence], chapters: list[Sentence], language: str) -> None:
    starts = {t: i for i, (t, _s) in reversed(list(enumerate(sents)))}
    pos = _positions(sents)
    entries = [{"pos": pos[starts[t]] if t in starts else 0.0, "title": title} for t, title in chapters]
    try:
        path = _base_file(text)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"language": language, "chapters": entries}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _load_base(text: str) -> dict | None:
    try:
        raw = json.loads(_base_file(text).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) and raw.get("chapters") else None
    except (OSError, ValueError):
        return None


_TRANSLATE = ("Переведи названия глав документального фильма на язык «{lang}». Коротко и живо, как оглавление "
              "(2–6 слов), без кавычек, нумерации и точки в конце. Порядок и количество — как во входе. "
              'Ответ только JSON: {{"titles": ["…"]}}')


def translate_chapters(base_text: str, sents: list[Sentence], duration: float, *, language: str,
                       llm: tuple[str, str, str] | None) -> list[Sentence]:
    """Перевод ролика: главы оригинала на тех же местах текста + переведённые названия (один короткий
    запрос вместо разметки всего текста заново). Не вышло — []: тогда размечаем как обычно."""
    base = _load_base(base_text)
    if not base or not sents or not (llm and llm[0]):
        return []
    entries = [e for e in base["chapters"] if isinstance(e, dict) and e.get("title")]
    from engine.slides.llm_client import LLMClient

    key, base_url, model = llm
    client = LLMClient(key, model, base_url)
    client.busy_wait = 120
    try:
        raw = client.complete(label="chapters_translate", system=_TRANSLATE.format(lang=language or "языке перевода"),
                              user=json.dumps([e["title"] for e in entries], ensure_ascii=False))
        titles = [_clean_title(str(t)) for t in (_parse_json(raw).get("titles") or [])]
    except Exception as exc:
        log(f"Главы: перевод названий не получился ({str(exc)[:120]}) — размечаю заново")
        return []
    finally:
        client.close()
    if len(titles) != len(entries) or not all(_title_ok(t) for t in titles):
        log("Главы: перевод названий не подошёл — размечаю заново")
        return []
    pos = _positions(sents)
    out = []
    for e, title in zip(entries, titles):
        i = min(range(len(pos)), key=lambda k: abs(pos[k] - float(e.get("pos") or 0)))
        out.append((sents[i][0], title))
    chapters = _tidy(out, duration, MAX_CHAPTERS)
    if len(chapters) >= 3:
        log(f"Главы: взяты из оригинала, названия переведены ({len(chapters)} шт.)")
    return chapters if len(chapters) >= 3 else []


def wanted_count(duration: float) -> tuple[int, int]:
    """Сколько глав просить: ролик короче — меньше (каждая глава хотя бы ~MIN_GAP)."""
    fit = int(duration // (MIN_GAP * 2))
    hi = max(3, min(MAX_CHAPTERS, fit))
    lo = max(3, min(MIN_CHAPTERS, hi))
    return lo, hi


def _parse_json(raw: str) -> dict:
    raw = (raw or "").strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


_SYSTEM = (
    "Ты редактор YouTube-канала с документальными фильмами. По тексту фильма с таймкодами фраз "
    "составляешь оглавление для описания видео — так, как его написал бы хороший редактор. Отвечаешь только JSON."
)

# название главы: законченная мысль в несколько слов
_TITLE_WORDS = (2, 8)
_ELLIPSIS = re.compile(r"(…|\.\.\.)\s*$")


# для названия главы хватает начала фразы: весь текст фильма в запросе — главная трата ключа LLM
SENTENCE_WORDS = 10


def _short(sentence: str) -> str:
    words = sentence.split()
    return " ".join(words[:SENTENCE_WORDS]) + (" …" if len(words) > SENTENCE_WORDS else "")


def _prompt(sents: list[Sentence], lo: int, hi: int, language: str, *, fix: str = "", head: bool = True) -> str:
    lines = [f"[{i}] {_clock(t)} {_short(s)}" for i, (t, s) in enumerate(sents)]
    lang = language or "языке текста"
    start = (
        "Первая глава всегда начинается с фразы [0].\n"
        if head
        else "Это не начало фильма, а его продолжение: глава с фразы [0] — только если там начинается новая тема.\n"
    )
    return (
        f"Разбей {'фильм' if head else 'эту часть фильма'} на главы: от {lo} до {hi} штук (чем длиннее — тем больше). "
        "Новая глава — там, где начинается новая часть истории: другая тема, место, время, герой, поворот. "
        "Главы идут по всему тексту, без совсем коротких (меньше минуты) и без огромных.\n"
        + start
        + f"Название главы — на языке текста ({lang}): короткая законченная мысль из 2–6 слов о сути этой части, "
        "живым человеческим языком, как оглавление документального фильма. Не цитата и не обрывок фразы из текста, "
        "без многоточий, без нумерации, кавычек, эмодзи и точки в конце.\n"
        "Хорошо: «Как государство следит за смартфонами», «Рынки, которые спасли страну», «Граница на замке».\n"
        "Плохо: «В этой стране можно иметь смартфон и…», «Такой вид не отменяет репрессий, но разрушает».\n"
        + (f"\nПрошлый ответ не подошёл: {fix}. Исправь.\n" if fix else "")
        + '\nОтвет: {"chapters": [{"i": номер фразы, где начинается глава, "title": "название"}]}\n\n'
        "Фразы фильма (от каждой — только начало):\n" + "\n".join(lines)
    )


def _title_ok(title: str) -> bool:
    n = len(_WORD.findall(title))
    return bool(title) and not _ELLIPSIS.search(title) and _TITLE_WORDS[0] <= n <= _TITLE_WORDS[1]


def _ask(client, sents: list[Sentence], lo: int, hi: int, language: str, fix: str = "", head: bool = True) -> list[tuple[int, str]]:
    data = _parse_json(client.complete(label="chapters", system=_SYSTEM, user=_prompt(sents, lo, hi, language, fix=fix, head=head)))
    picked: list[tuple[int, str]] = []
    for ch in data.get("chapters") or []:
        if not isinstance(ch, dict):
            continue
        try:
            i = int(ch.get("i"))
        except (TypeError, ValueError):
            continue
        title = _clean_title(str(ch.get("title") or ""))
        if 0 <= i < len(sents) and title:
            picked.append((i, title))
    return picked


def plan_chapters(
    sents: list[Sentence],
    duration: float,
    *,
    language: str = "",
    llm: tuple[str, str, str] | None = None,
) -> list[Sentence]:
    """Главы: [(начало от начала рассказа, название)]. Первая — с 0.

    Названия пишет только LLM. Отказ одной модели (перегрузка, ложное срабатывание контент-фильтра
    на тексте про войну/оружие) — пробуем другие модели сервиса. Не вышло — глав нет (лучше, чем
    обрывки фраз вместо названий).
    """
    if not sents or not (llm and llm[0]):
        return []
    from engine.slides import llm_models
    from engine.slides.llm_client import LLMClient, LLMQuotaError

    lo, hi = wanted_count(duration)
    key, base, model = llm
    # своя модель из настроек, потом самые дешёвые по прайсу
    models = list(dict.fromkeys([m for m in [model, *llm_models.chain(base, key, "light")] if m]))[:_MODEL_TRIES]
    t0 = time.monotonic()
    for use in models:
        if time.monotonic() - t0 > _DEADLINE_SEC:
            log(f"Главы: {_DEADLINE_SEC // 60} мин на главы вышли — ролик не задерживаю")
            break
        client = LLMClient(key, use, base)
        # ролик уже готов — долго ждать перегруженный сервис ради названий глав незачем
        client.busy_wait = 120
        try:
            picked = _ask_parts(client, sents, lo, hi, language)
            picked = [(i, t) for i, t in picked if _title_ok(t)]
            chapters = _tidy([(sents[i][0], title) for i, title in sorted(picked)], duration, hi)
            if len(chapters) >= 3:
                return chapters
            log(f"Главы: модель {use} дала мало годных глав ({len(chapters)})")
        except Exception as exc:  # главы — не повод ронять готовый ролик
            log(f"Главы: модель {use} не ответила ({str(exc)[:160]})")
            if isinstance(exc, LLMQuotaError):
                break  # кончилась квота ключа — другие модели того же ключа не помогут
        finally:
            client.close()
    log("Главы: LLM так и не написал оглавление — главы не делаю (обрывки фраз вместо названий хуже, чем ничего)")
    return []


# сколько разных моделей сервиса пробовать для глав
_MODEL_TRIES = 3
# всего времени на главы (ролик уже готов — не держим его из-за медленного сервиса)
_DEADLINE_SEC = 240
# фильтр сервиса отклонил текст (ложное срабатывание на документальном тексте про войну/оружие):
# делим на части до стольких уровней (2, 4, 8 частей); отклонённая часть остаётся без своей главы
_SPLIT_DEPTH = 3


def _blocked(exc: Exception) -> bool:
    text = str(exc).lower()
    return "content_policy" in text or "content policy" in text


def _ask_good(client, sents: list[Sentence], lo: int, hi: int, language: str, head: bool) -> list[tuple[int, str]]:
    """Главы одного куска; названия-обрывки — одна просьба переписать."""
    picked = _ask(client, sents, lo, hi, language, head=head)
    bad = [t for _i, t in picked if not _title_ok(t)]
    if picked and len(bad) > len(picked) // 4:
        log(f"Главы: {len(bad)} названий не годятся ({bad[:2]}) — прошу переписать")
        picked = _ask(client, sents, lo, hi, language, head=head,
                      fix="названия должны быть законченной мыслью из 2–6 слов, без многоточий, например: "
                      + "; ".join(f"«{t}» — плохо" for t in bad[:3]))
    return picked


def _ask_parts(client, sents: list[Sentence], lo: int, hi: int, language: str, *, head: bool = True,
               depth: int = 0) -> list[tuple[int, str]]:
    """Главы по всему тексту; отклонённый фильтром текст — пополам (половины параллельно)."""
    try:
        return _ask_good(client, sents, lo, hi, language, head)
    except Exception as exc:
        if not _blocked(exc) or depth >= _SPLIT_DEPTH or len(sents) < 8:
            if _blocked(exc):
                log(f"Главы: фильтр сервиса отклонил часть текста ({len(sents)} фраз) — она без своей главы")
                return []
            raise
    if depth == 0:
        log("Главы: фильтр сервиса отклонил текст целиком (ложное срабатывание) — размечаю по частям")
    mid = len(sents) // 2
    halves = [(sents[:mid], 0, head), (sents[mid:], mid, False)]

    def one(part: tuple[list[Sentence], int, bool]) -> list[tuple[int, str]]:
        chunk, shift, first = part
        got = _ask_parts(client, chunk, max(1, lo // 2), max(2, (hi + 1) // 2), language, head=first, depth=depth + 1)
        return [(i + shift, t) for i, t in got]

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as ex:
        a, b = ex.map(one, halves)
    return [*a, *b]


def _clean_title(title: str) -> str:
    title = re.sub(r"^\s*(\d+[.)]|[-—•])\s*", "", title)
    return title.strip().strip("«»\"'").rstrip(".").strip()


def _tidy(chapters: list[Sentence], duration: float, hi: int) -> list[Sentence]:
    out: list[Sentence] = []
    for t, title in sorted(chapters):
        if not out:
            out.append((0.0, title))
            continue
        if t - out[-1][0] < MIN_GAP or duration - t < MIN_GAP:
            continue
        out.append((t, title))
    return out[:hi]


def _clock(sec: float, *, hours: bool = False) -> str:
    s = int(max(0.0, sec))
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if hours or h else f"{m}:{s:02d}"


def format_chapters(chapters: list[Sentence], *, language: str = "", offset: float = 0.0, rate: float = 1.0, total: float = 0.0) -> str:
    """Текст для описания. offset — интро перед рассказом; rate — скорость копии ролика (0.99, 1.01…)."""
    code = (language or "").split("-")[0].lower()
    header = HEADERS.get(code, HEADERS["en"] if code and code != "ru" else HEADERS["ru"])
    hours = (total or 0) / max(rate, 0.01) >= 3600
    lines = [header]
    for k, (t, title) in enumerate(chapters):
        at = 0.0 if k == 0 else (t + offset) / max(rate, 0.01)
        lines.append(f"{_clock(at, hours=hours)} — {title}")
    return "\n".join(lines) + "\n"


def read_words(timings: Path | None) -> list[dict] | None:
    if timings is None or not timings.is_file():
        return None
    try:
        raw = json.loads(timings.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    words = raw.get("words") if isinstance(raw, dict) else None
    return words if isinstance(words, list) and words else None


def write_chapters(
    folder: Path,
    *,
    text: str,
    words: list[dict] | None,
    voice_duration: float,
    language: str = "",
    llm: tuple[str, str, str] | None = None,
    offset: float = 0.0,
    total: float = 0.0,
    copies: list[tuple[str, float]] | None = None,
    cache: Path | None = None,
    footer: str = "",
    preset: list[Sentence] | None = None,
    base_text: str = "",
) -> Path | None:
    """главы.txt в папку ролика (+ «99 процентов главы.txt» для копий: copies = [(имя, скорость)]).

    footer — дописать после глав (авторы нарезок YouTube): файл целиком вставляется в описание.
    base_text — текст оригинала, если это перевод ролика: главы берутся оттуда, названия переводятся.
    """
    chapters: list[Sentence] = list(preset or [])  # готовые главы (режим «Звёзды»: по героям)
    if not chapters and cache is not None and cache.is_file():
        try:
            chapters = [(float(t), str(s)) for t, s in json.loads(cache.read_text(encoding="utf-8"))]
        except (OSError, ValueError, TypeError):
            chapters = []
        if chapters and not base_text.strip() and _load_base(text) is None:
            # продолжение после сбоя: главы из кеша — запомнить и для будущих переводов
            _save_base(text, timed_sentences(text, words, voice_duration), chapters, language)
    if not chapters:
        if not language:
            from engine.core.langdetect import detect_language

            language = detect_language(text) or ""
        sents = timed_sentences(text, words, voice_duration)
        if base_text.strip() and " ".join(base_text.split()) != " ".join(text.split()):
            chapters = translate_chapters(base_text, sents, voice_duration, language=language, llm=llm)
        if not chapters:
            chapters = plan_chapters(sents, voice_duration, language=language, llm=llm)
            if chapters and not base_text.strip():
                _save_base(text, sents, chapters, language)
        if cache is not None and chapters:
            cache.write_text(json.dumps(chapters, ensure_ascii=False), encoding="utf-8")
    if len(chapters) < 3:
        # YouTube не примет меньше трёх глав (а ролик короче минуты — делить незачем)
        log("Главы: ролик слишком короткий или нет текста — без глав")
        chapters = []
        if not footer:
            return None
    if not language:
        from engine.core.langdetect import detect_language

        language = detect_language(text) or ""

    def body(rate: float) -> str:
        parts = [format_chapters(chapters, language=language, offset=offset, rate=rate, total=total)] if chapters else []
        return "\n".join([*parts, footer] if footer else parts)

    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / CHAPTERS_NAME
    dest.write_text(body(1.0), encoding="utf-8")
    for name, rate in copies or []:
        (folder / name).write_text(body(rate), encoding="utf-8")
    log(f"Главы: {len(chapters)} шт. → {dest.name}")
    return dest
