"""Материалы выкладки «Стоковых слайдов»: всё готовится под конкретный ролик.

- Название: по ссылке на оригинал (рерайт) — его название, переписанное синонимично с тем же крючком;
  без ссылки — придумывает нейросеть по тексту ролика. Язык — язык ролика.
- Обложка: по обложке оригинала (образец → наш вариант, см. engine.publish.cover); без ссылки — по сюжету.
- Описание: сначала крупный призыв подписаться, потом главы с тайм-кодами и авторы нарезок (файл «главы.txt»).
  У каждой копии скорости свои тайм-коды — описание своё для каждого файла.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from engine.core.cancel import CancelledError, CancelToken
from engine.core.logutil import log
from engine.publish import cover as cv

ProgressCb = Callable[[float, str], None]

DESC_MAX = 4900  # YouTube — до 5000 символов
COVER_DIR = "обложки"
DEFAULT_DESC_CTA = "🔔 ПОДПИШИТЕСЬ НА КАНАЛ И ВКЛЮЧИТЕ КОЛОКОЛЬЧИК — новые истории выходят каждый день!"


def chapters_file(folder: Path, video: str, pct: int) -> Path:
    from engine.core.chapters import CHAPTERS_NAME
    from engine.slides.speed_copies import copy_filename

    return folder / (CHAPTERS_NAME if int(pct) == 100 else copy_filename(int(pct), CHAPTERS_NAME))


def description(cta: str, chapters: str) -> str:
    """Призыв (отдельным абзацем, крупно — ЗАГЛАВНЫМИ и с эмодзи) + главы. Длинное — режем хвост по строкам."""
    text = "\n\n".join(x.strip() for x in (cta, chapters) if x and x.strip())
    text = text.replace("<", "‹").replace(">", "›")  # YouTube не принимает угловые скобки в описании
    if len(text) <= DESC_MAX:
        return text
    lines = text.splitlines()
    while lines and len("\n".join(lines)) > DESC_MAX:
        lines.pop()
    return "\n".join(lines)


def localize_cta(cta: str, language: str, llm, cache: Path) -> str:
    """Призыв на язык ролика (перевод запоминается в папке обложек)."""
    from engine.core.langdetect import detect_language

    code = (language or "").split("-")[0].lower()
    if not cta.strip() or not code or detect_language(cta) == code:
        return cta
    try:
        stored = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    key = f"{code}:{cta}"
    if key in stored:
        return str(stored[key])
    try:
        data = cv._ask_text(llm, "desc_cta", "Ты переводчик текстов YouTube-канала. Отвечай только JSON.",
                            f"Переведи призыв на язык {cv.lang_label(code)}. Сохрани ЗАГЛАВНЫЕ буквы, эмодзи и тон. "
                            f'Ответ — JSON: {{"text": "..."}}\nПризыв: {cta}')
        out = str(data.get("text") or "").strip() or cta
    except Exception as exc:
        log(f"Описание: призыв не перевёлся ({exc}) — оставляю как есть")
        return cta
    stored[key] = out
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(stored, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


COVER_FAILED = "⚠ Обложка не сгенерировалась"


def cover_failed_warning(exc: BaseException, source_url: str) -> str:
    """Понятная строка «ролик без обложки»: почему и где оригинал — чтобы сделать обложку самому."""
    from engine.slides.ai_fill import reason_of

    why = reason_of(str(exc))
    if why.startswith("ошибка RoyalTechno"):  # не узнали причину — как есть (может быть и сбой нейросети)
        why = str(exc)[:200]
    where = (f" Оригинал (обложку смотри тут): {source_url}" if source_url
             else " Оригинала нет (ролик не из рерайта) — придумай обложку по теме.")
    return (f"{COVER_FAILED} ({why}) — ролик выложен БЕЗ обложки, поставь её сам в Studio или сгенерируй "
            f"в разделе «Обложки».{where}")


def warnings_of(lines: list[str]) -> list[str]:
    return [x for x in lines if x.startswith(COVER_FAILED)]


def title_and_cover(*, folder: Path, source_url: str, language: str, story: str, cover: cv.CoverSettings,
                    make_cover: bool = True, base: dict | None = None, cancel: CancelToken | None = None,
                    progress: ProgressCb | None = None) -> tuple[str, str, list[str], list[str]]:
    """Название и обложка «по конкуренту»: оригинал рерайта (source_url) — его название переписывается,
    обложка переделывается по образцу; без оригинала — придумываются по тексту ролика.
    base — материалы основной версии (эта — перевод): название заново на языке версии, обложка — та же
    с переведёнными надписями. → (название, файл обложки, надписи обложки, строки для итогов)."""
    from engine.core.langdetect import detect_language

    lang = (language or "").split("-")[0].lower() or detect_language(story) or "ru"
    work = folder / COVER_DIR
    lines: list[str] = []
    orig_title, image = "", None
    if source_url and (not make_cover or base is not None):
        try:  # без обложки нужно только название оригинала
            orig_title = cv.youtube_original(source_url, with_image=False)[0]
        except Exception as exc:
            log(f"Выкладка: оригинал не открылся ({exc}) — название придумаю по тексту")
            lines.append(f"оригинал не открылся ({str(exc)[:120]}) — название по тексту ролика")
    elif source_url:
        try:
            orig_title, image, _ = cv.youtube_original(source_url)
        except Exception as exc:
            log(f"Выкладка: оригинал не открылся ({exc}) — обложку и название придумаю по тексту")
            lines.append(f"оригинал не открылся ({str(exc)[:120]}) — обложка и название по тексту ролика")
        if source_url and image is None and orig_title:
            lines.append("обложку оригинала скачать не вышло — обложка придумана по тексту ролика")
    if orig_title:
        title = cv.rewrite_title(cover.llm, orig_title, lang, story=story, variants=cover.title_variants,
                                 prompts=cover.prompts, notes=lines)
        lines.append(f"Название оригинала: {orig_title}")
    else:
        title = cv.title_from_story(cover.llm, story, lang, variants=cover.title_variants, prompts=cover.prompts,
                                     notes=lines)
    log(f"Выкладка: название — {title}")
    thumbnail, cover_text = "", []
    if base is not None:
        from engine.publish.localize import cover_for

        thumbnail = cover_for(cover, cover.llm, str(base.get("thumbnail") or ""), base.get("cover_text"), lang, work,
                              lines) if base.get("thumbnail") else ""
    elif make_cover:
        try:
            res = cv.make_cover(cover, work, reference=image, language=lang, story=story, title=orig_title,
                                cancel=cancel,
                                progress=progress)
        except CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — нет денег, нет связи, генератор упал: ролик выходит без обложки
            log(f"Выкладка: обложка не сгенерировалась ({exc}) — выкладываю без обложки")
            lines.append(cover_failed_warning(exc, source_url))
        else:
            thumbnail, cover_text = str(res.path), list(res.text)
            lines += [f"Обложка: {cv.model_label(res.model)}, лучший из {len(res.variants)} вариантов, все — в папке "
                      f"«{COVER_DIR}»", *res.notes]
    else:
        lines.append("Обложка не генерировалась (выключено) — поставишь сам в Studio")
    return title, thumbnail, cover_text, lines


def prepare(*, folder: Path, files: list[tuple[str, int]], source_url: str, language: str, story: str,
            cover: cv.CoverSettings, desc_cta: str, make_cover: bool = True, base: dict | None = None,
            cancel: CancelToken | None = None, progress: ProgressCb | None = None) -> dict[str, Any]:
    """→ материалы выкладки (как materials.pick().as_dict()) + описания по файлам и строки для итогов.
    base — материалы основной версии (эта — перевод): обложка та же (надписи — переводом), название —
    заново на языке этой версии, описание — из её глав."""
    from engine.core.langdetect import detect_language

    lang = (language or "").split("-")[0].lower() or detect_language(story) or "ru"
    work = folder / COVER_DIR
    title, thumbnail, cover_text, lines = title_and_cover(
        folder=folder, source_url=source_url, language=lang, story=story, cover=cover, make_cover=make_cover,
        base=base, cancel=cancel, progress=progress)
    cta = localize_cta(desc_cta, lang, cover.llm, work / "cta.json")
    descs: dict[str, str] = {}
    for f, pct in files:
        ch = chapters_file(folder, Path(f).name, pct)
        chapters = ch.read_text(encoding="utf-8") if ch.is_file() else ""
        if not chapters:
            lines.append(f"нет файла глав для {Path(f).name} — в описании только призыв")
        descs[str(f)] = description(cta, chapters)
    main = descs.get(str(files[0][0])) if files else description(cta, "")
    return {"title": title, "thumbnail": thumbnail, "cover_text": cover_text, "description": main or "", "descriptions": descs,
            "lines": [re.sub(r"\s+", " ", x) for x in lines], "warnings": warnings_of(lines)}
