"""Расход токенов встроенной нейросети по задачам: клиент сообщает каждый ответ, очередь складывает в карточку задачи.

Claude Code и Codex работают по подписке и токены не отдают — здесь только встроенная нейросеть (по ключу).
"""

from __future__ import annotations

from typing import Callable

from engine.core.logutil import current_job

UsageCb = Callable[[str | None, str, int, int, int], None]  # (id задачи, этап, вход, из них кеш, ответ)
_callback: UsageCb | None = None

# этапы по названиям запросов — человеку «judge_pool» ничего не говорит
_GROUPS = (
    (("rewrite_review",), "Проверка рерайта"),
    (("rewrite_",), "Рерайт"),
    (("chapters",), "Главы и переводы глав"),
    (("judge",), "Проверка кадров"),
    (("theme_", "story_context", "stars_cast", "queries", "strict_", "beat"), "Тема и запросы кадров"),
    (("ai_",), "AI-кадры"),
    (("cover",), "Обложки"),
)
OTHER = "Остальное"


def group_of(label: str) -> str:
    for prefixes, name in _GROUPS:
        if label.startswith(prefixes):
            return name
    return OTHER


def set_usage_callback(cb: UsageCb | None) -> None:
    global _callback
    _callback = cb


def record(label: str, tokens_in: int, cached: int, tokens_out: int) -> None:
    cb = _callback
    if cb is None:
        return
    try:
        cb(current_job.get(), label, int(tokens_in or 0), int(cached or 0), int(tokens_out or 0))
    except Exception:
        pass  # учёт не должен ронять запрос к нейросети


def add_to(tokens: dict, label: str, tokens_in: int, cached: int, tokens_out: int) -> None:
    """Прибавить ответ к счётчику задачи: {этап: [запросов, вход, кеш, ответ]}."""
    row = tokens.setdefault(group_of(label), [0, 0, 0, 0])
    row[0] += 1
    row[1] += tokens_in
    row[2] += cached
    row[3] += tokens_out


def summary(tokens: dict | None) -> dict | None:
    """Для карточки задачи: итоги и этапы по убыванию расхода; нет запросов — None."""
    if not tokens:
        return None
    rows = sorted(((name, *vals) for name, vals in tokens.items()), key=lambda r: -(r[2] - r[3] + r[4]))
    total = [sum(r[i] for r in rows) for i in (1, 2, 3, 4)]
    return {
        "calls": total[0], "in": total[1], "cached": total[2], "out": total[3],
        "by": [{"name": r[0], "calls": r[1], "in": r[2], "cached": r[3], "out": r[4]} for r in rows],
    }
