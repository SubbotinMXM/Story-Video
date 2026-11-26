"""Политика автоповторов: задача выполнится сама, пока не упрётся в то, что может сделать только человек.

Режим «без присмотра» (настройка `unattended`, по умолчанию включён): очередь можно оставить на двое суток.
  * сбой сети/сервиса/Studio/неопознанный — повтор с нарастающей паузой (3, 10, 20, 40, 60, 90 мин, дальше раз в 2 ч);
  * «нужен человек» (Google просит подтвердить вход) — повторять нельзя: лишние попытки только усугубляют проверку.
    Задача встаёт, приходит уведомление, остальные задачи идут дальше;
  * деньги/ключ/диск/AdsPower — человек может исправить издалека, поэтому проверяем раз в 3 часа;
  * неопознанная ошибка — не больше UNKNOWN_MAX попыток (если это не сбой, а баг, бесконечные повторы только жгут деньги);
  * ошибка настроек (ValueError) повтором не лечится;
  * всё это — не дольше HORIZON_H часов с первого сбоя, дальше задача ждёт человека.
"""

from __future__ import annotations

HORIZON_H = 72  # столько часов с первого сбоя задача пытается сама
TAIL_MIN = 120  # после коротких пауз — раз в 2 часа
SLOW_MIN = 180  # проблемы, которые человек может исправить издалека
UNKNOWN_MAX = 12  # неопознанная ошибка: не больше попыток
NO_RETRY_TAGS = frozenset({"verify"})  # нужен человек лично (вход в Google): повторы вредны
SLOW_TAGS = frozenset({"adspower", "money", "key", "disk"})
# тихие задачи сами запускаются по расписанию — их повторять не нужно
QUIET_KINDS = frozenset({"analytics", "scan", "niches", "competition", "collabs"})


def plan(text: str, *, n: int, kind: str, first_failure: float, now: float, early: tuple[float, ...]) -> tuple[float | None, str]:
    """(пауза в минутах до следующей попытки | None — не повторять, причина/пояснение).
    n — сколько автоповторов уже было, early — короткие паузы (AUTO_RETRY_MIN)."""
    from server.report import diagnose

    if not early or kind in QUIET_KINDS:
        return None, ""
    tag = diagnose(text)["tag"]
    if tag in NO_RETRY_TAGS:
        return None, "нужен человек лично"
    if first_failure and now - first_failure > HORIZON_H * 3600:
        return None, f"не получилось за {HORIZON_H} ч"
    if tag == "adspower" and n < 3:
        return float(early[min(n, len(early) - 1)]), tag  # Local API мог просто замолчать на минуту
    if tag in SLOW_TAGS:
        return float(SLOW_MIN), tag
    if tag in ("studio", "other") and n >= UNKNOWN_MAX:
        return None, "повторы не помогают"
    wait = early[n] if n < len(early) else float(TAIL_MIN)
    return float(wait), tag
