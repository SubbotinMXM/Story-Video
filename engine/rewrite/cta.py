"""Тексты в озвучку из полей раздела: перед началом, примерно на 30-й секунде и в конце ролика.

Одно правило для футажей и стоков, для своего текста и для рерайта: поле пустое — ничего не вставляется,
заполнено — вставляется ровно этот текст (на язык рассказа переводится, если язык другой). Никаких скрытых
призывов: интро/аутро-видео на тексты не влияют. При рерайте нейросеть своих призывов не пишет (NO_CTA),
а оставшиеся от исходника — убираются (strip_cta). Свой готовый текст пользователя не чистится.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from engine.core.jsonish import json_dict
from engine.core.logutil import log

# добавляется к промпту рерайта: призывы ставит программа из полей раздела, а не нейросеть
NO_CTA = (
    "\n\nВАЖНО ДЛЯ ЭТОГО ЗАПУСКА (важнее указаний выше про обращение к слушателям): не добавляй в рассказ "
    "никаких призывов подписаться, поставить лайк, написать комментарий или посмотреть другие видео — "
    "ни в начале, ни в середине, ни в конце. Если обращения к слушателям нужны, программа добавит их сама. "
    "Начинай сразу с сюжета."
)

# около 30-й секунды озвучки: ~140 слов в минуту
MID_WORDS = 70
# рассказ короче этого — «на 30-й секунде» не вставляем (это был бы уже конец)
MID_MIN_STORY = MID_WORDS + 20

_SENT = re.compile(r"(?<=[.!?…])\s+")
_KEYS = ("start", "mid", "end")


@dataclass(frozen=True)
class VoiceTexts:
    start: str = ""
    mid: str = ""
    end: str = ""

    def any(self) -> bool:
        return bool(self.start or self.mid or self.end)

    def as_dict(self) -> dict[str, str]:
        return {k: getattr(self, k) for k in _KEYS if getattr(self, k)}


def section_texts(cfg: dict, section: str) -> VoiceTexts:
    """Поля раздела: section — broll или slides."""
    def get(key: str) -> str:
        return " ".join(str(cfg.get(f"{section}_text_{key}") or "").split())

    return VoiceTexts(get("start"), get("mid"), get("end"))


def strip_cta(text: str) -> tuple[str, int]:
    """Убрать призывы, которые нейросеть всё-таки написала (по предложениям). → (текст, сколько убрано)."""
    from engine.slides.strict_media import is_cta

    out_paras, removed = [], 0
    for para in re.split(r"\n\s*\n", text or ""):
        kept = []
        for sent in _SENT.split(para.strip()):
            if sent and is_cta(sent):
                removed += 1
                continue
            kept.append(sent)
        if kept:
            out_paras.append(" ".join(kept))
    return "\n\n".join(out_paras), removed


def insert_after_words(text: str, insert: str, words: int = MID_WORDS) -> str | None:
    """Вставка отдельным абзацем после предложения, на котором набирается ~words слов (≈30-я секунда).
    Конец абзаца рядом (до +40 слов) — лучше: вставка не рвёт мысль. Текст короче — None."""
    if len(text.split()) < MID_MIN_STORY:
        return None
    paras = re.split(r"\n\s*\n", text.strip())
    n = 0
    for k, para in enumerate(paras):
        sents = _SENT.split(para.strip())
        for j, sent in enumerate(sents):
            n += len(sent.split())
            if n < words:
                continue
            rest_in_para = sum(len(x.split()) for x in sents[j + 1:])
            if 0 < rest_in_para <= 40:
                continue  # абзац скоро кончится — вставим после него
            head = " ".join(sents[: j + 1])
            tail = " ".join(sents[j + 1:])
            before = [*paras[:k], head]
            after = [tail, *paras[k + 1:]] if tail else paras[k + 1:]
            return "\n\n".join([*before, insert, *after]).strip()
    return None


def place_texts(story: str, texts: VoiceTexts, *, language: str | None = None, llm: tuple[str, str, str] | None = None,
                cache_dir: Path | None = None) -> tuple[str, list[str]]:
    """Вставить тексты из полей в рассказ. → (текст, что сделано — для журнала и итога задачи).
    Язык рассказа другой — тексты переводятся (один запрос, результат запоминается); не вышло — ошибка:
    вставлять текст не на языке рассказа нельзя."""
    text = (story or "").strip()
    if not texts.any() or not text:
        return text, []
    texts = localize(texts, text, language=language, llm=llm, cache_dir=cache_dir)
    notes: list[str] = []
    if texts.mid:
        placed = insert_after_words(text, texts.mid)
        if placed is None:
            notes.append("текст «примерно на 30-й секунде» не вставлен: рассказ слишком короткий")
        else:
            text = placed
            notes.append("примерно на 30-й секунде — текст из поля")
    if texts.start:
        text = f"{texts.start}\n\n{text}"
        notes.insert(0, "перед началом — текст из поля")
    if texts.end:
        text = f"{text}\n\n{texts.end}"
        notes.append("в конце — текст из поля")
    log("Тексты из полей: " + "; ".join(notes))
    return re.sub(r"\n{3,}", "\n\n", text).strip(), notes


def localize(texts: VoiceTexts, story: str, *, language: str | None = None, llm: tuple[str, str, str] | None = None,
             cache_dir: Path | None = None) -> VoiceTexts:
    """Тексты — на язык рассказа. Уже на нём — как есть, без запросов."""
    from engine.core.langdetect import detect_language

    lang = (language or "").split("-")[0].lower() or detect_language(story) or ""
    todo = texts.as_dict()
    if lang and all(_same_language(t, lang) for t in todo.values()):
        return texts
    # язык рассказа не узнали по словам — нейросеть определит его по отрывку
    sample = " ".join(story.split()[:80])
    key = hashlib.sha1(json.dumps([lang or sample, todo], ensure_ascii=False).encode()).hexdigest()[:16]
    cache = (cache_dir / "voice_texts.json") if cache_dir else None
    stored = _read(cache)
    if key in stored:
        return VoiceTexts(**{k: stored[key].get(k, "") for k in _KEYS})
    target = f"на язык с кодом «{lang}»" if lang else f"на тот язык, на котором написан этот отрывок рассказа: «{sample}»"
    raw = _ask(
        system="Ты переводчик текстов для озвучки YouTube-канала. Переводи естественно, как говорят носители, "
               "сохраняя смысл и тон; без цифр, сокращений и аббревиатур (их читает ИИ-диктор). Отвечай только JSON.",
        user=(f"Переведи тексты {target}. Если текст уже на этом языке — верни его без изменений.\n"
              f"Ответ — JSON с теми же ключами: {json.dumps(todo, ensure_ascii=False)}"),
        llm=llm,
    )
    data = json_dict(raw)
    out = {k: " ".join(str(data.get(k) or "").split()) for k in todo}
    if not all(out.values()):
        raise RuntimeError("Не получилось перевести тексты из полей на язык рассказа — повторю позже")
    if cache is not None:
        stored[key] = out
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(stored, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"Тексты из полей переведены на язык рассказа ({lang or 'по отрывку'})")
    return VoiceTexts(**{k: out.get(k, "") for k in _KEYS})


_CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")
_LATIN = re.compile(r"[a-zA-Z]")
_CYRILLIC_LANGS = {"ru", "uk", "be", "bg", "sr", "mk", "kk"}


def _same_language(text: str, lang: str) -> bool:
    """Короткий текст уже на языке рассказа? По словам, а для коротких — по алфавиту (русский рассказ +
    кириллица в поле — тот же язык). Не уверены — False: спросим нейросеть (один раз, ответ запомнится)."""
    from engine.core.langdetect import detect_language

    found = detect_language(text)
    if found:
        return found == lang
    cyr, lat = len(_CYRILLIC.findall(text)), len(_LATIN.findall(text))
    return lang == "ru" and cyr > 3 * lat


def _ask(*, system: str, user: str, llm: tuple[str, str, str] | None) -> str:
    """Встроенная нейросеть (сама уйдёт в Claude, если сервис лежит); без ключа — сразу Claude."""
    if llm and llm[0]:
        from engine.slides.llm_client import LLMClient

        client = LLMClient(llm[0], llm[2], llm[1])
        try:
            return client.complete(label="voice_texts", system=system, user=user)
        finally:
            client.close()
    from engine.slides.llm_client import claude_fallback_ok

    if not claude_fallback_ok():
        raise ValueError("Тексты из полей на другом языке, чем рассказ, а перевести нечем: "
                         "нужен ключ встроенной нейросети или Claude Code")
    from engine.rewrite.engines import run_claude

    return run_claude(f"{system}\n\n{user}", timeout=10 * 60)


def _read(path: Path | None) -> dict:
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
