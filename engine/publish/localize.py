"""Материалы выкладки на языке версии ролика: название, описание, надписи на обложке.

Ролик на испанском — название, описание и текст на обложке тоже на испанском. Обложка без надписей —
та же картинка; с надписями — та же картинка, переведены только надписи (генератор правит готовую обложку).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from engine.core.logutil import log
from engine.publish import cover as cv

_TRANSLATE = """Ты переводчик материалов YouTube-ролика. Переведи название и описание на язык {lang}.
Название: сохрани крючок и подачу (КАПСЛОК, «!», эмодзи — так же), звучать должно естественно для носителя,
как кликабельное название, а не дословный перевод; до 100 символов.
Описание: тайм-коды (0:00, 12:34), ссылки, @упоминания и #хештеги — без изменений; переводится только текст.
Если текст уже на языке {lang} — верни его без изменений.
Ответ — только JSON: {{"title": "...", "description": "..."}}"""

_READ = """Прочитай все надписи на этой обложке YouTube — дословно, как написано, по строкам (сверху вниз).
Значки, логотипы и цифры длительности ролика не считаются. Надписей нет — пустой список.
Ответ — только JSON: {"text": ["строка 1", …]}"""


def needs_translation(text: str, language: str) -> bool:
    """Нужен ли перевод: язык текста узнали и он другой — да; не узнали (коротко) — да (нейросеть сама
    вернёт как есть, если язык уже тот); совпадает — нет."""
    from engine.core.langdetect import detect_language

    lang = (language or "").split("-")[0].lower()
    if not lang:
        return False
    found = detect_language(text)
    return found != lang


def translate_text(llm, title: str, description: str, language: str) -> tuple[str, str]:
    data = cv._ask_text(llm, "publish_translate", _TRANSLATE.format(lang=cv.lang_label(language)),
                        json.dumps({"title": title, "description": description}, ensure_ascii=False))
    return (cv._clean_title(data.get("title")) or title, str(data.get("description") or description).strip())


def read_cover_text(llm, image: Path) -> list[str]:
    data = cv._ask_image(llm, "cover_read_text", _READ, "Прочитай надписи.", cv.to_jpeg(image.read_bytes()))
    return cv._lines(data.get("text"))


def cover_for(cs: cv.CoverSettings | None, llm, thumbnail: str, text: list[str] | None, language: str,
              work: Path, notes: list[str]) -> str:
    """Обложка для языка: без надписей — та же; с надписями — перевод надписей. → путь к файлу обложки."""
    if not thumbnail or not Path(thumbnail).is_file():
        return thumbnail
    src = Path(thumbnail)
    if text is None:
        try:
            text = read_cover_text(llm, src)
        except Exception as exc:
            notes.append(f"надписи на обложке прочитать не вышло ({str(exc)[:100]}) — обложка та же")
            return thumbnail
    code = (language or "").split("-")[0].lower()
    if not text:
        return thumbnail  # надписей нет — та же картинка на любом языке
    if cs is None or not cs.api_key.strip():
        notes.append(f"надписи на обложке не переведены на {code.upper()} — нужен ключ RoyalTechno (Настройки → AI-кадры)")
        return thumbnail
    work.mkdir(parents=True, exist_ok=True)
    try:
        out = cv.translate_cover(cs, src, text, code, work)
    except Exception as exc:
        from engine.publish.slides_materials import _wait_net_or

        _wait_net_or(exc, f"надписи обложки на {code.upper()} не перевелись")  # обрыв связи — подождать, а не
        # выкладывать обложку с надписями на чужом языке
        notes.append(f"перевод надписей обложки на {code.upper()} не получился ({str(exc)[:150]}) — обложка та же")
        return thumbnail
    notes.append(f"обложка: надписи переведены на {code.upper()}")
    return str(out)


def localize(mat: dict[str, Any], language: str, *, llm, cs: cv.CoverSettings | None, work: Path,
             force: bool) -> dict[str, Any]:
    """Материалы (название, описание, обложка) → на язык версии. force — переводить, даже если язык
    материалов не определился (версия-перевод); иначе — только если явно другой язык."""
    lang = (language or "").split("-")[0].lower()
    if not lang:
        return mat
    sample = f"{mat.get('title', '')}\n{mat.get('description', '')}"
    if not force and not needs_translation(sample, lang):
        return mat
    out = dict(mat)
    notes = list(mat.get("lines") or [])
    try:
        out["title"], out["description"] = translate_text(llm, mat.get("title", ""), mat.get("description", ""), lang)
        out["descriptions"] = {}
        log(f"Выкладка [{lang.upper()}]: название — {out['title']}")
    except Exception as exc:
        notes.append(f"название и описание на {lang.upper()} перевести не вышло ({str(exc)[:120]}) — оставлены как есть")
    out["thumbnail"] = cover_for(cs, llm, str(mat.get("thumbnail") or ""), mat.get("cover_text"), lang, work, notes)
    out["lines"] = notes
    return out


def copy_cover(src: str, dest_dir: Path) -> str:
    if not src or not Path(src).is_file():
        return ""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(src).name
    if dest.resolve() != Path(src).resolve():
        shutil.copy2(src, dest)
    return str(dest)
