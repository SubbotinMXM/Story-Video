"""Итоговый отчёт по пачке задач: что сделано, что нет, почему и как исправить.

Пачка — всё, что очередь сделала с прошлого простоя. Когда очередь опустела, из снимков задач собирается отчёт
(build): по каждой задаче — конкретные результаты (на какой канал что выложено, сколько голосований и т. д.),
отдельно — «проблемы». У каждой проблемы есть понятная причина и способ исправить:
  auto   — программа исправит сама (кнопка «Исправить»: продолжить задачу с места падения / принять соавторства);
  wait   — пройдёт само (лимит YouTube, сеть) — сказано, когда;
  manual — нужен человек (вход в Google, пополнить счёт, запустить AdsPower): пошаговая инструкция.
Отчёты хранятся в data/reports.json (последние REPORTS_KEEP, не старше KEEP_DAYS) и показываются окном в приложении.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from engine.core.config import DATA_DIR
from engine.core.logutil import log

KIND_LABEL = {
    "broll": "Ролик с футажами", "slides_full": "Ролик со слайдами", "slides_resume": "Ролик со слайдами",
    "slides_voice": "Озвучка слайдов", "slides_media": "Подбор кадров", "slides_render": "Рендер слайдов",
    "copies": "Копии с ускорением", "rewrite": "Рерайт текста", "cover": "Обложка", "publish": "Выкладка",
    "comments": "Комментарии", "polls": "Голосования", "niches": "Поиск ниш", "competition": "Конкуренция по языкам", "analytics": "Аналитика",
    "collabs": "Соавторства", "scan": "Поиск каналов",
}
# тихие задачи: без ошибок отдельного окна не стоят (раз в сутки обновилась аналитика — не событие)
QUIET_KINDS = frozenset({"analytics", "scan", "niches", "competition", "collabs"})
REPORTS_KEEP = 30
KEEP_DAYS = 5

_FILE = (Path(os.environ["STORY_VIDEO_JOBS_FILE"]).with_name("reports.json")
         if os.environ.get("STORY_VIDEO_JOBS_FILE") else DATA_DIR / "reports.json")
_LOCK = threading.Lock()

_ROUTINE_NOTES = ("в заставку и подсказку взял", "комментарии:", "продолжил черновик", "плейлистов на канале")


# ---------- диагностика ----------


def _verify_steps(profile: str) -> list[str]:
    where = f"«{profile}»" if profile else "соавтора"
    return [
        f"Откройте профиль {where} в AdsPower (кнопка Open) — обычное окно браузера, не скрытое.",
        "Когда Google покажет «Verify it's you» — нажмите Next и пройдите проверку (пароль аккаунта). "
        "Программа пароли не вводит — это делает только человек.",
        "Убедитесь, что YouTube Studio открывается без окна проверки, и закройте профиль.",
        "Вернитесь сюда и нажмите «Принять соавторства сейчас» — программа сама примет все ждущие приглашения.",
    ]


def diagnose(text: str, *, profile: str = "") -> dict[str, Any]:
    """Сырой текст ошибки → {tag, cause, mode, steps}. mode: auto | wait | manual."""
    low = (text or "").lower()

    def out(tag: str, cause: str, mode: str, steps: list[str] | None = None) -> dict[str, Any]:
        return {"tag": tag, "cause": cause, "mode": mode, "steps": steps or []}

    if "verify it" in low or "подтвердить вход" in low or "подтвердите, что это" in low:
        return out("verify", "Google просит подтвердить вход в аккаунт (окно «Verify it's you»)", "manual",
                   _verify_steps(profile))
    if "adspower" in low and any(w in low for w in ("не отвечает", "etimedout", "econnrefused", "не запущен", "недоступен")):
        return out("adspower", "AdsPower не запущен или потерял связь с интернетом", "manual", [
            "Запустите приложение AdsPower и убедитесь, что оно вошло в аккаунт и видит интернет.",
            "Вернитесь сюда и нажмите «Исправить» — работа продолжится с того места, где остановилась.",
        ])
    if "занят другим процессом" in low or "помечен занятым" in low or "profile busy" in low:
        return out("busy", "Профиль AdsPower был занят другой работой", "auto")
    if any(w in low for w in ("лимит загрузок", "upload limit", "uploadlimit", "quota", "достигнут лимит", "лимит youtube")):
        return out("limit", "YouTube временно ограничил число загрузок", "wait", [
            "Ничего делать не нужно: очередь сама продолжит, когда лимит снимется."])
    if any(w in low for w in ("insufficient", "недостаточно средств", "не хватает денег", "мало денег", "баланс", "payment required", " 402")):
        return out("money", "На счёте сервиса закончились деньги", "manual", [
            "Пополните баланс сервиса, на котором остановилась работа (название — в тексте ошибки ниже).",
            "Вернитесь сюда и нажмите «Исправить» — задача продолжится."])
    if any(w in low for w in ("invalid api key", "unauthorized", "неверный ключ", "нужен ключ", " 401", " 403", "api key")):
        return out("key", "Ключ доступа к сервису не подошёл или не задан", "manual", [
            "Откройте «Настройки» и проверьте ключ сервиса (название — в тексте ошибки ниже).",
            "Сохраните настройки, вернитесь сюда и нажмите «Исправить»."])
    if any(w in low for w in ("нет места", "no space left", "недостаточно места")):
        return out("disk", "На диске не хватило места", "manual", [
            "Освободите место на диске (старые ролики из папок вывода, корзина).",
            "Вернитесь сюда и нажмите «Исправить»."])
    from server.jobs import _TRANSIENT

    if any(k in low for k in _TRANSIENT):
        return out("net", "Временный сбой сети или сервиса", "auto")
    if any(w in low for w in ("нет элемента", "не дождался элемента", "вёрстк", "не открылось окно", "не сработало")):
        return out("studio", "YouTube Studio отреагировала не так, как ждала программа (сбой страницы или смена вёрстки)",
                   "auto")
    return out("other", "Причина не опознана — смотрите текст ошибки ниже", "auto")


# ---------- разбор результатов ----------


def _clean(s: Any, n: int = 400) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:n]


def _is_routine(note: str) -> bool:
    return any(k in note.lower() for k in _ROUTINE_NOTES)


def _when(iso: str) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})", iso or "")
    return f"{m.group(3)}.{m.group(2)} {m.group(4)}:{m.group(5)} МСК" if m else ""


def _label_lines(lines: list[Any]) -> list[dict[str, Any]]:
    """Строки результата вида «✓ …» / «✗ …» / «• …» → строки отчёта."""
    rows = []
    for ln in lines or []:
        t = _clean(ln, 300)
        if t[:1] in ("✓", "✗", "•"):
            rows.append({"ok": t[0] != "✗", "info": t[0] == "•", "text": t[1:].strip()})
    return rows


def _publish_rows(snap: dict, names: Callable[[list[str]], list[str]]) -> tuple[list[dict], list[dict]]:
    """(что выложено, проблемы каналов) по состоянию задачи выкладки."""
    rows, problems = [], []
    channels = (snap.get("state") or {}).get("channels") or {}
    title = _clean(((snap.get("state") or {}).get("materials") or {}).get("title"), 120)
    for key, rec in channels.items():
        label = (names([key]) or [key])[0]
        if rec.get("status") == "done":
            notes = [_clean(n, 200) for n in rec.get("notes") or [] if n and not _is_routine(n)
                     and not str(n).lower().startswith("соавтор")]
            date_note = next((n for n in notes if "не вижу запланированной даты" in n), "")
            rows.append({
                "ok": True, "channel": label, "title": title, "url": rec.get("url") or "",
                "when": _when(rec.get("publish_at", "")),
                "coauthors_ok": len(rec.get("coauthors_ok") or []),
                "coauthors_total": len(rec.get("coauthors_ok") or []) + len(rec.get("coauthors_failed") or []),
                "note": "Studio ещё обрабатывала видео и дату не показала — ролик сохранён, дату можно сверить позже"
                if date_note else "",
            })
        else:
            err = _clean(rec.get("error") or rec.get("stage") or "не выложено", 500)
            problems.append({"channel": label, "text": err, "title": title})
    return rows, problems


# ---------- сборка отчёта ----------


def _problem(*, scope: str, title: str, text: str, diag: dict, action: dict | None, job_id: str = "",
             severity: str = "bad") -> dict[str, Any]:
    return {"id": uuid.uuid4().hex[:8], "scope": scope, "title": title, "text": text, "job_id": job_id,
            "severity": severity, "tag": diag["tag"], "cause": diag["cause"], "mode": diag["mode"],
            "steps": diag["steps"], "action": action}


def build(snaps: list[dict[str, Any]], *, names: Callable[[list[str]], list[str]],
          pending_pairs: list[dict[str, Any]] | None = None, started: float = 0.0, finished: float = 0.0,
          waiting: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Снимки задач пачки (Job.stored()) + ждущие пары соавторств → отчёт."""
    items: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    counts = {"jobs": 0, "ok": 0, "failed": 0, "videos": 0, "polls": 0, "comments": 0, "covers": 0, "texts": 0}
    pairs = pending_pairs or []

    for snap in sorted(snaps, key=lambda s: float(s.get("finished") or 0)):
        kind = snap.get("kind", "")
        res = snap.get("result") or {}
        status = snap.get("status", "")
        counts["jobs"] += 1
        item: dict[str, Any] = {
            "job_id": snap.get("id", ""), "kind": kind, "kind_label": KIND_LABEL.get(kind, kind),
            "title": _clean(snap.get("title"), 160), "status": "ok" if status == "done" else "failed",
            "summary": _clean(res.get("summary") or snap.get("message"), 300), "rows": [], "details": [],
        }
        if status == "done":
            counts["ok"] += 1
        else:
            counts["failed"] += 1

        if kind == "publish":
            rows, ch_problems = _publish_rows(snap, names)
            item["rows"] = rows
            counts["videos"] += len(rows)
            for cp in ch_problems:
                d = diagnose(cp["text"])
                problems.append(_problem(
                    scope="publish", title=f"Не выложено: {cp['channel']}", text=cp["text"], diag=d, job_id=item["job_id"],
                    action={"type": "resume", "job_id": item["job_id"]} if status in ("failed", "cancelled") else None))
        elif kind in ("polls", "comments"):
            item["rows"] = _label_lines(res.get("lines") or [])
            ok_n = sum(1 for r in item["rows"] if r["ok"] and not r["info"])
            counts["polls" if kind == "polls" else "comments"] += ok_n
        elif kind == "cover":
            counts["covers"] += 1 if status == "done" else 0
            item["details"] = [_clean(x, 200) for x in (res.get("lines") or [])[:3]]
        elif kind == "rewrite":
            counts["texts"] += 1 if status == "done" else 0
            item["details"] = [_clean(x, 200) for x in (res.get("lines") or [])[:3]]
        elif status == "done":
            item["details"] = [_clean(x, 200) for x in (res.get("lines") or [])[:4]]

        if status != "done" and not any(p["job_id"] == item["job_id"] for p in problems):
            err = _clean(snap.get("error") or snap.get("message") or "неизвестная ошибка", 600)
            d = diagnose(err)
            targets = names((snap.get("params") or {}).get("channels") or []) if kind in ("polls", "comments", "publish") else []
            where = f" · {', '.join(targets[:3])}" if targets else ""
            problems.append(_problem(
                scope="job", title=f"{KIND_LABEL.get(kind, kind)} не выполнена{where}", text=err, diag=d,
                job_id=item["job_id"], action={"type": "resume", "job_id": item["job_id"]}))
        # предупреждения готовой задачи, которых не покрывают другие разделы (соавторов ведёт журнал пар)
        if status == "done":
            for w in res.get("warnings") or []:
                w = _clean(w, 400)
                if not w or w.startswith("Соавтор ") or "соавтор" in w.lower()[:20]:
                    continue
                problems.append(_problem(
                    scope="warn", title=f"{KIND_LABEL.get(kind, kind)}: замечание", text=w,
                    diag={"tag": "warn", "cause": "Задача выполнена, но есть замечание", "mode": "wait", "steps": []},
                    job_id=item["job_id"], severity="warn", action=None))
        items.append(item)

    # соавторства: источник правды — журнал пар (принятое из него уже вычеркнуто)
    by_co: dict[str, list[dict]] = {}
    for p in pairs:
        by_co.setdefault(p["co"], []).append(p)
    for co_key, recs in by_co.items():
        co_name = (names([co_key]) or [co_key])[0]
        err = _clean(next((r.get("error") for r in recs if r.get("error")), ""), 400)
        videos = sum(max(1, len(r.get("videos") or [])) for r in recs)
        d = diagnose(err, profile=co_name)
        action = {"type": "collabs"}
        if d["tag"] == "other":
            d = {**d, "cause": "Соавторство ещё не принято", "mode": "auto"}
        problems.append(_problem(
            scope="collabs", title=f"Соавтор {co_name} ещё не принял приглашения ({videos})",
            text=err or "ещё не пробовали принять", diag=d, action=action))

    for w in waiting or []:  # задача ждёт повтора (лимит YouTube, сбой сети) — продолжится сама
        at = float(w.get("retry_at") or 0)
        hhmm = time.strftime("%d.%m в %H:%M", time.localtime(at)) if at else ""
        problems.append(_problem(
            scope="wait", title=f"Ждёт повтора: {_clean(w.get('title'), 120)}", text=_clean(w.get("message"), 300),
            diag={"tag": "wait", "cause": "Задача ждёт, когда можно будет продолжить",
                  "mode": "wait", "steps": [f"Ничего делать не нужно: очередь сама продолжит {hhmm}." if hhmm
                                            else "Ничего делать не нужно: очередь сама продолжит."]},
            severity="bad", action=None))

    problems.sort(key=lambda p: ({"manual": 0, "auto": 1, "wait": 2}.get(p["mode"], 3), p["severity"] != "bad"))
    manual = [p for p in problems if p["mode"] == "manual" and p["severity"] == "bad"]
    auto = [p for p in problems if p["mode"] == "auto" and p["action"]]
    fix_jobs = list(dict.fromkeys(p["action"]["job_id"] for p in auto if p["action"]["type"] == "resume"))
    fix_collabs = any(p["action"] and p["action"]["type"] == "collabs" and p["mode"] == "auto" for p in problems)

    total = counts["jobs"]
    if not problems and counts["failed"] == 0:
        tone, title = "ok", f"Всё готово: {counts['ok']} из {total}" if total > 1 else "Всё готово"
    elif counts["ok"] == 0 and counts["failed"]:
        tone, title = "bad", "Не получилось"
    else:
        tone, title = ("bad" if counts["failed"] else "warn"), f"Готово {counts['ok']} из {total}"
    bits = []
    if counts["videos"]:
        bits.append(f"выложено роликов: {counts['videos']}")
    if counts["polls"]:
        bits.append(f"голосований: {counts['polls']}")
    if counts["comments"]:
        bits.append(f"каналов с комментариями: {counts['comments']}")
    if counts["texts"]:
        bits.append(f"текстов: {counts['texts']}")
    if counts["covers"]:
        bits.append(f"обложек: {counts['covers']}")
    attention = [f"требуют вас: {len(manual)}"] if manual else []
    if auto:
        attention.append(f"можно исправить автоматически: {len(auto)}")
    text = "; ".join(bits + attention).capitalize() if (bits or attention) else "Все задачи завершены"
    return {
        "id": uuid.uuid4().hex[:10], "created": time.time(), "started": started, "finished": finished or time.time(),
        "tone": tone, "title": title, "text": text, "counts": counts, "items": items, "problems": problems,
        "fix": {"jobs": fix_jobs, "collabs": fix_collabs, "count": len(fix_jobs) + (1 if fix_collabs else 0)},
        "manual": len(manual), "seen": False,
    }


# ---------- хранилище ----------


def _load() -> list[dict[str, Any]]:
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(reports: list[dict[str, Any]]) -> None:
    keep_after = time.time() - KEEP_DAYS * 86400
    reports = [r for r in reports if float(r.get("created") or 0) >= keep_after][-REPORTS_KEEP:]
    try:
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(reports, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_FILE)
    except OSError as exc:
        log(f"Отчёт не сохранён: {exc}")


def add(report: dict[str, Any]) -> None:
    with _LOCK:
        reports = _load()
        reports.append(report)
        _save(reports)


def get(report_id: str) -> dict[str, Any] | None:
    with _LOCK:
        return next((r for r in _load() if r.get("id") == report_id), None)


def listing() -> list[dict[str, Any]]:
    """Короткий список (новые первыми): без тяжёлых строк."""
    with _LOCK:
        return [{k: r.get(k) for k in ("id", "created", "finished", "tone", "title", "text", "seen", "manual")}
                for r in reversed(_load())]


def mark_seen(report_id: str | None = None) -> None:
    with _LOCK:
        reports = _load()
        for r in reports:
            if report_id is None or r.get("id") == report_id:
                r["seen"] = True
        _save(reports)


def unseen() -> dict[str, Any] | None:
    with _LOCK:
        return next((r for r in reversed(_load()) if not r.get("seen")), None)
