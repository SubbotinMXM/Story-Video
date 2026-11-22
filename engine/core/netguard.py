"""Сторож связи: пока работает программа, YouTube должен открываться (у нас — только через VPN).

Раз в минуту проверяем доступ к YouTube. Пропал — очередь встаёт на паузу (задачи не тратят попытки,
профили не открываются), программа сама переподключает VPN (Happ: `happ://connect`, не помогло —
перезапуск Happ), показывает уведомление и проверяет связь каждые 20 с. Вернулась — ждущие задачи
продолжаются сразу, без ожидания следующей попытки по расписанию.

Почему так: Happ выключает туннель, если его окно 10 с не отвечает (Mac под нагрузкой, мало памяти),
и обратно сам не включает — без VPN YouTube недоступен, а очередь до утра падала с «таймаутом».
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from engine.core.logutil import log

# адреса, которые отвечают пустым 204 без cookies и редиректов: доступ к YouTube (страницы и картинки)
PROBE_URLS = ("https://www.youtube.com/generate_204", "https://i.ytimg.com/generate_204")
PROBE_TIMEOUT = 8.0
CHECK_EVERY = 60.0  # связь есть — проверяем раз в минуту
DOWN_CHECK_EVERY = 20.0  # связи нет — чаще: продолжить работу, как только VPN поднимется
CONFIRM_DELAY = 5.0  # одна неудача — ещё не обрыв: перепроверяем через 5 с
# переподключение VPN: через сколько секунд обрыва какое действие (дальше — повтор последних по кругу)
CONNECT_EVERY = 60.0  # «подключиться» (Happ уже подключён — пропустит сам)
RESTART_AFTER = 150.0  # «подключиться» не помогло за 2,5 мин — перезапускаем Happ
RESTART_EVERY = 15 * 60.0
NOTIFY_STUCK_AFTER = 10 * 60.0  # столько без связи — напоминаем человеку, что VPN сам не поднимается

VPN_APPS = {
    # приложение VPN → (путь, команда «подключиться»)
    "Happ": ("/Applications/Happ.app", "happ://connect"),
}


class _State:
    def __init__(self) -> None:
        self.up = True
        self.down_since = 0.0
        self.reason = ""
        self.checked_at = 0.0
        self.connects = 0  # сколько раз за этот обрыв просили VPN подключиться
        self.restarts = 0
        self.last_connect = 0.0
        self.last_restart = 0.0
        self.stuck_notified = False
        self.action = ""  # что делаем сейчас (для интерфейса)


_state = _State()
_lock = threading.Lock()
_wake = threading.Event()
_listeners: list[Callable[[dict[str, Any]], None]] = []


def on_change(fn: Callable[[dict[str, Any]], None]) -> None:
    """Подписка на смену состояния (интерфейс, очередь)."""
    _listeners.append(fn)


def snapshot() -> dict[str, Any]:
    with _lock:
        s = _state
        return {"up": s.up, "down_since": s.down_since, "reason": s.reason, "checked_at": s.checked_at,
                "connects": s.connects, "restarts": s.restarts, "action": s.action}


def is_down() -> bool:
    return not _state.up


def probe(timeout: float = PROBE_TIMEOUT) -> tuple[bool, str]:
    """Открывается ли YouTube прямо сейчас (любой из адресов). (да/нет, причина)."""
    import httpx

    err = ""
    for url in PROBE_URLS:
        try:
            r = httpx.get(url, timeout=timeout, follow_redirects=False)
            if r.status_code < 400:
                return True, ""
            err = f"{url}: ответ {r.status_code}"
        except Exception as exc:  # noqa: BLE001 — любой сбой соединения = нет доступа
            err = f"{type(exc).__name__}: {str(exc).splitlines()[0][:160] if str(exc) else ''}".strip()
    return False, err


def enabled() -> bool:
    return not os.environ.get("STORY_VIDEO_NO_NETGUARD")


def check_now() -> bool:
    """Проверить связь сейчас (задача упала — из-за сети?). Обрыв подтверждаем второй проверкой."""
    if not enabled():
        return True
    up, err = probe()
    if not up:
        time.sleep(CONFIRM_DELAY)
        up, err = probe()
    _set(up, err)
    if not up:
        _wake.set()  # сторож — сразу переподключать, не дожидаясь своей минуты
    return up


def kick() -> None:
    """Попросить сторожа проверить связь вне очереди."""
    _wake.set()


def _set(up: bool, reason: str) -> None:
    now = time.time()
    with _lock:
        s = _state
        s.checked_at = now
        changed = up != s.up
        if up:
            was = s.down_since
            s.up, s.reason, s.action = True, "", ""
            s.down_since = 0.0
            s.connects = s.restarts = 0
            s.last_connect = s.last_restart = 0.0
            s.stuck_notified = False
        else:
            s.reason = reason
            if s.up:
                s.up, s.down_since = False, now
    if not changed:
        return
    if up:
        mins = (now - was) / 60 if was else 0
        log(f"Связь с YouTube вернулась (не было {mins:.0f} мин) — очередь продолжает работу")
        if mins >= 2:
            _notify("Story Video: связь вернулась", f"YouTube снова доступен (не было {mins:.0f} мин) — очередь продолжена.")
    else:
        log(f"⚠ Нет доступа к YouTube ({reason}) — очередь на паузе, переподключаю VPN")
        _notify("Story Video: нет доступа к YouTube",
                "Похоже, отключился VPN. Очередь на паузе, переподключаю VPN сам.")
    _emit()


def _emit() -> None:
    snap = snapshot()
    for fn in list(_listeners):
        try:
            fn(snap)
        except Exception as exc:  # noqa: BLE001 — подписчик не должен ронять сторожа
            log(f"Сторож связи: подписчик упал ({exc})")


def _notify(title: str, text: str) -> None:
    try:
        from server.notify import notify

        notify(title, text)
    except Exception:  # noqa: BLE001
        pass


# ---------- переподключение VPN ----------


def vpn_app(cfg: dict[str, Any]) -> tuple[str, str, str] | None:
    """(имя, путь, команда «подключиться») VPN, который умеем переподключать; None — выключено/не установлен."""
    if cfg.get("vpn_reconnect") is False:
        return None
    name = str(cfg.get("vpn_app") or "Happ")
    app = VPN_APPS.get(name)
    if app is None or not Path(app[0]).is_dir():
        return None
    return name, app[0], app[1]


def _running(name: str) -> bool:
    try:
        return subprocess.run(["pgrep", "-x", name], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _connect(name: str, path: str, url: str) -> None:
    """Попросить VPN подключиться (окно на передний план не выходит)."""
    if not _running(name):
        subprocess.run(["open", "-g", "-a", path], capture_output=True, timeout=20, check=False)
        time.sleep(6)
    subprocess.run(["open", "-g", url], capture_output=True, timeout=20, check=False)


def _restart(name: str, path: str, url: str) -> None:
    """Перезапустить VPN-приложение: окно Happ могло зависнуть и потерять связь со своей службой."""
    subprocess.run(["osascript", "-e", f'tell application "{name}" to quit'], capture_output=True, timeout=20,
                   check=False)
    for _ in range(20):
        if not _running(name):
            break
        time.sleep(0.5)
    else:
        subprocess.run(["pkill", "-x", name], capture_output=True, timeout=5, check=False)
        time.sleep(2)
    subprocess.run(["open", "-g", "-a", path], capture_output=True, timeout=20, check=False)
    time.sleep(8)
    subprocess.run(["open", "-g", url], capture_output=True, timeout=20, check=False)


def _repair(cfg: dict[str, Any]) -> None:
    """Связи нет: по расписанию — «подключиться», потом перезапуск Happ; долго не выходит — напомнить человеку."""
    app = vpn_app(cfg)
    now = time.time()
    s = _state
    down_for = now - (s.down_since or now)
    if app is not None:
        name, path, url = app
        try:
            restart_due = down_for >= RESTART_AFTER and now - s.last_restart >= RESTART_EVERY
            if restart_due:
                with _lock:
                    s.restarts += 1
                    s.last_restart = s.last_connect = now
                    s.action = f"перезапускаю {name} (раз {s.restarts})"
                log(f"Сторож связи: VPN не поднялся — перезапускаю {name}")
                _emit()
                _restart(name, path, url)
            elif now - s.last_connect >= CONNECT_EVERY:
                with _lock:
                    s.connects += 1
                    s.last_connect = now
                    s.action = f"переподключаю {name} (попытка {s.connects})"
                log(f"Сторож связи: прошу {name} подключиться (попытка {s.connects})")
                _emit()
                _connect(name, path, url)
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"Сторож связи: не получилось управлять {name}: {exc}")
    elif not s.action:
        with _lock:
            s.action = "жду, пока появится связь (переподключение VPN выключено или VPN не найден)"
        _emit()
    if down_for >= NOTIFY_STUCK_AFTER and not s.stuck_notified:
        s.stuck_notified = True
        _notify("Story Video: VPN не поднимается",
                f"YouTube недоступен уже {down_for / 60:.0f} мин. Проверь VPN — очередь продолжит сама, как только он заработает.")


def loop(load_config: Callable[[], dict[str, Any]]) -> None:
    """Фоновая проверка, пока работает приложение."""
    if not enabled() or os.environ.get("STORY_VIDEO_NO_BACKGROUND") or sys.platform != "darwin":
        return
    time.sleep(10)  # дать приложению подняться
    while True:
        try:
            up, err = probe()
            if not up:
                time.sleep(CONFIRM_DELAY)
                up, err = probe()
            _set(up, err)
            if not up:
                _repair(load_config())
        except Exception as exc:  # noqa: BLE001 — сторож не должен умирать
            log(f"Сторож связи: сбой проверки ({exc})")
        _wake.wait(CHECK_EVERY if _state.up else DOWN_CHECK_EVERY)
        _wake.clear()
