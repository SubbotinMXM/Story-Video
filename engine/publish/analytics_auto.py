"""Автообновление «Аналитики» по расписанию: каждый день в заданное время (МСК) — синхронизация каналов группы.

Настраивается во вкладке «Аналитика» (config: analytics_auto = {enabled, group, time}), а не в коде.
- Раз в сутки: день, за который уже запускали, помним (data/publish/analytics_auto.json).
- Mac спал или приложение было закрыто в это время — запуск, как только программа снова работает в тот же день.
- Только что включили или поменяли время/группу — ближайший запуск в следующее наступление этого времени
  (а не сразу же, если сегодняшнее время уже прошло).
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

from engine.publish.schedule import MSK
from engine.publish.store import LOCK, read_json, write_json

STATE_FILE = "analytics_auto.json"
DEFAULT = {"enabled": False, "group": "", "time": "07:00"}


def normalize(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    t = str(raw.get("time") or "").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        t = DEFAULT["time"]
    else:
        t = f"{int(m.group(1)):02d}:{m.group(2)}"
    return {"enabled": bool(raw.get("enabled")), "group": str(raw.get("group") or "").strip(), "time": t}


def _sig(setting: dict) -> str:
    return f"{setting['enabled']}|{setting['group']}|{setting['time']}"


def _at(day: datetime, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def state() -> dict:
    return read_json(STATE_FILE, {})


def due(setting: dict, now: datetime | None = None) -> bool:
    """Пора ли запускать сейчас. Заодно запоминает момент, с которого действует текущая настройка."""
    setting = normalize(setting)
    now = (now or datetime.now(MSK)).astimezone(MSK)
    with LOCK:
        st = state()
        if st.get("sig") != _sig(setting):  # включили / сменили время или группу — считаем от этого момента
            st.update(sig=_sig(setting), since=now.timestamp())
            write_json(STATE_FILE, st)
    if not setting["enabled"] or not setting["group"]:
        return False
    slot = _at(now, setting["time"])
    return (now >= slot and slot.timestamp() > float(st.get("since") or 0)
            and st.get("last_day") != now.date().isoformat())


def mark(job_id: str = "", now: datetime | None = None) -> None:
    now = (now or datetime.now(MSK)).astimezone(MSK)
    with LOCK:
        st = state()
        st.update(last_day=now.date().isoformat(), last_at=now.timestamp(), last_job=job_id)
        write_json(STATE_FILE, st)


def next_run(setting: dict, now: datetime | None = None) -> float:
    """Когда ближайший запуск (unix-время); 0 — выключено."""
    setting = normalize(setting)
    if not setting["enabled"] or not setting["group"]:
        return 0.0
    now = (now or datetime.now(MSK)).astimezone(MSK)
    st = state()
    # настройку только что поменяли и проверка её ещё не видела — она начнёт отсчёт с этого момента
    since = float(st.get("since") or 0) if st.get("sig") == _sig(setting) else now.timestamp()
    slot = _at(now, setting["time"])
    today_pending = st.get("last_day") != now.date().isoformat() and slot.timestamp() > since
    if today_pending:
        return max(slot, now).timestamp()
    return (slot + timedelta(days=1)).timestamp()


def group_channels(cfg: dict, group_id: str) -> tuple[str, list[str]]:
    """Имя группы и id её каналов (из «Аналитики»); группы нет — ("", [])."""
    for g in cfg.get("analytics_groups") or []:
        if g.get("id") == group_id:
            return str(g.get("name") or ""), [str(c) for c in g.get("channels") or []]
    return "", []


def tick(manager, cfg: dict) -> str | None:
    """Проверка по расписанию: пора — поставить задачу «Аналитика» по группе в очередь. → id задачи."""
    setting = normalize(cfg.get("analytics_auto"))
    if not due(setting):
        return None
    from engine.core.logutil import log

    for j in manager.list():
        if j["kind"] == "analytics" and j["status"] in ("queued", "running"):
            mark(j["id"])  # уже идёт синхронизация — второй раз не ставим
            return j["id"]
    name, chans = group_channels(cfg, setting["group"])
    if not chans:
        log(f"Аналитика по расписанию: в группе «{name or setting['group']}» нет каналов — пропуск")
        mark()
        return None
    job = manager.add(manager.create("analytics", {"group": setting["group"], "auto": True}))
    try:
        # идущая заливка не прерывается, но как только она кончится, первой берётся аналитика —
        # раньше остальных роликов, ждущих выкладки
        manager.move(job.id, "top")
    except ValueError:
        pass  # уже стартовала — выше некуда
    log(f"Аналитика по расписанию: группа «{name}», каналов {len(chans)}")
    mark(job.id)
    return job.id


def loop(manager, load_config, every: float = 30.0) -> None:
    """Фоновая проверка расписания (раз в every секунд), пока работает приложение."""
    from engine.core.logutil import log

    while True:
        try:
            cfg = load_config()
            # расписание включено — Mac не засыпает (иначе во сне рвётся VPN и запуск по расписанию без сети)
            if normalize(cfg.get("analytics_auto")).get("enabled"):
                manager.awake.hold("schedule")
            else:
                manager.awake.release("schedule")
            tick(manager, cfg)
        except Exception as exc:  # noqa: BLE001 — сбой проверки не должен останавливать приложение
            log(f"Аналитика по расписанию: {exc}")
        time.sleep(every)
