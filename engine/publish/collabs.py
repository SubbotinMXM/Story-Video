"""Соавторства: каждое приглашение, которое программа отправила, обязательно должно быть принято.

Приглашение — это пара «канал-владелец → канал-соавтор» (ссылка в Studio одна на пару, принимаются все ждущие
ролики владельца сразу). Программа пишет каждую такую пару в журнал (data/publish/collabs.json) в момент,
когда создала приглашение, и вычёркивает только тогда, когда в профиле соавтора по ссылке не осталось ждущих
приглашений. Поэтому приглашение не потеряется, что бы ни случилось с выкладкой: приложение закрыли, профиль
AdsPower упал, Google попросил пароль, Studio поменяла вёрстку.

Что не принято — подтверждает фоновая задача «Подтверждение соавторств» (сама ставится в очередь выкладки, когда
есть что подтверждать и выкладка не идёт; повторы через 10, 30, 60 минут, дальше раз в час). Не получается —
причина пишется в журнал справа, пара остаётся в списке и пробуется снова.
"""

from __future__ import annotations

import time
from typing import Any

from engine.core.cancel import CancelToken
from engine.core.logutil import log
from engine.publish.store import LOCK, read_json, write_json

FILE = "collabs.json"
PROFILE_TRIES = 3  # браузер профиля закрылся посреди работы — открываем заново, но не бесконечно
RETRY_MIN = (10, 30, 60)  # пауза перед повтором после неудачи; дальше — раз в час
KINDS_BUSY = ("publish", "collabs")  # пока идёт выкладка или подтверждение — новое не ставим


def _data() -> dict[str, Any]:
    raw = read_json(FILE, {})
    return raw if isinstance(raw, dict) else {}


def _pairs(data: dict[str, Any]) -> dict[str, dict]:
    pairs = data.get("pairs")
    if not isinstance(pairs, dict):
        pairs = data["pairs"] = {}
    return pairs


def pair_key(owner_key: str, co_key: str) -> str:
    return f"{owner_key}>{co_key}"


def record_invites(owner_key: str, owner_label: str, invites: dict[str, str], *, title: str = "", url: str = "") -> int:
    """Выкладка создала приглашения («профиль/канал» соавтора → ссылка): каждая пара ждёт подтверждения."""
    n = 0
    with LOCK:
        data = _data()
        pairs = _pairs(data)
        for co_key, link in (invites or {}).items():
            if not link:
                continue
            rec = pairs.setdefault(pair_key(owner_key, co_key), {"owner": owner_key, "co": co_key, "tries": 0})
            was = bool(rec.get("pending"))
            rec.update(link=link, owner_label=owner_label, pending=True)
            if not was:  # новая порция приглашений — отсчёт заново
                rec.update(since=time.time(), tries=0, next_try=0.0, error="")
            vids = rec.setdefault("videos", [])
            if title and title not in vids:
                vids.append(title)
                del vids[:-20]
            n += 1
        write_json(FILE, data)
    return n


def mark_accepted(owner_key: str, co_key: str) -> None:
    """В профиле соавтора по ссылке не осталось ждущих приглашений — пара подтверждена."""
    with LOCK:
        data = _data()
        rec = _pairs(data).get(pair_key(owner_key, co_key))
        if rec is not None:
            rec.update(pending=False, error="", tries=0, accepted_at=time.time(), videos=[])
            write_json(FILE, data)


def mark_failed(owner_key: str, co_key: str, reason: str, *, link: str = "", owner_label: str = "") -> None:
    """Принять не получилось: пара остаётся ждущей, причина запоминается, следующая попытка — позже."""
    with LOCK:
        data = _data()
        pairs = _pairs(data)
        rec = pairs.setdefault(pair_key(owner_key, co_key), {"owner": owner_key, "co": co_key, "tries": 0,
                                                              "since": time.time()})
        if link:
            rec["link"] = link
        if owner_label:
            rec["owner_label"] = owner_label
        n = int(rec.get("tries") or 0)
        rec.update(pending=True, error=" ".join(str(reason).split())[:300], tries=n + 1,
                   next_try=time.time() + RETRY_MIN[min(n, len(RETRY_MIN) - 1)] * 60)
        write_json(FILE, data)


def pending(now: float | None = None, *, only_due: bool = False) -> list[dict]:
    """Ждущие подтверждения пары (самые старые первыми). only_due — те, у кого подошёл срок следующей попытки."""
    now = time.time() if now is None else now
    out = [dict(r) for r in _pairs(_data()).values() if r.get("pending") and r.get("link")]
    if only_due:
        out = [r for r in out if float(r.get("next_try") or 0) <= now]
    return sorted(out, key=lambda r: float(r.get("since") or 0))


def backfill(states: list[dict]) -> int:
    """Один раз при первом запуске новой версии: пары из уже выложенных роликов (их приглашения могли
    принять не все — в прошлых версиях принималось одно приглашение на соавтора вместо каждого). → сколько пар."""
    with LOCK:
        data = _data()
        if data.get("backfilled"):
            return 0
        pairs = _pairs(data)
        n = 0
        for st in states:
            for owner_key, rec in (st.get("channels") or {}).items():
                if rec.get("status") != "done":
                    continue
                for co_key, link in (rec.get("invites") or {}).items():
                    pk = pair_key(owner_key, co_key)
                    if link and pk not in pairs:
                        pairs[pk] = {"owner": owner_key, "co": co_key, "link": link, "pending": True, "tries": 0,
                                     "since": time.time(), "next_try": 0.0, "error": "", "videos": [],
                                     "owner_label": owner_key, "backfill": True}
                        n += 1
        data["backfilled"] = True
        write_json(FILE, data)
    return n


# ---------- подтверждение ----------


def reconcile(api_key: str, url: str, *, cancel: CancelToken | None = None, progress=None,
              headless: bool = True) -> dict[str, Any]:
    """Принять всё, что ждёт, во всех профилях соавторов. → {"ok": [...], "failed": [...], "total": n}."""
    from engine.publish import registry, youtube
    from engine.publish.adspower import AdsPower, AdsPowerDown

    todo = pending()
    out: dict[str, Any] = {"ok": [], "failed": [], "total": len(todo)}
    if not todo:
        return out
    by_co: dict[str, list[dict]] = {}
    for r in todo:
        by_co.setdefault(r["co"], []).append(r)

    def fail(rec: dict, reason: str) -> None:
        mark_failed(rec["owner"], rec["co"], reason)
        label = rec.get("owner_label") or rec["owner"]
        out["failed"].append(f"{label} → {rec['co']}: {reason}")
        log(f"Соавторство НЕ подтверждено: {label} → {rec['co']} — {reason}. Попробую позже.")

    with AdsPower(api_key, url) as ap:
        if not ap.alive():
            raise AdsPowerDown("AdsPower не отвечает — соавторства подтвердятся, когда он вернётся")
        for k, (co_key, recs) in enumerate(by_co.items()):
            if cancel:
                cancel.check()
            if progress:
                progress(k / len(by_co), f"Соавторства: профиль {k + 1} из {len(by_co)}")
            try:
                co = registry.resolve([co_key], what="соавтора")[0]
            except Exception as exc:
                for r in recs:
                    fail(r, f"канала-соавтора нет в учёте ({exc})")
                continue
            links = list(dict.fromkeys(r["link"] for r in recs))
            got: dict | None = None
            err = ""
            for attempt in range(1, PROFILE_TRIES + 1):
                try:
                    session = ap.start(co.profile_id, headless=headless)
                except AdsPowerDown:
                    raise
                except Exception as exc:
                    err = f"профиль не запустился: {exc}"
                    break
                try:
                    got = youtube.accept_coauthor_links(session.cdp_url, co.channel, links, cancel=cancel)
                    break
                except Exception as exc:
                    if cancel is not None and cancel.is_cancelled():
                        raise
                    err = str(exc).splitlines()[0][:200] if str(exc) else exc.__class__.__name__
                    lost = youtube.browser_lost(exc)
                    if lost and attempt < PROFILE_TRIES:
                        log(f"Соавторства: браузер профиля закрылся ({err}) — открою заново (попытка {attempt + 1})")
                        continue
                    break
                finally:
                    try:
                        ap.stop(co.profile_id)
                    except Exception:
                        pass
            if got is None:
                for r in recs:
                    fail(r, err or "не удалось")
                continue
            for r in recs:
                res = got.get(r["link"])
                if isinstance(res, list):
                    mark_accepted(r["owner"], r["co"])
                    out["ok"].append(f"{r.get('owner_label') or r['owner']} → {co.channel.name}"
                                     + (f": принято {len(res)}" if res else ": ждущих не было"))
                else:
                    fail(r, str(res or "не удалось открыть приглашение"))
    return out


# ---------- само подтверждение: фоновая проверка ----------


def tick(manager, now: float | None = None) -> str:
    """Есть что подтверждать, подошёл срок, выкладка и подтверждение сейчас не идут — поставить задачу в очередь.
    → id задачи или «»."""
    now = time.time() if now is None else now
    if not pending(now, only_due=True):
        return ""
    jobs = manager.list()
    if any(j.get("kind") in KINDS_BUSY and j.get("status") in ("queued", "running") for j in jobs):
        return ""
    try:
        job = manager.add(manager.create("collabs", {"auto": True}))
    except Exception as exc:  # noqa: BLE001 — нет ключа AdsPower и т. п.: пробуем в следующий раз
        log(f"Соавторства: задачу не поставил ({str(exc)[:160]})")
        return ""
    log(f"Соавторства: ждут подтверждения {len(pending())} — ставлю задачу «Подтверждение соавторств»")
    return str(getattr(job, "id", "") or "")


def loop(manager, every: float = 60.0) -> None:
    """Фоновая проверка раз в every секунд, пока работает приложение (и сразу после запуска)."""
    while True:
        try:
            tick(manager)
        except Exception as exc:  # noqa: BLE001 — сбой проверки не должен останавливать приложение
            log(f"Соавторства: проверка не удалась ({exc})")
        time.sleep(every)
