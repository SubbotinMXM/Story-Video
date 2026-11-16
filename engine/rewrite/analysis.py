"""Пошаговый промпт рерайта: строка «===ШАГ===» делит его на разбор и рерайт.

Шаг 1 (текст до разделителя) — отдельный запрос: модель только разбирает исходник (что вредит удержанию,
фактические ошибки, слабые места) и ничего не переписывает. Разбор сохраняется в папку рерайта
(«разбор.txt») и уходит в шаг 2 (текст после разделителя) с требованием исправить всё найденное.
Нет разделителя — промпт идёт одним заданием, как раньше.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from engine.core.cancel import CancelToken
from engine.core.logutil import log

STEP_MARK = "===ШАГ==="
_MARK = re.compile(r"^\s*=+\s*ШАГ\s*=+\s*$", re.M)
FILE = "разбор.txt"
# разбор необязателен: встроенный сервис тормозит — не держим ролик дольше этого
LLM_TIMEOUT = 12 * 60
BUSY_WAIT = 3 * 60

_FORMAT = """

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ЭТО ШАГ 1 ИЗ 2 — ТОЛЬКО РАЗБОР
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Сейчас текст НЕ переписывай и рассказ не пиши. Дай разбор исходника по пунктам: что именно плохо влияет на
удержание и вовлечение (где затянуто, где провисает интерес, что повторяется, где слабое начало и переходы,
где логические дыры и путаница), какие есть фактические ошибки и неточности — с конкретными местами и тем,
как это исправить. Без вступлений и без общих слов. Коротко и по делу: не больше 700 слов, только то, что
реально стоит исправить, самое важное — первым. Файлы не создавай — всё прямо в ответе."""

_CARRY = """

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
РАЗБОР ИСХОДНИКА (шаг 1, уже сделан) — в новом тексте исправь всё найденное здесь:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{analysis}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


def split_steps(prompt: str) -> tuple[str, str]:
    """→ (задание разбора, задание рерайта). Разделителя нет — ("", весь промпт)."""
    m = _MARK.search(prompt or "")
    if not m:
        return "", prompt
    first, rest = prompt[:m.start()].strip(), prompt[m.end():].strip()
    return (first, rest) if first and rest else ("", _MARK.sub("", prompt).strip())


def with_analysis(prompt: str, analysis: str) -> str:
    return prompt + _CARRY.format(analysis=analysis.strip()) if analysis.strip() else prompt


def analyze(task: str, source: str, folder: Path, *, engine: str, llm, claude_model: str = "",
            codex_model: str = "", fallback: bool = True, cancel: CancelToken | None = None) -> str:
    """Шаг 1: разбор исходника → текст разбора (и файл «разбор.txt»). Готовый разбор того же исходника и
    задания берётся из файла. Не вышло — пустая строка: рерайт идёт без разбора (с пометкой в журнале)."""
    sig = hashlib.sha256(f"{task}␟{source}".encode("utf-8")).hexdigest()[:16]
    path = folder / FILE
    mark = f"[{sig}]\n"
    try:
        saved = path.read_text(encoding="utf-8")
        if saved.startswith(mark):
            return saved[len(mark):].strip()
    except OSError:
        pass
    ask = task + _FORMAT
    text = ""
    if engine in ("claude_code", "codex"):
        from engine.core import switches
        from engine.rewrite.engines import SUB_ENGINES, RewriteError, run_sub, sub_ready

        chain = switches.order_from(engine) if fallback else [engine]
        prev, why = "", ""
        for n, eng in enumerate(chain):
            if n > 0 and not sub_ready(eng):
                continue
            if prev:
                switches.report("разбор исходника", prev, eng, why)
            try:
                text = run_sub(eng, f"{ask}\n\nИСХОДНИК:\n{source}",
                               model=codex_model if eng == "codex" else claude_model, cancel=cancel)
                break
            except RewriteError as exc:
                if cancel is not None:
                    cancel.check()
                log(f"Разбор исходника: {SUB_ENGINES[eng]} не ответил ({str(exc)[:120]})")
                prev, why = eng, str(exc)[:160]
        if not text:
            if not (fallback and llm and llm[0]):
                return ""
            if prev:
                switches.report("разбор исходника", prev, "llm", why)
    if not text and llm and llm[0]:
        from engine.slides.llm_client import LLMClient

        import threading

        key, base, model = llm
        client = LLMClient(key, model, base)
        client.busy_wait = BUSY_WAIT
        if cancel is not None:
            cancel.register(client)
        box: dict = {}

        def ask_llm() -> None:
            try:
                box["text"] = client.complete(label="rewrite_analysis", system=ask, user=f"ИСХОДНИК:\n{source}")
            except Exception as exc:  # noqa: BLE001
                box["error"] = exc

        import contextvars

        # копия контекста: журнал и уведомления о смене модели — в карточку этой задачи
        t = threading.Thread(target=contextvars.copy_context().run, args=(ask_llm,), name="rewrite-analysis", daemon=True)
        t.start()
        t.join(LLM_TIMEOUT)
        client.close()  # завис дольше предела — обрываем запрос
        if cancel is not None:
            cancel.check()
        if "text" not in box:
            why = str(box.get("error") or f"нет ответа дольше {LLM_TIMEOUT // 60} мин")[:120]
            log(f"Разбор исходника не получился ({why}) — переписываю без него")
            return ""
        text = box["text"]
    text = (text or "").strip()
    if text:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(mark + text + "\n", encoding="utf-8")
        except OSError as exc:  # разбор всё равно уходит в шаг 2 — файл лишь для тебя и повторов
            log(f"Разбор исходника: файл не записался ({exc})")
        log(f"Разбор исходника готов: {len(text.split())} слов → {path.name}")
    return text
