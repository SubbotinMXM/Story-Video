"""Быстрая проверка рерайта программой: путаница имён, грубые смысловые ошибки и сила хука.

Один запрос на весь текст: Claude → Codex → встроенная нейросеть (кто доступен первым). Ответ — не новый текст, а короткий список точечных
правок «найти → заменить» и, если начало слабое, новый хук на первые 15–30 секунд. Так дёшево по
токенам и не ломает остальной текст. Свой текст пользователя не проверяется — только рерайт.
Любой сбой проверки — оставляем текст как есть (проверка не должна валить ролик).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from engine.core.jsonish import json_dict
from engine.core.cancel import CancelToken
from engine.core.logutil import log

# хук — первые 15–30 секунд озвучки: ~35–75 слов
HOOK_MIN_WORDS, HOOK_MAX_WORDS = 35, 75
OPENING_WORDS = 70  # сколько слов начала показываем как «текущий хук»
HOOK_GOOD = 8  # оценка хука, при которой начало не трогаем
MAX_FIXES = 15
SOURCE_WORDS = 6000  # исходник для сверки фактов (стоки) — не больше этого
REVIEW_BUSY_WAIT = 3 * 60  # сервис перегружен — ждём недолго: проверка необязательна
REVIEW_SUB_TIMEOUT = 10 * 60  # Claude/Codex на проверку (ответ — короткий список правок)

_COMMON = """Ты — выпускающий редактор. Перед тобой текст для озвучки, его написала нейросеть по частям.
Задача — БЫСТРО найти только явные ошибки и дать минимальные точечные правки. Стиль не улучшай,
текст не сокращай и не переписывай, мелочи не трогай. Нет явных ошибок — пустой список.

Что считать ошибкой:
{checks}
- повтор: тот же абзац или та же сцена рассказаны дважды подряд почти одинаково.
- слово, которое ИИ-диктор прочтёт с неверным ударением: омограф без однозначного контекста («дома», «замок»,
  «мука», «стоит», «плачу», «узнаю», «полки»…), сокращение, инициалы, цифры — замени на однозначный вариант,
  меняя как можно меньше (например «был дома» → «был у себя дома»). Сначала правь смысловые ошибки, потом это.

Призывы подписаться/лайкнуть/написать комментарий не трогай и не добавляй.

{hook_style}
Оцени текущее начало по 10-балльной шкале. Если оценка ниже {good} — напиши новое начало ({hmin}–{hmax} слов)
на языке текста, с теми же именами и фактами; оно заменит показанный фрагмент НАЧАЛО и должно гладко перейти
в текст после него.

Ответь только JSON:
{{"fixes": [{{"find": "точная цитата из текста, 5–30 слов, скопирована символ в символ",
             "replace": "исправленный фрагмент", "why": "очень коротко"}}],
 "hook_score": число 1–10,
 "hook": "новое начало или пустая строка"}}
Правок — не больше {maxfix}, только самые важные."""

_HOOK_STYLE = {
    "broll": """ХУК. Начало (первые 15–30 секунд, ~{hmin}–{hmax} слов) должно мощно цеплять: самый острый момент,
загадка, ставка или неожиданный факт ИМЕННО этой истории, чтобы слушатель захотел узнать, что дальше.
Без приветствий, без «в этом видео», без призывов, без пересказа финала. Без затёртых шаблонов,
кочующих из рассказа в рассказ (требование подписать документы, нотариус, угроза выгнать из квартиры), если сюжет
не построен именно на этом: такой шаблонный хук оценивай не выше 6 и замени на хук из сути этой истории.""",
    "slides": """ХУК документального ролика — первые ~30 секунд ({hmin}–{hmax} слов), главное место всего ролика:
здесь зритель решает, смотреть ли дальше. Начало должно цеплять максимально, можно провокационно:
- сразу яркая картинка или вопрос: «Представьте, что…», «Неужели…», «Страна, где…»;
- 3–5 самых шокирующих, неожиданных, пикантных фактов ИЗ ЭТОГО ТЕКСТА — короткими вопросами или контрастами
  («женщины в хиджабах сидят рядом с женщинами в мини-юбках»), ответы на которые будут дальше в ролике
  (сами ответы не раскрывай);
- если ролик про жизнь людей или женщин страны — самое интригующее о них (быт, отношения, традиции,
  неожиданные обычаи) — только то, что есть в тексте;
- в конце одна фраза-обещание раскрыть всё по ходу ролика (не призыв подписаться и не «лайк»).
Только факты из текста, без выдумок; без приветствий, без «в этом видео», без пересказа финала.
Спокойное «информационное» начало («X — страна в Азии…», «Сегодня мы расскажем…») оценивай не выше 5.""",
}
# слова начала: (минимум, максимум, сколько слов показываем как текущий хук, оценка «хватит»)
HOOK_PARAMS = {"broll": (HOOK_MIN_WORDS, HOOK_MAX_WORDS, OPENING_WORDS, HOOK_GOOD), "slides": (60, 110, 100, 9)}

_CHECKS = {
    "broll": """- путаница имён: один и тот же герой назван разными именами, имя одного героя отдано другому,
  перепутан пол героя (он/она, окончания), перепутаны родственные связи (сын ↔ внук, муж ↔ брат);
- противоречия сюжета: умерший герой снова действует без объяснения, возраст или годы скачут,
  событие из прошлого пересказано иначе, герой знает то, чего не мог знать;
- грубая бессмыслица: оборванная мысль, фраза не по смыслу, противоречие в соседних предложениях.""",
    "slides": """- грубые фактические ошибки: не та страна, столица, эпоха или век, перепутанные даты ключевых событий,
  известный человек назван не тем, кем был (профессия, страна, роль), перепутанные люди;
  сверяйся с ИСХОДНИКОМ, а где его нет — правь только то, в чём уверен полностью; не уверен — не трогай;
- путаница имён и пола одного и того же человека в разных местах текста;
- грубая бессмыслица: оборванная мысль, противоречие в соседних предложениях.""",
}


@dataclass
class Review:
    text: str
    fixed: int = 0  # сколько правок применено
    hook: bool = False  # заменено начало
    hook_score: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.fixed:
            parts.append(f"исправлено мест: {self.fixed}")
        if self.hook:
            parts.append(f"усилен хук (было {self.hook_score}/10)")
        elif self.hook_score:
            parts.append(f"хук {self.hook_score}/10 — оставлен")
        return "Проверка рерайта: " + (", ".join(parts) if parts else "явных ошибок нет")


def opening(text: str, words: int = OPENING_WORDS) -> tuple[str, str]:
    """(начало ~words слов целыми предложениями, остальное)."""
    text = text.strip()
    pos = n = 0
    for s in re.split(r"(?<=[.!?…])\s+", text):
        if n >= words:
            break
        i = text.find(s, pos)
        if i < 0:
            break
        pos = i + len(s)
        n += len(s.split())
    return text[:pos].strip(), text[pos:].lstrip()


# слова, которые без «ё» всегда значат одно и то же: ИИ-диктор без «ё» читает их неверно («еще», «ее»)
_YO = {"еще": "ещё", "Еще": "Ещё", "ЕЩЕ": "ЕЩЁ", "ее": "её", "Ее": "Её", "ЕЕ": "ЕЁ"}
_YO_RE = re.compile(r"(?<![\w-])(" + "|".join(_YO) + r")(?![\w-])")


def fix_yo(text: str) -> tuple[str, int]:
    """«еще» → «ещё», «ее» → «её» (русский текст). → (текст, сколько замен)."""
    out, n = _YO_RE.subn(lambda m: _YO[m.group(1)], text)
    return out, n


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def apply_fix(text: str, find: str, replace: str) -> str | None:
    """Точечная правка; цитата не найдена (нейросеть исказила) — None."""
    find, replace = find.strip(), replace.strip()
    if not find or find == replace or len(find.split()) < 2:
        return None
    if text.count(find) >= 1:
        return text.replace(find, replace, 1)
    # цитата с другими пробелами/переносами: ищем по словам
    pattern = r"\s+".join(re.escape(w) for w in find.split())
    m = re.search(pattern, text)
    if m:
        return text[: m.start()] + replace + text[m.end():]
    return None


def _hook_ok(hook: str, hmin: int = HOOK_MIN_WORDS, hmax: int = HOOK_MAX_WORDS) -> bool:
    n = len(hook.split())
    if not (hmin * 0.7 <= n <= hmax * 1.4):
        return False
    from engine.rewrite.cta import strip_cta

    _clean, removed = strip_cta(hook)
    return not removed


def review_story(
    story: str,
    *,
    section: str,
    llm: tuple[str, str, str] | None,
    source: str = "",
    cast: list[str] | None = None,
    cancel: CancelToken | None = None,
    subs: list[tuple[str, str]] | None = None,
) -> Review:
    """Одна проверка: точечные правки + при слабом начале новый хук. Без ключа или при сбое — текст как был.
    subs — подписки по приоритету [(claude_code|codex, модель)]: проверяют они (весь текст одним запросом, как
    сам рерайт); не смогли — следующая, последней — встроенная нейросеть. Каждая смена — уведомление."""
    has_llm = bool(llm and llm[0])
    if (not has_llm and not subs) or len(story.split()) < 200:
        return Review(story)

    hmin, hmax, head_words, good = HOOK_PARAMS.get(section, HOOK_PARAMS["broll"])
    head = opening(story, head_words)[0]
    style = _HOOK_STYLE.get(section, _HOOK_STYLE["broll"]).format(hmin=hmin, hmax=hmax)
    system = _COMMON.format(checks=_CHECKS.get(section, _CHECKS["broll"]), hmin=hmin, hmax=hmax,
                            good=good, maxfix=MAX_FIXES, hook_style=style)
    user = ""
    if cast:
        user += "ГЕРОИ ПО ПЛАНУ (имена должны совпадать везде):\n" + "\n".join(f"- {c}" for c in cast[:20]) + "\n\n"
    if section == "slides" and source.strip():
        src = " ".join(source.split()[:SOURCE_WORDS])
        user += f"ИСХОДНИК (факты сверяй с ним):\n{src}\n\n"
    user += f"НАЧАЛО (текущий хук):\n{head}\n\nТЕКСТ ЦЕЛИКОМ:\n{story}"
    raw = _ask(system, user, llm if has_llm else None, subs or [], cancel)
    if raw is None:
        return Review(story, notes=["проверка пропущена"])
    data = _parse(raw)
    text = story
    rv = Review(story)
    for fx in (data.get("fixes") or [])[:MAX_FIXES]:
        if not isinstance(fx, dict):
            continue
        new = apply_fix(text, str(fx.get("find") or ""), str(fx.get("replace") or ""))
        if new is None:
            continue
        text = new
        rv.fixed += 1
        rv.notes.append(str(fx.get("why") or "").strip()[:120])
    try:
        rv.hook_score = int(float(data.get("hook_score") or 0))
    except (TypeError, ValueError):
        rv.hook_score = 0
    hook = _norm(str(data.get("hook") or ""))
    if hook and rv.hook_score < good and _hook_ok(hook, hmin, hmax):
        # хук — отдельным абзацем, сразу после вступления канала (если оно есть)
        text = f"{hook}\n\n{opening(text, head_words)[1]}"
        rv.hook = True
    rv.text = text.strip()
    log(rv.summary() + (f" ({'; '.join(n for n in rv.notes if n)})" if rv.notes else ""))
    return rv


def _ask(system: str, user: str, llm, subs: list[tuple[str, str]], cancel: CancelToken | None) -> str | None:
    """Подписки по порядку, потом встроенная нейросеть. → ответ; None — никто не смог (текст остаётся как был)."""
    from engine.core import switches

    prev, why = "", ""
    if subs:
        from engine.rewrite.engines import RewriteError, run_sub

        prompt = (f"{system}\n\n=====\n\n{user}\n\n=====\nОтветь строго JSON в формате, который требуется выше, "
                  "без пояснений и без markdown-обёртки.")
        for engine, model in subs:
            if prev:
                switches.report("проверка рерайта", prev, engine, why)
            try:
                return run_sub(engine, prompt, model=model, cancel=cancel, timeout=REVIEW_SUB_TIMEOUT)
            except RewriteError as exc:
                if cancel is not None:
                    cancel.check()
                prev, why = engine, str(exc).splitlines()[0][:160]
                log(f"Проверка рерайта: {switches.name(engine)} не смог ({why})")
    if not llm:
        log("Проверка рерайта не получилась — текст оставляю как есть")
        return None
    if prev:
        switches.report("проверка рерайта", prev, "llm", why)
    from engine.slides.llm_client import LLMClient

    key, base, model = llm
    client = LLMClient(key, model, base)
    client.busy_wait = REVIEW_BUSY_WAIT
    if cancel is not None:
        cancel.register(client)
    try:
        return client.complete(label="rewrite_review", system=system, user=user)
    except Exception as exc:
        if cancel is not None:
            cancel.check()
        log(f"Проверка рерайта не получилась ({str(exc)[:160]}) — текст оставляю как есть")
        return None
    finally:
        client.close()


_parse = json_dict
