"""Ролик YouTube → уникальный текст: субтитры → переписывание выбранным способом → файлы.

Папка рерайта (data/rewrites/<id ролика>): оригинал, рассказ, пакет (названия, описание…)
и state.json (план и готовые части — для продолжения после сбоя).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from engine.core.cancel import CancelToken
from engine.core.config import DATA_DIR
from engine.core.logutil import log
from engine.rewrite.engines import SUB_ENGINES, Result, RewriteError, rewrite_claude, rewrite_llm
from engine.rewrite.prompts import DEFAULT_WORDS, fill, word_range
from engine.rewrite.source import Source, fetch_source

ProgressCb = Callable[[float, str], None]
REWRITES_DIR = DATA_DIR / "rewrites"
ENGINES = {"llm": "Встроенная нейросеть, частями", "claude_code": "Claude через подписку",
           "codex": "Codex через подписку ChatGPT"}
# что можно выбрать в интерфейсе: встроенная — только запасной вариант (порядок: выбранная → другая → встроенная)
PICKABLE = {k: ENGINES[k] for k in ("claude_code", "codex")}


@dataclass
class RewriteRequest:
    url: str
    prompt: str  # текст промпта (с переменными)
    language: str = ""  # код языка рерайта; пусто — язык оригинала
    words: str = DEFAULT_WORDS
    engine: str = "llm"
    llm: tuple[str, str, str] | None = None
    claude_model: str = ""
    codex_model: str = ""
    # Claude недоступен / кончился лимит / оборвался — дописать встроенной нейросетью
    fallback: bool = True
    section: str = "broll"  # broll — рассказы; slides — документальные (страны, звёзды): проверка фактов
    # вместо ссылки — расшифровка, скопированная руками (с таймкодами YouTube / .srt): url тогда не нужен
    source_text: str = ""
    source_name: str = ""  # имя файла — название рерайта


@dataclass
class RewriteOutput:
    source: Source
    story_file: Path
    package_file: Path | None
    words: int
    language: str
    folder: Path
    engine_note: str = ""  # кто писал: «Claude», «встроенная нейросеть (у Claude лимит)»…
    review: str = ""  # итог проверки рерайта


def text_source(raw: str, name: str = "") -> Source:
    """Расшифровка из файла/вставки → исходник: таймкоды и служебные пометки прочь, сеть не нужна."""
    from engine.core.langdetect import detect_language
    from engine.rewrite.source import SourceError
    from engine.rewrite.transcript import clean_transcript

    text = clean_transcript(raw)
    if len(text.split()) < 50:
        raise SourceError("В тексте почти нет речи (меньше 50 слов после удаления таймкодов) — переписывать нечего")
    stem = Path(name).stem.strip() if name else ""
    title = stem or " ".join(text.split()[:6])
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    log(f"Исходник из текста «{title}»: {len(text.split())} слов (таймкоды убраны)")
    return Source(url="", video_id=f"text-{digest}", title=title, channel="", language=detect_language(text) or "",
                  text=text, origin="text")


def safe_name(text: str, limit: int = 70) -> str:
    s = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", text or "")
    s = re.sub(r"\s+", " ", s).strip(" .")
    return (s[:limit].rstrip(" .") or "рассказ")


def rewrite_video(req: RewriteRequest, *, cancel: CancelToken | None = None, progress: ProgressCb | None = None) -> RewriteOutput:
    def step(p: float, msg: str) -> None:
        log(msg)
        if progress:
            progress(p, msg)

    src = text_source(req.source_text, req.source_name) if req.source_text.strip() else \
        fetch_source(req.url, cancel=cancel, progress=lambda p, m: step(p * 0.15, m))
    folder = REWRITES_DIR / (src.video_id or safe_name(src.title))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "оригинал.txt").write_text(src.text, encoding="utf-8")
    (folder / "source.json").write_text(json.dumps({"url": src.url, "title": src.title, "channel": src.channel,
                                                    "language": src.language, "origin": src.origin}, ensure_ascii=False),
                                        encoding="utf-8")
    language = req.language or src.language or "ru"
    prompt = fill(req.prompt, language=language, words=req.words)
    story_file = folder / f"{safe_name(src.title)}.txt"
    package_path = folder / "описание.txt"
    sig = hashlib.sha256("\u241f".join([src.text, prompt, req.words, req.engine]).encode("utf-8")).hexdigest()[:16]
    done = folder / "done.json"
    try:
        same = json.loads(done.read_text(encoding="utf-8")).get("sig") == sig and story_file.is_file()
    except (OSError, ValueError):
        same = False
    if same:
        # повтор задачи после сбоя дальше по цепочке: текст уже переписан — заново не тратим время и лимит
        words = len(story_file.read_text(encoding="utf-8").split())
        step(1.0, f"Рерайт уже готов: {words} слов")
        return RewriteOutput(src, story_file, package_path if package_path.is_file() else None, words, language, folder)
    step(0.2, f"Исходник: {src.words} слов → рерайт на «{language}», {req.words} слов ({SUB_ENGINES.get(req.engine, 'встроенная нейросеть')})")
    # пошаговый промпт (строка «===ШАГ===»): сначала отдельный разбор исходника, потом рерайт с этим разбором
    from engine.rewrite.analysis import FILE as ANALYSIS_FILE, analyze, split_steps, with_analysis

    task, prompt = split_steps(prompt)
    if task:
        step(0.2, "Шаг 1: разбираю исходник — что мешает удержанию, фактические ошибки…")
        found = analyze(task, src.text, folder, engine=req.engine, llm=req.llm, claude_model=req.claude_model,
                        codex_model=req.codex_model, fallback=req.fallback, cancel=cancel)
        prompt = with_analysis(prompt, found)
        step(0.22, f"Шаг 2: рерайт с учётом разбора ({ANALYSIS_FILE})" if found else "Шаг 2: рерайт (разбор не получился)")
    def llm(prefix: str = "") -> Result:
        if not req.llm or not req.llm[0]:
            raise RewriteError("Нет ключа встроенной нейросети (Настройки → LLM)")
        return rewrite_llm(prompt, src.text, words=req.words, llm=req.llm, state_file=folder / "state.json",
                           cancel=cancel, progress=lambda p, m: step(0.2 + 0.75 * p, m), prefix=prefix)

    note = "встроенная нейросеть"
    if req.engine in SUB_ENGINES:
        res, note = _rewrite_claude_or_fallback(req, prompt, src.text, folder, sig, llm, step, cancel)
    else:
        res = llm()
    lo, _hi = word_range(req.words)
    if res.words < lo * 0.6:
        log(f"Рерайт короче нужного: {res.words} слов при цели {req.words}")
    # быстрая проверка: путаница имён, грубые ошибки, сила хука — точечными правками
    from engine.rewrite.review import review_story

    step(0.96, "Проверяю рерайт: имена, смысл, хук…")
    rv = review_story(res.story, section=req.section, llm=req.llm, source=src.text, cast=_cast(folder), cancel=cancel,
                      subs=_review_subs(req))
    res.story = rv.text
    if language == "ru":
        from engine.rewrite.review import fix_yo

        res.story, yo = fix_yo(res.story)
        if yo:
            log(f"Рерайт: поставил «ё» в {yo} словах (ещё, её) — диктор читает верно")
    story_file.write_text(res.story + "\n", encoding="utf-8")
    package_file = None
    if res.package.strip():
        package_file = package_path
        package_file.write_text(res.package.strip() + "\n", encoding="utf-8")
    done.write_text(json.dumps({"sig": sig}), encoding="utf-8")
    words = len(res.story.split())
    step(1.0, f"Рерайт готов: {words} слов ({note}). {rv.summary()}")
    return RewriteOutput(src, story_file, package_file, words, language, folder, note, review=rv.summary())


def _review_subs(req: RewriteRequest) -> list[tuple[str, str]]:
    """Кто проверяет рерайт: подписки по приоритету (с выбранной), без тех, у кого лимит или нет входа."""
    import time

    from engine.core import switches

    if req.engine not in SUB_ENGINES:
        return []
    chain = switches.order_from(req.engine) if req.fallback else [req.engine]
    return [(e, _model_for(req, e)) for e in chain
            if claude_blocked_until(e) <= time.time() and (e == req.engine or _sub_ready(e))]


def _cast(folder: Path) -> list[str]:
    """Герои из плана встроенной нейросети (если писала она) — для сверки имён."""
    try:
        plan = json.loads((folder / "state.json").read_text(encoding="utf-8")).get("plan") or {}
    except (OSError, ValueError):
        return []
    out = []
    for c in plan.get("characters") or []:
        if isinstance(c, dict) and c.get("name"):
            out.append(", ".join(str(c[k]) for k in ("name", "role", "age") if c.get(k)))
    return out


LIMIT_FILE = REWRITES_DIR / "claude_limit.json"
LIMIT_DEFAULT_SEC = 60 * 60  # сервис не сказал, когда лимит снимется — пробуем снова через час


def _limit_file(engine: str = "claude_code") -> Path:
    return LIMIT_FILE if engine == "claude_code" else REWRITES_DIR / f"{engine}_limit.json"


def claude_blocked_until(engine: str = "claude_code") -> float:
    try:
        return float(json.loads(_limit_file(engine).read_text(encoding="utf-8")).get("until") or 0)
    except (OSError, ValueError):
        return 0.0


def _block_claude(until: float | None, engine: str = "claude_code") -> None:
    import time

    f = _limit_file(engine)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"until": until or time.time() + LIMIT_DEFAULT_SEC}), encoding="utf-8")


def _model_for(req: RewriteRequest, engine: str) -> str:
    return req.codex_model if engine == "codex" else req.claude_model


def _sub_ready(engine: str) -> bool:
    """Запасная подписка готова: установлена и выполнен вход (быстрая проверка, без запроса к модели)."""
    from engine.rewrite.engines import sub_ready

    return sub_ready(engine)


def _why(exc: RewriteError) -> str:
    """Почему подписка не смогла — коротко, для уведомления о смене."""
    import time

    if exc.kind == "limit":
        return "кончился лимит подписки" + (f" до {time.strftime('%H:%M', time.localtime(exc.until))}" if exc.until else "")
    if exc.kind == "auth":
        return "не выполнен вход в аккаунт"
    if exc.kind == "install":
        return "программа не установлена"
    return f"ошибка: {str(exc)[:120]}"


def _rewrite_claude_or_fallback(req: RewriteRequest, prompt: str, source: str, folder: Path, sig: str, llm, step, cancel,
                                task: str = "рерайт"):
    """Подписки по приоритету — Claude → Codex (начиная с выбранной), потом встроенная нейросеть. Подписка не
    смогла (лимит, нет входа, ошибка) — следующая; запасная берётся, только если в неё выполнен вход.
    Оборвалась на середине — её начало остаётся, встроенная дописывает с места обрыва. О каждой смене —
    уведомление (что, на что, почему). → (результат, кто писал)."""
    import time

    from engine.core import switches

    who = SUB_ENGINES.get(req.engine, "Claude")
    partial = folder / f"claude_partial_{sig}.txt"
    if partial.is_file() and req.fallback:
        # повтор задачи: начало уже написано — дописываем, подписку второй раз не тратим
        return llm(prefix=partial.read_text(encoding="utf-8")), f"{who} + продолжение встроенной нейросетью"
    chain = switches.order_from(req.engine) if req.fallback else [req.engine]
    prev, reason, first_reason = "", "", ""
    notes: list[str] = []  # почему не подошла каждая подписка — в итоговое уведомление
    for n, engine in enumerate(chain):
        name = SUB_ENGINES.get(engine, engine)
        until = claude_blocked_until(engine)
        if until > time.time():
            why = f"лимит подписки до {time.strftime('%H:%M', time.localtime(until))}"
            if not req.fallback:
                raise RewriteError(f"{name}: {why} — включи запасной вариант или подожди")
            step(0.2, f"У {name} {why}")
            prev, reason = engine, why
            first_reason = first_reason or why
            notes.append(f"{name}: {why}")
            continue
        if n > 0 and not _sub_ready(engine):
            log(f"Рерайт: запасная подписка {name} без входа — пропускаю")
            notes.append(f"{name}: не выполнен вход")
            continue
        if prev:
            switches.report(task, prev, engine, reason)
            step(0.22, f"Пишу через {name}: у {SUB_ENGINES.get(prev, prev)} {reason}")
        try:
            res = rewrite_claude(prompt, source, words=req.words, model=_model_for(req, engine), cancel=cancel,
                                 progress=lambda p, m: step(0.2 + 0.75 * p, m), engine=engine)
        except RewriteError as exc:
            if exc.kind == "limit":
                _block_claude(exc.until, engine)
            if not req.fallback:
                raise
            prev, reason = engine, _why(exc)
            first_reason = first_reason or reason
            notes.append(f"{name}: {reason}")
            step(0.25, f"{name}: {reason} — беру следующий инструмент")
            continue
        if isinstance(res.error, RewriteError) and res.error.kind == "limit":
            _block_claude(res.error.until, engine)
        if not res.complete:
            # сорвался между частями (лимит, сеть) — у текста нет финала: начало сохраняем, дописываем
            partial.write_text(res.story, encoding="utf-8")
            if not req.fallback:
                raise RewriteError(f"{name} оборвал текст на {res.words} словах — включи дописывание встроенной нейросетью")
            cut = _why(res.error) if isinstance(res.error, RewriteError) else "ответ оборвался"
            switches.report(task, engine, "llm", f"{name} оборвался на {res.words} словах ({cut}) — "
                                                 "встроенная дописывает с места обрыва")
            step(0.6, f"{name} оборвался на {res.words} словах — дописываю встроенной нейросетью с места обрыва")
            return llm(prefix=res.story), f"{name} + продолжение встроенной нейросетью"
        return res, name if engine == req.engine else f"{name} (у {who} {first_reason})"
    if prev:
        switches.report(task, prev, "llm", "; ".join(notes) or reason)
    step(0.25, "Подписки недоступны — переписываю встроенной нейросетью")
    return llm(), f"встроенная нейросеть (у {who} {first_reason or reason or 'нет доступа'})"
