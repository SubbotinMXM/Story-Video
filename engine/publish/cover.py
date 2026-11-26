"""Обложки YouTube по образцу: оригинал → промпт → несколько вариантов → лучший → (переводы текста).

Как это работает:
1. Обложка-образец (оригинал конкурента или своя картинка) уходит нейросети с «глазами»: она пишет промпт
   для генератора — та же композиция, ракурс, эмоции, обстановка, приёмы кликбейта, но ДРУГИЕ люди и лица,
   немного другая расстановка и текст с тем же смыслом (на языке ролика), яркая цветокоррекция.
2. Генератор (RoyalTechno: Nano Banana Pro и др.) делает N вариантов; образец идёт ему как картинка-референс.
3. Нейросеть смотрит на образец и все варианты одним листом и выбирает лучший: цепляет, текст читается
   и без ошибок, нет уродливых рук и одинаковых лиц. Все плохие — ещё один круг (MAX_ROUNDS).
4. Перевод: та же картинка, меняется только текст (генератор правит готовую обложку).

Всё сохраняется в папку работы: образец, варианты, промпт, выбор (cover.json) — можно посмотреть и заменить.
Отправленные задачи генератора записываются сразу (cover_jobs.json): после сбоя «Продолжить» не платит повторно.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from engine.core.jsonish import json_object
from engine.core.cancel import CancelledError, CancelToken
from engine.core.logutil import log
from engine.publish.cover_prompts import render as _P

ProgressCb = Callable[[float, str], None]

DEFAULT_MODEL = "nano-banana-pro-2k"
DEFAULT_VARIANTS = 3
MAX_VARIANTS = 6
MAX_ROUNDS = 2  # все варианты плохие — ещё один круг генерации, дальше берём лучший из того, что есть
WAIT_SEC = 15 * 60
POLL_SEC = 5.0
JOBS_FILE = "cover_jobs.json"
INFO_FILE = "cover.json"
REF_MAX_SIDE = 1600  # образец для генератора и нейросети — не больше (data URI ≤ 5 МБ)


class CoverError(RuntimeError):
    pass


@dataclass
class CoverSettings:
    api_key: str  # RoyalTechno
    model: str = DEFAULT_MODEL
    variants: int = DEFAULT_VARIANTS
    llm: tuple[str, str, str] | None = None  # (ключ, адрес, модель)
    prompts: dict[str, str] = field(default_factory=dict)  # роль → свой текст промпта (пусто — стандартный)
    title_variants: int = 3  # сколько вариантов названия писать (выбирается лучший)

    def __post_init__(self) -> None:
        self.variants = max(1, min(MAX_VARIANTS, int(self.variants or DEFAULT_VARIANTS)))
        self.model = self.model or DEFAULT_MODEL


@dataclass
class CoverResult:
    path: Path  # выбранная обложка
    variants: list[Path]
    prompt: str
    text: list[str]  # надписи на обложке (строки)
    notes: list[str] = field(default_factory=list)
    translations: dict[str, Path] = field(default_factory=dict)  # язык → обложка
    model: str = ""  # id модели генератора (nano-banana-pro-2k)
    acceptable: bool = True  # редактор признал лучший вариант годным (не слабее образца)

    def as_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "variants": [str(v) for v in self.variants], "prompt": self.prompt,
                "text": self.text, "notes": self.notes, "model": self.model, "model_label": model_label(self.model),
                "translations": {k: str(v) for k, v in self.translations.items()}}


# ---------- промпты ----------
# Тексты промптов — в engine/publish/cover_prompts.py (стандартные) и в настройках (свои версии, раздел «Обложки»).



# ---------- нейросеть ----------


def _client(llm: tuple[str, str, str] | None):
    if not llm or not llm[0]:
        raise CoverError("Для обложек нужен ключ LLM (Настройки → LLM)")
    from engine.slides.llm_client import LLMClient

    return LLMClient(llm[0], llm[2], llm[1])


def _json(raw: str) -> dict:
    data = json_object(raw)
    if data is None:
        raise CoverError(f"нейросеть ответила не JSON-объектом: {(raw or '')[:200]}")
    return data


def _lines(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.splitlines()
    return [str(x).strip() for x in (value or []) if str(x).strip()]


def _ask_text(llm, label: str, system: str, user: str) -> dict:
    client = _client(llm)
    try:
        return _json(client.complete(label=label, system=system, user=user))
    finally:
        client.close()


def _ask_image(llm, label: str, system: str, user: str, image: bytes) -> dict:
    client = _client(llm)
    try:
        raw = client.complete_vision(label=label, system=system, user_text=user,
                                     image_b64=base64.b64encode(image).decode(), mime="image/jpeg")
        return _json(raw)
    finally:
        client.close()


def lang_label(code: str) -> str:
    names = {"ru": "русский", "en": "английский", "es": "испанский", "de": "немецкий", "fr": "французский",
             "pt": "португальский", "it": "итальянский", "pl": "польский", "uk": "украинский", "tr": "турецкий",
             "ro": "румынский", "nl": "нидерландский", "cs": "чешский", "ja": "японский", "ko": "корейский",
             "zh": "китайский", "ar": "арабский", "hi": "хинди", "id": "индонезийский", "vi": "вьетнамский"}
    c = (code or "").split("-")[0].lower()
    return f"{names[c]} ({c})" if c in names else (c or "тот же язык, что в надписях образца")


def analyze(llm, reference: bytes, language: str, *, prompts: dict | None = None) -> tuple[str, list[str], list[str]]:
    """Образец → (промпт, надписи нашей обложки, надписи образца)."""
    a = analyze_full(llm, reference, language, prompts=prompts)
    return a["prompt"], a["text"], a["original_text"]


def _people(value: Any) -> list[dict[str, str]]:
    """Известные люди с образца: [{"name", "where"}] — без пустых и повторов, не больше MAX_PEOPLE."""
    from engine.publish.cover_people import MAX_PEOPLE

    out: list[dict[str, str]] = []
    for it in value if isinstance(value, list) else []:
        name = " ".join(str((it or {}).get("name") or "").split())[:80] if isinstance(it, dict) else ""
        if name and name.lower() not in {p["name"].lower() for p in out}:
            out.append({"name": name, "where": " ".join(str(it.get("where") or "").split())[:80]})
    return out[:MAX_PEOPLE]


def analyze_full(llm, reference: bytes, language: str, *, prompts: dict | None = None,
                 context: str = "") -> dict[str, Any]:
    """Образец → {prompt, text, original_text, people}. context — название/начало текста ролика: по нему
    нейросеть узнаёт, кто из известных людей на обложке."""
    user = "Вот обложка-образец. Составь промпт по правилам: перерисуй именно этот образец."
    if context.strip():
        user += ("\n\nО чём ролик — ТОЛЬКО чтобы понять, кто из известных людей на обложке; обложку строй по образцу, "
                 "а не по тексту ролика:\n" + context.strip()[:1500])
    data = _ask_image(llm, "cover_analyze", _P(prompts, "analyze", язык=lang_label(language)), user, reference)
    prompt = str(data.get("prompt") or "").strip()
    if len(prompt) < 80:
        raise CoverError("нейросеть не написала промпт для обложки")
    style = " ".join(str(data.get("style") or "").split())[:200]
    if style:  # стиль образца — первой строкой: генератор держится его, что бы ни было ниже
        log(f"Обложка: стиль образца — {style}")
        prompt = f"VISUAL STYLE (match the reference exactly, overrides other style words): {style}.\n{prompt}"
    return {"prompt": prompt, "text": _lines(data.get("text")), "original_text": _lines(data.get("original_text")),
            "people": _people(data.get("people"))}


def people_block(people: list[dict], photos: dict[str, Path | None]) -> tuple[str, list[Path]]:
    """Известные люди → (абзац для генератора, их фото по порядку). Картинка 1 — образец, дальше — фото людей."""
    from engine.publish.cover_prompts import REAL_PEOPLE_EN

    if not people:
        return "", []
    lines, files = [], []
    for p in people:
        where = f" ({p['where']})" if p.get("where") else ""
        photo = photos.get(p["name"])
        if photo:
            files.append(photo)
            lines.append(f"- {p['name']}{where}: use the face from attached image {len(files) + 1}")
        else:
            lines.append(f"- {p['name']}{where}: keep this same person from the reference, in a new pose and angle")
    return REAL_PEOPLE_EN + "\n" + "\n".join(lines), files


def concept(llm, story: str, language: str, *, prompts: dict | None = None) -> tuple[str, list[str]]:
    """Без образца: обложку придумывает нейросеть по тексту ролика."""
    sample = " ".join(story.split()[:900])
    data = _ask_text(llm, "cover_concept", _P(prompts, "concept", язык=lang_label(language)), f"Текст ролика:\n{sample}")
    prompt = str(data.get("prompt") or "").strip()
    if len(prompt) < 60:
        raise CoverError("нейросеть не придумала обложку")
    return prompt, _lines(data.get("text"))


DEFAULT_TITLE_VARIANTS = 3


def rewrite_title(llm, title: str, language: str, *, story: str = "", variants: int = DEFAULT_TITLE_VARIANTS,
                  prompts: dict | None = None, notes: list[str] | None = None) -> str:
    """Название оригинала → наше: несколько вариантов (похоже на оригинал, кликбейт, SEO; ключи — из текста
    ролика), нейросеть выбирает лучший. notes — сюда строки для итогов (варианты и почему выбран)."""
    # только название донора: текст рерайта перетягивает тему на детали («мотоциклы»), а формула донора
    # уже сработала — её и обыгрываем (наш ролик — переписанный донор, суть та же)
    user = f"Название ролика-донора (оно уже сработало — переделываем его): {title}"
    return _make_title(llm, "title", user, language, variants=variants, original=title, story="",
                       prompts=prompts, notes=notes) or title


def title_from_story(llm, story: str, language: str, *, variants: int = DEFAULT_TITLE_VARIANTS,
                     prompts: dict | None = None, notes: list[str] | None = None) -> str:
    out = _make_title(llm, "title_story", "Текст ролика:\n" + " ".join(story.split()[:900]), language,
                      variants=variants, original="", story=story, prompts=prompts, notes=notes)
    if not out:
        raise CoverError("нейросеть не придумала название")
    return out


def _make_title(llm, role: str, user: str, language: str, *, variants: int, original: str, story: str,
                prompts: dict | None, notes: list[str] | None) -> str:
    n = max(1, min(8, int(variants or 1)))
    data = _ask_text(llm, "cover_title", _P(prompts, role, язык=lang_label(language), вариантов=str(n)), user)
    found = _title_variants(data)
    if not found:
        return ""
    best, why = 0, ""
    if len(found) > 1:
        best, why = _pick_title(llm, found, original=original, story=story, language=language, prompts=prompts)
    title, keys = found[best]
    log(f"Название: «{title}» ({len(title)} симв.)" + (f", ключевые слова: {', '.join(keys)}" if keys else "")
        + (f" — лучший из {len(found)}: {why}" if len(found) > 1 else ""))
    if notes is not None and len(found) > 1:
        notes.append(f"Название — лучшее из {len(found)}" + (f" ({why})" if why else "") + ". Другие варианты: "
                     + " | ".join(t for k, (t, _) in enumerate(found) if k != best))
    return title


def _title_variants(data: dict) -> list[tuple[str, list[str]]]:
    """Ответ нейросети → [(название, ключевые слова)] без пустых и повторов (понимает и старый ответ {"title"})."""
    raw = data.get("titles")
    items = raw if isinstance(raw, list) else [data]
    out: list[tuple[str, list[str]]] = []
    for it in items:
        t = _clean_title(it.get("title") if isinstance(it, dict) else it)
        keys = [str(k).strip() for k in (it.get("keywords") if isinstance(it, dict) else None) or [] if str(k).strip()][:4]
        if t and all(t.lower() != x.lower() for x, _ in out):
            out.append((t, keys))
    return out


def _pick_title(llm, found: list[tuple[str, list[str]]], *, original: str, story: str, language: str,
                prompts: dict | None) -> tuple[int, str]:
    """Нейросеть выбирает лучший вариант названия. Не вышло — первый."""
    lines = [f"{k}. {t} ({len(t)} симв.)" for k, (t, _) in enumerate(found, 1)]
    user = (f"Название ролика-донора: {original}\n\n" if original else "") + "Варианты:\n" + "\n".join(lines)
    if story.strip():
        user += "\n\nНачало текста ролика:\n" + " ".join(story.split()[:400])
    try:
        data = _ask_text(llm, "cover_title_pick", _P(prompts, "title_pick", язык=lang_label(language)), user)
        best = int(data.get("best")) - 1
    except Exception as exc:
        log(f"Название: выбрать лучший вариант не вышло ({exc}) — беру первый")
        return 0, ""
    return (best if 0 <= best < len(found) else 0), str(data.get("reason") or "")[:200]


def _clean_title(value: Any) -> str:
    t = re.sub(r"\s+", " ", str(value or "")).strip().strip('"«»').strip()
    return t[:100].rstrip()


def translate_text(llm, lines: list[str], language: str, *, prompts: dict | None = None) -> list[str]:
    data = _ask_text(llm, "cover_translate", "Ты переводчик рекламных надписей. Отвечай только JSON.",
                     _P(prompts, "translate", язык=lang_label(language), надписи=json.dumps(lines, ensure_ascii=False)))
    out = _lines(data.get("text"))
    if not out:
        raise CoverError("нейросеть не перевела надписи обложки")
    return out


# ---------- картинки ----------


def to_jpeg(data: bytes, max_side: int = REF_MAX_SIDE) -> bytes:
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    img = img.convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


YT_MAX_BYTES = 2 * 1024 * 1024  # YouTube принимает обложку до 2 МБ


def save_for_youtube(src: Path, dest: Path) -> Path:
    """Обложка для YouTube: JPEG 1920×1080 (16:9, обрезка по центру), не больше 2 МБ."""
    from PIL import Image, ImageOps

    img = ImageOps.fit(Image.open(src).convert("RGB"), (1920, 1080), Image.LANCZOS)
    for quality in (92, 88, 82, 75, 68, 60):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        if buf.tell() <= YT_MAX_BYTES - 50_000:
            break
    dest.write_bytes(buf.getvalue())
    return dest


def data_uri(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()


def contact_sheet(original: bytes | None, variants: list[Path]) -> tuple[bytes, str]:
    """Образец и варианты на одном листе с номерами (нейросеть с «глазами» видит одну картинку за раз)."""
    from PIL import Image, ImageDraw, ImageFont

    tiles: list[tuple[str, Image.Image]] = []
    if original:
        tiles.append(("ОБРАЗЕЦ", Image.open(io.BytesIO(original)).convert("RGB")))
    for n, v in enumerate(variants, 1):
        tiles.append((f"ВАРИАНТ {n}", Image.open(v).convert("RGB")))
    w, h = 800, 450
    cols = 2
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * w + (cols + 1) * 10, rows * (h + 50) + 10), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 34)
    except OSError:
        font = ImageFont.load_default()
    for i, (label, img) in enumerate(tiles):
        x = 10 + (i % cols) * (w + 10)
        y = 10 + (i // cols) * (h + 50)
        draw.text((x, y), label, fill="red" if label == "ОБРАЗЕЦ" else "black", font=font)
        sheet.paste(img.resize((w, h)), (x, y + 42))
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=85)
    layout = ("слева сверху — ОБРАЗЕЦ (оригинал), дальше ВАРИАНТ 1, ВАРИАНТ 2… (номера подписаны над картинками)"
              if original else "ВАРИАНТ 1, ВАРИАНТ 2… (номера подписаны над картинками)")
    return buf.getvalue(), layout


_READ_EXACT = """Ты — корректор надписей на картинке. Ожидаемый текст (эталон), по строкам:
{expected}
Сравни надпись на картинке с эталоном ПО КАЖДОЙ БУКВЕ, по начертанию, как корректор под лупой. Не читай «по
смыслу» и не додумывай: мозг сам исправляет ошибки, а тебе нужно найти именно их. Ошибка — любая буква,
нарисованная не так, как в эталоне: искажённая, лишняя, пропущенная, из другого алфавита (например «Н» нарисована
как «Њ», «И» как «Ӣ», латинская буква внутри русского слова), склеенная, с лишним штрихом или хвостом; слово
написано иначе; лишние слова или символы.
Ответ — только JSON: {{"match": true/false (надпись совпадает с эталоном полностью), "text": ["как нарисовано на
самом деле", …], "defects": "какие буквы и в каком слове не так — или пустая строка"}}"""


def _norm_text(lines: list[str]) -> str:
    import unicodedata

    t = unicodedata.normalize("NFC", " ".join(lines)).upper().replace("Ё", "Е")
    return re.sub(r"[^\w]+", " ", t).strip()


_CYR_OK = set("АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюяІіЇїЄєҐґ")


def _bad_glyphs(text: str) -> bool:
    """Испорченные буквы: в кириллическом слове — буква не из обычного алфавита (сербская «Њ» и т.п.)
    или латиница вперемешку; символы, которых в надписях не бывает."""
    for word in re.findall(r"[^\s]+", text):
        letters = [c for c in word if c.isalpha()]
        cyr = [c for c in letters if "\u0400" <= c <= "\u04ff"]
        if cyr and (any(c not in _CYR_OK for c in cyr) or len(cyr) != len(letters)):
            return True
    return bool(re.search(r"[^\w\s!?.,:;«»\"'()\-–—+%№&$€/]", text))


_RU_ONLY = set("АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ")


def suspects(read: list, expected: list[str], language: str) -> list[tuple[str, str, object, bool]]:
    """Сверка буквального чтения (macOS Vision, без автокоррекции) с заданной надписью по словам.
    → подозрения: (как прочитано, как должно быть, строка ocr.Line, чужая буква?). Слово прочитано почти как заданное,
    но не так же («СЛУЧАЙНІЕ» вместо «СЛУЧАЙНЫЕ»), или в слове чужая буква. Слова нет вовсе — не считаем
    (распознавание могло не взять шрифт); склеенные соседние слова («МИРАЗИИ») — не ошибка. Большинство
    слов не прочиталось — шрифт ему не по зубам, судить не берёмся. Подозрение — ещё не ошибка:
    распознавание и само путает буквы (теряет «Е» в «ВЬЕТНАМ»), поэтому такое потом смотрит нейросеть.
    А вот буква не из алфавита языка («І», «Њ» в русском слове) — ошибка наверняка: сама её распознавание
    не выдумывает, а нейросеть такую «дочитывает» до правильной."""
    import difflib

    want = _norm_text(expected).split()
    words = [(w, ln) for ln in read for w in _norm_text([ln.text]).split()]
    got = [w for w, _ in words]
    if not want or not got:
        return []
    glued = "".join(got)
    found = [w for w in want if w in got or w in glued]
    if len(found) * 2 < len(want):
        return []
    ru = (language or "").split("-")[0].lower() == "ru"
    def foreign(g: str) -> bool:
        letters = [c for c in g if c.isalpha()]
        return ru and any("\u0400" <= c <= "\u04ff" for c in letters) and any(c not in _RU_ONLY for c in letters)

    out: list[tuple[str, str, object, bool]] = []
    for w in want:
        if w in got:
            continue
        g, ln = max(words, key=lambda x: difflib.SequenceMatcher(None, w, x[0]).ratio())
        if w in g or difflib.SequenceMatcher(None, w, g).ratio() < 0.7:
            continue
        out.append((g, w, ln, foreign(g)))
    for g, ln in words:  # буквы не из алфавита языка в слове, которого в надписи нет
        if foreign(g) and g not in want and not any(g == o[0] for o in out):
            close = max(want, key=lambda w: difflib.SequenceMatcher(None, w, g).ratio())
            out.append((g, close, ln, True))
    return out


ARBITER_ASKS = 2  # подозрение снимается, только если нейросеть столько раз подряд сказала «верно»
_ARBITER = """Ты — строгий корректор надписей на обложке. На фрагменте картинки должно быть написано слово «{want}».
Программа распознавания текста прочитала там «{read}». Распознавание иногда ошибается само, а генератор картинок
иногда рисует испорченные буквы (другой алфавит, лишний штрих, склеенные или пропущенные буквы) — нужно понять,
кто прав. Рассмотри слово «{want}» на фрагменте буква за буквой, по начертанию, особенно места, где «{want}» и
«{read}» расходятся. Не исправляй увиденное по смыслу: если буква нарисована криво или не той — это ошибка.
Ответ — только JSON: {{"letters": "буквы слова по порядку, как нарисованы", "correct": true/false (слово нарисовано
ровно как «{want}», без искажённых букв), "defect": "какая буква не так — или пустая строка"}}"""


def _arbitrate(llm, image: Path, read: str, want: str, line) -> tuple[bool, str]:
    """Нейросеть смотрит на крупный фрагмент строки: подозрение — настоящая ошибка? → (ошибка?, что не так)."""
    from engine.publish import ocr

    data = _ask_image(llm, "cover_text_arbiter", _ARBITER.format(want=want, read=read),
                      f"Как на самом деле нарисовано слово «{want}»?", ocr.crop(image, line.box))
    if data.get("correct") is True:
        return False, ""
    return True, str(data.get("defect") or f"«{read}» вместо «{want}»").strip()


def check_text(llm, image: Path, expected: list[str], language: str) -> tuple[bool, str]:
    """Надписи на готовом варианте совпадают с заданными буква в буквку? → (да/нет, что прочитано/что не так).
    Нет надписей по плану — проверять нечего. Проверка не удалась (сеть) — считаем годной, но пишем в журнал."""
    if not expected:
        return True, ""
    from engine.publish import ocr

    literal = ocr.read(image, language)
    if literal:  # буквальное чтение есть — проверяем по нему, нейросеть разбирает подозрения
        problems = []
        for read, want, line, alien in suspects(literal, expected, language):
            bad, why = True, f"«{read}» вместо «{want}»"
            if not alien:  # пропущенная/лишняя буква — может, ошиблось распознавание: верно — только если
                try:       # нейросеть дважды подряд увидела слово целым
                    for _ in range(ARBITER_ASKS):
                        bad, why = _arbitrate(llm, image, read, want, line)
                        if bad:
                            break
                except Exception as exc:
                    log(f"Обложка: разобрать «{read}» на {image.name} не вышло ({str(exc)[:100]}) — считаю ошибкой")
                    bad, why = True, f"«{read}» вместо «{want}»"
            if bad:
                problems.append(f"{want}: {why}")
            else:
                log(f"Обложка: {image.name} — «{want}» нарисовано верно (распознавание прочитало «{read}»)")
        if problems:
            found = "; ".join(problems)
            log(f"Обложка: {image.name} — ошибка в буквах надписи: {found}")
            return False, found[:200]
        return True, " / ".join(ln.text for ln in literal)[:200]
    try:
        data = _ask_image(llm, "cover_text_check",
                          _READ_EXACT.format(expected="\n".join(f"«{t}»" for t in expected)),
                          "Проверь надпись по буквам.", to_jpeg(image.read_bytes(), 2048))
    except Exception as exc:
        log(f"Обложка: проверить надпись на {image.name} не вышло ({str(exc)[:100]})")
        return True, ""
    read = _lines(data.get("text"))
    defects = str(data.get("defects") or "").strip()
    same = (not read or _norm_text(read) == _norm_text(expected)) and data.get("match") is not False
    bad_glyph = _bad_glyphs(" ".join(read))
    ok = same and not defects and not bad_glyph
    shown = " / ".join(read)
    if not ok:
        log(f"Обложка: {image.name} — надпись с ошибкой: «{shown}»" + (f" ({defects})" if defects else ""))
    return ok, (defects or shown)[:200]


def pick_best(llm, original: bytes | None, variants: list[Path], text: list[str], language: str, *,
              prompts: dict | None = None) -> tuple[int, bool, str]:
    """→ (индекс лучшего, годится ли, почему). Нейросеть без «глаз» / недоступна — первый, «годится»."""
    if len(variants) == 1:
        return 0, True, "один вариант"
    sheet, layout = contact_sheet(original, variants)
    try:
        data = _ask_image(llm, "cover_pick", _P(prompts, "pick", лист=layout, язык=lang_label(language),
                                                    надписи=" / ".join(text) or "—"),
                          "Выбери лучший вариант.", sheet)
    except Exception as exc:
        log(f"Обложка: выбрать лучший не вышло ({exc}) — беру вариант 1")
        return 0, True, "выбор не удался — первый вариант"
    try:
        best = int(data.get("best")) - 1
    except (TypeError, ValueError):
        best = 0
    best = best if 0 <= best < len(variants) else 0
    return best, bool(data.get("acceptable", True)), str(data.get("reason") or "")[:300]


# ---------- генератор ----------


def model_label(model_id: str) -> str:
    """Понятное имя модели для подписей и папок: «Nano Banana Pro 2K». Нет в прайсе — сам id."""
    from engine.slides import ai_gen

    try:
        e = ai_gen.entry(model_id, offline=True)  # подпись в списке задач — без похода в сеть
    except Exception:
        e = None
    return str((e or {}).get("display_name") or model_id or "")


def _model(model_id: str) -> tuple[str, str]:
    """Модель из прайса → (имя в API, разрешение)."""
    from engine.slides import ai_gen

    e = ai_gen.entry(model_id)
    if e is None or e.get("media_type") != "image":
        raise CoverError(f"нет такой модели картинок: {model_id} — выбери в настройках обложек")
    return str(e["api_name"]), ai_gen.resolution_of(e)


# длина промпта у моделей генератора (документация RoyalTechno): Nano Banana и GPT Image — 32000,
# Seedream — 1600, Grok — 2500, остальные — 4000. Из ошибки «accepts at most N» узнаём точнее.
_PROMPT_CAPS = (("nano-banana", 32000), ("gpt-image", 32000), ("seedream", 1600), ("grok", 2500))
_PROMPT_CAPS_SEEN: dict[str, int] = {}

_SHORTEN = """Сократи промпт для генератора изображений до {limit} символов или меньше (сейчас {length}).
Сохрани: все надписи в кавычках — ДОСЛОВНО, с разбивкой на строки (мат и грубые слова тоже — без смягчения);
композицию, героев и их привлекательность, одежду, эмоции, обстановку, цвета и стиль; раздел REAL PEOPLE
целиком (имена известных людей и номера их фото); обязательные правила (другие лица у безымянных людей,
взрослые, без наготы, без логотипов и плашек, 16:9). Убери повторы и второстепенное. Язык промпта не меняй.
Ответ — только сокращённый промпт, без пояснений."""


def prompt_cap(model_id: str) -> int:
    if model_id in _PROMPT_CAPS_SEEN:
        return _PROMPT_CAPS_SEEN[model_id]
    try:
        from engine.slides import ai_gen

        cap = int((ai_gen.entry(model_id) or {}).get("prompt_max") or 0)
    except Exception:
        cap = 0
    return cap or next((c for k, c in _PROMPT_CAPS if model_id.startswith(k)), 4000)


def fit_prompt(cs: CoverSettings, prompt: str, work: Path) -> str:
    """Промпт длиннее, чем принимает модель, — нейросеть сжимает его (результат запоминается в папке работы:
    повтор берёт тот же). Не вышло — режем хвост (надписи и главное — в начале промпта)."""
    limit = prompt_cap(cs.model) - 100  # запас: RoyalTechno может дописать к промпту свой стиль ключа
    if len(prompt) <= limit:
        return prompt
    cache = work / f"промпт_до_{limit}.txt"
    sig = hashlib.sha1(prompt.encode()).hexdigest()[:12]
    if cache.is_file():
        head, _, body = cache.read_text(encoding="utf-8").partition("\n")
        if head == sig and 0 < len(body) <= limit:
            return body
    short = ""
    for _ in range(2):
        try:
            client = _client(cs.llm)
            try:
                short = client.complete(label="cover_shorten", system=_SHORTEN.format(limit=limit - 100, length=len(prompt)),
                                        user=short if len(short) > limit else prompt).strip()
            finally:
                client.close()
        except Exception as exc:
            log(f"Обложка: сжать промпт не вышло ({exc})")
            break
        if len(short) <= limit:
            break
    if not short or len(short) > limit:
        short = (short if short and len(short) < len(prompt) else prompt)[:limit].rsplit(" ", 1)[0]
    log(f"Обложка: промпт сжат под модель {model_label(cs.model)}: {len(prompt)} → {len(short)} символов (лимит {limit + 100})")
    cache.write_text(f"{sig}\n{short}", encoding="utf-8")
    return short


NO_REFERENCE_MODELS: set[str] = set()  # модели, которые не принимают образец картинкой (узнаём из ответа сервиса)


TEMP_RETRIES = 2
TEMP_RETRY_PAUSE = 10.0


def _temporary(err: dict, msg: str) -> bool:
    from engine.slides.ai_gen import _UPSTREAM_CODES

    return err.get("code") in _UPSTREAM_CODES or "temporar" in msg.lower() or "retry" in msg.lower()


def _no_refs_error(exc: Exception) -> bool:
    t = str(exc).lower()
    return "reference image" in t or "unsupported_combination" in t


def generate(cs: CoverSettings, prompt: str, refs: list[str], work: Path, tag: str, count: int, *,
             cancel: CancelToken | None = None, progress: ProgressCb | None = None,
             edit_only: bool = False) -> list[Path]:
    """count картинок по промпту (refs — образцы: data URI / https). Отправленные задачи — в cover_jobs.json:
    повтор после сбоя дожидается тех же (не платим второй раз). → скачанные файлы (сколько получилось)."""
    from engine.slides.ai_gen import AiGenError, RoyalClient, _output_url

    if not cs.api_key.strip():
        raise CoverError("Нет ключа RoyalTechno (Настройки → AI-кадры) — обложки генерировать нечем")
    api_name, res = _model(cs.model)
    work.mkdir(parents=True, exist_ok=True)
    prompt = fit_prompt(cs, prompt, work)
    state_file = work / JOBS_FILE
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    # подпись задачи (повтор после сбоя ждёт те же задачи): с одним образцом — как раньше, с фото людей — все картинки
    ref_sig: Any = (refs[:1] and refs[0][-200:]) if len(refs) <= 1 else [r[-200:] for r in refs]
    sig = hashlib.sha1(json.dumps([prompt, ref_sig, cs.model], ensure_ascii=False).encode()).hexdigest()[:10]

    def save() -> None:
        state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    client = RoyalClient(cs.api_key)
    if cancel is not None:
        cancel.register(client)
    out: list[Path] = []
    try:
        flying: dict[str, tuple[str, Path]] = {}  # job_id → (ключ, файл)
        inp_of: dict[str, dict] = {}  # ключ → что отправляли (для повтора после временного сбоя)
        errors: list[str] = []
        for n in range(1, count + 1):
            key = f"{tag}-{sig}-{n}"
            dest = work / f"{tag}_{n}.png"
            rec = state.get(key) or {}
            if rec.get("done") and dest.is_file():
                out.append(dest)
                continue
            if rec.get("job_id") and not rec.get("failed"):
                flying[rec["job_id"]] = (key, dest)
                continue
            inp: dict[str, Any] = {"prompt": prompt, "aspect_ratio": "landscape"}
            if res:
                inp["resolution"] = res
            if refs and cs.model not in NO_REFERENCE_MODELS:
                inp["reference_image_urls"] = refs
            idem = rec.get("idempotency") if rec and not rec.get("failed") else None
            idem = idem or str(uuid.uuid4())
            state[key] = {"idempotency": idem}
            save()
            try:
                try:
                    job = client.submit(api_name, inp, idem)
                except AiGenError as exc:
                    if _no_refs_error(exc) and inp.get("reference_image_urls"):
                        # модель не принимает образец картинкой (GPT Image) — генерируем по текстовому описанию
                        NO_REFERENCE_MODELS.add(cs.model)
                        if edit_only:
                            raise CoverError(f"модель {model_label(cs.model)} не умеет править готовую картинку — "
                                             "для перевода надписей выбери Nano Banana или Seedream") from None
                        inp.pop("reference_image_urls")
                        refs = []
                        log(f"Обложка: {model_label(cs.model)} не принимает образец картинкой — генерирую по описанию")
                        from engine.core import switches

                        switches.report("обложка", f"{model_label(cs.model)} по образцу-картинке",
                                        f"{model_label(cs.model)} по текстовому описанию",
                                        "модель не принимает образец картинкой")
                        idem = str(uuid.uuid4())
                        state[key] = {"idempotency": idem}
                        save()
                        job = client.submit(api_name, inp, idem)
                        state[key]["job_id"] = job["id"]
                        save()
                        inp_of[key] = dict(inp)
                        flying[job["id"]] = (key, dest)
                        continue
                    cap = re.search(r"accepts at most (\d+)", str(exc))
                    if not cap:
                        raise
                    # у модели лимит меньше, чем мы знали, — сжимаем под него и отправляем ещё раз
                    seen = int(cap.group(1))
                    sent = re.search(r"Prompt is (\d+) characters", str(exc))
                    if sent and int(sent.group(1)) > len(prompt) > 0:
                        # сервис считает длиннее, чем мы (другие единицы счёта): урезаем в той же пропорции
                        seen = int(seen * len(prompt) / int(sent.group(1)))
                    _PROMPT_CAPS_SEEN[cs.model] = min(seen, _PROMPT_CAPS_SEEN.get(cs.model, seen))
                    inp["prompt"] = prompt = fit_prompt(cs, prompt, work)
                    idem = str(uuid.uuid4())
                    state[key] = {"idempotency": idem}
                    save()
                    job = client.submit(api_name, inp, idem)
            except (AiGenError, httpx.HTTPError) as exc:
                errors.append(str(exc))
                log(f"Обложка: задача генератора не принята — {exc}")
                if any(s in str(exc) for s in (" 401", " 402", " 403")):
                    break
                continue
            state[key]["job_id"] = job["id"]
            save()
            inp_of[key] = dict(inp)
            flying[job["id"]] = (key, dest)
        deadline = time.monotonic() + WAIT_SEC
        total = len(flying) + len(out)
        while flying and time.monotonic() < deadline:
            if cancel:
                cancel.check()
            time.sleep(POLL_SEC)
            for jid in list(flying):
                key, dest = flying[jid]
                try:
                    job = client.poll(jid)
                except (AiGenError, httpx.HTTPError) as exc:
                    log(f"Обложка: опрос {jid}: {exc}")
                    continue
                status = job.get("status")
                if status == "succeeded":
                    url = _output_url(job)
                    if url and client.download(url, dest):
                        state[key].update(done=True, url=url, cost=job.get("cost_usd_cents"))
                        out.append(dest)
                    else:
                        errors.append("готовая картинка не скачалась")
                        state[key]["failed"] = True
                    save()
                    del flying[jid]
                    if progress:
                        progress(len(out) / max(1, total), f"Обложка: готово {len(out)}/{total}")
                elif status in ("failed", "canceled", "cancelled"):
                    err = job.get("error") or {}
                    msg = str(err.get("message") or status)
                    del flying[jid]
                    tries = int(state[key].get("tries") or 0)
                    if _temporary(err, msg) and tries < TEMP_RETRIES and key in inp_of:
                        # кратковременный сбой у провайдера (деньги за упавшую задачу возвращаются) — ещё раз
                        log(f"Обложка: временный сбой генератора ({msg}) — повторяю ({tries + 1}/{TEMP_RETRIES})")
                        time.sleep(TEMP_RETRY_PAUSE)
                        try:
                            again = client.submit(api_name, inp_of[key], str(uuid.uuid4()))
                        except (AiGenError, httpx.HTTPError) as exc:
                            errors.append(str(exc))
                        else:
                            state[key] = {**state[key], "job_id": again["id"], "tries": tries + 1}
                            save()
                            flying[again["id"]] = (key, dest)
                            continue
                    errors.append(msg)
                    log(f"Обложка: вариант не сгенерировался ({msg})")
                    state[key]["failed"] = True
                    save()
        for jid in flying:
            client.cancel(jid)
            state[flying[jid][0]]["failed"] = True
        if flying:
            save()
            errors.append("генератор не успел за 15 минут")
        if not out:
            raise CoverError("генератор не сделал ни одной обложки" + (f": {errors[-1]}" if errors else ""))
        return sorted(out)
    finally:
        client.close()


def url_of(path: Path) -> str:
    """Публичная ссылка готового варианта на CDN генератора (если есть) — для правки без повторной загрузки."""
    try:
        state = json.loads((path.parent / JOBS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    for key, rec in state.items():
        if rec.get("done") and key.startswith(path.stem.rsplit("_", 1)[0]) and key.endswith("-" + path.stem.rsplit("_", 1)[-1]):
            return str(rec.get("url") or "")
    return ""


# ---------- целиком ----------


def make_cover(cs: CoverSettings, work: Path, *, reference: bytes | None, language: str, story: str = "",
               title: str = "", cancel: CancelToken | None = None, progress: ProgressCb | None = None,
               _redo: str = "") -> CoverResult:
    """Обложка целиком. reference — образец (байты картинки) или None (придумать по тексту story).
    title — название ролика-оригинала (подсказка, кто из известных людей на обложке).
    Известные люди образца остаются теми же: генератор получает их другие настоящие фото."""
    def step(p: float, msg: str) -> None:
        log(msg)
        if progress:
            progress(p, msg)

    work.mkdir(parents=True, exist_ok=True)
    info_path = work / INFO_FILE
    info: dict[str, Any] = {}
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}

    def keep(**values: Any) -> None:
        info.update(values)
        info_path.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")

    ref = to_jpeg(reference) if reference else None
    if ref:
        (work / "образец.jpg").write_bytes(ref)
    # описание обложки из прошлой попытки годится, только если сделано по ТОЙ ЖЕ картинке-образцу: 22.11 ночью
    # обложку донора не скачали и придумали по тексту, а доделка взяла это описание вместо разбора настоящего образца
    source_key = hashlib.sha1(ref).hexdigest() if ref else "concept"
    if info.get("prompt") and info.get("source") != source_key:
        info = {}
    if not info.get("prompt"):
        step(0.05, "Обложка: нейросеть разбирает образец…" if ref else "Обложка: нейросеть придумывает обложку…")
        if ref:
            context = "\n".join(s for s in (f"Название: {title}" if title else "", (story or "")[:1000]) if s)
            if _redo:  # прошлый разбор дал обложку слабее образца — редактор сказал, чем именно
                context += ("\n\nПРОШЛАЯ ПОПЫТКА ЗАБРАКОВАНА редактором (обложка не похожа на образец / слабее): "
                            f"{_redo[:600]}\nПерерисуй образец точнее: те же люди по типу, фигурам и откровенности "
                            "нарядов, та же сцена и подача.")
            a = analyze_full(cs.llm, ref, language, prompts=cs.prompts, context=context)
            keep(prompt=a["prompt"], text=a["text"], original_text=a["original_text"], people=a["people"],
                 language=language, source=source_key)
        else:
            prompt, text = concept(cs.llm, story, language, prompts=cs.prompts)
            keep(prompt=prompt, text=text, language=language, source=source_key)
    prompt, text = str(info["prompt"]), list(info.get("text") or [])
    shown = (" / ".join(f'"{t}"' for t in text) + ".") if text else "(no text at all — do not add any lettering)."
    notes: list[str] = []
    people = list(info.get("people") or []) if ref else []
    photos: dict[str, Path | None] = {}
    if people:
        from engine.publish import cover_people

        step(0.1, "Обложка: ищу другие фото известных людей: " + ", ".join(p["name"] for p in people) + "…")
        try:
            photos = cover_people.find_photos([p["name"] for p in people], ref, work, cancel=cancel)
        except CancelledError:
            raise
        except Exception as exc:  # поиск картинок упал — генератор держится лиц образца
            log(f"Обложка: поиск фото людей не удался ({exc})")
        missing = [p["name"] for p in people if not photos.get(p["name"])]
        notes.append("известные люди: " + ", ".join(p["name"] for p in people)
                     + (f" (другое фото не нашлось: {', '.join(missing)} — лицо взято с образца)" if missing else
                        " — у каждого другое настоящее фото"))
    block, people_files = people_block(people, photos)
    full = prompt + ("\n\n" + block if block else "") + "\n\n" + _P(cs.prompts, "generate", надписи=shown)
    refs = ([data_uri(ref)] if ref else []) + [data_uri(f.read_bytes()) for f in people_files]
    variants: list[Path] = []
    text_ok: dict[Path, tuple[bool, str]] = {}
    best, ok, why = 0, False, ""
    for rnd in range(1, MAX_ROUNDS + 1):
        if cancel:
            cancel.check()
        step(0.15 + 0.35 * (rnd - 1), f"Обложка: генерирую {cs.variants} вариант(а/ов)" + (f", круг {rnd}" if rnd > 1 else "") + "…")
        got = generate(cs, full, refs, work, f"вариант{rnd}", cs.variants, cancel=cancel,
                       progress=lambda p, m: progress and progress(0.15 + 0.3 * p, m))
        variants += got
        # надписи: строгая проверка буква в буквку — вариант с ошибкой лучшим быть не может
        for v in got:
            text_ok[v] = check_text(cs.llm, v, text, language)
        clean = [v for v in variants if text_ok.get(v, (True, ""))[0]]
        if not clean:
            if rnd < MAX_ROUNDS:
                notes.append(f"круг {rnd}: во всех вариантах ошибки в надписи — генерирую ещё")
                continue
            clean = variants
        step(0.5, "Обложка: нейросеть выбирает лучший вариант…")
        k, ok, why = pick_best(cs.llm, ref, clean, text, language, prompts=cs.prompts)
        best = variants.index(clean[k])
        log(f"Обложка: лучший — {variants[best].name}{'' if ok else ' (с замечаниями)'}: {why}")
        if ok and text_ok.get(variants[best], (True, ""))[0]:
            break
        if rnd < MAX_ROUNDS:
            notes.append(f"круг {rnd}: все варианты с браком ({why}) — генерирую ещё")
    if not text_ok.get(variants[best], (True, ""))[0]:
        notes.append(f"⚠ в надписи на обложке ошибка («{text_ok[variants[best]][1]}») — во всех вариантах; "
                     "исправь надпись или сгенерируй заново")
        ok = False
    elif not ok:
        notes.append(f"идеального варианта не вышло — взял лучший: {why}")
    if ref and cs.model in NO_REFERENCE_MODELS:
        notes.append(f"{model_label(cs.model)} не принимает образец картинкой — обложка сделана по текстовому "
                     "описанию образца (сходство ниже)")
    if not ok and ref and not _redo:
        # лучший вариант слабее образца — разбираем образец заново (с замечаниями редактора) и генерируем ещё раз
        log(f"Обложка: лучший вариант слабее образца ({why[:160]}) — разбираю образец заново")
        keep(prompt="", text=[])
        again = make_cover(cs, work, reference=reference, language=language, story=story, title=title,
                           cancel=cancel, progress=progress, _redo=why or "не похожа на образец")
        again.notes[:0] = [f"первая попытка слабее образца ({why[:120]}) — образец разобран заново", *notes]
        return again
    chosen = save_for_youtube(variants[best], work / "обложка.jpg")
    keep(chosen=variants[best].name, reason=why, acceptable=ok, model=cs.model, model_label=model_label(cs.model),
         generated=time.strftime("%d.%m.%Y %H:%M"))
    return CoverResult(path=chosen, variants=variants, prompt=prompt, text=text, notes=notes, model=cs.model,
                       acceptable=bool(ok))


def translate_cover(cs: CoverSettings, image: Path, text: list[str], language: str, work: Path, *,
                    cancel: CancelToken | None = None, progress: ProgressCb | None = None) -> Path:
    """Та же обложка, надписи — на другом языке (меняется только текст)."""
    code = (language or "").split("-")[0].lower()
    if not text:
        raise CoverError("на обложке нет надписей — переводить нечего")
    new = translate_text(cs.llm, text, code, prompts=cs.prompts)
    prompt = _P(cs.prompts, "edit_text", надписи="\n".join(f'"{t}"' for t in new))
    ref = url_of(image) or data_uri(to_jpeg(image.read_bytes(), 2048))
    got = generate(cs, prompt, [ref], work, f"перевод_{code}", min(2, cs.variants), cancel=cancel, progress=progress,
                   edit_only=True)
    best, _ok, why = pick_best(cs.llm, to_jpeg(image.read_bytes()), got, new, code, prompts=cs.prompts)
    dest = save_for_youtube(got[best], work / f"обложка_{code}.jpg")
    log(f"Обложка: перевод на {code} — {dest.name} ({why})")
    return dest


# ---------- оригинал на YouTube ----------


def youtube_original(url: str, *, with_image: bool = True) -> tuple[str, bytes | None, str]:
    """Ролик-оригинал → (название, обложка в максимальном размере, язык)."""
    from engine.rewrite.source import SourceError, video_info

    try:
        info = video_info(url)
    except SourceError:
        # Страница не открылась (проверка на робота и т.п.), но картинка обложки лежит на i.ytimg.com
        # по id ролика и от этой проверки не зависит: без неё обложка не должна теряться.
        m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([\w-]{11})", url or "")
        if not with_image or not m:
            raise
        info = {"id": m.group(1)}
    if not with_image:
        return str(info.get("title") or ""), None, str(info.get("language") or "")
    vid = str(info.get("id") or "")
    candidates = [f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg"] if vid else []
    thumbs = sorted((t for t in info.get("thumbnails") or [] if t.get("url")),
                    key=lambda t: int(t.get("width") or 0) * int(t.get("height") or 0), reverse=True)
    candidates += [str(t["url"]) for t in thumbs[:3]]
    if info.get("thumbnail"):
        candidates.append(str(info["thumbnail"]))
    if vid:
        candidates.append(f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg")
    image = None
    with httpx.Client(timeout=30, follow_redirects=True, trust_env=False) as http:
        for u in dict.fromkeys(candidates):
            try:
                r = http.get(u)
            except httpx.HTTPError:
                continue
            if r.status_code == 200 and len(r.content) > 5000:
                image = r.content
                break
    return str(info.get("title") or ""), image, str(info.get("language") or "")


# ---------- раздел «Обложки» ----------

OUT_DIR_NAME = "Обложки"


def output_root() -> Path:
    from engine.slides.paths import DESKTOP

    return DESKTOP / OUT_DIR_NAME


def run_cover_job(cs: CoverSettings, *, image: Path | None, url: str, language: str, translations: list[str],
                  work: Path, cancel: CancelToken | None = None, progress: ProgressCb | None = None) -> dict[str, Any]:
    """Раздел «Обложки»: образец (файл или ссылка на ролик YouTube) → N вариантов → лучший → переводы."""
    title = ""
    reference: bytes | None = None
    if image is not None:
        reference = image.read_bytes()
    elif url:
        title, reference, _ = youtube_original(url)
        if reference is None:
            raise CoverError("обложку этого ролика скачать не вышло")
    if reference is None:
        raise CoverError("нужен образец: картинка или ссылка на ролик YouTube")
    res = make_cover(cs, work, reference=reference, language=language, title=title, cancel=cancel,
                     progress=lambda p, m: progress and progress(0.7 * p, m))
    for n, code in enumerate(dict.fromkeys(c.split("-")[0].lower() for c in translations if c.strip()), 1):
        if code == (language or "").split("-")[0].lower():
            continue
        if progress:
            progress(0.7 + 0.3 * (n - 1) / max(1, len(translations)), f"Обложка: перевод на {code}…")
        try:
            res.translations[code] = translate_cover(cs, res.path, res.text, code, work, cancel=cancel)
        except CancelledError:
            raise
        except Exception as exc:
            res.notes.append(f"перевод на {code} не получился: {exc}")
            log(f"Обложка: перевод на {code} не получился ({exc})")
    return {"title": title, **res.as_dict()}
