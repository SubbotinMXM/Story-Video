"""AdsPower Local API: профили браузеров, в которых выполнен вход в каналы YouTube.

Программа AdsPower должна быть открыта на этом Mac (API только локальный). Ключ — в заголовке
«Authorization: Bearer …». Сервис отвечает «Too many request per second» на частые запросы — все
запросы программы идут через один замок с паузой между ними.

Запуск профиля даёт адрес CDP (ws.puppeteer): к нему подключается Playwright (connect_over_cdp)
и работает в уже залогиненном браузере. Пароли программа не вводит никогда — вход в каналы делает
пользователь сам в профилях AdsPower.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass

import httpx

from engine.core.logutil import log

DEFAULT_URL = "http://local.adspower.com:50325"
MIN_INTERVAL = 1.1  # сек между запросами: чаще сервис отказывает
_TIMEOUT = httpx.Timeout(60.0, connect=5.0)  # запуск профиля бывает небыстрым
_lock = threading.Lock()
_last = 0.0


class AdsPowerError(RuntimeError):
    pass


class AdsPowerDown(AdsPowerError):
    """Программа AdsPower не отвечает (закрыта, зависла, перезапускается) — ждём её, а не считаем это ошибкой выкладки."""


class ProfileBusy(AdsPowerError):
    """Профиль открыт у другого пользователя/на другом компьютере (командный AdsPower) — открыть нельзя."""


@dataclass(frozen=True)
class Profile:
    id: str  # user_id в AdsPower (profile_id в API v2)
    number: str  # serial_number — номер профиля в списке AdsPower
    name: str
    group_id: str = ""
    group_name: str = ""
    remark: str = ""
    last_open: int = 0

    def label(self) -> str:
        return f"№{self.number} {self.name}".strip() + (f" ({self.group_name})" if self.group_name else "")


@dataclass(frozen=True)
class Group:
    id: str
    name: str
    remark: str = ""


@dataclass(frozen=True)
class Session:
    """Открытый браузер профиля."""

    profile_id: str
    cdp_url: str  # ws://127.0.0.1:…/devtools/browser/… — для Playwright connect_over_cdp
    debug_port: str = ""
    webdriver: str = ""


TRANSPORT_PAUSES = (4, 10, 25)  # AdsPower не ответил — пауза перед следующей попыткой, с
BUSY_WAITS = (5, 15, 30, 40)  # «профиль занят» — освобождаем и ждём столько секунд между попытками


GUARD_SEC = 10 * 60  # нет движения столько минут — браузер завис (большой файл при выборе в Studio идёт без отметок)


class AdsPower:
    def __init__(self, api_key: str, base_url: str = "", *, transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = (base_url or DEFAULT_URL).strip().rstrip("/")
        self.api_key = api_key.strip()
        self._http = httpx.Client(base_url=self.base_url, timeout=_TIMEOUT, trust_env=False, transport=transport)

    def close(self) -> None:
        self._end_guards()
        self._http.close()

    def _end_guards(self) -> None:
        for g in list(getattr(self, "_guards", {}).values()):
            try:
                g.__exit__(None, None, None)
            except Exception:
                pass
        self._guards = {}

    def __enter__(self) -> AdsPower:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- запросы ----------

    def _request_retrying(self, method: str, path: str, params: dict | None, body: dict | None):
        """Запрос к Local API. Таймаут или обрыв — не «AdsPower закрыт»: под нагрузкой (рендер, десяток профилей,
        медленный прокси) он иногда отвечает дольше минуты. Пробуем ещё пару раз, и только потом сдаёмся."""
        for k, pause in enumerate((*TRANSPORT_PAUSES, None)):
            try:
                return self._http.request(method, path, params=params, json=body,
                                          headers={"Authorization": f"Bearer {self.api_key}"})
            except httpx.TransportError as exc:
                if pause is None:
                    raise AdsPowerDown(
                        f"AdsPower не отвечает ({self.base_url}) — открой программу AdsPower на этом Mac "
                        "и проверь, что Local API включён"
                    ) from exc
                log(f"AdsPower: {path} не ответил ({type(exc).__name__}) — пробую снова через {pause} с ({k + 1}/{len(TRANSPORT_PAUSES)})")
                time.sleep(pause)

    def _call(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None,
              retries: int = 3) -> dict:
        global _last
        if not self.api_key:
            raise AdsPowerError("Нет ключа AdsPower API (Настройки → AdsPower)")
        for attempt in range(1, retries + 1):
            with _lock:
                wait = MIN_INTERVAL - (time.monotonic() - _last)
                if wait > 0:
                    time.sleep(wait)
                try:
                    resp = self._request_retrying(method, path, params, body)
                finally:
                    _last = time.monotonic()
            try:
                data = resp.json()
            except ValueError:
                raise AdsPowerError(f"AdsPower: непонятный ответ ({resp.status_code})") from None
            if data.get("code") == 0:
                return data.get("data") or {}
            msg = str(data.get("msg") or "ошибка")
            low = msg.lower()
            if "too many request" in low and attempt < retries:
                time.sleep(MIN_INTERVAL * attempt)
                continue
            busy = re.search(r"is being used by \[?([^\]\s]+)\]?", msg)
            if busy:
                raise ProfileBusy(
                    f"профиль занят: AdsPower считает его открытым у {busy.group(1)} — на другом компьютере или "
                    "под другим входом в AdsPower. Закрой его там. Если он нигде не открыт (например, браузер "
                    "закрылся аварийно) — в AdsPower у этого профиля нажми «Закрыть»/«Освободить» или подожди "
                    "несколько минут и повтори")
            if "api-key" in low or "api key" in low or "apikey" in low:
                raise AdsPowerError(f"AdsPower не принял ключ API ({msg}) — скопируй ключ заново в AdsPower → API")
            raise AdsPowerError(f"AdsPower: {msg}")
        raise AdsPowerError("AdsPower: слишком частые запросы")

    def status(self) -> bool:
        resp = self._call("GET", "/status")
        return resp is not None

    def alive(self) -> bool:
        """AdsPower сейчас отвечает (быстрая проверка, без исключений)."""
        try:
            self._call("GET", "/status", retries=1)
            return True
        except AdsPowerError:
            return False

    # ---------- профили ----------

    def groups(self) -> list[Group]:
        data = self._call("GET", "/api/v1/group/list", params={"page": 1, "page_size": 2000})
        return [Group(str(g.get("group_id") or ""), str(g.get("group_name") or ""), str(g.get("remark") or ""))
                for g in data.get("list") or []]

    def profiles(self, *, group_id: str = "") -> list[Profile]:
        """Все профили (постранично по 100), по номеру."""
        out: list[Profile] = []
        page = 1
        while True:
            params: dict = {"page": page, "page_size": 100}
            if group_id:
                params["group_id"] = group_id
            items = self._call("GET", "/api/v1/user/list", params=params).get("list") or []
            out += [_profile(x) for x in items]
            if len(items) < 100:
                break
            page += 1
        return sorted(out, key=lambda p: (int(p.number) if p.number.isdigit() else 10**9, p.name))

    def profile(self, profile_id: str) -> Profile | None:
        items = self._call("GET", "/api/v1/user/list", params={"user_id": profile_id, "page_size": 1}).get("list") or []
        return _profile(items[0]) if items else None

    # ---------- браузер ----------

    def start(self, profile_id: str, *, headless: bool = False) -> Session:
        self._guard(profile_id)
        session = self._start_retrying(profile_id, headless=headless)
        _claim(profile_id)
        self._watch(profile_id)
        return session

    def _watch(self, profile_id: str) -> None:
        """Сторож зависаний: пока профиль открыт этим потоком, нет ни одного действия (пауза, клик, шаг, проверка
        «Стоп») дольше GUARD_SEC — браузер завис: профиль закрывается, подвисшее действие обрывается ошибкой, выше
        работа начинается заново. Так ночная работа не может простоять часами."""
        if os.environ.get("STORY_VIDEO_NO_GUARD"):
            return
        from engine.publish.scan import guard

        self._guards = getattr(self, "_guards", {})
        old = self._guards.pop(profile_id, None)
        if old is not None:
            old.__exit__(None, None, None)
        g = guard(self.api_key, self.base_url, profile_id, stall=GUARD_SEC, what="браузер профиля")
        g.__enter__()
        self._guards[profile_id] = g

    def _guard(self, profile_id: str) -> None:
        """Профиль занят другим процессом (программа выкладывает, а второй процесс — скрипт, вторая копия приложения —
        полез в тот же браузер): не открываем и не закрываем его. 14.11 закрытие профиля посреди загрузки оборвало
        файл ролика, и ролик ушёл в расписание пустым."""
        other = _claimed_by_other(profile_id)
        if other:
            raise ProfileBusy(f"профиль {profile_id} сейчас занят другим процессом (pid {other}) — идёт работа в его "
                              "браузере. Дождись её конца: открывать и закрывать такой профиль нельзя")

    def _start_retrying(self, profile_id: str, *, headless: bool = False) -> Session:
        """Открыть браузер профиля (уже открыт — AdsPower вернёт его же). → адрес для Playwright.
        «Профиль занят» после того, как браузер закрыли руками или он упал, — это подвисшая отметка AdsPower:
        освобождаем профиль (stop) и пробуем снова, с паузами. Занят по-настоящему (открыт на другом
        компьютере) — понятная ошибка ProfileBusy."""
        for k, wait in enumerate((*BUSY_WAITS, None)):
            try:
                return self._start(profile_id, headless=headless)
            except ProfileBusy:
                if wait is None:
                    raise
                log(f"AdsPower: профиль {profile_id} помечен занятым — освобождаю и пробую снова через {wait} с "
                    f"({k + 1}/{len(BUSY_WAITS)})")
                try:
                    self.stop(profile_id)
                except AdsPowerError:
                    pass
                time.sleep(wait)
        raise AssertionError("недостижимо")

    def _start(self, profile_id: str, *, headless: bool = False) -> Session:
        data = self._call("POST", "/api/v2/browser-profile/start", body={
            "profile_id": profile_id,
            "headless": "1" if headless else "0",
            "last_opened_tabs": "0",  # не тянуть вкладки прошлого раза
            "proxy_detection": "0",  # без вкладки проверки прокси
        })
        ws = (data.get("ws") or {}).get("puppeteer") or ""
        if not ws:
            raise AdsPowerError(f"AdsPower открыл профиль {profile_id}, но не дал адрес управления браузером")
        return Session(profile_id, ws, str(data.get("debug_port") or ""), str(data.get("webdriver") or ""))

    def stop(self, profile_id: str) -> None:
        g = getattr(self, "_guards", {}).pop(profile_id, None)
        if g is not None:
            g.__exit__(None, None, None)
        self._guard(profile_id)
        self._call("POST", "/api/v2/browser-profile/stop", body={"profile_id": profile_id})
        _release(profile_id)

    def is_open(self, profile_id: str) -> bool:
        data = self._call("GET", "/api/v1/browser/active", params={"user_id": profile_id})
        return str(data.get("status") or "").lower() == "active"


def _profile(x: dict) -> Profile:
    try:
        last = int(x.get("last_open_time") or 0)
    except (TypeError, ValueError):
        last = 0
    return Profile(
        id=str(x.get("user_id") or ""),
        number=str(x.get("serial_number") or ""),
        name=str(x.get("name") or ""),
        group_id=str(x.get("group_id") or ""),
        group_name=str(x.get("group_name") or ""),
        remark=str(x.get("remark") or ""),
        last_open=last,
    )


def ping(api_key: str, base_url: str = "") -> str:
    """Для кнопки «Проверить»: связь, ключ и сколько профилей/групп."""
    with AdsPower(api_key, base_url) as ap:
        ap.status()
        profiles = ap.profiles()
        groups = ap.groups()
    names = ", ".join(p.label() for p in profiles[:5]) + (" …" if len(profiles) > 5 else "")
    return (f"AdsPower на связи. Профилей: {len(profiles)}" + (f" ({names})" if names else "")
            + f". Групп: {len(groups)}.")


# ---------- отметка «профиль занят этим процессом» ----------


def _lock_file(profile_id: str):
    from engine.publish.store import publish_dir

    return publish_dir() / "locks" / f"{profile_id}.json"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _claim(profile_id: str) -> None:
    """Профиль открыт этим процессом — пометить (файл с номером процесса). Брошенная отметка (процесс умер) не мешает."""
    try:
        f = _lock_file(profile_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"pid": os.getpid(), "at": time.time()}), encoding="utf-8")
    except OSError:
        pass


def _release(profile_id: str) -> None:
    try:
        f = _lock_file(profile_id)
        if f.is_file() and json.loads(f.read_text(encoding="utf-8")).get("pid") == os.getpid():
            f.unlink()
    except (OSError, ValueError):
        pass


def _claimed_by_other(profile_id: str) -> int:
    """pid другого живого процесса, который держит профиль; 0 — профиль свободен или он наш."""
    try:
        pid = int(json.loads(_lock_file(profile_id).read_text(encoding="utf-8")).get("pid") or 0)
    except (OSError, ValueError, TypeError):
        return 0
    return pid if pid and pid != os.getpid() and _alive(pid) else 0
