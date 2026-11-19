"""Переписывание сценария: встроенная LLM по частям или Claude через подписку (Claude Code).

Встроенная LLM за один ответ длинный текст (7–8 тысяч слов) не пишет, поэтому — по частям:
1. План: LLM читает исходник целиком по промпту пользователя и составляет «паспорт» нового
   рассказа: персонажи (замена имён, если задание её требует), место, стиль и 6–7 частей —
   что в каждой происходит и сколько в ней слов.
2. Части пишутся по очереди. Каждая получает промпт пользователя (правила), паспорт, план всех
   частей, материал исходника для своей части и последние строки предыдущей — история цельная,
   стыки гладкие, имена одинаковые. Короткую часть просим дописать.
3. Сопроводительный пакет (названия, описание, выжимка…) — отдельным запросом по готовому тексту.
Каждый шаг сохраняется (state.json) — после сбоя сервиса работа продолжается с места падения.

Claude Code пишет весь текст за один запрос, от аккаунта, в который выполнен вход на этом Mac.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from engine.core.jsonish import json_dict
from engine.core.cancel import CancelToken
from engine.core.logutil import log
from engine.rewrite.prompts import word_range

ProgressCb = Callable[[float, str], None]
STORY_MARK = "===РАССКАЗ==="
PACKAGE_MARK = "===ПАКЕТ==="
# слов на одну часть встроенной нейросети: столько она пишет за ответ уверенно (проверено: просили 1900 →
# 1780, 2500 → 2751). Меньше частей — реже повторяются длинный промпт, паспорт и план в запросах
LLM_PART_WORDS = 2400
# итог рассказа должен попасть в заданный объём ±10%
WORDS_TOLERANCE = 0.1
CLAUDE_TIMEOUT = 40 * 60

class RewriteError(RuntimeError):
    """kind: limit (кончился лимит подписки Claude), auth, install, other; until — когда лимит снимется."""

    def __init__(self, message: str, *, kind: str = "other", until: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.until = until


_LIMIT = re.compile(r"usage limit|rate limit|limit reached|limit exceeded|out of (?:usage|credits)|quota|too many requests|429|5-hour", re.I)


def _reset_time(text: str) -> float | None:
    """Claude Code пишет «…limit reached|1730000000» — момент, когда лимит снимется."""
    m = re.search(r"\|(\d{10})\b", text or "")
    return float(m.group(1)) if m else None


@dataclass
class Result:
    story: str
    package: str
    complete: bool = True
    error: Exception | None = None  # Claude сорвался посреди частей: текст до срыва есть, причина — здесь

    @property
    def words(self) -> int:
        return len(self.story.split())


def count_words(text: str) -> int:
    return len((text or "").split())


def clean_story(text: str) -> str:
    """Текст на озвучку: без markdown, заголовков, служебных строк и счётчиков слов."""
    lines = []
    for line in (text or "").replace("\r", "").split("\n"):
        s = line.strip()
        if not s:
            lines.append("")
            continue
        if re.match(r"^(#+\s|\*\*.*\*\*$|(часть|глава|part|chapter)\s+\w+\s*[:.]?$)", s, re.I):
            continue
        if re.match(r"^(количество слов|word count|слов\s*:)", s, re.I):
            continue
        lines.append(re.sub(r"[*_`#]+", "", s))
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


# ---------- Claude через подписку ----------


def claude_binary() -> str | None:
    """Где Claude Code: в PATH или в стандартных местах установки."""
    if os.environ.get("STORY_VIDEO_SUBS_OFF"):  # тесты: настоящие подписки не трогаем
        return None
    found = shutil.which("claude")
    if found:
        return found
    for p in (Path.home() / ".local/bin/claude", Path.home() / ".claude/local/claude",
              Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude")):
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


CLAUDE_SYSTEM = ("Ты — помощник программы Story Video. Выполняй задание из сообщения пользователя точно и полностью, "
                 "на языке задания. Отвечай только самим результатом в требуемом формате — без вступлений, пояснений "
                 "и markdown-обёртки, если формат её не требует. Файлы не создавай.")


def run_claude(prompt: str, *, model: str = "", timeout: float = CLAUDE_TIMEOUT, cancel: CancelToken | None = None,
               images: list[tuple[bytes, str]] | None = None) -> str:
    """Один запрос к Claude Code в фоновом режиме (claude -p) → текст ответа. images — [(байты, расширение)]:
    картинки кладутся во временную папку, Claude смотрит их инструментом чтения (только он и разрешён)."""
    binary = claude_binary()
    if binary is None:
        raise RewriteError("Claude Code не установлен — Настройки → Переписывание → «Установить и войти»", kind="install")
    # без инструментов: иначе Claude пишет рассказ в файл и упирается в лимит ходов
    cmd = [binary, "-p", "--output-format", "json", "--tools", "Read" if images else "", "--max-turns",
           "6" if images else "3"]
    if images:
        cmd += ["--allowedTools", "Read"]
    if model:
        cmd += ["--model", model]
    # свой короткий системный промпт вместо служебного промпта Claude Code (он про код, файлы и инструменты):
    # на каждый вызов ~6,5 тыс. токенов → ~0,8 тыс. (замер 14.11). Задание и правила — целиком в самом запросе
    cmd += ["--system-prompt", CLAUDE_SYSTEM]
    env = {**os.environ, "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000"}
    # пустая временная папка: Claude Code не видит файлов проекта и ничего не трогает
    with tempfile.TemporaryDirectory() as tmp:
        names = _save_images(Path(tmp), images)
        if names:
            prompt = ("Сначала посмотри картинки (прочитай файлы): " + ", ".join(names) + ". Потом выполни задание.\n\n"
                      + prompt)
        proc = subprocess.Popen(cmd, cwd=tmp, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        if cancel is not None:
            cancel.register(_Killer(proc))
        try:
            out, err = proc.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RewriteError(f"Claude не ответил за {timeout / 60:.0f} мин") from None
    if cancel is not None:
        cancel.check()
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        data = {}
    text = str(data.get("result") or "")
    if proc.returncode != 0 or data.get("is_error") or not text:
        why = text or (err or out or "").strip()[-400:]
        low = why.lower()
        if _LIMIT.search(why):
            raise RewriteError(f"Claude: кончился лимит подписки ({why[:160]})", kind="limit", until=_reset_time(why))
        if data.get("subtype") == "error_max_turns":
            raise RewriteError("Claude Code ушёл в инструменты вместо ответа текстом")
        if "login" in low or "auth" in low or "not logged" in low or "invalid api key" in low:
            raise RewriteError("Claude Code: не выполнен вход в аккаунт — Настройки → Переписывание → «Войти»", kind="auth")
        raise RewriteError(f"Claude Code: {why[:300] or 'пустой ответ'}")
    return text


# ---------- Codex (ChatGPT) через подписку ----------

CODEX_TIMEOUT = CLAUDE_TIMEOUT
SUB_ENGINES = {"claude_code": "Claude", "codex": "Codex (ChatGPT)"}  # тексты по подписке через их CLI


def codex_binary() -> str | None:
    """Где Codex CLI (OpenAI): в PATH или в стандартных местах установки (Homebrew, npm)."""
    if os.environ.get("STORY_VIDEO_SUBS_OFF"):  # тесты: настоящие подписки не трогаем
        return None
    found = shutil.which("codex")
    if found:
        return found
    for p in (Path("/opt/homebrew/bin/codex"), Path("/usr/local/bin/codex"), Path.home() / ".local/bin/codex",
              Path.home() / ".npm-global/bin/codex"):
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def _jwt_claims(token: str) -> dict:
    """Полезная часть id_token (без проверки подписи — только показать, чей вход)."""
    import base64

    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return {}


def codex_account() -> dict:
    """Чей вход в Codex: почта и тариф ChatGPT из ~/.codex/auth.json (только чтение; токены не отдаём)."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        data = json.loads((home / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    claims = _jwt_claims(str((data.get("tokens") or {}).get("id_token") or ""))
    auth = claims.get("https://api.openai.com/auth") or {}
    out = {"email": str(claims.get("email") or ""), "plan": str(auth.get("chatgpt_plan_type") or "")}
    if not out["email"] and data.get("OPENAI_API_KEY"):
        out["method"] = "api_key"
    return out


def codex_status() -> dict:
    """Установлен ли Codex и выполнен ли вход (быстро, без запроса к модели)."""
    binary = codex_binary()
    if binary is None:
        return {"installed": False, "logged_in": False}
    try:
        proc = subprocess.run([binary, "login", "status"], capture_output=True, text=True, timeout=20)
        text = f"{proc.stdout}\n{proc.stderr}".strip()
        logged = proc.returncode == 0 and "not logged" not in text.lower()
    except (OSError, subprocess.SubprocessError) as exc:
        text, logged = str(exc), False
    return {"installed": True, "logged_in": logged, "path": binary, "detail": text.splitlines()[0][:120] if text else "",
            **(codex_account() if logged else {})}


def codex_models() -> list[dict]:
    """Модели, которые Codex показывает в выборе (живой каталог CLI), по порядку важности."""
    binary = codex_binary()
    if binary is None:
        return []
    try:
        proc = subprocess.run([binary, "debug", "models"], capture_output=True, text=True, timeout=30)
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    items = data.get("models", data) if isinstance(data, dict) else data
    rows = [m for m in items or [] if isinstance(m, dict) and m.get("slug") and m.get("visibility") == "list"]
    rows.sort(key=lambda m: m.get("priority") or 999)
    return [{"id": m["slug"], "label": f"{m.get('display_name') or m['slug']} — {m.get('description') or ''}".strip(" —")}
            for m in rows]


_CODEX_ONLY_TEXT = ("\n\n(Отвечай только текстом прямо в ответе: не запускай команды, не создавай и не читай файлы.)")


def _save_images(folder: Path, images: list[tuple[bytes, str]] | None) -> list[str]:
    names = []
    for n, (data, ext) in enumerate(images or [], 1):
        name = f"image_{n}.{(ext or 'jpg').lstrip('.')}"
        (folder / name).write_bytes(data)
        names.append(name)
    return names


def run_codex(prompt: str, *, model: str = "", timeout: float = CODEX_TIMEOUT, cancel: CancelToken | None = None,
              images: list[tuple[bytes, str]] | None = None) -> str:
    """Один запрос к Codex CLI в фоновом режиме (codex exec) от аккаунта ChatGPT → текст ответа.
    Пустая временная папка и песочница «только чтение»: Codex не видит файлов проекта и ничего не меняет."""
    binary = codex_binary()
    if binary is None:
        raise RewriteError("Codex не установлен — Настройки → Переписывание → «Установить Codex»", kind="install")
    st = codex_status()
    if not st.get("logged_in"):
        # без входа Codex 5 раз переподключается и падает с 401 — проверяем заранее
        raise RewriteError("Codex: не выполнен вход в аккаунт ChatGPT — Настройки → Переписывание → «Войти»", kind="auth")
    with tempfile.TemporaryDirectory() as tmp:
        out_file = Path(tmp) / "answer.txt"
        work = Path(tmp) / "work"
        work.mkdir()
        cmd = [binary, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--ephemeral", "--color", "never",
               "-C", str(work), "-o", str(out_file)]
        if model:
            cmd += ["-m", model]
        for name in _save_images(Path(tmp), images):
            cmd += ["-i", str(Path(tmp) / name)]
        cmd.append("-")  # задание — из stdin (длинный промпт не влезает в аргументы)
        proc = subprocess.Popen(cmd, cwd=work, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        if cancel is not None:
            cancel.register(_Killer(proc))
        try:
            out, err = proc.communicate(prompt + _CODEX_ONLY_TEXT, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RewriteError(f"Codex не ответил за {timeout / 60:.0f} мин") from None
        if cancel is not None:
            cancel.check()
        try:
            text = out_file.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
    if proc.returncode == 0 and text:
        return text
    why = "\n".join(ln for ln in f"{err}\n{out}".splitlines() if ln.strip() and "Reconnecting" not in ln)[-500:].strip()
    low = why.lower()
    if _LIMIT.search(why) or "usage limit" in low:
        raise RewriteError(f"Codex: кончился лимит подписки ChatGPT ({why[-160:]})", kind="limit",
                           until=_codex_reset(why))
    if "401" in why or "unauthorized" in low or "not logged" in low or "login" in low:
        raise RewriteError("Codex: вход в ChatGPT не действует — Настройки → Переписывание → «Войти»", kind="auth")
    raise RewriteError(f"Codex: {why[-300:] or 'пустой ответ'}")


def _codex_reset(text: str) -> float | None:
    """«…try again in 2 hours 13 minutes» / «in 45 minutes» → когда снимется лимит."""
    import time as _time

    for m in re.finditer(r"\bin\s+(?:(\d+)\s*(?:hours?|hrs?|h)\b\s*)?(?:(\d+)\s*(?:minutes?|mins?|m)\b)?",
                         text or "", re.I):
        if m.group(1) or m.group(2):
            return _time.time() + int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60
    return None


def run_sub(engine: str, prompt: str, *, model: str = "", cancel: CancelToken | None = None,
            timeout: float | None = None, images: list[tuple[bytes, str]] | None = None) -> str:
    """Запрос к сервису по подписке: Claude Code или Codex (images — картинки к заданию)."""
    extra = {**({"timeout": timeout} if timeout else {}), **({"images": images} if images else {})}
    if engine == "codex":
        return run_codex(prompt, model=model, cancel=cancel, **extra)
    return run_claude(prompt, model=model, cancel=cancel, **extra)


# ---------- общий порядок для всех задач: Claude → Codex → встроенная ----------

_READY_TTL = 60.0  # готовность подписки (вход) перепроверяем не чаще раза в минуту — запросов много
_ready: dict[str, tuple[float, bool]] = {}


def sub_available(engine: str) -> bool:
    """Подписку можно звать сейчас: установлена, выполнен вход, лимит не исчерпан."""
    import time

    from engine.rewrite.run import claude_blocked_until

    if claude_blocked_until(engine) > time.time():
        return False
    now = time.monotonic()
    hit = _ready.get(engine)
    if hit is None or now - hit[0] > _READY_TTL:
        hit = (now, sub_ready(engine))
        _ready[engine] = hit
    return hit[1]


def note_limit(engine: str, exc: "RewriteError") -> None:
    """Лимит подписки кончился — запомнить (общий учёт для рерайта и всех остальных задач)."""
    if exc.kind == "limit":
        from engine.rewrite.run import _block_claude

        _block_claude(exc.until, engine)
    elif exc.kind in ("auth", "install"):
        _ready[engine] = (__import__("time").monotonic(), False)


def sub_ready(engine: str) -> bool:
    """Подписка готова к запросу: установлена и выполнен вход (быстро, без запроса к модели)."""
    try:
        return bool((codex_status() if engine == "codex" else claude_account()).get("logged_in"))
    except Exception:  # noqa: BLE001
        return False


def claude_account() -> dict:
    """Чей вход в Claude Code (claude auth status): почта и тариф; без входа — logged_in False."""
    binary = claude_binary()
    if binary is None:
        return {"installed": False, "logged_in": False}
    try:
        proc = subprocess.run([binary, "auth", "status", "--json"], capture_output=True, text=True, timeout=20)
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        data = {}
    return {"installed": True, "path": binary, "logged_in": bool(data.get("loggedIn")),
            "email": str(data.get("email") or ""), "plan": str(data.get("subscriptionType") or ""),
            "method": str(data.get("authMethod") or "")}


class _Killer:
    """Для CancelToken.register: «Стоп» убивает процесс Claude Code."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()


PLAN_MARK = "===ПЛАН==="
# столько слов Claude надёжно пишет за один ответ; длиннее — «сжимается» (3–6 тыс. вместо 7–8)
CLAUDE_PART_WORDS = 2800
CLAUDE_MAX_CALLS = 7
_FORMAT_HEAD = """

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ФОРМАТ ОТВЕТА ДЛЯ ПРОГРАММЫ (важнее любых слов выше про файлы, ссылки, объём и формат итогового ответа)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Рассказ пишется по частям за несколько ответов, объём считает программа — сам слова не считай и не пиши их число.
Файлы не создавай и ссылок не давай — всё прямо в ответе. Пиши полноценными сценами с диалогами и деталями,
а не пересказом: краткое содержание или план вместо текста не годятся.
"""
_CLAUDE_FIRST = _FORMAT_HEAD + """Сейчас:
1) короткий план ВСЕЙ истории (10–15 строк: герои с именами и возрастом, место, предмет конфликта, повороты
по порядку, финал) — по нему будут написаны следующие части;
2) ПЕРВАЯ часть рассказа — около {want} слов (от {min} до {max}), начиная с хука. Историю на ней НЕ заканчивай,
остановись на естественной паузе. Сопроводительный пакет (названия, описание) сейчас не пиши.
Ровно так:
{plan_mark}
<план>
{story_mark}
<текст первой части>
"""
_CLAUDE_NEXT = _FORMAT_HEAD + """Продолжи рассказ ровно с места, где он остановился (текст ниже), строго по плану, тем же стилем, без
повторов уже написанного, без вступлений и пересказа. Следующая часть — около {want} слов (от {min} до {max}, больше не надо).
{ending}
Ровно так:
{story_mark}
<текст этой части>{package}
"""
_CLAUDE_MIDDLE = "Историю пока не заканчивай: веди её дальше по плану и остановись на естественной паузе."
_CLAUDE_LAST = ("Это последняя часть: доведи историю по плану до завершённого финала. После рассказа — весь "
                "сопроводительный пакет из задания (кроме подсчёта слов).")
_CLAUDE_WHOLE = _FORMAT_HEAD + """Напиши весь рассказ целиком — около {want} слов (от {min} до {max}), потом весь сопроводительный пакет
из задания (кроме подсчёта слов). Ровно так:
{story_mark}
<весь рассказ>
{package_mark}
<пакет>
"""
# пункты нумерованного списка (тире не считаем: с него начинаются реплики диалога)
_OUTLINE_LINE = re.compile(r"^\s*\d+[.)]\s", re.M)


def looks_like_outline(text: str) -> bool:
    """Пересказ/план вместо рассказа: много пунктов списка на небольшой текст."""
    return len(_OUTLINE_LINE.findall(text)) >= max(4, count_words(text) // 150)


def _split_marks(raw: str) -> tuple[str, str, str]:
    """(план, текст, пакет) из ответа с метками."""
    raw = raw or ""
    plan = ""
    if PLAN_MARK in raw:
        plan = raw.split(PLAN_MARK, 1)[1].split(STORY_MARK, 1)[0].strip()
    body = raw.split(STORY_MARK, 1)[1] if STORY_MARK in raw else raw
    story, _, package = body.partition(PACKAGE_MARK)
    return plan, clean_story(story), package.strip()


def rewrite_claude(prompt: str, source: str, *, words: str = "", model: str = "", cancel: CancelToken | None = None,
                   progress: ProgressCb | None = None, engine: str = "claude_code") -> Result:
    """Claude по частям ~CLAUDE_PART_WORDS слов: план + первая часть, дальше продолжения. Объём следующей части —
    по остатку до цели (недобор/перебор выравнивают следующие части — без переписывания), последняя часть —
    повтор, только если итог вне ±10%. Пересказ вместо текста — повтор. Срыв посреди (лимит, сеть) —
    возвращаем написанное (complete=False, error): дописывает встроенная.

    Неизменное (промпт, исходник/план, уже написанное) — в начале запроса, указания этой части — в конце:
    одинаковое начало Claude берёт из кеша, лимит подписки тратится меньше."""
    lo, hi = word_range(words)
    target = (lo + hi) // 2
    if target <= CLAUDE_PART_WORDS * 1.3:
        return _claude_whole(prompt, source, lo, hi, model, cancel, progress, engine)
    plan, written, package = "", "", ""
    n_parts = max(2, math.ceil(target / CLAUDE_PART_WORDS))
    # сколько Claude реально пишет от просимого: пишет меньше/больше — следующие части просим с поправкой
    ratio = 1.0
    for call in range(CLAUDE_MAX_CALLS):
        done = count_words(written)
        remaining = target - done
        last = call > 0 and remaining <= CLAUDE_PART_WORDS * 1.3 or call == CLAUDE_MAX_CALLS - 1
        if call == 0:
            want = target // n_parts
        elif last:
            want = remaining
        else:
            want = remaining // max(1, math.ceil(remaining / CLAUDE_PART_WORDS))
        want = max(400, want)
        ask_words = int(want / ratio)
        if progress:
            progress(0.25 + 0.7 * min(1.0, done / target),
                     f"{SUB_ENGINES.get(engine, 'Claude')} пишет {'последнюю' if last else f'{call + 1}-ю'} часть (~{want} слов, готово {done} из ~{target})…")
        if call == 0:
            head = f"{prompt}\n\nИСХОДНИК:\n{source}"
            fmt = _CLAUDE_FIRST.format(want=ask_words, min=int(ask_words * 0.85), max=int(ask_words * 1.15),
                                       plan_mark=PLAN_MARK, story_mark=STORY_MARK)
        else:
            head = f"{prompt}\n\nПЛАН ИСТОРИИ:\n{plan}\n\nУЖЕ НАПИСАНО:\n{written}"
            fmt = _CLAUDE_NEXT.format(want=ask_words, min=int(ask_words * 0.85), max=int(ask_words * 1.15), story_mark=STORY_MARK,
                                      ending=_CLAUDE_LAST if last else _CLAUDE_MIDDLE,
                                      package=f"\n{PACKAGE_MARK}\n<пакет>" if last else "")
        try:
            part_plan, text, pkg = _claude_part(head + fmt, ask_words, model, cancel, engine)
            if last and not _within(done + count_words(text), lo, hi):
                # финал: компенсировать уже нечем — один повтор с точным объёмом
                fix = max(400, int((target - done) / ratio))
                log(f"Claude: итог {done + count_words(text)} слов вне цели {lo}–{hi} — переписываю финал на ~{fix}")
                note = (f"\n\nПРОШЛЫЙ ВАРИАНТ ЭТОЙ ЧАСТИ БЫЛ {count_words(text)} СЛОВ — нужно около {fix} "
                        f"(от {int(fix * 0.9)} до {int(fix * 1.1)}). Напиши её заново полностью.")
                _p2, text2, pkg2 = _claude_part(head + fmt + note, fix, model, cancel, engine)
                if abs(done + count_words(text2) - target) < abs(done + count_words(text) - target):
                    text, pkg = text2, pkg2 or pkg
        except RewriteError as exc:
            if not written:
                raise
            log(f"Claude сорвался на {done} словах ({exc}) — дальше допишет встроенная нейросеть")
            return Result(written, "", complete=False, error=exc)
        if call == 0:
            plan = part_plan or "(план не прислан — продолжай по смыслу уже написанного)"
        written = f"{written} {text}".strip()
        ratio = min(1.4, max(0.6, count_words(text) / ask_words * ratio))
        log(f"Claude: часть {call + 1} — {count_words(text)} слов, всего {count_words(written)} из ~{target}")
        if last:
            package = pkg
            break
    if not _within(count_words(written), lo, hi):
        log(f"Claude: вышло {count_words(written)} слов при цели {words}")
    return Result(written, package, complete=bool(package))


def _claude_part(ask: str, want: int, model: str, cancel: CancelToken | None,
                 engine: str = "claude_code") -> tuple[str, str, str]:
    """Один ответ Claude; пересказ/план вместо текста или почти пусто — один повтор с замечанием.
    Объём здесь не правим: его выравнивают следующие части (а финал — rewrite_claude)."""
    plan, text, pkg = _split_marks(run_sub(engine, ask, model=model, cancel=cancel))
    if count_words(text) < want * 0.4 or looks_like_outline(text):
        why = "это пересказ/план, а нужен полноценный текст сценами" if looks_like_outline(text) else \
            f"вышло всего {count_words(text)} слов, а нужно около {want}"
        log(f"Claude: часть не подошла ({why}) — прошу переписать")
        plan2, text2, pkg2 = _split_marks(run_sub(
            engine, f"{ask}\n\nПРОШЛЫЙ ОТВЕТ НЕ ПОДОШЁЛ: {why}. Напиши эту часть заново полностью, около {want} слов.",
            model=model, cancel=cancel))
        if count_words(text2) > count_words(text) and not looks_like_outline(text2):
            plan, text, pkg = plan2 or plan, text2, pkg2 or pkg
    if count_words(text) < 150 or looks_like_outline(text):
        raise RewriteError(f"{SUB_ENGINES.get(engine, 'Claude')} вместо рассказа вернул пересказ или слишком короткий текст")
    return plan, text, pkg


def _claude_whole(prompt: str, source: str, lo: int, hi: int, model: str, cancel: CancelToken | None,
                  progress: ProgressCb | None, engine: str = "claude_code") -> Result:
    """Короткий рассказ (до ~3,5 тыс. слов) — одним ответом; вне ±10% цели — один повтор с точным объёмом."""
    target = (lo + hi) // 2
    if progress:
        progress(0.3, f"{SUB_ENGINES.get(engine, 'Claude')} пишет рассказ (~{target} слов)…")
    head = f"{prompt}\n\nИСХОДНИК:\n{source}"
    fmt = _CLAUDE_WHOLE.format(want=target, min=int(target * 0.85), max=int(target * 1.1), story_mark=STORY_MARK,
                               package_mark=PACKAGE_MARK)
    _plan, text, pkg = _claude_part(head + fmt, target, model, cancel, engine)
    if not _within(count_words(text), lo, hi):
        log(f"Claude: {count_words(text)} слов вне цели {lo}–{hi} — прошу переписать в нужном объёме")
        note = (f"\n\nПРОШЛЫЙ ВАРИАНТ БЫЛ {count_words(text)} СЛОВ — нужно около {target} "
                f"(от {int(target * 0.9)} до {int(target * 1.1)}). Напиши заново полностью.")
        _p2, text2, pkg2 = _claude_part(head + fmt + note, target, model, cancel, engine)
        if abs(count_words(text2) - target) < abs(count_words(text) - target):
            text, pkg = text2, pkg2 or pkg
    return Result(text, pkg, complete=bool(pkg))


# ---------- встроенная LLM по частям ----------

_PLAN = """Ты — редактор, который готовит план рерайта по заданию автора. Сам рассказ сейчас НЕ пиши.
Прочитай задание и исходник и составь паспорт нового текста строго по заданию (если задание велит
менять имена, место, детали — уже в паспорте придумай новые; если велит сохранить суть и людей —
сохрани). Ответь только JSON:
{"names": {"старое имя из исходника": "новое имя"}  — только если задание требует замены имён, иначе {},
 "characters": [{"name": "...", "role": "...", "age": "...", "notes": "..."}],
 "setting": "где и когда происходит",
 "style": "главное о стиле и тоне по заданию",
 "parts": [{"n": 1, "beats": "что именно происходит в этой части, 3–6 предложений", "words": число}]}
Частей ровно {parts}. Сумма words — {lo}–{hi}. Первая часть — сильное начало по заданию, последняя — финал."""

_PLAN_CONT = """Ты — редактор, который готовит план рерайта по заданию автора. Начало текста уже написано
(ниже, «УЖЕ НАПИСАНО») — оно оборвалось. Паспорт возьми ИЗ НАПИСАННОГО: те же имена, место, стиль, факты —
ничего не придумывай заново. Составь план ТОЛЬКО оставшихся частей, чтобы продолжить ровно с места обрыва
и довести текст до конца по заданию и исходнику. Ответь только JSON:
{"names": {}, "characters": [{"name": "...", "role": "...", "age": "...", "notes": "..."}],
 "setting": "...", "style": "...",
 "parts": [{"n": 1, "beats": "что происходит в этой части, 3–6 предложений", "words": число}]}
Частей ровно {parts}. Сумма words — {lo}–{hi}. Последняя часть — финал."""

_PART = """Сейчас пишешь ЧАСТЬ {n} из {total} этого текста. Правила задания выше действуют полностью,
кроме объёма: объём ЭТОЙ части — около {words} слов (от {min_words} до {max_words}). Слова считает программа —
сам их число не пиши.
Пиши только текст этой части — без заголовков, номеров, пояснений и пометок, сразу на озвучку.
{position}
Держись паспорта и плана: те же имена, возрасты, места, факты. Не повторяй уже рассказанное."""

_FIRST = ("Это начало: сильное вступление (хук) из самого острого момента ИМЕННО этой истории — без шаблонов вроде "
          "требования подписать документы, нотариуса или угрозы выгнать из квартиры, если сюжет не об этом; "
          "и всё, что задание прямо требует поставить в начале текста (если задание запрещает призывы — никаких призывов).")
_ONLY = "Это весь текст целиком: от хука до завершённого финала, и в конце всё, что задание прямо требует в конце текста."
_MIDDLE = "Это середина: продолжай ровно с того места, где закончилась предыдущая часть, без вступлений, приветствий и призывов. Историю не заканчивай."
_LAST = "Это последняя часть: продолжай с места предыдущей, приведи историю к финалу и добавь в конце всё, что задание прямо требует в конце текста (если задание запрещает призывы — никаких призывов)."


def _within(words: int, lo: int, hi: int) -> bool:
    """Объём попадает в заданный диапазон с допуском ±10%."""
    return lo * (1 - WORDS_TOLERANCE) <= words <= hi * (1 + WORDS_TOLERANCE)


_json = json_dict


def _split_source(text: str, n: int) -> list[str]:
    """Исходник на n примерно равных кусков по границам предложений (материал для частей)."""
    sents = re.split(r"(?<=[.!?…])\s+", text) if re.search(r"[.!?…]", text) else text.split(" ")
    total = sum(len(s.split()) for s in sents) or 1
    out, cur, acc = [], [], 0
    for s in sents:
        cur.append(s)
        acc += len(s.split())
        if acc >= total * (len(out) + 1) / n and len(out) < n - 1:
            out.append(" ".join(cur))
            cur = []
    out.append(" ".join(cur))
    return out + [""] * (n - len(out))


class _State:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        try:
            self.data = json.loads(path.read_text(encoding="utf-8")) if path and path.is_file() else {}
        except (OSError, ValueError):
            self.data = {}

    def save(self) -> None:
        if self.path:
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")


def rewrite_llm(
    prompt: str,
    source: str,
    *,
    words: str,
    llm: tuple[str, str, str],
    state_file: Path | None = None,
    cancel: CancelToken | None = None,
    progress: ProgressCb | None = None,
    prefix: str = "",
) -> Result:
    """prefix — начало, которое уже написал другой способ (Claude оборвался): продолжаем с места обрыва."""
    from engine.slides.llm_client import LLMClient

    lo, hi = word_range(words)
    written = trim_to_sentence(clean_story(prefix)) if prefix.strip() else ""
    n_written = count_words(written)
    full_lo, full_hi = lo, hi  # объём всего рассказа (с уже написанным началом) — для итоговой проверки
    if written:
        # осталось дописать: объём за вычетом готового (на финал — хотя бы 300 слов)
        lo, hi = max(300, lo - n_written), max(500, hi - n_written)
        cut = int(len(source) * n_written / max(1, n_written + (lo + hi) / 2))
        source_rest = source[max(0, cut - 2000):]  # исходник дальше того места, до которого дописано
    else:
        source_rest = source
    n_parts = max(2 if written else 1, math.ceil(((lo + hi) / 2) / LLM_PART_WORDS))
    key, base, model = llm
    st = _State(state_file)
    sig = hashlib.sha256(f"{source}\u241f{prompt}\u241f{words}\u241f{written}".encode("utf-8")).hexdigest()[:16]
    if st.data.get("sig") != sig:
        st.data = {"sig": sig}
    client = LLMClient(key, model, base)
    if cancel is not None:
        cancel.register(client)

    def step(p: float, msg: str) -> None:
        log(msg)
        if progress:
            progress(p, msg)

    try:
        # 1. план
        plan = st.data.get("plan")
        if not plan:
            if written:
                step(0.1, f"Переписывание: продолжаю с места обрыва ({n_written} слов уже готово) — план оставшегося…")
                raw = client.complete(label="rewrite_plan", system=_PLAN_CONT.replace("{parts}", str(n_parts))
                                      .replace("{lo}", str(lo)).replace("{hi}", str(hi)),
                                      user=f"ЗАДАНИЕ АВТОРА:\n{prompt}\n\nУЖЕ НАПИСАНО:\n{written[-40000:]}\n\n"
                                      f"ИСХОДНИК (дальше этого места):\n{source_rest[:60000]}")
            else:
                step(0.1, "Переписывание: план нового текста…")
                raw = client.complete(label="rewrite_plan", system=_PLAN.replace("{parts}", str(n_parts))
                                      .replace("{lo}", str(lo)).replace("{hi}", str(hi)),
                                      user=f"ЗАДАНИЕ АВТОРА:\n{prompt}\n\nИСХОДНИК:\n{source[:90000]}")
            plan = _json(raw)
            parts = [p for p in plan.get("parts") or [] if isinstance(p, dict)]
            if len(parts) < min(n_parts, 2):
                raise RewriteError("LLM не составила план рерайта — попробуй ещё раз или через Claude")
            plan["parts"] = parts
            st.data["plan"] = plan
            st.save()
        parts = plan["parts"]
        target = (lo + hi) // 2
        material = _split_source(source_rest, len(parts))
        passport = json.dumps({k: plan.get(k) for k in ("names", "characters", "setting", "style")}, ensure_ascii=False)
        outline = "\n".join(f"{p.get('n', i + 1)}. {p.get('beats', '')}" for i, p in enumerate(parts))
        texts: list[str] = list(st.data.get("texts") or [])

        def ask_part(i: int, want: int, note: str = "") -> str:
            last = i == len(parts) - 1
            first = i == 0 and not written
            position = f"{_FIRST} {_ONLY}" if first and last else (_LAST if last else (_FIRST if first else _MIDDLE))
            system = prompt + "\n\n" + _PART.format(n=i + 1, total=len(parts), words=want, min_words=int(want * 0.85),
                                                    max_words=int(want * 1.15), position=position)
            # продолжение чужого начала — побольше образца, чтобы стиль и интонация совпали
            prev = texts[-1] if texts else written
            tail = " ".join(prev.split()[-(600 if written and not texts else 180):]) if prev else ""
            user = (f"ПАСПОРТ ТЕКСТА:\n{passport}\n\nПЛАН ВСЕХ ЧАСТЕЙ:\n{outline}\n\n"
                    f"ЧАСТЬ {i + 1} — ЧТО ПРОИСХОДИТ:\n{parts[i].get('beats', '')}\n\n"
                    f"МАТЕРИАЛ ИСХОДНИКА ДЛЯ ЭТОЙ ЧАСТИ (используй строго по заданию):\n{material[i][:30000]}\n\n"
                    + (f"ПОСЛЕДНИЕ СТРОКИ ПРЕДЫДУЩЕЙ ЧАСТИ (продолжай прямо отсюда):\n…{tail}\n\n" if tail else "")
                    + f"Напиши часть {i + 1}.{note}")
            return clean_story(client.complete(label=f"rewrite_part_{i + 1}", system=system, user=user))

        # 2. части по очереди; объём каждой — по остатку до цели: недобор/перебор прошлых частей
        # выравнивают следующие, переписывать их не нужно (лишние токены)
        for i in range(len(texts), len(parts)):
            done = sum(count_words(t) for t in texts)
            left = len(parts) - i
            want = max(300, (target - done) // left)
            step(0.15 + 0.7 * i / len(parts), f"Переписывание: часть {i + 1} из {len(parts)} (~{want} слов)…")
            text = ask_part(i, want)
            if count_words(text) < want * 0.4:
                # отказ или обрывок вместо части — один повтор
                step(0.15 + 0.7 * (i + 0.5) / len(parts), f"Переписывание: часть {i + 1} почти пустая — повторяю")
                text = max(text, ask_part(i, want), key=count_words)
            if i == len(parts) - 1:
                # последняя часть: компенсировать уже нечем — итог вне ±10% цели, повторяем один раз
                total = n_written + done + count_words(text)
                if not _within(total, full_lo, full_hi):
                    fix = (full_lo + full_hi) // 2 - n_written - done  # ровно остаток до середины цели
                    step(0.85, f"Переписывание: итог {total} слов вне цели {full_lo}–{full_hi} — переписываю финал на ~{fix} слов")
                    again = ask_part(i, max(300, fix), f" Прошлый вариант этой части был {count_words(text)} слов — "
                                                        f"нужно около {max(300, fix)}.")
                    if abs(n_written + done + count_words(again) - (full_lo + full_hi) / 2) < abs(total - (full_lo + full_hi) / 2):
                        text = again
            texts.append(_fix_names(text, plan.get("names") or {}))
            st.data["texts"] = texts
            st.save()

        story = "\n\n".join([written, *texts] if written else texts)

        # 3. сопроводительный пакет
        package = st.data.get("package")
        if package is None:
            step(0.9, "Переписывание: названия, описание, выжимка…")
            try:
                # для названий и описания хватает паспорта, плана по частям, начала и финала — весь текст
                # (десятки тысяч символов) в запрос не нужен
                words_all = story.split()
                package = client.complete(
                    label="rewrite_package", system=prompt,
                    user=("Текст уже написан. Не переписывай его. Выполни только ту часть задания, что идёт ПОСЛЕ "
                          "самого текста: названия, описание, выжимку, фразы для обложки и всё остальное, что задание просит "
                          f"кроме текста. Объём текста: {len(words_all)} слов.\n\nПАСПОРТ:\n{passport}\n\n"
                          f"ЧТО ПРОИСХОДИТ ПО ЧАСТЯМ:\n{outline}\n\nНАЧАЛО ТЕКСТА:\n{' '.join(words_all[:400])}\n\n"
                          f"ФИНАЛ ТЕКСТА:\n{' '.join(words_all[-350:])}"),
                ).strip()
            except Exception as exc:  # без пакета текст всё равно готов
                log(f"Переписывание: пакет (названия, описание) не получился ({exc})")
                package = ""
            st.data["package"] = package
            st.save()
    finally:
        client.close()
    return Result(story, package)


def trim_to_sentence(text: str) -> str:
    """Оборванный текст — до конца последнего законченного предложения (недописанный хвост — прочь)."""
    text = text.rstrip()
    m = list(re.finditer(r"[.!?…»”\"](?=\s|$)", text))
    return text[: m[-1].end()] if m else text


def _fix_names(text: str, names: dict) -> str:
    """Старое имя из исходника всё-таки проскочило — заменяем на новое из паспорта."""
    for old, new in (names or {}).items():
        old, new = str(old).strip(), str(new).strip()
        if len(old) >= 3 and new and old != new and re.search(rf"\b{re.escape(old)}\b", text):
            log(f"Переписывание: заменяю старое имя «{old}» → «{new}»")
            text = re.sub(rf"\b{re.escape(old)}\b", new, text)
    return text
