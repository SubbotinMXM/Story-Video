"""Когда выходит ролик: дата и время по Москве (GMT+3), не больше одного ролика в день на канал.

Даты по умолчанию — через «шаг» дней после последнего запланированного на канале ролика (шаг 1 — каждый день
подряд, 2 — через день, 3 — через два дня…; по учёту программы и по данным YouTube Studio — что позже). Свои даты —
из календаря запуска (план): ролики занимают их по порядку готовности; роликов больше, чем дат, — дальше с тем же шагом.
Время — своё или случайное с 16:00 до 20:00. У каждого канала свой ближайший свободный день.
"""

from __future__ import annotations

import random
import uuid
from datetime import date, datetime, time, timedelta, timezone

from engine.publish.store import LOCK, read_json, write_json

MSK = timezone(timedelta(hours=3), "MSK")
RANDOM_FROM, RANDOM_TO = time(16, 0), time(20, 0)
STEP_MIN = 15  # YouTube Studio предлагает время с шагом 15 минут
MIN_AHEAD = timedelta(hours=1)  # запланировать «на через 5 минут» Studio не даст
BOOK_FILE = "schedule.json"
PLANS_FILE = "plans.json"


def parse_time(raw: str) -> time | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        h, m = (int(x) for x in raw.split(":")[:2])
        return time(h, m)
    except ValueError:
        raise ValueError(f"Время публикации — в виде ЧЧ:ММ, а не «{raw}»") from None


def random_time(rng: random.Random) -> time:
    start = RANDOM_FROM.hour * 60 + RANDOM_FROM.minute
    end = RANDOM_TO.hour * 60 + RANDOM_TO.minute
    minute = rng.randrange(start, end, STEP_MIN)
    return time(minute // 60, minute % 60)


# ---------- план запуска: свои даты из календаря ----------


MAX_STEP = 30


def clamp_step(raw) -> int:
    """Шаг между роликами, дней: 1 — каждый день, 2 — через день… Не число/меньше 1 — 1."""
    try:
        return max(1, min(MAX_STEP, int(raw)))
    except (TypeError, ValueError):
        return 1


def create_plan(dates: list[str], at: str = "", step: int = 1) -> str:
    """Запуск с выбранными датами → id плана (ролики запуска берут даты по порядку готовности).
    step — через сколько дней друг от друга выходят ролики (1 — каждый день)."""
    clean = sorted({date.fromisoformat(d).isoformat() for d in dates if d})
    parse_time(at)
    plan_id = uuid.uuid4().hex[:10]
    with LOCK:
        plans = read_json(PLANS_FILE, {})
        plans[plan_id] = {"dates": clean, "time": at.strip(), "next": 0, "step": clamp_step(step)}
        write_json(PLANS_FILE, plans)
    return plan_id


def take_plan_date(plan_id: str) -> tuple[date | None, str]:
    """Следующая дата плана для очередного готового ролика. → (дата или None — «по умолчанию», время).
    Дат меньше, чем роликов: лишние — после последней выбранной, с шагом плана."""
    with LOCK:
        plans = read_json(PLANS_FILE, {})
        plan = plans.get(plan_id)
        if not plan:
            return None, ""
        k = int(plan.get("next") or 0)
        plan["next"] = k + 1
        write_json(PLANS_FILE, plans)
    dates = [date.fromisoformat(d) for d in plan.get("dates") or []]
    at = str(plan.get("time") or "")
    if not dates:
        return None, at
    if k < len(dates):
        return dates[k], at
    return dates[-1] + timedelta(days=clamp_step(plan.get("step")) * (k - len(dates) + 1)), at


def plan_step(plan_id: str) -> int:
    """Шаг плана (дней между роликами); нет плана — 1."""
    if not plan_id:
        return 1
    with LOCK:
        return clamp_step((read_json(PLANS_FILE, {}).get(plan_id) or {}).get("step"))


# ---------- занятые дни каналов ----------


def booked(channel: str) -> set[date]:
    return {date.fromisoformat(d) for d in read_json(BOOK_FILE, {}).get(channel, [])}


def slot(channel: str, *, wanted: date | None = None, at: str = "", studio_last: date | None = None,
         now: datetime | None = None, rng: random.Random | None = None, step: int = 1) -> datetime:
    """Дата и время выхода ролика на канале (по Москве) — и сразу занять этот день.
    wanted — дата из календаря запуска; занята на канале — ближайший свободный день после неё.
    step — без своей даты ролик выходит через step дней после последнего запланированного на канале."""
    rng = rng or random.Random()
    now = (now or datetime.now(MSK)).astimezone(MSK)
    clock = parse_time(at) or random_time(rng)
    with LOCK:
        book = read_json(BOOK_FILE, {})
        taken = {date.fromisoformat(d) for d in book.get(channel, [])}
        if wanted is not None:
            day = wanted
        else:
            last = max([*taken, *([studio_last] if studio_last else [])], default=None)
            day = last + timedelta(days=clamp_step(step)) if last else now.date()
        day = max(day, now.date())
        while day in taken or datetime.combine(day, clock, MSK) < now + MIN_AHEAD:
            day += timedelta(days=1)
        book[channel] = sorted({*(d.isoformat() for d in taken), day.isoformat()})
        write_json(BOOK_FILE, book)
    return datetime.combine(day, clock, MSK)


def release(channel: str, when: datetime) -> None:
    """Выкладка на канал отменена совсем — день снова свободен."""
    with LOCK:
        book = read_json(BOOK_FILE, {})
        days = [d for d in book.get(channel, []) if d != when.astimezone(MSK).date().isoformat()]
        book[channel] = days
        write_json(BOOK_FILE, book)
