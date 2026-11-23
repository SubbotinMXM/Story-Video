"""Обновление программы из репозитория (то, что клиенты делали руками: git pull) — кнопкой и по расписанию.

- check(): git fetch + сколько новых изменений на сервере и что в них (без изменения файлов);
- pull(): git pull --ff-only (только «перемотка»: ничего не склеивает и не теряет);
- restart(): запускает ./run.sh после выхода этой копии — run.sh сам доставит новые библиотеки и пересоберёт, что нужно;
- расписание (config update_auto = {enabled, time}): раз в сутки в time (МСК) — проверка, и если есть новое —
  обновление. Идущую работу НЕ прерываем: обновление и перезапуск ждут, пока очередь освободится.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from engine.core.logutil import log
from engine.publish.schedule import MSK
from engine.publish.store import LOCK, read_json, write_json

REPO = Path(__file__).resolve().parents[1]
STATE_FILE = "update_state.json"
DEFAULT = {"enabled": False, "time": "04:00"}
CHECK_EVERY = 30 * 60  # как часто смотреть, есть ли новое (чтобы значок в программе был живым)
GIT_TIMEOUT = 45

_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=15",
        "LC_ALL": "C"}
_pull_lock = threading.Lock()


class UpdateError(RuntimeError):
    pass


def normalize(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    t = str(raw.get("time") or "").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        t = DEFAULT["time"]
    else:
        t = f"{int(m.group(1)):02d}:{m.group(2)}"
    return {"enabled": bool(raw.get("enabled")), "time": t}


def _git(*args: str, repo: Path = REPO, timeout: float = GIT_TIMEOUT) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=timeout, env=_ENV)
    except FileNotFoundError:
        raise UpdateError("на этом Mac нет git — поставь Command Line Tools: xcode-select --install") from None
    except subprocess.TimeoutExpired:
        raise UpdateError("сервер обновлений не отвечает (таймаут) — проверь интернет и попробуй позже") from None
    return p.returncode, (p.stdout + p.stderr).strip()


def _path(line: str) -> str:
    """Путь из строки git status --porcelain (первая строка вывода уже без начального пробела)."""
    return re.sub(r"^\S+\s+", "", line.strip())


def _nice(err: str) -> str:
    low = err.lower()
    if "permission denied" in low or "publickey" in low or "authentication failed" in low:
        return "нет доступа к репозиторию обновлений (ключ или логин GitHub не подходят)"
    if "could not resolve" in low or "unable to access" in low or "network" in low or "timed out" in low:
        return "нет связи с сервером обновлений — проверь интернет"
    return err.splitlines()[0][:200] if err else "неизвестная ошибка git"


def _upstream(repo: Path) -> str:
    rc, out = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", repo=repo)
    if rc == 0 and out:
        return out
    rc, branch = _git("rev-parse", "--abbrev-ref", "HEAD", repo=repo)
    return f"origin/{branch if rc == 0 and branch != 'HEAD' else 'main'}"


def _release_of(repo: Path, ref: str) -> tuple[str, str]:
    """(версия, название) верхнего выпуска в changelog.json на сервере."""
    rc, out = _git("show", f"{ref}:changelog.json", repo=repo)
    if rc != 0:
        return "", ""
    try:
        top = (json.loads(out).get("releases") or [{}])[0]
        return str(top.get("version") or ""), str(top.get("title") or "")
    except (ValueError, AttributeError, IndexError):
        return "", ""


def status(repo: Path = REPO, *, fetch: bool = False) -> dict:
    """Что с обновлениями: {ok, error, current, behind, commits, version, title, dirty, checked_at}.
    fetch=True — сначала спросить сервер (git fetch); иначе по тому, что известно с прошлой проверки."""
    out: dict = {"ok": True, "error": "", "current": "", "behind": 0, "commits": [], "version": "", "title": "",
                 "dirty": [], "checked_at": 0.0}
    try:
        if fetch:
            rc, err = _git("fetch", "--quiet", "origin", repo=repo)
            if rc != 0:
                raise UpdateError(_nice(err))
            out["checked_at"] = time.time()
        rc, head = _git("rev-parse", "--short", "HEAD", repo=repo)
        if rc != 0:
            raise UpdateError("папка программы — не репозиторий git: обновляться нечем")
        out["current"] = head
        up = _upstream(repo)
        rc, n = _git("rev-list", "--count", f"HEAD..{up}", repo=repo)
        if rc == 0 and n.isdigit():
            out["behind"] = int(n)
        if out["behind"]:
            _rc, log_ = _git("log", f"HEAD..{up}", "--format=%s", "-n", "30", repo=repo)
            out["commits"] = [ln for ln in log_.splitlines() if ln.strip()]
            out["version"], out["title"] = _release_of(repo, up)
        _rc, dirty = _git("status", "--porcelain", "--untracked-files=no", repo=repo)
        out["dirty"] = [_path(ln) for ln in dirty.splitlines() if ln.strip()][:20]
    except UpdateError as exc:
        out.update(ok=False, error=str(exc))
    if not out["checked_at"]:
        out["checked_at"] = float(read_json(STATE_FILE, {}).get("checked_at") or 0)
    return out


def pull(repo: Path = REPO) -> str:
    """git pull --ff-only. → вывод git. Ошибка — UpdateError с понятным объяснением."""
    with _pull_lock:
        rc, err = _git("pull", "--ff-only", "--quiet", repo=repo, timeout=180)
        if rc != 0:
            low = err.lower()
            if "overwritten by merge" in low or "local changes" in low or "would be overwritten" in low:
                _rc, dirty = _git("status", "--porcelain", "--untracked-files=no", repo=repo)
                files = ", ".join(_path(ln) for ln in dirty.splitlines()[:5])
                raise UpdateError(f"в папке программы есть правки, которые мешают обновлению ({files}) — "
                                  "сохрани или отмени их и повтори")
            if "not possible to fast-forward" in low or "diverging" in low:
                raise UpdateError("у этой копии есть свои изменения, которых нет на сервере — обновление "
                                  "«перемоткой» невозможно, нужна ручная работа")
            raise UpdateError(_nice(err))
        return err


# ---------- состояние ----------


def state() -> dict:
    return read_json(STATE_FILE, {})


def _update_state(**values) -> None:
    with LOCK:
        st = state()
        st.update(values)
        write_json(STATE_FILE, st)


def pending() -> dict:
    """Ждущее обновление: {mode, since} — «обновить и перезапустить, когда очередь освободится». {} — нет."""
    p = state().get("pending")
    return p if isinstance(p, dict) else {}


def set_pending(mode: str = "update", source: str = "user") -> None:
    _update_state(pending={"mode": mode, "since": time.time(), "source": source})


def clear_pending() -> None:
    _update_state(pending={})


# ---------- перезапуск ----------


def restart(pid: int | None = None, argv: list[str] | None = None, *, repo: Path = REPO, exit_after: float | None = 1.5) -> None:
    """Запустить ./run.sh, когда эта копия закроется, и закрыть её. run.sh докачает библиотеки и соберёт интерфейс,
    если обновление их поменяло. Окно закроется на секунду-две и откроется снова."""
    pid = pid or os.getpid()
    args = " ".join(_q(a) for a in (sys.argv[1:] if argv is None else argv))
    log_path = _q(str(repo / "data" / "restart.log"))
    script = (f'while kill -0 {pid} 2>/dev/null; do sleep 0.5; done; cd {_q(str(repo))} && '
              f'exec ./run.sh {args} >> {log_path} 2>&1')
    subprocess.Popen(["/bin/sh", "-c", script], start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log("Обновление: перезапускаю программу…")
    if exit_after is not None:
        threading.Timer(exit_after, os._exit, [0]).start()


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def apply_and_restart(manager, *, restart_fn=restart) -> dict:
    """Подтянуть обновление и перезапустить. Очередь сохраняется (идущие задачи → «Прервано закрытием», их можно
    продолжить). → итог проверки."""
    st = status(fetch=True)
    if not st["ok"]:
        raise UpdateError(st["error"])
    if not st["behind"]:
        clear_pending()
        return st
    pull()
    log(f"Обновление: подтянуто {st['behind']} изм. ({st['version'] or st['current']})")
    clear_pending()
    _update_state(last_applied=time.time())
    try:
        manager._save()
        from server.jobs import QUEUED, RUNNING, mark_resume

        with manager._lock:  # идущие и ждущие задачи после перезапуска продолжатся сами
            mark_resume([j.id for j in manager._jobs if j.status in (QUEUED, RUNNING)])
    except Exception:  # noqa: BLE001
        pass
    restart_fn()
    return st


# ---------- расписание и фон ----------


def _at(day: datetime, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def due(setting: dict, now: datetime | None = None) -> bool:
    setting = normalize(setting)
    now = (now or datetime.now(MSK)).astimezone(MSK)
    with LOCK:
        st = state()
        sig = f"{setting['enabled']}|{setting['time']}"
        if st.get("sig") != sig:  # включили или сменили время — отсчёт с этого момента
            st.update(sig=sig, since=now.timestamp())
            write_json(STATE_FILE, st)
    if not setting["enabled"]:
        return False
    slot = _at(now, setting["time"])
    return now >= slot and slot.timestamp() > float(st.get("since") or 0) and st.get("last_day") != now.date().isoformat()


def next_run(setting: dict, now: datetime | None = None) -> float:
    setting = normalize(setting)
    if not setting["enabled"]:
        return 0.0
    now = (now or datetime.now(MSK)).astimezone(MSK)
    st = state()
    since = float(st.get("since") or 0) if st.get("sig") == f"{setting['enabled']}|{setting['time']}" else now.timestamp()
    slot = _at(now, setting["time"])
    if st.get("last_day") != now.date().isoformat() and slot.timestamp() > since:
        return max(slot, now).timestamp()
    return (slot + timedelta(days=1)).timestamp()


def tick(manager, cfg: dict, *, restart_fn=restart, status_fn=status) -> str:
    """Один проход фона. → что сделано ('' — ничего). Идущая работа не прерывается: ждущее обновление применяется,
    только когда очередь пуста."""
    setting = normalize(cfg.get("update_auto"))
    if due(setting):
        _update_state(last_day=datetime.now(MSK).date().isoformat())
        st = status_fn(fetch=True)
        _update_state(checked_at=st.get("checked_at") or time.time(), last_auto_at=time.time())
        if st["ok"] and st["behind"]:
            set_pending("update", "auto")
            log(f"Обновление по расписанию: на сервере {st['behind']} нов. изм. ({st['version'] or '…'}) — "
                "обновлюсь, когда очередь освободится")
        elif not st["ok"]:
            log(f"Обновление по расписанию: {st['error']}")
    if pending() and not manager.busy():
        try:
            apply_and_restart(manager, restart_fn=restart_fn)
            return "applied"
        except UpdateError as exc:
            clear_pending()
            _update_state(last_error=str(exc), last_error_at=time.time())
            log(f"Обновление не вышло: {exc}")
            return "error"
    if time.time() - float(state().get("checked_at") or 0) > CHECK_EVERY:
        st = status_fn(fetch=True)
        _update_state(checked_at=st.get("checked_at") or time.time())
        return "checked"
    return ""


def loop(manager, load_config, every: float = 30.0) -> None:
    while True:
        try:
            tick(manager, load_config())
        except Exception as exc:  # noqa: BLE001 — сбой проверки не должен останавливать приложение
            log(f"Обновление: {exc}")
        time.sleep(every)
