"""«Обновить каналы» профиля: открыть браузер, прочитать каналы — и ни при каком сбое не оставить профиль
занятым или запрос висящим.

- поиск идёт с пределом по времени (SCAN_TIMEOUT): завис (вкладка перестала отвечать) — профиль закрывается
  в AdsPower, зависшие действия браузера от этого обрываются, ответ — понятная ошибка; следующее нажатие
  сразу открывает новую сессию;
- браузер закрыли руками / он упал посреди поиска — профиль освобождается (stop) и поиск начинается заново
  (один раз);
- профиль, который ты открыл сам, после поиска не закрывается (кроме зависания — тогда иначе нельзя).
"""

from __future__ import annotations

import threading
import time

from engine.core.logutil import log
from engine.publish.adspower import AdsPower, AdsPowerError

SCAN_TIMEOUT = 10 * 60
RELEASE_PAUSE = 5.0


class Pulse:
    """Признак жизни долгой работы в браузере: каждый шаг отмечается (touch) — нет отметок дольше STALL_SEC,
    значит браузер завис на каком-то чтении страницы (у них нет своего предела по времени)."""

    def __init__(self, progress=None) -> None:
        self.last = time.monotonic()
        self.progress = progress

    def touch(self, frac: float | None = None, msg: str = "") -> None:
        self.last = time.monotonic()
        if msg:
            log(msg)
        if self.progress is not None and msg:
            self.progress(frac if frac is not None else 0.0, msg)


STALL_SEC = 3 * 60


def scan_profile(api_key: str, url: str, profile_id: str, *, headless: bool = True, timeout: float = SCAN_TIMEOUT,
                 cancel=None, progress=None, env: dict | None = None) -> list:
    """Каналы профиля. env — сюда же: как выглядит Studio в профиле (youtube.studio_env)."""
    from engine.publish import youtube

    return in_profile(api_key, url, profile_id,
                      lambda cdp, pulse: youtube.scan_channels(cdp, cancel=cancel, pulse=pulse, env=env),
                      headless=headless, timeout=timeout, cancel=cancel, progress=progress,
                      what="поиск каналов", again="нажми «Найти каналы» ещё раз")


def in_profile(api_key: str, url: str, profile_id: str, work, *, headless: bool = True,
               timeout: float = SCAN_TIMEOUT, cancel=None, progress=None, stall: float | None = None,
               what: str = "работа в профиле", again: str = "попробуй ещё раз"):
    """Открыть профиль, выполнить work(cdp_url, pulse) в его браузере и закрыть профиль (если его не открыл ты сам).
    - нет движения (pulse.touch) дольше stall или всё дольше timeout — завис: профиль закрывается в AdsPower
      (зависшие действия браузера от этого обрываются) и работа начинается заново (один раз);
    - браузер закрыли руками / он упал — профиль освобождается и работа начинается заново (один раз);
    - «Стоп» (cancel) — сразу: профиль, открытый программой, закрывается."""
    from engine.core.cancel import CancelledError
    from engine.publish import youtube

    stall = STALL_SEC if stall is None else stall
    with AdsPower(api_key, url) as ap:
        was_open = ap.is_open(profile_id)  # профиль открыт тобой — не закрываем его после работы
    for attempt in (1, 2):
        box: dict = {}
        pulse = Pulse(progress)
        pulse.touch(0.02, f"{what.capitalize()}: открываю профиль" + (" (скрыто)" if headless else ""))

        def run() -> None:
            try:
                with AdsPower(api_key, url) as ap:
                    session = ap.start(profile_id, headless=headless)
                    pulse.touch()
                    try:
                        box["result"] = work(session.cdp_url, pulse)
                    finally:
                        # брошенная попытка (зависла, профиль уже перезапущен) не закрывает профиль следующей
                        if not was_open and not box.get("abandoned"):
                            _stop(ap, profile_id)
            except BaseException as exc:  # noqa: BLE001 — передаём в основной поток как есть
                box["error"] = exc

        t = threading.Thread(target=run, name=f"profile-{profile_id}", daemon=True)
        t.start()
        started = time.monotonic()
        hung = ""
        while t.is_alive():
            t.join(1.0)
            if cancel is not None and cancel.is_cancelled():
                log(f"{what.capitalize()} {profile_id}: остановлено — закрываю профиль")
                box["abandoned"] = True
                if not was_open:
                    release(api_key, url, profile_id)  # закрытие профиля обрывает зависшие действия браузера
                t.join(3)
                raise CancelledError("Остановлено пользователем")
            now = time.monotonic()
            if now - pulse.last > stall:
                hung = f"нет движения {stall / 60:.0f} мин"
            elif now - started > timeout:
                hung = f"дольше {timeout / 60:.0f} мин"
            if hung:
                break
        if hung:
            log(f"{what.capitalize()} {profile_id}: завис ({hung}) — закрываю профиль в AdsPower")
            box["abandoned"] = True
            release(api_key, url, profile_id)
            t.join(30)
            if attempt == 1:
                pulse.touch(0.02, f"{what.capitalize()}: браузер завис ({hung}) — профиль перезапущен, начинаю заново")
                continue
            raise AdsPowerError(f"{what}: завис (браузер профиля перестал отвечать) — профиль закрыт и "
                                f"освобождён, {again}")
        err = box.get("error")
        if err is None:
            return box["result"]
        if attempt == 1 and youtube.browser_lost(err):
            log(f"{what.capitalize()} {profile_id}: браузер профиля закрылся — освобождаю профиль и начинаю заново")
            release(api_key, url, profile_id)
            continue
        raise err
    raise AssertionError("недостижимо")


def release(api_key: str, url: str, profile_id: str) -> None:
    """Освободить профиль в AdsPower (закрыть его сессию), чтобы следующее открытие прошло."""
    try:
        with AdsPower(api_key, url) as ap:
            _stop(ap, profile_id)
    except AdsPowerError:
        pass
    time.sleep(RELEASE_PAUSE)


def _stop(ap: AdsPower, profile_id: str) -> None:
    try:
        ap.stop(profile_id)
    except AdsPowerError as exc:
        log(f"AdsPower: закрыть профиль {profile_id} не вышло ({exc}) — возможно, уже закрыт")
