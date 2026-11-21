"""YouTube Studio в браузере профиля AdsPower (Playwright по адресу CDP).

Поиск каналов: список каналов аккаунтов профиля (youtube.com/channel_switcher), их ID (по @адресу),
плейлисты и последняя дата ролика из YouTube Studio. Studio работает только с активным каналом профиля —
переключаемся кликом в списке каналов, как человек, и проверяем, что открыт нужный.
Загрузка: диалог Studio «Details → Video elements → Checks → Visibility», перед каждым действием — пауза
1.5–5.5 с. Время выхода — по Москве, Studio считает в поясе профиля: пересчитываем в него.
Соавторство: на странице видео — приглашение по ссылке; в профиле соавтора — «Принять».
Язык Studio у профилей разный: кнопки — по внутренним меткам, даты — в формате языка страницы (Intl).
"""

from __future__ import annotations

import random
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from engine.core.cancel import CancelledError, CancelToken
from engine.core.logutil import log
from engine.publish import human, pause


class StudioError(RuntimeError):
    """Что-то в YouTube Studio пошло не так на этом канале (остальные каналы продолжаются)."""


class UploadLimit(StudioError):
    """YouTube: суточный лимит загрузок на канале. until — когда пробовать снова (unix-время)."""

    def __init__(self, message: str, until: float) -> None:
        super().__init__(message)
        self.until = until


class NeedsUser(StudioError):
    """Окно, которое программа не проходит сама: вход, «подтвердите, что это вы», новые правила."""


NOT_READY = "загрузка в YouTube Studio подключается на следующем этапе (нужны профили с выполненным входом)"
# работа с живой Studio подключена (поиск каналов и загрузка)
READY = True


def require_ready(what: str = "Загрузка") -> None:
    if not READY:
        raise StudioError(f"{what}: {NOT_READY}")


@dataclass(frozen=True)
class UploadJob:
    file: Path
    title: str
    description: str
    thumbnail: Path | None  # None — без обложки (поставишь сам в Studio)
    publish_at: datetime  # по Москве
    playlist_hint: list[str]  # плейлисты канала по последнему поиску (ровно один — выбираем его)
    coauthors: list  # registry.Target — каналы-соавторы
    private_only: bool = False  # проверка: в конце «Private» + «Save» вместо запланированной публикации
    publish_now: bool = False  # выложить сразу («Public» + «Publish»), без расписания


@dataclass(frozen=True)
class Uploaded:
    url: str
    notes: list[str]  # «плейлистов 3 — оставь один», «соавторство недоступно» …
    invites: dict | None = None  # ключ соавтора («профиль/канал») → ссылка-приглашение
    file_ok: bool = False  # Studio ПОКАЗАЛА видеофайл (длительность в списке роликов): только тогда файл с компьютера можно удалять


SWITCHER = "https://www.youtube.com/channel_switcher"
STUDIO = "https://studio.youtube.com"
MAX_ACCOUNTS = 10  # Google пускает в браузер не больше 10 аккаунтов
_UC = re.compile(r'"(?:externalId|channelId)":"(UC[\w-]{22})"')


@contextmanager
def studio_page(cdp_url: str):
    """Новая вкладка в браузере профиля (на переднем плане: в фоне Studio подвисает)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        page.bring_to_front()
        fit_window(page)
        try:
            yield page
        finally:
            try:
                page.close()
            except Exception:
                pass


MIN_WINDOW = (1200, 650)  # окно меньше — Studio (редакторы заставки, подсказок, дат) не помещается: растим. 1280×713 у рабочих профилей — не трогаем
TARGET_WINDOW = (1366, 860)  # до какого размера растим


def fit_window(page, *, force: bool = False) -> tuple[int, int]:
    """Окно браузера профиля — не меньше MIN_WINDOW. В профилях AdsPower окно бывает крошечным (800×513 у профиля
    «Жизненные рассказы 2»): редактор конечной заставки не помещается, кнопка «Save» оказывается за краем экрана,
    и выкладка падает «element is outside of the viewport». Сначала меняем само окно (как это сделал бы человек),
    не вышло — размер области страницы. → (ширина, высота) области страницы после правки."""
    min_w, min_h = MIN_WINDOW
    want_w, want_h = TARGET_WINDOW

    def size() -> tuple[int, int]:
        w, h = page.evaluate("() => [window.innerWidth, window.innerHeight]")
        return int(w), int(h)

    try:
        w, h = size()
        if not force and w >= min_w and h >= min_h:
            return w, h
        log(f"Окно браузера мало ({w}×{h}) — увеличиваю до {want_w}×{want_h}")
        try:
            cdp = page.context.new_cdp_session(page)
            win = cdp.send("Browser.getWindowForTarget")["windowId"]
            cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": {"windowState": "normal"}})
            cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": {"width": want_w + 16, "height": want_h + 110}})
            page.wait_for_timeout(700)
            w, h = size()
            if w < want_w or h < want_h:  # экран меньше — на весь экран
                cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": {"windowState": "maximized"}})
                page.wait_for_timeout(700)
                w, h = size()
        except Exception as exc:
            log(f"Окно браузера не удалось изменить ({str(exc)[:100]})")
        if w < want_w or h < want_h:
            # окно не растёт (меньше экрана, ограничение профиля) — задаём размер области страницы
            page.set_viewport_size({"width": max(w, want_w), "height": max(h, want_h)})
            page.wait_for_timeout(300)
            w, h = size()
        log(f"Окно браузера: {w}×{h}")
        return w, h
    except Exception as exc:
        log(f"Размер окна не проверен ({str(exc)[:100]})")
        return 0, 0


def _check_signed_in(page) -> None:
    url = page.url
    if "accounts.google.com" in url or "/signin" in url or "ServiceLogin" in url:
        raise NeedsUser("в профиле не выполнен вход в Google — зайди в профиль вручную и войди в аккаунт")


def _switcher_items(page, authuser: int) -> list[dict]:
    page.goto(f"{SWITCHER}?authuser={authuser}", wait_until="domcontentloaded")
    _check_signed_in(page)
    try:
        page.wait_for_selector("ytd-account-item-renderer", timeout=20000)
    except Exception:
        return []
    return page.eval_on_selector_all("ytd-account-item-renderer", """els => els.map(e => {
        const d = e.data || {};
        const tokens = ((d.serviceEndpoint || {}).selectActiveIdentityEndpoint || {}).supportedTokens || [];
        const gaia = (tokens.find(t => t.accountStateToken) || {}).accountStateToken || {};
        const lines = e.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
        return {name: lines[0] || '', handle: lines.find(s => s.startsWith('@')) || '',
                gaia: gaia.obfuscatedGaiaId || '', selected: !!d.isSelected};
    })""")


def channel_id(page, handle: str) -> str:
    page.goto(f"https://www.youtube.com/{handle}", wait_until="domcontentloaded")
    m = _UC.search(page.content())
    if not m:
        raise StudioError(f"не нашёл ID канала {handle}")
    return m.group(1)


SWITCH_WAIT_SEC = 120  # YouTube подхватывает выбранный канал за 10–50 с (живая проверка) — с запасом


def switch_to(page, channel, *, cancel: CancelToken | None = None) -> None:
    """Сделать канал активным в профиле (клик в списке каналов) и убедиться, что он активен ВЕЗДЕ:
    на youtube.com (от его имени пишутся комментарии) и в Studio (выкладка).

    Канал ищется в списке по @-имени (оно уникально), проверка — по ID канала: одинаковые названия
    каналов (два «Точки на карте» на одном аккаунте) ни на что не влияют. Прямой адрес канала в Studio
    для проверки не годится: при нескольких каналах на аккаунте он открывается и у неактивного канала,
    а у только что выбранного — «нет доступа», пока Studio не открыть с главной."""
    if _is_active(page, channel.id):
        return  # уже активен — лишних кликов не делаем
    for item_i, item in enumerate(_switcher_items(page, channel.authuser)):
        if item["handle"] == channel.handle:
            for attempt in range(2):
                pause(cancel)
                try:
                    # клик переключает канал перезагрузкой страницы — дождаться её, иначе переключение оборвётся
                    with page.expect_navigation(wait_until="commit", timeout=30000):
                        human.click(page, page.locator("ytd-account-item-renderer").nth(item_i), timeout=20000)
                    break
                except Exception:
                    if attempt:
                        raise StudioError(f"не нажался канал {channel.handle} в списке каналов") from None
                    _switcher_items(page, channel.authuser)  # страница не успела — открыть список заново
            break
    else:
        raise StudioError(f"канала {channel.handle or channel.name} нет в списке каналов аккаунта — обнови каналы профиля")
    deadline = time.monotonic() + SWITCH_WAIT_SEC
    seen = ""
    while time.monotonic() < deadline:  # YouTube и Studio подхватывают новый активный канал не сразу
        if cancel:
            cancel.check()
        if _is_active(page, channel.id):
            return
        seen = active_channel(page)
        page.wait_for_timeout(3000)
    raise StudioError(f"не переключился на канал {channel.handle or channel.name} ({channel.id}) — "
                      f"активен другой канал{f' ({seen})' if seen else ''}")


def active_channel(page) -> str:
    """ID канала, от имени которого сейчас действует YouTube в профиле: youtube.com/profile перенаправляет
    на него. Адрес после перенаправления известен сразу — полную загрузку тяжёлой страницы канала (с видео)
    не ждём: она может не наступить вовсе, и проверка зависла бы."""
    try:
        goto(page, "https://www.youtube.com/profile", wait_until="commit", timeout=30000)
    except StudioError as exc:
        if not _UC_URL.search(page.url):
            raise StudioError(f"youtube.com не открылся ({exc})") from None
    _check_signed_in(page)
    m = _UC_URL.search(page.url)
    if m:
        return m.group(1)
    try:  # перенаправило на /@имя — ID канала из ссылки canonical
        href = page.locator('link[rel="canonical"]').first.get_attribute("href", timeout=15000) or ""
    except Exception:
        href = ""
    m = _UC_URL.search(href)
    return m.group(1) if m else ""


_UC_URL = re.compile(r"/channel/(UC[\w-]{22})")


def _is_active(page, cid: str) -> bool:
    """Канал активен и на youtube.com, и в Studio (главная Studio открывает активный канал и синхронизирует
    Studio с переключением — после неё прямые адреса канала работают)."""
    if active_channel(page) != cid:
        return False
    return studio_channel(page, STUDIO) == cid or studio_channel(page, f"{STUDIO}/channel/{cid}/videos/upload") == cid


GOTO_TRIES = 3  # обрыв связи с Studio (ERR_CONNECTION_CLOSED и т.п. — сеть/прокси профиля) — повторяем загрузку


def goto(page, url: str, *, wait_until: str = "domcontentloaded", timeout: int = 45000) -> None:
    """Открыть адрес. Обрыв связи (net::ERR_…, страница ошибки браузера) — повтор с паузой; после
    GOTO_TRIES попыток — StudioError. Просто долгая загрузка — не ошибка: дальше проверяем, что успело."""
    for attempt in range(1, GOTO_TRIES + 1):
        broken = ""
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout)
        except Exception as exc:
            first = str(exc).splitlines()[0][:100]
            if "net::ERR_" in first and "ERR_ABORTED" not in first:
                broken = first
            else:
                log(f"Studio: страница открывается долго ({first}) — проверяю, что успело загрузиться")
        if not broken and page.url.startswith("chrome-error://"):
            broken = "страница ошибки браузера"
        if not broken:
            return
        if attempt == GOTO_TRIES:
            raise StudioError(f"страница не открывается — связь обрывается ({broken}); проверь интернет/прокси профиля")
        log(f"Studio: связь оборвалась ({broken}) — повторяю загрузку ({attempt}/{GOTO_TRIES - 1})")
        page.wait_for_timeout(5000 * attempt)


def studio_channel(page, url: str) -> str:
    """Открыть страницу Studio → ID канала, от имени которого она открыта («» — нет доступа).
    Обрыв соединения — загрузка повторяется; страница ещё перезагружается («контекст уничтожен») — ждём и
    читаем снова, а не падаем."""
    goto(page, url)
    _check_signed_in(page)
    for _ in range(15):
        try:
            cid = page.evaluate("() => { try { return ytcfg.get('CHANNEL_ID') || '' } catch (e) { return '' } }")
            if cid or "permission" in page.inner_text("body", timeout=10000)[:300].lower():
                return cid
        except Exception as exc:
            if not _navigating(exc):
                raise
        page.wait_for_timeout(1000)
    return ""


def _navigating(exc: BaseException) -> bool:
    """Чтение страницы сорвалось, потому что она в этот момент перезагружается — подождать и прочитать снова."""
    text = str(exc).lower()
    return "execution context was destroyed" in text or "most likely because of a navigation" in text \
        or "cannot find context" in text


def _playlists(page, channel_id_: str) -> list[str]:
    page.goto(f"{STUDIO}/channel/{channel_id_}/content/playlists", wait_until="domcontentloaded")
    try:
        page.wait_for_selector("#playlist-title, ytcp-video-list-cell-playlist, .empty-state", timeout=20000)
    except Exception:
        pass
    page.wait_for_timeout(2000)
    return [t.strip() for t in page.eval_on_selector_all("#playlist-title", "els => els.map(e => e.innerText)") if t.strip()]


def _titles_language(page) -> str:
    """Язык названий роликов на открытой странице «Контент» (для подсказки языка канала)."""
    from engine.core.langdetect import detect_language

    try:
        titles = page.eval_on_selector_all("ytcp-video-row #video-title", "els => els.map(e => e.innerText)")
    except Exception:
        return ""
    return detect_language(" ".join(t for t in titles[:15] if t)) or ""


def _last_date(page, channel_id_: str) -> str:
    """Самая поздняя дата среди роликов канала (запланированные — будущие даты) → YYYY-MM-DD или «»."""
    page.goto(f"{STUDIO}/channel/{channel_id_}/videos/upload", wait_until="domcontentloaded")
    try:
        page.wait_for_selector("ytcp-video-row, .empty-state", timeout=20000)
    except Exception:
        return ""
    page.wait_for_timeout(2000)
    rows = page.eval_on_selector_all("ytcp-video-row", "els => els.map(e => e.innerText.replace(/\\s+/g, ' ').slice(-120))")
    months = page.evaluate("""() => [...Array(12).keys()].map(m =>
        new Intl.DateTimeFormat(document.documentElement.lang || 'en', {month: 'short'}).format(new Date(2026, m, 15)))""")
    days = [d for r in rows if counts_for_schedule(r) for d in find_dates(r, months)]
    return max(days).isoformat() if days else ""


# слово «запланировано» в интерфейсе Studio на разных языках (строка ролика в «Контенте»)
_SCHEDULED_WORDS = re.compile(r"scheduled|запланир|отложен|programad|geplant|programm|zaplanow|programat|заплануван|planlagt|planerad",
                              re.I)
SCHEDULED_PAGES = 10  # страниц «Контента» (по 30 роликов) — запланированных больше 300 не бывает


def scheduled_in_rows(rows: list[str], months: list[str], today: date) -> tuple[int, bool]:
    """Строки роликов из «Контента» → (сколько запланировано, все ли строки запланированные).
    Запланированный — со словом «Scheduled» (на языке Studio) или с будущей датой у ролика не в черновиках/«по ссылке»."""
    n = 0
    for row in rows:
        hit = bool(_SCHEDULED_WORDS.search(row)) or (
            counts_for_schedule(row) and any(d > today for d in find_dates(row, months)))
        n += hit
    return n, bool(rows) and n == len(rows)


def count_scheduled(page, channel_id_: str) -> int | None:
    """Сколько роликов на канале запланировано, но ещё не вышло. Только чтение: страница «Контент» открыта по дате
    (запланированные — сверху), считаем строки; пока вся страница запланированная — листаем дальше. None — не прочиталось."""
    from engine.publish.schedule import MSK

    page.goto(f"{STUDIO}/channel/{channel_id_}/videos/upload", wait_until="domcontentloaded")
    try:
        page.wait_for_selector("ytcp-video-row, .empty-state", timeout=25000)
    except Exception:
        return None
    page.wait_for_timeout(2000)
    months = page.evaluate("""() => [...Array(12).keys()].map(m =>
        new Intl.DateTimeFormat(document.documentElement.lang || 'en', {month: 'short'}).format(new Date(2026, m, 15)))""")
    today = datetime.now(MSK).date()
    total = 0
    for _ in range(SCHEDULED_PAGES):
        rows = page.eval_on_selector_all("ytcp-video-row", "els => els.map(e => e.innerText.replace(/\\s+/g, ' ').slice(-200))")
        n, every = scheduled_in_rows(rows, months, today)
        total += n
        if not every:
            break  # дальше пошли вышедшие — запланированные кончились
        nxt = page.locator("#navigate-after:not([disabled])").first
        if not nxt.count() or nxt.get_attribute("aria-disabled") == "true":
            break
        nxt.click()
        page.wait_for_timeout(2500)
    return total


VIDEO_PAGES = 40  # страниц «Контента» (по 30 роликов): до 1200 роликов на канал


def visibility_of(cell: str, row: str, months: list[str], today: date) -> str:
    """Статус ролика в строке «Контента»: scheduled | public | unlisted | private | draft.
    cell — текст ячейки «Доступ» (если нашлась), row — вся строка (запасной путь), язык Studio любой."""
    text = (cell.strip() or row).lower()
    if re.search(r"private|закрыт|огранич|privad|privat|privé", text):
        return "private"
    if re.search(r"unlisted|по ссылке|no listado|nicht gelistet|non répertorié|non repertori", text):
        return "unlisted"
    if re.search(r"draft|черновик|borrador|entwurf|brouillon|bozza", text):
        return "draft"
    if _SCHEDULED_WORDS.search(text) or (counts_for_schedule(row) and any(d > today for d in find_dates(row, months))):
        return "scheduled"
    return "public"


_ROW_CELLS_JS = """els => els.map(e => {
    const v = e.querySelector('.tablecell-visibility, [class*="visibility"]');
    return {cell: v ? v.innerText.replace(/\\s+/g, ' ').trim() : '', row: e.innerText.replace(/\\s+/g, ' ').slice(-220)};
})"""


def count_videos(page, channel_id_: str) -> dict[str, int] | None:
    """Сколько на канале роликов по доступу: scheduled (ещё не вышли), public (открытый доступ), unlisted (по ссылке),
    private (ограниченный/закрытый), draft. Только чтение: страница «Контент», листаем все страницы.
    None — страница не прочиталась."""
    from engine.publish.schedule import MSK

    page.goto(f"{STUDIO}/channel/{channel_id_}/videos/upload", wait_until="domcontentloaded")
    try:
        page.wait_for_selector("ytcp-video-row, .empty-state", timeout=25000)
    except Exception:
        return None
    page.wait_for_timeout(2000)
    months = page.evaluate("""() => [...Array(12).keys()].map(m =>
        new Intl.DateTimeFormat(document.documentElement.lang || 'en', {month: 'short'}).format(new Date(2026, m, 15)))""")
    today = datetime.now(MSK).date()
    out = {"scheduled": 0, "public": 0, "unlisted": 0, "private": 0, "draft": 0}
    prev: tuple = ()
    for _ in range(VIDEO_PAGES):
        rows = page.eval_on_selector_all("ytcp-video-row", _ROW_CELLS_JS)
        key = tuple(r["row"] for r in rows)
        if not rows or key == prev:
            break  # страница не сменилась — листать нечего
        prev = key
        for r in rows:
            out[visibility_of(r["cell"], r["row"], months, today)] += 1
        nxt = page.locator("#navigate-after:not([disabled])").first
        if not nxt.count() or nxt.get_attribute("aria-disabled") == "true":
            break
        nxt.click()
        page.wait_for_timeout(2000)
    return out


# ролики с ограниченным доступом, по ссылке и черновики расписание канала не задают — только вышедшие и запланированные
_NOT_SCHEDULE = ("private", "unlisted", "draft", "ограниченный доступ", "доступ по ссылке", "черновик")


def counts_for_schedule(row: str) -> bool:
    low = row.lower()
    return not any(w in low for w in _NOT_SCHEDULE)


def find_dates(text: str, months: list[str]) -> list[date]:
    """Даты из строки Studio на языке страницы: «10 Nov 2026», «Nov 10, 2026», «13 сент. 2026 г.».
    months — короткие названия месяцев этого языка (Intl), английские — всегда в запасе."""
    names: dict[str, int] = {}
    for k, name in enumerate(months or []):
        names[name.lower().rstrip(".")] = k + 1
    for k, name in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")):
        names.setdefault(name, k + 1)
    names.setdefault("sept", 9)
    alt = "|".join(sorted((re.escape(n) for n in names), key=len, reverse=True))
    out: list[date] = []
    for m in re.finditer(rf"(\d{{1,2}})\s+({alt})\.?\s+(\d{{4}})|({alt})\.?\s+(\d{{1,2}}),\s+(\d{{4}})", text, re.I):
        day, mon, year = (m.group(1), m.group(2), m.group(3)) if m.group(1) else (m.group(5), m.group(4), m.group(6))
        try:
            out.append(date(int(year), names[mon.lower().rstrip(".")], int(day)))
        except (KeyError, ValueError):
            continue
    return out


_STUDIO_ENV_JS = """() => {
    const lang = document.documentElement.lang || navigator.language || '';
    const o = new Intl.DateTimeFormat(lang || 'en', {hour: 'numeric'}).resolvedOptions();
    return {lang, hour12: !!o.hour12, tz: Intl.DateTimeFormat().resolvedOptions().timeZone || '',
            offset: -new Date().getTimezoneOffset(), ua: navigator.userAgent};
}"""


def studio_env(page) -> dict:
    """Как выглядит Studio в профиле (только чтение): язык интерфейса, 12/24-часовое время, часовой пояс,
    платформа отпечатка. Программа справляется с любым — это чтобы заранее видеть, чем профили отличаются."""
    try:
        env = dict(page.evaluate(_STUDIO_ENV_JS))
    except Exception:
        return {}
    ua = str(env.pop("ua", ""))
    env["platform"] = "Windows" if "Windows" in ua else "Mac" if "Macintosh" in ua else ("Linux" if ua else "")
    env["checked_at"] = time.time()
    return env


def env_warnings(env: dict) -> list[str]:
    """Что в Studio профиля стоит поправить руками. Windows-отпечаток и 12-часовое время — не проблема."""
    out = []
    lang = str(env.get("lang") or "")
    if lang and lang.lower() not in ("en", "en-us"):
        out.append(f"язык Studio «{lang}» — поставь English (US): Studio → аватар → Language")
    return out


def scan_channels(cdp_url: str, *, cancel: CancelToken | None = None, pulse=None, env: dict | None = None) -> list:
    """Аккаунты Google и каналы в открытом профиле → [registry.Channel] (с плейлистами и последней датой).
    pulse — отметки хода (прогресс в очереди и сторож зависаний, engine.publish.scan)."""
    from engine.publish.registry import Channel

    def step_(frac: float, msg: str) -> None:
        if pulse is not None:
            pulse.touch(frac, msg)

    channels: list[Channel] = []
    with studio_page(cdp_url) as page:
        seen_accounts: set[str] = set()
        for authuser in range(MAX_ACCOUNTS):
            if cancel:
                cancel.check()
            step_(0.05 + 0.02 * authuser, f"Поиск каналов: аккаунт Google {authuser + 1} — список его каналов")
            items = _switcher_items(page, authuser)
            gaia = next((i["gaia"] for i in items if i["gaia"]), "") or f"#{authuser}"
            if not items or gaia in seen_accounts:
                break  # аккаунтов больше нет: Google вернул уже виденный
            seen_accounts.add(gaia)
            for item in items:
                if not item["handle"]:
                    log(f"Поиск каналов: у канала «{item['name']}» нет @-имени — пропускаю (без него канал не выбрать надёжно)")
                    continue
                step_(0.05 + 0.02 * authuser, f"Поиск каналов: аккаунт {authuser + 1} — канал {item['handle']}")
                cid = channel_id(page, item["handle"])
                if any(c.id == cid for c in channels):
                    continue  # тот же канал виден из второго аккаунта Google профиля
                channels.append(Channel(id=cid, name=item["name"], handle=item["handle"], authuser=authuser,
                                        account=f"аккаунт {authuser + 1}"))
        from engine.publish.studio_kit import set_channel, step

        failed: list[str] = []
        step_(0.3, f"Поиск каналов: найдено каналов {len(channels)} — читаю плейлисты и даты")
        for i, ch in enumerate(channels):
            if cancel:
                cancel.check()
            set_channel(ch.handle or ch.id)
            step_(0.3 + 0.65 * i / max(1, len(channels)),
                  f"Поиск каналов: {i + 1} из {len(channels)} — {ch.name} ({ch.handle}): переключаюсь, плейлисты, даты")
            try:
                with step(page, "поиск каналов: переключение на канал"):
                    switch_to(page, ch, cancel=cancel)
                if env is not None and not env and "studio.youtube.com" in page.url:
                    env.update(studio_env(page))  # один раз — Studio открыта
                    step_(0.3 + 0.65 * i / max(1, len(channels)),
                          f"Поиск каналов: Studio профиля — язык {env.get('lang') or '?'}, "
                          f"{'12' if env.get('hour12') else '24'} ч, {env.get('tz') or '?'}, отпечаток {env.get('platform') or '?'}")
                with step(page, "поиск каналов: плейлисты и последняя дата"):
                    ch.playlists = _playlists(page, ch.id)
                    ch.last_scheduled = _last_date(page, ch.id)
                    ch.titles_language = _titles_language(page)
            except StudioError as exc:
                # один канал не открылся — остальные всё равно ищем; этот останется в списке без плейлистов
                failed.append(f"{ch.handle}: {exc}")
                continue
            log(f"Канал {ch.name} ({ch.handle}): плейлистов {len(ch.playlists)}, последняя дата {ch.last_scheduled or '—'}")
        if failed and len(failed) == len(channels):
            raise StudioError("ни один канал профиля не открылся: " + "; ".join(failed))
    return channels


TITLE_MAX, DESCRIPTION_MAX = 100, 5000
# загрузку не обрываем никогда: ждём, сколько нужно. Процент стоит 15 минут — подсвечиваем канал в очереди
SLOW_SEC = 15 * 60
_TZ = re.compile(r"GMT\s*([+-])(\d{1,2}):?(\d{2})?")
_LIMIT_WORDS = ("daily upload limit", "upload limit", "limit reached", "лимит")


_FIND_MARKED = """(() => { const walk = (root) => { const el = root.querySelector('[data-sv-file="1"]'); if (el) return el;
    for (const n of root.querySelectorAll('*')) if (n.shadowRoot) { const r = walk(n.shadowRoot); if (r) return r; }
    return null; }; return walk(document); })()"""


def set_file(page, locator, path) -> None:
    """Выбрать файл в поле загрузки. Playwright к браузеру AdsPower подключён «как к удалённому» и не передаёт
    файлы больше 50 МБ — поэтому отдаём браузеру путь к файлу напрямую (команда Chrome DOM.setFileInputFiles):
    браузер и файл на одном Mac, читает сам, без ограничения размера."""
    path = str(Path(path).resolve())
    locator.evaluate("e => e.setAttribute('data-sv-file', '1')")
    cdp = page.context.new_cdp_session(page)
    try:
        found = cdp.send("Runtime.evaluate", {"expression": _FIND_MARKED})
        object_id = (found.get("result") or {}).get("objectId")
        if not object_id:
            raise StudioError("не нашёл поле выбора файла для передачи браузеру")
        cdp.send("DOM.setFileInputFiles", {"files": [path], "objectId": object_id})
    finally:
        try:
            locator.evaluate("e => e.removeAttribute('data-sv-file')")
        except Exception:
            pass
        cdp.detach()


# кнопки «Выбрать файл» в Studio: окно загрузки ролика и блок обложки (у нового ролика — «Upload file»)
_UPLOAD_BUTTONS = ["ytcp-uploads-dialog ytcp-uploads-file-picker #select-files-button",
                   "ytcp-uploads-dialog #select-files-button", "ytcp-uploads-dialog #reupload-button"]
_THUMB_BUTTONS = ["ytcp-uploads-dialog ytcp-thumbnail-uploader #select-button",  # вёрстка Studio с ноября 2026
                  "ytcp-uploads-dialog ytcp-thumbnails-compact-editor-uploader #select-button",
                  "ytcp-uploads-dialog ytcp-thumbnails-compact-editor-uploader-old #select-button",
                  "ytcp-uploads-dialog ytcp-thumbnails-compact-editor-uploader",
                  "ytcp-uploads-dialog ytcp-thumbnails-compact-editor-uploader-old"]


def _act(cancel: CancelToken | None) -> None:
    """Перед каждым действием в браузере — случайная пауза 1.5–5.5 с."""
    pause(cancel)


def _type_into(page, locator, text: str, cancel: CancelToken | None) -> None:
    """Поле: клик мышью, выделить всё и стереть, набрать (короткое — по буквам, длинное — вставкой).
    Потом сверка: в поле ровно этот текст. Не так — очистить и набрать ещё раз; снова не так — ошибка
    (кривое название/описание не уходит дальше)."""
    want = " ".join(text.split())
    for attempt in (1, 2):
        _act(cancel)
        human.click(page, locator)
        try:
            human.clear_field(page, locator)
        except RuntimeError as exc:
            if attempt == 2:
                raise StudioError(str(exc)) from None
            continue
        human.type_text(page, text, cancel)
        page.wait_for_timeout(500)
        got = " ".join(human.field_text(locator).split())
        if got == want or not got and not want:
            return
        log(f"Studio: в поле не то, что набирал («{got[:80]}» вместо «{want[:80]}») — "
            + ("набираю заново" if attempt == 1 else "сдаюсь"))
    raise StudioError(f"поле заполнилось неверно: «{got[:120]}» вместо «{want[:120]}»")


COVER_NOT_SET = "⚠ Обложка не поставилась"
# Studio не приняла картинку обложки (слишком большая, не тот формат…) — слова из её сообщения
_THUMB_FAIL_WORDS = ("couldn't upload", "could not upload", "couldn’t upload", "thumbnail must be", "file is too big",
                     "не удалось загрузить", "слишком большой")


def cover_not_set(why: str) -> str:
    """Строка-предупреждение «ролик выложен без обложки»: почему и что сделать (всплывает в итогах задачи)."""
    return f"{COVER_NOT_SET} ({why}) — ролик выложен без неё, поставь обложку сам в Studio"


def _set_thumbnail(page, channel, thumbnail: Path, cancel) -> list[str]:
    """Обложка в окне загрузки. Не встала (файла нет, кнопки нет, Studio не приняла, канал не подтверждён) —
    ролик всё равно выкладываем: название, описание, плейлист, соавторы, дата; → строки-предупреждения."""
    from engine.publish.studio_kit import el, step

    if not thumbnail.is_file():
        return [cover_not_set(f"файла обложки нет: {thumbnail}")]
    try:
        with step(page, f"обложка {thumbnail.name}"):
            _act(cancel)
            how = human.choose_file(page, _THUMB_BUTTONS, thumbnail, what="обложка",
                                    fallback_input=el(page, "thumbnail_input", state="attached"))
            log(f"Studio {channel.handle}: обложка выбрана {how}")
            page.wait_for_timeout(4000)
            text = _dialog_text(page).lower()
            if "verify" in text:
                return [cover_not_set("канал не подтверждён по телефону — свои обложки YouTube разрешает после "
                                      "подтверждения")]
            if any(w in text for w in _THUMB_FAIL_WORDS):
                return [cover_not_set("Studio не приняла файл обложки")]
    except StudioError as exc:
        log(f"Studio {channel.handle}: обложка не поставилась — выкладываю без неё ({exc})")
        return [cover_not_set(str(exc).split(". Снимок")[0][:200])]
    return []


def _dialog_text(page) -> str:
    try:
        return page.locator("ytcp-uploads-dialog").inner_text(timeout=3000)
    except Exception:
        return ""


def _check_dialog_problems(page) -> None:
    text = _dialog_text(page).lower()
    if any(w in text for w in _LIMIT_WORDS) and ("upload" in text or "загруз" in text):
        raise UploadLimit("YouTube: на канале исчерпан суточный лимит загрузок", time.time() + 24 * 3600)
    if ("verify" in text and "phone" in text) or ("подтверд" in text and "телефон" in text):
        raise NeedsUser("YouTube просит подтвердить канал по телефону — сделай это вручную в YouTube Studio")


def _profile_offset(page) -> tuple[int, int] | None:
    """Часовой пояс, в котором Studio показывает время (подсказка «Default set to local time zone (GMT+0100)»)."""
    text = page.eval_on_selector_all("ytcp-datetime-picker ytcp-paper-tooltip-placeholder, ytcp-datetime-picker [id*=tooltip]",
                                     "els => els.map(e => e.innerText).join(' ')")
    m = _TZ.search(text or "")
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    return sign * int(m.group(2)), sign * int(m.group(3) or 0)


def _wait_uploaded(page, progress, cancel: CancelToken | None) -> None:
    """Дождаться, пока файл загрузится на 100% (закрыть вкладку раньше — загрузка оборвётся).
    Без ограничения по времени: много роликов на несколько каналов грузятся долго, но догружаются.
    Процент не меняется 15 минут — в очереди видно «долго грузится», ожидание продолжается."""
    last, moved, warned = "", time.monotonic(), 0
    while True:
        if cancel:
            cancel.check()
        _check_dialog_problems(page)
        status = page.locator("ytcp-uploads-dialog .progress-label, ytcp-uploads-dialog ytcp-video-upload-progress").first
        try:
            text = status.inner_text(timeout=3000).strip()
        except Exception:
            text = ""
        if _upload_finished(text) or _checks_started(page):
            _wait_checks(page, progress, cancel)
            return
        m = re.search(r"(\d{1,3})\s*%", text)
        pct = int(m.group(1)) / 100 if m else 0.0
        if text != last:
            last, moved, warned = text, time.monotonic(), 0
        msg = f"загрузка файла {m.group(1)}%" if m else f"загрузка файла: {text[:60] or 'жду Studio'}"
        idle = int((time.monotonic() - moved) // 60)
        if idle * 60 >= SLOW_SEC:
            msg = f"⚠ долго грузится: {last or 'без процента'} уже {idle} мин — жду дальше"
            if idle >= warned + 15:
                warned = idle
                log(f"Выкладка: {msg}")
        if progress:
            progress(pct, msg)  # каждые ~5 с — живой статус в очереди
        page.wait_for_timeout(5000)


CHECKS_WAIT_SEC = 30 * 60  # проверки Studio (авторские права, реклама) на длинном ролике идут до получаса


def _checks_done(text: str) -> bool:
    low = text.lower()
    return ("checks complete" in low or "no issues found" in low or "issues found" in low
            or "проверка завершена" in low or "проверки завершены" in low or "проблем не найдено" in low)


def _wait_checks(page, progress, cancel: CancelToken | None) -> None:
    """Файл загружен — дождаться конца проверок Studio, и только потом «Schedule»: нажатая раньше кнопка даёт окно
    «We're still checking your content», и ролик остаётся черновиком (14.11). Не дождались — идём дальше (там
    окно-предупреждение разберёт _close_done_dialog)."""
    end = time.monotonic() + CHECKS_WAIT_SEC
    shown, blank = "", 0
    while time.monotonic() < end:
        if cancel:
            cancel.check()
        text = _progress_text(page)
        if _checks_done(text):
            return
        blank = 0 if text.strip() else blank + 5
        if blank >= 120:  # индикатора нет совсем — ждать нечего
            return
        if progress and text != shown:
            shown = text
            progress(1.0, f"проверки Studio: {text[:70] or 'идут'}")
        page.wait_for_timeout(5000)
    log("Studio: проверки не закончились за полчаса — сохраняю как есть")


def _checks_started(page) -> bool:
    """Без опоры на язык: после полной загрузки Studio начинает проверки — у индикатора появляется
    атрибут checks-can-start / статус проверок не «NOT_STARTED»."""
    try:
        attrs = page.locator("ytcp-uploads-dialog ytcp-video-upload-progress").first.evaluate(
            "e => ({can: e.hasAttribute('checks-can-start'), st: e.getAttribute('checks-summary-status-v2') || ''})")
    except Exception:
        return False
    st = attrs.get("st") or ""
    return bool(attrs.get("can")) or (bool(st) and not st.endswith("NOT_STARTED"))


def _upload_finished(text: str) -> bool:
    """Файл загружен целиком: Studio пишет «Upload complete» / «Checks complete» (или 100%)."""
    low = text.lower()
    return ("upload complete" in low or "checks complete" in low or "загрузка завершена" in low
            or "загрузка видео завершена" in low or "проверка завершена" in low
            or re.search(r"\b100\s*%", low) is not None)


def upload(cdp_url: str, channel, job: UploadJob, *, cancel: CancelToken | None = None,
           progress=None, draft_url: str = "", on_draft=None) -> Uploaded:
    """Загрузить файл на канал (channel — registry.Channel) с отложенной публикацией в job.publish_at (МСК).
    Каждый шаг — в журнал; ошибка шага — снимок страницы (studio_kit.step).

    Продолжение после падения: draft_url — видео прошлой попытки на этом канале. Оно уже сохранено —
    повторно не грузим (никаких дублей); черновик — открываем и дозаполняем; пропало — грузим заново.
    on_draft(url) — как только Studio выдала ссылку на новое видео (задача запоминает её сразу)."""
    from datetime import timedelta, timezone

    from engine.publish.studio_kit import LayoutError, el, set_channel, step

    set_channel(channel.handle or channel.id)
    notes: list[str] = []
    with studio_page(cdp_url) as page:
        with step(page, "переключение на канал"):
            switch_to(page, channel, cancel=cancel)
        resumed = ""
        if draft_url:
            with step(page, f"продолжение: видео прошлой попытки {draft_url}"):
                resumed = _open_previous(page, draft_url, job, cancel, channel.id)
            if resumed == "saved":
                # прошлая попытка дошла до «Schedule», но не успела это записать — второй раз не грузим
                with step(page, "проверка, что видео сохранено"):
                    _confirm_saved(page, draft_url)
                if not job.private_only and not job.publish_now:
                    notes += _check_scheduled(page, page.evaluate(_DATE_JS, job.publish_at.date().isoformat()),
                                              draft_url)
                notes.append("видео уже было сохранено прошлой попыткой — повторно не загружал")
                notes += _audit_playlist(page, draft_url, job, cancel)
                file_ok = _check_file_uploaded(page, channel.id, draft_url, job.title)
                if not file_ok:
                    notes.append("не смог убедиться, что Studio приняла видеофайл — файл на компьютере оставлен")
                notes += _video_elements(page, draft_url, job, cancel)
                invites = _invite_safely(page, draft_url, job, cancel, notes)
                log(f"Выкладка: {channel.name} {channel.handle} — {draft_url} (уже было сохранено)")
                return Uploaded(draft_url, notes, invites, file_ok)
            if resumed == "draft":
                notes.append("продолжил черновик прошлой попытки — без повторной загрузки файла")
        if resumed != "draft":
            if resumed == "gone":
                notes.append(f"видео прошлой попытки не нашлось ({draft_url}) — загрузил заново")
            if resumed == "interrupted":
                notes.append(f"в черновике прошлой попытки загрузка прервалась ({draft_url}) — загрузил заново; "
                             "тот черновик удали в Studio")
            with step(page, "открытие окна загрузки"):
                _open_upload_dialog(page, channel, cancel)
            with step(page, f"выбор файла {job.file.name}"):
                _act(cancel)
                how = human.choose_file(page, _UPLOAD_BUTTONS, job.file, what="файл ролика",
                                        fallback_input=el(page, "file_input", state="attached"))
                log(f"Studio {channel.handle}: файл ролика выбран {how}")
                el(page, "title", timeout=120000)
                page.wait_for_timeout(2000)
                _check_dialog_problems(page)
        if on_draft is not None:
            link = _wait_video_url(page)
            if link:
                on_draft(link)

        try:
            # ---- Details ----
            with step(page, "название"):
                _type_into(page, el(page, "title"), job.title[:TITLE_MAX], cancel)
            with step(page, "описание"):
                _type_into(page, el(page, "description"), job.description[:DESCRIPTION_MAX], cancel)
            if job.thumbnail is not None:
                notes += _set_thumbnail(page, channel, job.thumbnail, cancel)
            else:
                notes.append("без обложки — поставишь сам в Studio")
            with step(page, "плейлист"):
                notes += _pick_playlist(page, cancel)
            with step(page, "«не для детей»"):
                _act(cancel)
                human.click(page, el(page, "not_for_kids"))
            with step(page, "«Show more» и «ИИ-контента нет»"):
                _act(cancel)
                human.click(page, el(page, "show_more"))
                altered = el(page, "altered_no")
                _act(cancel)
                human.click(page, altered)

            # ---- шаги до Visibility: у канала на монетизации — ещё Monetization и Ad suitability ----
            notes += _walk_to_visibility(page, cancel)
            with step(page, "шаг Visibility"):
                el(page, "private", state="attached", timeout=30000)
                page.wait_for_timeout(2000)  # шаг ещё выезжает — клик раньше времени не засчитывается

            expected_date = ""
            if job.publish_now:
                with step(page, "доступ «Public» — выложить сразу"):
                    radio = page.locator(PUBLIC_RADIO).first
                    if not radio.is_visible():
                        _act(cancel)
                        human.click(page, page.locator("ytcp-uploads-dialog #first-container").first)
                        page.wait_for_timeout(1500)
                    for _try in range(3):
                        _act(cancel)
                        human.click(page, radio)
                        page.wait_for_timeout(1000)
                        if radio.get_attribute("aria-checked") == "true":
                            break
                    else:
                        raise StudioError("не выбрался доступ «Public»")
            else:
                with step(page, "дата и время публикации"):
                    if not page.locator(SELECTORS_DATE).first.is_visible():
                        _act(cancel)
                        human.click(page, el(page, "schedule_expand"))
                    el(page, "date_trigger")
                    offset = _profile_offset(page)
                    when = job.publish_at
                    if offset is not None:
                        when = studio_time(job.publish_at, offset, _browser_zone(page))
                    else:
                        notes.append("не понял часовой пояс Studio — время вписано по Москве")
                    log(f"Studio {channel.handle}: выход {job.publish_at:%d.%m %H:%M} МСК = {when:%d.%m %H:%M} в поясе Studio "
                        f"(GMT{'%+d' % offset[0] if offset else '?'})")
                    _set_date(page, when, cancel)
                    expected_date = page.evaluate(_DATE_JS, when.date().isoformat())
                    time_box = el(page, "time_input")
                    _type_into(page, time_box, f"{when:%H:%M}", cancel)
                    human.press(page, "Enter")
                    page.wait_for_timeout(1000)
                    shown_time = time_box.input_value().strip()
                    if clock_of(shown_time) != (when.hour, when.minute):
                        raise StudioError(f"время публикации не вписалось (в Studio «{shown_time}», нужно {when:%H:%M})")

            if job.private_only:
                with step(page, "проверочный режим: «Private»"):
                    # расписание заполнено и сверено, но сохраняем как «Private» — ничего не выйдет в эфир
                    if not page.locator(SELECTORS_PRIVATE).first.is_visible():
                        _act(cancel)
                        human.click(page, page.locator("ytcp-uploads-dialog #first-container").first)
                    radio = el(page, "private")
                    for _try in range(3):
                        _act(cancel)
                        human.click(page, radio)
                        page.wait_for_timeout(1000)
                        if radio.get_attribute("aria-checked") == "true":
                            break
                    else:
                        raise StudioError("не выбрался доступ «Private»")

        except (StudioError, LayoutError) as exc:
            # ошибка на шаге после выбора файла: профиль сейчас закроется — и загрузка видео оборвётся
            # («Upload interrupted», черновик без видео). Сначала дать файлу догрузиться: черновик сохранится
            # с видео, а повтор только дозаполнит его (13.11)
            _finish_upload_before_fail(page, channel, cancel, exc)
            raise
        url = _video_url(page)
        with step(page, "ожидание полной загрузки файла"):
            _wait_uploaded(page, progress, cancel)
        claim = checks_problem(page)
        if claim:
            notes.append(claim)
            log(f"Studio {channel.handle}: {claim}")
            from engine.publish.studio_kit import report

            report(claim)
        with step(page, "сохранение («Publish»)" if job.publish_now else "сохранение («Schedule»)"):
            done = el(page, "done")
            log(f"Studio {channel.handle}: кнопка сохранения «{_visible_text(done)}», доступ: "
                f"{'Public' if job.publish_now else 'расписание'}; внизу окна: "
                f"«{_visible_text(page.locator('ytcp-uploads-dialog ytcp-video-upload-progress').first)[:120]}»")
            _act(cancel)
            human.click(page, done)
            page.wait_for_timeout(2500)
            log(f"Studio {channel.handle}: после нажатия — окно загрузки "
                f"{'открыто' if page.locator('ytcp-uploads-dialog').first.is_visible() else 'закрыто'}; "
                f"видно: «{_visible_dialogs(page)[:300]}»")
            _close_done_dialog(page)
        with step(page, "проверка, что видео сохранено"):
            _confirm_saved(page, url)
        with step(page, "проверка плейлиста на ролике"):
            notes += _audit_playlist(page, url, job, cancel)
        if not job.private_only and not job.publish_now:
            notes += _check_scheduled(page, expected_date, url)
        with step(page, "проверка, что видеофайл загружен"):
            file_ok = _check_file_uploaded(page, channel.id, url, job.title)
        if not file_ok:
            notes.append("не смог убедиться, что Studio приняла видеофайл — файл на компьютере оставлен, проверь ролик")
        notes += _video_elements(page, url, job, cancel)
        invites = _invite_safely(page, url, job, cancel, notes)
    log(f"Выкладка: {channel.name} {channel.handle} — {url or 'ссылка не найдена'}")
    return Uploaded(url, notes, invites, file_ok)


FINISH_UPLOAD_SEC = 45 * 60  # столько ждём догрузки файла перед тем, как сдаться на ошибке шага


def _finish_upload_before_fail(page, channel, cancel, exc) -> None:
    """Перед закрытием профиля после ошибки — дождаться конца загрузки файла (черновик сохранится с видео).
    Не дождались (сеть, предел времени) — как есть: повтор загрузит заново."""
    if isinstance(exc, UploadLimit):
        return  # лимит загрузок: файл всё равно не загрузится — ждать нечего
    try:
        if _upload_finished(_progress_text(page)) or _checks_started(page):
            return
        log(f"Studio {channel.handle}: ошибка ({str(exc)[:120]}) — сначала дам файлу догрузиться, чтобы черновик "
            "сохранился с видео")
        end = time.monotonic() + FINISH_UPLOAD_SEC
        blind = 0  # сколько раз подряд не видно хода загрузки (окно закрылось / загрузка оборвалась)
        while time.monotonic() < end:
            if cancel is not None:
                cancel.check()
            page.wait_for_timeout(10000)
            text = _progress_text(page)
            if not text.strip() or "interrupt" in text.lower() or "прерван" in text.lower():
                blind += 1
                if blind >= 6:  # минута без хода загрузки — ждать нечего, повтор продолжит черновик
                    log(f"Studio {channel.handle}: хода загрузки не видно ({text.strip()[:60] or 'окно закрыто'}) — "
                        "не жду, повтор продолжит черновик")
                    return
                continue
            blind = 0
            if _upload_finished(text) or _checks_started(page):
                page.wait_for_timeout(5000)  # Studio сохраняет черновик
                log(f"Studio {channel.handle}: файл догружен — черновик сохранён, повтор его дозаполнит")
                return
        log(f"Studio {channel.handle}: файл не догрузился за {FINISH_UPLOAD_SEC // 60} мин — повтор загрузит заново")
    except CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — это лишь попытка спасти загрузку; исходная ошибка важнее
        log(f"Studio {channel.handle}: дождаться загрузки не вышло ({str(e)[:100]})")


def _video_elements(page, url: str, job: UploadJob, cancel) -> list[str]:
    """Конечная заставка (видео + плейлист + подписка) и подсказка-плейлист в начале — в каждом ролике.
    Не вышло — StudioError: ролик уже сохранён, повтор продолжит с этого шага (дублей не будет)."""
    from engine.publish.elements import add_video_elements
    from engine.publish.studio_kit import step

    with step(page, "конечная заставка и подсказка"):
        return add_video_elements(page, url, list(job.playlist_hint or []), cancel=cancel)


def _invite_safely(page, url: str, job: UploadJob, cancel, notes: list[str]) -> dict[str, str]:
    """Пригласить соавторов; ролик уже выложен — сбой только пометка (упал браузер — пусть разберутся выше)."""
    from engine.publish.studio_kit import step

    if not job.coauthors:
        return {}
    try:
        with step(page, "приглашение соавторов"):
            return invite_coauthors(page, url, job.coauthors, cancel=cancel)
    except Exception as exc:
        if browser_lost(exc) or (cancel is not None and cancel.is_cancelled()):
            raise
        notes.append(f"соавторы не приглашены: {str(exc).splitlines()[0][:300]}")
        return {}


def _wait_video_url(page, seconds: int = 30) -> str:
    """Ссылка на видео появляется в окне загрузки не сразу («Creating link…»)."""
    for _ in range(seconds):
        url = _video_url(page)
        if url:
            return url
        page.wait_for_timeout(1000)
    return ""


# кнопка «Edit draft» на плашке черновика (страница видео в Studio). Класс плашки Studio меняла:
# было «entity-banner», стало «ytcpEntityPageEntityBanner» — берём оба
PUBLIC_RADIO = "ytcp-uploads-dialog tp-yt-paper-radio-button[name=PUBLIC]"  # доступ «Public» (служебная метка, от языка не зависит)
DRAFT_BANNER_BUTTON = 'ytcp-banner.entity-banner #action-1, ytcp-banner[class*="EntityBanner"] #action-1'


def _open_previous(page, url: str, job: UploadJob, cancel, channel_id: str = "") -> str:
    """Видео прошлой попытки: «saved» — уже сохранено (не черновик); «draft» — черновик открыт в окне загрузки
    для дозаполнения (файл недогружен — подаём его ещё раз в то же видео); «interrupted» — загрузка в черновике
    оборвалась и подать файл некуда (грузим заново); «gone» — видео нет."""
    from engine.publish.studio_kit import el

    if channel_id:
        row = _list_row(page, channel_id, _video_id(url), job.title)
        if row and re.search(r"abandoned|failed to upload|upload failed|upload aborted", row, re.I):
            log("Studio: у черновика прошлой попытки нет видеофайла (Processing abandoned) — загружаю ролик заново")
            return "interrupted"
        if row and re.search(r"resume upload|upload interrupted", row, re.I) and _resume_from_list(page, url, job, cancel, channel_id):
            return "draft"
    page.goto(f"{STUDIO}/video/{_video_id(url)}/edit", wait_until="domcontentloaded")
    state = ""
    for _ in range(20):
        page.wait_for_timeout(1500)
        if page.locator("ytcp-video-metadata-visibility").count():
            state = "saved"
            break
        if page.locator(DRAFT_BANNER_BUTTON).count():
            state = "draft"
            break
    if state != "draft":
        return state or "gone"
    _act(cancel)
    human.click(page, page.locator(DRAFT_BANNER_BUTTON).first)  # «Edit draft» / «Редактировать черновик»
    el(page, "title", timeout=60000)
    page.wait_for_timeout(2500)
    # загрузка файла оборвалась — Studio просит файл снова: подаём тот же файл в это же видео
    again = page.locator("ytcp-uploads-dialog input[type=file]:not(#file-loader)")
    if not (_checks_started(page) or _upload_finished(_progress_text(page))):
        if not again.count():
            # загрузка оборвалась (профиль закрылся посреди неё): у такого черновика в списке роликов есть
            # «Resume upload» — подаём тот же файл, загрузка продолжается с того же места (14.11)
            if channel_id and _resume_from_list(page, url, job, cancel, channel_id):
                return "draft"
            log("Studio: в черновике прошлой попытки загрузка прервана, продолжить нельзя — загружаю ролик заново")
            return "interrupted"
        log("Studio: файл черновика недогружен — подаю его ещё раз в то же видео")
        _act(cancel)
        human.choose_file(page, _UPLOAD_BUTTONS, job.file, what="файл ролика (повторно)", fallback_input=again.first)
        page.wait_for_timeout(3000)
    return "draft"


def _resume_from_list(page, url: str, job: UploadJob, cancel, channel_id: str) -> bool:
    """Черновик с оборванной загрузкой: в списке роликов канала — «Resume upload» → тот же файл. → вышло ли."""
    from engine.publish.studio_kit import current_channel

    vid = _video_id(url)
    try:
        human.press(page, "Escape")  # окно черновика без файла — закрыть, список роликов под ним
        page.wait_for_timeout(1500)
        page.goto(f"{STUDIO}/channel/{channel_id}/videos/upload", wait_until="domcontentloaded")
        button = f'ytcp-video-row:has(a[href*="{vid}"]) ytcp-button:has-text("Resume upload")'
        page.locator(button).first.wait_for(state="visible", timeout=30000)
    except Exception:
        log(f"Studio {current_channel()}: у черновика {vid} нет «Resume upload» в списке роликов")
        return False
    _act(cancel)
    how = human.choose_file(page, [button], job.file, what="файл ролика (продолжение загрузки)")
    log(f"Studio: загрузка черновика {vid} продолжена ({how})")
    try:
        page.locator("ytcp-uploads-dialog").first.wait_for(state="visible", timeout=30000)
    except Exception:
        human.click(page, page.locator(f'ytcp-video-row:has(a[href*="{vid}"]) ytcp-button:has-text("Edit draft")').first)
    from engine.publish.studio_kit import el

    el(page, "title", timeout=60000)
    page.wait_for_timeout(2500)
    return True


def _progress_text(page) -> str:
    try:
        return page.locator("ytcp-uploads-dialog ytcp-video-upload-progress").first.inner_text(timeout=3000)
    except Exception:
        return ""


SELECTORS_DATE = "ytcp-uploads-dialog #datepicker-trigger"
SELECTORS_PRIVATE = "ytcp-uploads-dialog tp-yt-paper-radio-button[name=PRIVATE]"


_DATE_JS = """(iso) => { const [y, m, d] = iso.split('-').map(Number);
    return new Intl.DateTimeFormat(document.documentElement.lang || 'en', {day: 'numeric', month: 'short', year: 'numeric'})
        .format(new Date(y, m - 1, d)); }"""


def norm_date(text: str) -> str:
    """Дата для сравнения: Studio пишет «20 нояб. 2026\u202fг.» (узкий неразрывный пробел), а Intl браузера отдаёт
    обычный пробел или «\u00a0» — буквально строки не совпадают, хотя дата одна и та же. Без регистра, пробелов,
    знаков препинания и хвоста «г.»."""
    words = re.sub(r"[^\w]+", " ", str(text or "").casefold()).split()
    if words and words[-1] == "г":
        words.pop()
    return " ".join(words)


# Дата, которую показывает Studio, → «2026-11-20» (пусто — не поняли). Названия месяцев берём у самого браузера на языке
# страницы (Intl): так читается любой язык Studio и любой формат («20 нояб. 2026 г.», «Nov 20, 2026», «20 de nov. de 2026»,
# «2026. 11. 20.»), без списка языков в коде. Месяц — по самому длинному совпадающему началу слова.
_PARSE_DATE_JS = r"""(text) => {
    const lang = document.documentElement.lang || 'en';
    const clean = (t) => String(t).toLowerCase().replace(/[.,]/g, ' ').replace(/[\u00a0\u202f]/g, ' ');
    const names = [];
    for (let m = 0; m < 12; m++) {
        const set = new Set();
        for (const style of ['short', 'long']) {
            for (const f of [{month: style}, {month: style, day: 'numeric'}]) {
                const part = new Intl.DateTimeFormat(lang, f).formatToParts(new Date(2026, m, 15)).find(p => p.type === 'month');
                if (part) set.add(clean(part.value).trim());
            }
        }
        names.push([...set].filter(x => x.length >= 2 && !/^\d+$/.test(x)));
    }
    const words = clean(text).split(/\s+/).filter(Boolean);
    const nums = words.filter(w => /^\d+$/.test(w)).map(Number);
    const year = nums.find(n => n >= 2000 && n <= 2100);
    let month = -1, best = 0;
    for (const w of words) {
        if (/^\d+$/.test(w)) continue;
        names.forEach((alts, m) => alts.forEach(n => {
            const stem = n.length > 4 ? n.slice(0, Math.max(3, n.length - 1)) : n;
            if ((w === n || w.startsWith(stem)) && stem.length > best) { best = stem.length; month = m; }
        }));
    }
    if (month < 0 || !year) {  // числовой формат: год-месяц-день, день.месяц.год или месяц/день/год — по порядку полей Intl
        const order = new Intl.DateTimeFormat(lang, {year: 'numeric', month: '2-digit', day: '2-digit'})
            .formatToParts(new Date(2026, 10, 20)).filter(p => ['year', 'month', 'day'].includes(p.type)).map(p => p.type);
        const n3 = clean(text).match(/\d+/g) || [];
        if (n3.length >= 3 && order.length === 3) {
            const v = {}; order.forEach((t, i) => v[t] = Number(n3[i]));
            if (v.year > 2000 && v.month >= 1 && v.month <= 12) { month = v.month - 1; return `${v.year}-${String(v.month).padStart(2, '0')}-${String(v.day).padStart(2, '0')}`; }
        }
        return '';
    }
    const day = nums.find(n => n >= 1 && n <= 31 && n !== year);
    return day ? `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}` : '';
}"""

# Клик по дню в открытом календаре Studio (запасной путь, если набрать дату не получилось): нужный месяц листаем,
# день — span.calendar-day без disabled/invisible; помечаем его атрибутом, дальше кликает человекоподобная мышь.
_PICK_DAY_JS = r"""([iso, parse]) => {
    const [y, m, d] = iso.split('-').map(Number);
    const find = (new Function('return ' + parse))();
    const months = [...document.querySelectorAll('ytcp-date-picker .calendar-month')];
    const box = document.querySelector('ytcp-date-picker #calendar-main');
    for (const mon of months) {
        const label = mon.getAttribute('aria-label') || (mon.querySelector('.calendar-month-label') || {}).textContent || '';
        const got = find(label + ' 1');  // «Нояб. 2026 г.» + день → «2026-11-01»
        if (got && got.startsWith(`${y}-${String(m).padStart(2, '0')}`)) {
            const cell = [...mon.querySelectorAll('.calendar-day')].find(c => c.textContent.trim() === String(d)
                && !c.classList.contains('invisible') && !c.classList.contains('disabled'));
            if (!cell) return 'disabled';
            cell.scrollIntoView({block: 'center'});
            document.querySelectorAll('[data-sv-pick]').forEach(e => e.removeAttribute('data-sv-pick'));
            cell.setAttribute('data-sv-pick', '1');
            return 'ok';
        }
    }
    if (box) box.scrollBy(0, Math.max(200, box.clientHeight * 0.8));  // нужного месяца в списке нет — листаем вниз
    return 'scroll';
}"""


def studio_date(page, text: str) -> str:
    """Дата из текста Studio («20 нояб. 2026 г.») → «2026-11-20»; не поняли — пусто."""
    try:
        return str(page.evaluate(_PARSE_DATE_JS, text) or "")
    except Exception:
        return ""


def _date_popup_open(page) -> bool:
    """Календарь даты уже раскрыт (после неудачной попытки он остаётся на экране и перекрывает кнопки)."""
    try:
        return page.locator("ytcp-date-picker input").first.is_visible()
    except Exception:
        return False


def _date_ok(page, want_iso: str, typed: str) -> bool:
    """Studio держит именно эту дату: по смыслу (любой язык и формат), а не только по совпадению строк."""
    from engine.publish.studio_kit import el

    shown = el(page, "date_trigger").inner_text()
    return studio_date(page, shown) == want_iso or norm_date(shown) == norm_date(typed)


def _pick_calendar_day(page, when, cancel: CancelToken | None) -> bool:
    """Запасной путь: кликнуть нужный день в календаре, как человек (не зависит от языка и формата даты)."""
    iso = when.date().isoformat()
    for _ in range(30):  # до ~2,5 лет вперёд: календарь листается по ~6 месяцев за раз
        _act(cancel)
        try:
            res = page.evaluate(_PICK_DAY_JS, [iso, _PARSE_DATE_JS])
        except Exception:
            return False
        if res == "ok":
            page.wait_for_timeout(300)
            cell = page.locator('ytcp-date-picker [data-sv-pick="1"]').first
            human.click(page, cell)
            page.wait_for_timeout(1000)
            return True
        if res == "disabled":
            return False  # дата в прошлом / недоступна — кликать нечего
        page.wait_for_timeout(400)
    return False


def _set_date(page, when, cancel: CancelToken | None) -> None:
    """Дата публикации. Сначала набираем в формате языка Studio (как форматирует сам браузер: «12 Nov 2027»,
    «12 нояб. 2027 г.»), потом запасные английские форматы; после каждого — сверяем, какую дату Studio держит ПО
    СМЫСЛУ (не по строке: пробелы, «г.», порядок полей — не важны). Не получилось набрать — кликаем день в
    календаре. Календарь после неудачи остаётся открытым — второй раз по полю не кликаем, а вводим в него; если не
    вышло совсем — закрываем его, чтобы повтор начинался с чистого экрана."""
    want = when.date().isoformat()
    variants = [page.evaluate(_DATE_JS, want), f"{when.day} {when:%b %Y}", f"{when:%b} {when.day}, {when:%Y}"]
    from engine.publish.studio_kit import el

    for text in dict.fromkeys(variants):
        _act(cancel)
        if not _date_popup_open(page):
            human.click(page, el(page, "date_trigger"))
        _type_into(page, el(page, "date_input"), text, cancel)
        human.press(page, "Enter")
        page.wait_for_timeout(1200)
        if _date_ok(page, want, text):
            return
        log(f"Studio: дата «{text}» не принята — пробую другой формат")
    log("Studio: дату не удалось набрать — выбираю день в календаре")
    if not _date_popup_open(page):
        human.click(page, el(page, "date_trigger"))
    if _pick_calendar_day(page, when, cancel) and studio_date(page, el(page, "date_trigger").inner_text()) == want:
        return
    shown = el(page, "date_trigger").inner_text().strip()
    if _date_popup_open(page):  # не оставляем календарь поверх окна: повтор дозаполнит черновик с чистого экрана
        try:
            human.press(page, "Escape")
        except Exception:
            pass
    raise StudioError(f"дата публикации не вписалась (в Studio «{shown}», нужно {when:%d.%m.%Y})")


def _open_upload_dialog(page, channel, cancel: CancelToken | None) -> None:
    """Окно загрузки: прямой адрес, а если Studio его не открыла (на канале висят незаконченные загрузки) —
    как человек: «Create» → «Upload videos» (по служебным меткам, на любом языке)."""
    page.goto(f"{STUDIO}/channel/{channel.id}/videos/upload?d=ud", wait_until="domcontentloaded")
    file_input = page.locator("ytcp-uploads-dialog input[type=file]")
    try:
        file_input.first.wait_for(state="attached", timeout=20000)
        return
    except Exception:
        pass
    from engine.publish.studio_kit import el

    log("Studio: окно загрузки не открылось по ссылке — открываю кнопкой «Create» → «Upload videos»")
    _act(cancel)
    human.click(page, el(page, "create_button"))
    item = el(page, "create_upload")
    _act(cancel)
    human.click(page, item)
    try:
        file_input.first.wait_for(state="attached", timeout=30000)
    except Exception:
        raise StudioError("не открылось окно загрузки видео") from None


def _clear_popups(page) -> None:
    """Studio иногда сама показывает всплывающие подсказки («что нового» и т.п.) — они перекрывают кнопки.
    Закрываем, как человек: Escape."""
    for _ in range(3):
        if not page.locator("tp-yt-iron-overlay-backdrop.opened").count():
            return
        human.press(page, "Escape")
        page.wait_for_timeout(1000)


def browser_lost(exc: BaseException) -> bool:
    """Браузер профиля закрылся/упал посреди работы (AdsPower бывает нестабилен) — профиль перезапускаем."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(k in text for k in ("targetclosed", "has been closed", "browser closed", "target closed",
                                   "connection closed", "websocket", "econnrefused", "connect_over_cdp",
                                   "browser has disconnected", "page crashed", "session closed"))


PENDING_VISITS = 4  # Studio обрабатывает видео — столько раз (через ~20 с) смотрим, не появилась ли дата
_PENDING = re.compile(r"pending|processing|обрабатыва|ожида", re.I)


def _shown_has_date(page, shown: str, expected_text: str) -> bool:
    """В тексте блока доступа («Запланировано 20 нояб. 2026 г., 19:00») та же дата, что ожидали — по смыслу."""
    want = studio_date(page, expected_text)
    if not want:
        return False
    return any(studio_date(page, chunk) == want for chunk in re.split(r"[;|•]|\s{2,}", shown) + [shown])


def _check_scheduled(page, expected_date: str, url: str) -> list[str]:
    """На странице видео (после _confirm_saved): в блоке доступа должна стоять дата выхода. Не видно —
    не ошибка (видео сохранено, повтор создал бы дубль), а пометка «проверь в Studio»."""
    shown = ""
    for visit in range(PENDING_VISITS):
        if visit:  # Studio ещё обрабатывает видео («Visibility Pending») — дата появится чуть позже
            page.wait_for_timeout(20000)
            try:
                page.goto(f"{STUDIO}/video/{_video_id(url)}/edit", wait_until="domcontentloaded")
                page.locator("ytcp-video-metadata-visibility").first.wait_for(timeout=30000)
                page.wait_for_timeout(2000)
            except Exception:
                continue
        try:
            shown = page.locator("ytcp-video-metadata-visibility").first.inner_text().replace("\n", " ").strip()
        except Exception:
            shown = ""
            continue
        if expected_date and (norm_date(expected_date) in norm_date(shown) or _shown_has_date(page, shown, expected_date)):
            log(f"Studio: видео запланировано — {shown}")
            return []
        if not _PENDING.search(shown):
            break
    if not shown:
        return [f"не смог прочитать доступ видео — проверь в Studio, что оно запланировано ({url})"]
    if _PENDING.search(shown):
        log(f"Studio: доступ видео пока «{shown}» — Studio ещё обрабатывает, дату не показывает")
        return [f"Studio ещё обрабатывала видео («{shown[:60]}») — дату расписания не показала; ставили "
                f"{expected_date}, при желании глянь позже ({url})"]
    log(f"Studio: в доступе видео не вижу даты {expected_date}: «{shown}»")
    return [f"проверь в Studio: не вижу запланированной даты {expected_date} (в Studio: «{shown[:80]}») — {url}"]


DEAD_ROW = re.compile(r"processing abandoned|failed to upload|upload interrupted|upload aborted|upload failed", re.I)


def _list_row(page, channel_id: str, vid: str, title: str) -> str:
    """Строка ролика в списке «Content» канала: статус загрузки виден только там («Processing abandoned»,
    «Upload interrupted»); на странице самого видео его нет. Нет строки / список не открылся — пусто."""
    try:
        page.goto(f"{STUDIO}/channel/{channel_id}/videos/upload", wait_until="domcontentloaded")
        page.locator("ytcp-video-row").first.wait_for(state="visible", timeout=30000)
    except Exception:
        return ""
    page.wait_for_timeout(2500)
    rows = page.locator("ytcp-video-row")
    want = " ".join(title.split())[:40]
    for i in range(min(rows.count(), 30)):
        row = rows.nth(i)
        try:
            text = " ".join(row.inner_text(timeout=5000).split())
            linked = bool(vid) and row.locator(f'a[href*="{vid}"]').count() > 0
        except Exception:
            continue
        if linked or (want and want in text):  # у битого ролика ссылки на видео в строке нет — узнаём по названию
            return text
    return ""


_DURATION = re.compile(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\b")


def _check_file_uploaded(page, channel_id: str, url: str, title: str) -> bool:
    """Ролик сохранён по расписанию, но файл мог не загрузиться (профиль закрылся посреди загрузки): тогда в списке
    «Processing abandoned / Video failed to upload», а локальный файл программа уже удаляла бы (14.11).
    → True: Studio показывает видео с длительностью (файл есть) — только такой ролик можно считать выложенным;
    False: строку не нашли / длительности нет — не доказано (файл с компьютера оставляем). Битый ролик — ошибка."""
    row = _list_row(page, channel_id, _video_id(url), title)
    if row and DEAD_ROW.search(row):
        raise StudioError(f"в Studio у ролика нет видеофайла («{DEAD_ROW.search(row).group(0)}») — выложен пустой "
                          f"карточкой ({url}). Файл на компьютере сохранён; повтор загрузит ролик заново, "
                          "а пустую карточку удали в Studio")
    return bool(row and _DURATION.search(row))


def _audit_playlist(page, url: str, job: UploadJob, cancel) -> list[str]:
    """После сохранения — прочитать плейлист на странице видео и, если он не стоит, поставить (14.11: на повторах
    плейлист снимался). Единственный плейлист канала должен быть выбран; не вышло — пометка, а не молчание."""
    from engine.publish.studio_kit import LayoutError

    names = [str(x).strip() for x in (job.playlist_hint or []) if str(x).strip()]
    if len(names) != 1:
        return []  # несколько плейлистов / нет ни одного — это отдельная пометка при выборе

    def shown() -> str:
        try:
            return " ".join(page.locator("ytcp-video-metadata-playlists").first.inner_text(timeout=8000).split())
        except Exception:
            return ""

    def has(text: str) -> bool:
        return bool(text) and names[0].casefold() in text.casefold()

    page.goto(f"{STUDIO}/video/{_video_id(url)}/edit", wait_until="domcontentloaded")
    page.wait_for_timeout(6000)
    if has(shown()):
        return []
    log(f"Studio: плейлист «{names[0]}» на ролике не стоит (поле: «{shown()[:40]}») — ставлю")
    trigger = page.locator("ytcp-video-metadata-playlists").first
    try:
        notes = _pick_playlist(page, cancel, trigger=trigger)
        _act(cancel)
        human.click(page, page.locator("ytcp-button#save").first)
        page.wait_for_timeout(5000)
        page.goto(f"{STUDIO}/video/{_video_id(url)}/edit", wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
    except (StudioError, LayoutError):
        raise
    except Exception as exc:  # noqa: BLE001 — исправление не вышло: пометка, ролик уже сохранён
        if browser_lost(exc) or (cancel is not None and cancel.is_cancelled()):
            raise
        return [f"плейлист «{names[0]}» не стоит и поставить не вышло ({str(exc).splitlines()[0][:80]}) — поставь в Studio"]
    if has(shown()):
        return [f"плейлист «{names[0]}» не стоял — поставил после сохранения", *notes]
    return [f"плейлист «{names[0]}» не стоит — поставь в Studio сам ({url})", *notes]


def _confirm_saved(page, url: str) -> None:
    """Studio могла оставить ролик черновиком (клик не засчитался) — проверяем на странице видео."""
    vid = url.rstrip("/").rsplit("/", 1)[-1].split("=")[-1] if url else ""
    if not vid:
        raise StudioError("не нашёл ссылку на загруженное видео — проверь его в YouTube Studio")
    # не черновик = на странице есть блок доступа видео (у черновика вместо него баннер «Edit draft»);
    # Studio сохраняет не мгновенно — смотрим несколько раз; без подтверждения файл с компьютера не удаляем
    draft = False
    for _visit in range(4):
        page.goto(f"{STUDIO}/video/{vid}/edit", wait_until="domcontentloaded")
        for _ in range(12):
            page.wait_for_timeout(1500)
            if page.locator("ytcp-video-metadata-visibility").count():
                return
            if page.locator(DRAFT_BANNER_BUTTON).count():
                draft = True
                break
        page.wait_for_timeout(8000)
    if draft:
        raise StudioError(f"видео осталось черновиком в Studio ({url}) — «Продолжить» попробует снова")
    raise StudioError(f"Studio не подтвердила, что видео сохранено ({url})")


def _playlist_checked(box) -> bool:
    """Галочка плейлиста стоит: у ytcp-checkbox-lit нет aria-checked — состояние в атрибуте checked и во внутреннем
    div#checkbox. (Раньше смотрели aria-checked, он всегда пуст — и на повторе «продолжил черновик» программа
    каждый раз кликала по уже выбранному плейлисту и СНИМАЛА его, 14.11.)"""
    try:
        if box.get_attribute("checked", timeout=3000) is not None:
            return True
        return box.locator("div#checkbox").first.get_attribute("aria-checked", timeout=3000) == "true"
    except Exception:
        return False


def _pick_playlist(page, cancel: CancelToken | None, trigger=None) -> list[str]:
    """Ровно один плейлист на канале — выбираем его (уже выбран — не трогаем); иначе ни одного и пометка.
    trigger — поле «Playlists» (на странице видео); по умолчанию — поле в окне загрузки."""
    from engine.publish.studio_kit import el

    _act(cancel)
    human.click(page, trigger if trigger is not None else el(page, "playlist_trigger"))
    el(page, "playlist_list")
    page.wait_for_timeout(1500)
    boxes = page.locator("#playlists-list ytcp-checkbox-lit")
    n = boxes.count()
    notes: list[str] = []
    if n == 1:
        for _try in range(2):
            if _playlist_checked(boxes.first):
                break
            _act(cancel)
            human.click(page, boxes.first)
            page.wait_for_timeout(800)
        if not _playlist_checked(boxes.first):
            notes.append("плейлист не удалось отметить — поставь его в Studio сам")
    else:
        notes.append(f"плейлистов на канале {n} — не выбран ни один, оставь один плейлист" if n else
                     "на канале нет плейлиста — ролик без плейлиста")
    _act(cancel)
    human.click(page, page.locator("ytcp-playlist-dialog ytcp-button").last)  # «Done» / «Готово» — последняя кнопка
    page.wait_for_timeout(1000)
    return notes


def _video_url(page) -> str:
    links = page.eval_on_selector_all("ytcp-uploads-dialog a[href*='youtu']", "els => els.map(e => e.href)")
    return next((u for u in links if "youtu.be/" in u or "watch?v=" in u), "")


def music_claims(cdp_url: str, url: str) -> list[str]:
    """Страница претензий ролика в Studio (/video/<id>/claims) → треки нашей фоновой музыки, на которые
    претензия; они вносятся в чёрный список (engine.slides.music) и в ролики больше не попадут."""
    from engine.slides.music import claims_in

    vid = _video_id(url) if url else ""
    if not vid:
        return []
    with studio_page(cdp_url) as page:
        page.goto(f"{STUDIO}/video/{vid}/claims", wait_until="domcontentloaded")
        text = ""
        for _ in range(10):
            page.wait_for_timeout(1500)
            text = " ".join(page.locator("body").inner_text(timeout=5000).split())
            if "claim" in text.lower() or "претенз" in text.lower():
                break
    return claims_in(text, video=url)


CHECKS_ERROR = "ytcp-uploads-dialog ytcp-stepper button[test-id=CHECKS][state=error]"
DETAILS_ERROR = "ytcp-uploads-dialog ytcp-stepper button[test-id=DETAILS][state=error]"
_FIELD_ERRORS = "ytcp-uploads-dialog #error-message:visible, ytcp-uploads-dialog .error-message:visible"


def _browser_zone(page) -> str:
    """Часовой пояс браузера профиля (Europe/Berlin…), только чтение."""
    try:
        return str(page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone") or "")
    except Exception:
        return ""


def studio_time(publish_at: datetime, offset: tuple[int, int], zone: str = "", now: datetime | None = None) -> datetime:
    """Время выхода в поясе Studio. Подсказка Studio даёт смещение на СЕГОДНЯ («GMT+0100»); если до даты
    выхода пояс перейдёт на летнее/зимнее время, смещение будет другим. Поэтому: известен пояс браузера и
    сегодня он совпадает с подсказкой — считаем по нему (с переходами); иначе — по смещению из подсказки."""
    fixed = timezone(timedelta(hours=offset[0], minutes=offset[1]))
    if zone:
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(zone)
            now = now or datetime.now(timezone.utc)
            if now.astimezone(tz).utcoffset() == now.astimezone(fixed).utcoffset():
                return publish_at.astimezone(tz)
        except Exception:
            pass
    return publish_at.astimezone(fixed)


def clock_of(text: str) -> tuple[int, int] | None:
    """Время, как его показывает Studio, → (часы 0–23, минуты): «14:00», «2:00 PM», «12:30 AM», «2:00 пп».
    Профиль с английской (США) локалью показывает 12-часовой формат (13.11: «2:00 PM» вместо 14:00)."""
    m = re.search(r"(\d{1,2})[:.](\d{2})\s*([AaPpАаПп][\.\s]?[MmМм]?\.?|[дп]п|[ДП]П)?", str(text or ""))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    mark = (m.group(3) or "").lower().replace(".", "").replace(" ", "")
    if mark:
        pm = mark.startswith(("p", "п")) and mark != "дп"
        h = h % 12 + (12 if pm else 0)
    return h, mi


STEP_NAMES = {"DETAILS": "Details", "MONETIZATION": "Monetization", "CONTENT_RATINGS": "Ad suitability",
              "VIDEO_ELEMENTS": "Video elements", "CHECKS": "Checks", "REVIEW": "Visibility"}
WALK_MAX = 10


def _walk_to_visibility(page, cancel: CancelToken | None) -> list[str]:
    """«Далее» по шагам окна загрузки до Visibility. Шаги монетизации (есть только у канала на монетизации)
    программа проходит сама; незнакомый шаг — остановка со снимком, без молчаливого пропуска."""
    from engine.publish import monetize
    from engine.publish.studio_kit import el, step

    notes: list[str] = []
    seen = monetize.steps(page)
    if "MONETIZATION" in seen:
        log(f"Studio: канал на монетизации — шаги: {', '.join(STEP_NAMES.get(s, s) for s in seen)}")
    for _ in range(WALK_MAX):
        cur = monetize.active_step(page)
        name = STEP_NAMES.get(cur, cur or "?")
        if cur == "REVIEW" or (not cur and page.locator(SELECTORS_PRIVATE).count()):
            return notes
        if cur == "MONETIZATION":
            with step(page, "монетизация: реклама каждые 20 с (NexLev)"):
                notes += monetize.monetization(page, cancel)
        elif cur == "CONTENT_RATINGS":
            with step(page, "пригодность для рекламы: нарушений нет"):
                notes += monetize.ad_suitability(page, cancel)
        elif cur and cur not in STEP_NAMES:
            from engine.publish.studio_kit import diagnose

            folder = diagnose(page, f"незнакомый шаг {cur}", RuntimeError(cur))
            raise NeedsUser(f"в окне загрузки незнакомый шаг «{cur}» — пройди его руками и «Продолжить». Снимок: {folder}")
        with step(page, f"«Далее» после шага {name}"):
            _act(cancel)
            human.click(page, el(page, "next"))
            page.wait_for_timeout(2500)
            _check_dialog_problems(page)
            _check_details_error(page)
            after = monetize.active_step(page)
            if after == cur and cur:
                page.wait_for_timeout(2500)
                if monetize.active_step(page) == cur:
                    raise StudioError(f"«Далее» не пускает дальше шага {name} — на нём ошибка")
    raise StudioError("не дошёл до шага Visibility")


def _check_details_error(page) -> None:
    """Шаг Details с ошибкой (красный «!»): «Далее» не пускает — сказать настоящую причину (длина названия,
    запрещённые символы…), а не «нет кнопки» на следующем шаге."""
    try:
        bad = page.locator(DETAILS_ERROR).count() > 0
    except Exception:
        return
    if not bad:
        return
    try:
        msgs = [" ".join(t.split()) for t in page.locator(_FIELD_ERRORS).all_inner_texts() if t.strip()]
    except Exception:
        msgs = []
    raise StudioError("YouTube не принимает данные на шаге «Детали»"
                      + (f": {'; '.join(dict.fromkeys(msgs))}" if msgs else " (красный «!» у шага Details)"))


def checks_problem(page) -> str:
    """Проверки YouTube нашли проблему (шаг «Initial check» с ошибкой: чаще всего претензия правообладателя на
    чужой контент) → понятная пометка; нет — «». Признак — служебные метки окна, от языка не зависит."""
    try:
        if not page.locator(CHECKS_ERROR).count():
            return ""
        status = " ".join(page.locator("ytcp-uploads-dialog ytcp-video-upload-progress").first
                          .inner_text(timeout=3000).split())[:200]
    except Exception:
        status = ""
    return ("проверки YouTube нашли проблему — скорее всего претензию правообладателя на чужой контент"
            + (f" («{status}»)" if status else "") + ". Ролик можно выложить, но проверь в Studio «Checks»: "
            "блокировка в странах / доход правообладателю")


def _close_done_dialog(page) -> None:
    """После «Schedule/Save» Studio показывает окно со ссылкой — закрываем его. Окно-предупреждение проверок
    (авторские права, «проверки не закончились») — не жмём сами: снимок, текст в журнал и «ждёт тебя»."""
    from engine.publish.studio_kit import diagnose

    for _ in range(30):
        warn = page.locator("ytcp-prechecks-warning-dialog")
        if warn.count() and warn.first.is_visible():
            text = _visible_text(warn.first)
            folder = diagnose(page, "предупреждение после Schedule", RuntimeError(text or "предупреждение"))
            log(f"Studio: после «Schedule» предупреждение: «{text}» — снимок: {folder}")
            raise NeedsUser(f"Studio перед сохранением показала предупреждение: «{text[:300]}». Ролик не сохранён "
                            f"(черновик). Посмотри снимок ({folder}) и реши в Studio сам: выкладываем — нажми там "
                            "«Schedule»/«Save» с нужной датой, и «Продолжить» в программе закончит остальное "
                            "(соавторы, комментарии); не выкладываем — отмени задачу")
        still = _still_checking(page)
        if still:
            raise StudioError(f"Studio не сохранила ролик: проверки ещё не закончились («{still[:120]}»). Ролик остался "
                              "черновиком — повтор подождёт конца проверок и сохранит")
        dialog = page.locator("ytcp-video-share-dialog")
        if dialog.count() and dialog.first.is_visible():
            close = page.locator("ytcp-video-share-dialog #close-button, ytcp-video-share-dialog #close-icon-button")
            if close.count():
                pause()
                human.click(page, close.first)
            return
        if not page.locator("ytcp-uploads-dialog").is_visible():
            page.wait_for_timeout(6000)  # окно закрылось, а Studio ещё дописывает сохранение — не уходить сразу
            return
        page.wait_for_timeout(1000)
    shown = _visible_text(page.locator("ytcp-uploads-dialog").first)
    raise StudioError(f"окно загрузки не закрылось после «Schedule» — Studio показывает: «{shown[-300:]}»")


def _still_checking(page) -> str:
    """Окно «We're still checking your content…» (проверки не закончились) → его текст; нет окна — пусто."""
    text = _visible_dialogs(page)
    m = re.search(r"(we.re still checking your content[^|]*)", text, re.I)
    return m.group(1).strip() if m else ""


def _visible_dialogs(page) -> str:
    """Тексты открытых окон Studio (для журнала: что показала Studio после нажатия)."""
    try:
        return " | ".join(t for t in page.eval_on_selector_all(
            "ytcp-video-share-dialog, ytcp-prechecks-warning-dialog, ytcp-dialog, tp-yt-paper-dialog",
            "els => els.filter(e => e.offsetParent !== null).map(e => (e.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 150))")
            if t)
    except Exception:
        return ""


def _visible_text(locator) -> str:
    try:
        return " ".join(locator.inner_text(timeout=5000).split())[:600]
    except Exception:
        return ""


def _video_id(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1].split("=")[-1] if url else ""


def invite_coauthors(page, video_url: str, coauthors: list, *, cancel: CancelToken | None = None) -> dict[str, str]:
    """На странице видео: «Invite a collaborator» → каждый соавтор по @адресу → «Create link» (ссылка-приглашение)
    → «Save» в окне и на странице. → {ключ соавтора: ссылка}. Ссылка становится рабочей после сохранения."""
    vid = _video_id(video_url)
    page.goto(f"{STUDIO}/video/{vid}/edit", wait_until="domcontentloaded")
    page.wait_for_selector("#toggle-button", timeout=60000)
    page.wait_for_timeout(3000)
    from engine.publish.studio_kit import LayoutError, el

    _act(cancel)
    human.click(page, page.locator("#toggle-button").first)  # «Show more»: там раздел Collaboration
    try:
        button = el(page, "collab_button")
    except LayoutError:
        raise StudioError("на канале нет соавторства (кнопки «Invite a collaborator»)") from None
    # оболочка окна у Studio нулевого размера (и бывает скрытая копия) — ждём видимые элементы внутри
    search = page.locator("#collaborators-dialog #search-input:visible").first
    for attempt in range(3):  # окно соавторов иногда открывается медленно
        if not search.count():
            _clear_popups(page)
            _act(cancel)
            try:
                human.click(page, button, timeout=15000)
            except Exception:
                continue
        try:
            search.wait_for(timeout=20000)
            break
        except Exception:
            if attempt == 2:
                raise StudioError("не открылось окно «Invite a collaborator»") from None
    links: dict[str, str] = {}
    for co in coauthors:
        handle = co.channel.handle
        _type_into(page, search, handle, cancel)
        page.wait_for_timeout(4000)
        item = page.locator("#collaborators-dialog #channel-results-container tp-yt-paper-item:visible").filter(
            has_text=handle).first
        try:
            item.wait_for(timeout=15000)
        except Exception:
            raise StudioError(f"соавтор {handle} не нашёлся в поиске (или исчерпан лимит приглашений)") from None
        _act(cancel)
        human.click(page, item)
        create = el(page, "collab_create_link")
        _act(cancel)
        human.click(page, create)
        page.wait_for_timeout(4000)
        found = page.evaluate("""() => { const t = [...document.querySelectorAll('*')].map(e => e.value || '').join(' ')
            + document.body.innerText; return t.match(/https:\\/\\/studio\\.youtube\\.com\\/channel\\/UC[\\w-]+\\/collaboration\\/UC[\\w-]+/g) || []; }""")
        link = next((u for u in found if f"/channel/{co.channel.id}/" in u), "")
        if not link:
            raise StudioError(f"не получил ссылку-приглашение для {handle}")
        links[co.key] = link
        _act(cancel)
        human.click(page, el(page, "collab_link_close"))
        page.wait_for_timeout(1500)
    _act(cancel)
    human.click(page, el(page, "collab_save"))
    page.wait_for_timeout(2500)
    _act(cancel)
    human.click(page, el(page, "video_save"))  # сохранить видео — ссылки становятся рабочими
    page.wait_for_timeout(6000)
    log(f"Соавторы приглашены: {', '.join(co.channel.handle for co in coauthors)}")
    return links


VERIFY_RE = re.compile(r"verify it.?s you|подтвердите,? что это (вы|ты)|confirm it.?s really you", re.I)
_COLLAB_LIST = "ytcp-video-collaborations-list-dialog tp-yt-paper-dialog:visible"


def _verify_gate(page) -> None:
    """Google «Verify it's you» поверх Studio — дальше только с паролем, его программа не вводит."""
    try:
        texts = page.locator("tp-yt-paper-dialog:visible, ytcp-dialog:visible, [role=dialog]:visible").all_inner_texts()
    except Exception:
        return
    if any(VERIFY_RE.search(t or "") for t in texts):
        raise NeedsUser("Google просит подтвердить вход («Verify it's you») — пароль программа не вводит")


ACCEPT_ROUNDS = 12  # больше приглашений от одного канала за раз не бывает (страховка от зацикливания)


def accept_coauthor(cdp_url: str, channel, invite_link: str, *, title: str = "",
                    cancel: CancelToken | None = None, allow_none: bool = False) -> list[str]:
    """В профиле соавтора: открыть ссылку-приглашение от имени его канала и принять ВСЕ ждущие приглашения
    от канала-поставщика. Ссылка — на пару каналов, не на ролик: одно приглашение — окно «Accept»,
    несколько — список «Collaboration requests»; тогда открываем ролики списка по очереди и принимаем каждый.
    → названия принятых роликов. Ничего не принято, а ролика title нет — ошибка."""
    from engine.publish.studio_kit import set_channel, step

    if not invite_link:
        raise StudioError("нет ссылки-приглашения (приглашение не создалось)")
    set_channel(channel.handle or channel.id)
    accepted: list[str] = []
    with studio_page(cdp_url) as page:
        with step(page, "соавтор: переключение на канал"):
            switch_to(page, channel, cancel=cancel)
        with step(page, "соавтор: ссылка-приглашение и «Принять»"):
            for _ in range(ACCEPT_ROUNDS):
                try:
                    name = _accept_one(page, invite_link, cancel, skip=accepted)
                except Exception as exc:
                    if not accepted or browser_lost(exc) or (cancel is not None and cancel.is_cancelled()):
                        raise
                    # часть уже принята — её не теряем; ролики, которых нет среди принятых, разберёт вызывающий
                    log(f"Соавтор {channel.name}: принято {len(accepted)}, дальше сбой — {str(exc).splitlines()[0][:200]}")
                    break
                if name is None:
                    break
                accepted.append(name)
                log(f"Соавторство принято: {channel.name} {channel.handle} — «{name[:80]}»")
            if accepted:
                if len(accepted) > 1:
                    log(f"Соавтор {channel.name}: принято приглашений от канала — {len(accepted)}")
                return accepted
            if allow_none:
                return []  # проверка: ждущих нет — значит, всё принято
            raise StudioError("ждущих приглашений нет — возможно, уже принято или отозвано"
                              + (f" (ролик «{title[:60]}»)" if title else ""))
    return accepted


def accept_coauthor_links(cdp_url: str, channel, links: list[str], *,
                          cancel: CancelToken | None = None) -> dict[str, list[str] | str]:
    """Тот же соавтор, несколько ссылок-приглашений (от разных каналов-владельцев) за один заход в профиль:
    для каждой принимаются ВСЕ ждущие приглашения от канала. → {ссылка: [принятые ролики] (пусто — ждущих не было)
    или текст ошибки}. Одна ссылка не получилась — остальные всё равно проходим."""
    from engine.publish.studio_kit import set_channel, step

    set_channel(channel.handle or channel.id)
    out: dict[str, list[str] | str] = {}
    with studio_page(cdp_url) as page:
        with step(page, "соавтор: переключение на канал"):
            switch_to(page, channel, cancel=cancel)
        for link in links:
            if cancel:
                cancel.check()
            accepted: list[str] = []
            try:
                with step(page, "соавтор: ссылка-приглашение и «Принять»"):
                    for _ in range(ACCEPT_ROUNDS):
                        name = _accept_one(page, link, cancel, skip=accepted)
                        if name is None:
                            break
                        accepted.append(name)
                        log(f"Соавторство принято: {channel.name} {channel.handle} — «{name[:80]}»")
                out[link] = accepted
            except NeedsUser:
                raise  # Google просит пароль — для всех остальных ссылок так же
            except Exception as exc:
                if browser_lost(exc) or (cancel is not None and cancel.is_cancelled()):
                    raise
                out[link] = " ".join(str(exc).split())[:300] or exc.__class__.__name__
    return out


def _accept_one(page, invite_link: str, cancel, skip: list[str] = ()) -> str | None:
    """Открыть ссылку и принять одно ждущее приглашение. → название ролика; None — ждущих больше нет.
    skip — уже принятые за этот заход: если Studio ещё показывает их в списке, не кликаем по кругу."""
    page.goto(invite_link, wait_until="domcontentloaded")
    dialog = page.locator("ytcp-collaboration-acceptance-dialog tp-yt-paper-dialog:visible, "
                          "ytcp-collaboration-acceptance-dialog:visible").first
    confirm = page.locator("ytcp-collaboration-acceptance-dialog #confirm-button:visible").first
    listing = page.locator(_COLLAB_LIST).first
    for _ in range(40):  # что откроется: одно приглашение, список или проверка Google
        page.wait_for_timeout(1000)
        _verify_gate(page)
        if confirm.count() or listing.count():
            break
    else:
        return None
    name = ""
    if not confirm.count():
        rows = listing.locator("ytcp-video-row")
        for _ in range(20):  # список приглашений подгружается после того, как окно уже открылось (14.11: «пусто» за 3 с)
            if rows.count() or confirm.count():
                break
            page.wait_for_timeout(1000)
    if not confirm.count():
        row = None
        for i in range(min(rows.count(), 50)):
            try:
                label = (rows.nth(i).locator("#video-title").first.get_attribute("aria-label") or "").strip()
            except Exception:
                label = ""
            if label and label in skip:
                continue
            row, name = rows.nth(i), label
            break
        if row is None:
            return None
        _act(cancel)
        human.click(page, row.locator("#video-title").first)  # именно по названию: клик в другое место строки окно не открывает
        for _ in range(20):
            page.wait_for_timeout(1000)
            _verify_gate(page)
            if confirm.count():
                break
        else:
            raise StudioError("в списке «Collaboration requests» ролик не открылся (нет кнопки «Accept»)")
    if not name:
        try:
            m = re.search(r'collaborate on\s+["“«](.+?)["”»]', dialog.inner_text())
            name = m.group(1) if m else ""
        except Exception:
            name = ""
    _act(cancel)
    try:
        human.click(page, confirm)
    except Exception:
        # Google поверх окна приглашения: «Verify it's you» перекрывает «Accept», клик упирается в затемнение и падает
        # по таймауту. Причина — окно Google (его нужно назвать), а не «не дождался элемента» (20.11: 17 неудач подряд)
        _verify_gate(page)
        raise
    for _ in range(20):
        page.wait_for_timeout(1000)
        _verify_gate(page)
        if not confirm.is_visible():
            return name or "(без названия)"
    raise StudioError("«Принять» не сработало — окно приглашения не закрылось")


# ---------- комментарий-призыв под вышедшими роликами ----------

COMMENTS_FILE = "comments.json"
COMMENT_LOOKBACK = 10  # больше роликов назад за раз не смотрим (первый запуск на старом канале)
_EMOJI = re.compile(r"[^\w\s.,!?:;—–()«»\"'-]+")


def _comment_key(text: str) -> str:
    """Чем узнать свой комментарий: текст без смайликов (YouTube показывает их картинками), первые 40 знаков."""
    return re.sub(r"\s+", " ", _EMOJI.sub("", text)).strip().lower()[:40]


def backfill_comments(cdp_url: str, channel, text: str, *, cancel: CancelToken | None = None) -> list[str]:
    """Под вышедшими роликами канала — комментарий-призыв от имени канала, закреплённый.
    Идём от самого нового вышедшего ролика назад, пока не встретим ролик, где он уже есть (обычно это
    один ролик). Запланированные ещё не вышли — до них очередь дойдёт при следующей выкладке.
    → пометки для итогов. Сбой — только пометка: выкладку он не портит."""
    from engine.publish.store import LOCK, read_json, write_json
    from engine.publish.studio_kit import set_channel, step

    if not text.strip():
        return []
    set_channel(channel.handle or channel.id)
    notes: list[str] = []
    done_ids = set((read_json(COMMENTS_FILE, {}).get(channel.id) or []))
    posted = unpinned = 0
    with studio_page(cdp_url) as page:
        with step(page, "комментарии: переключение на канал"):
            switch_to(page, channel, cancel=cancel)
        with step(page, "комментарии: список вышедших роликов"):
            videos = _public_videos(page, channel.id)
        for vid in videos[:COMMENT_LOOKBACK]:
            if cancel:
                cancel.check()
            if vid in done_ids:
                break  # дальше уже прокомментировано программой
            if posted:  # подряд одинаковые комментарии YouTube считает спамом — разносим по времени
                _comment_gap(cancel)
            with step(page, f"комментарий под роликом {vid}"):
                state = _comment_on(page, vid, text, cancel, channel.handle)
            if state == "hidden":
                notes.append(f"YouTube убрал комментарий-призыв под роликом {vid} сразу после отправки (похоже на "
                             "спам-фильтр: много одинаковых комментариев подряд) — повторю при следующей выкладке")
                log(f"✗ Studio {channel.handle}: комментарий под роликом {vid} пропал после перезагрузки — "
                    "на этот раз комментарии на канале прекращаю, повторю при следующей выкладке")
                break  # не отмечаем ролик сделанным и не долбим дальше
            if state in ("posted", "posted_unpinned"):
                posted += 1
            if state == "posted_unpinned":
                unpinned += 1
            with LOCK:
                data = read_json(COMMENTS_FILE, {})
                data[channel.id] = sorted({*(data.get(channel.id) or []), vid})[-200:]
                write_json(COMMENTS_FILE, data)
            if state == "exists":
                break  # уже был (вручную или раньше) — старше тоже есть
            if state == "off":
                notes.append(f"комментарии отключены под роликом {vid}")
    if posted:
        pinned = posted - unpinned
        notes.append(f"комментарий-призыв оставлен под вышедшими роликами: {posted}"
                     + (f" (закреплён: {pinned}" + (f", закрепление недоступно: {unpinned}" if unpinned else "") + ")"))
    return notes


def _public_videos(page, channel_id_: str) -> list[str]:
    """Вышедшие ролики канала, новые первыми (вкладка «Видео» на youtube.com — только опубликованные)."""
    page.goto(f"https://www.youtube.com/channel/{channel_id_}/videos", wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    links = page.eval_on_selector_all("ytd-rich-grid-renderer a[href*='watch?v='], ytd-browse a[href*='watch?v=']",
                                      "els => els.map(e => e.getAttribute('href'))")
    ids = [m.group(1) for h in links for m in [re.search(r"v=([\w-]{11})", h or "")] if m]
    return list(dict.fromkeys(ids))


def _comment_on(page, vid: str, text: str, cancel: CancelToken | None, handle: str = "") -> str:
    """Ролик: есть наш комментарий → «exists»; комментарии выключены → «off»; иначе пишем, закрепляем → «posted»."""
    from engine.publish.studio_kit import LayoutError, el

    page.goto(f"https://www.youtube.com/watch?v={vid}", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    for _ in range(6):  # комментарии подгружаются при прокрутке
        page.mouse.wheel(0, random.randint(500, 900))  # колесом мыши, неровно
        page.wait_for_timeout(random.randint(700, 1500))
    try:
        box = el(page, "comment_box", timeout=25000)
    except LayoutError:
        if page.locator("ytd-comments ytd-message-renderer").count():
            return "off"
        raise
    if _has_ours(page, text, handle):
        log(f"Studio: под роликом {vid} комментарий-призыв уже есть")
        return "exists"
    _act(cancel)
    human.click(page, box)
    editor = el(page, "comment_editor")
    _act(cancel)
    human.click(page, editor)
    human.type_text(page, text, cancel)
    _act(cancel)
    human.click(page, el(page, "comment_submit"))
    page.wait_for_timeout(4000)
    thread = page.locator("ytd-comment-thread-renderer").filter(has_text=_comment_key(text)[:30]).first
    try:
        thread.wait_for(timeout=20000)
    except Exception:
        raise StudioError("комментарий не появился после отправки") from None
    pinned = _pin(page, thread, vid, cancel)
    if not _still_there(page, text, handle, cancel):
        return "hidden"
    log(f"Studio: комментарий-призыв оставлен под роликом {vid} (после перезагрузки на месте)")
    return "posted" if pinned else "posted_unpinned"


COMMENT_GAP = (60.0, 150.0)  # пауза между комментариями-призывами, сек
VERIFY_DELAY = 12000  # сколько ждать перед проверкой, что комментарий не убран (мс)


def _comment_gap(cancel: CancelToken | None) -> None:
    import random
    import time

    end = time.monotonic() + random.uniform(*COMMENT_GAP)
    while time.monotonic() < end:
        if cancel:
            cancel.check()
        time.sleep(1)


def _still_there(page, text: str, handle: str, cancel: CancelToken | None) -> bool:
    """Комментарий сразу после отправки показывается всегда, даже если YouTube его тут же отклонит.
    Проверяем по-честному: ждём, перезагружаем страницу и ищем его снова."""
    page.wait_for_timeout(VERIFY_DELAY)
    if cancel:
        cancel.check()
    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    for _ in range(6):
        page.mouse.wheel(0, random.randint(500, 900))  # колесом мыши, неровно
        page.wait_for_timeout(random.randint(700, 1500))
    for _ in range(3):
        if _has_ours(page, text, handle):
            return True
        page.mouse.wheel(0, random.randint(500, 900))
        page.wait_for_timeout(random.randint(1800, 3200))
    return False


# пункт меню комментария «Закрепить» на языках интерфейса (иконки у пунктов нет — только текст)
_PIN_WORDS = ("pin", "закреп", "fixar", "fijar", "anheften", "épingler", "fissa", "przypnij", "sabitle")


def _pin(page, thread, vid: str, cancel: CancelToken | None) -> bool:
    """Закрепить свой комментарий, если на канале это есть. Нет пункта «Закрепить» — просто комментарий."""
    try:
        _act(cancel)
        human.click(page, thread.locator("#action-menu button").first)
        page.wait_for_timeout(1500)
        items = page.locator("ytd-menu-popup-renderer ytd-menu-navigation-item-renderer:visible, "
                             "ytd-menu-popup-renderer ytd-menu-service-item-renderer:visible")
        labels = [t.strip().lower() for t in items.all_inner_texts()]
        k = next((i for i, t in enumerate(labels) if any(t.startswith(w) for w in _PIN_WORDS)), None)
        if k is None:
            human.press(page, "Escape")
            log(f"Studio: под роликом {vid} закрепление недоступно (в меню: {', '.join(labels) or '—'}) — оставил комментарий")
            return False
        _act(cancel)
        human.click(page, items.nth(k))
        from engine.publish.studio_kit import el

        _act(cancel)
        human.click(page, el(page, "pin_confirm"))
        page.wait_for_timeout(2500)
        log(f"Studio: комментарий закреплён под роликом {vid}")
        return True
    except CancelledError:
        raise
    except Exception as exc:
        why_dialog = _visible_dialog_text(page)
        try:
            human.press(page, "Escape")
        except Exception:
            pass
        why = f"YouTube показал: «{why_dialog}»" if why_dialog else (str(exc).splitlines() or [""])[0][:120]
        log(f"Studio: закрепить комментарий под роликом {vid} не вышло ({why}) — оставил просто комментарий")
        return False


def _visible_dialog_text(page) -> str:
    """Текст открытого окна YouTube (например, «подтвердите аккаунт, чтобы закреплять») — для журнала."""
    try:
        texts = page.eval_on_selector_all(
            "tp-yt-paper-dialog, yt-confirm-dialog-renderer, ytd-popup-container [role=dialog]",
            "els => els.filter(e => e.offsetParent !== null).map(e => (e.innerText || '').replace(/\\s+/g, ' ').trim())")
        return next((t for t in texts if t), "")[:200]
    except Exception:
        return ""


def _has_ours(page, text: str, handle: str = "") -> bool:
    """Наш комментарий уже есть: среди загруженных (закреплённый — первым) — с тем же текстом от владельца канала."""
    key = _comment_key(text)
    threads = page.eval_on_selector_all("ytd-comment-thread-renderer", """els => els.slice(0, 40).map(e => ({
        author: ((e.querySelector('#header-author') || {}).innerText || ''),
        text: ((e.querySelector('#content-text') || {}).innerText || '')}))""")
    return any(_comment_key(t["text"]).startswith(key[:30]) and (not handle or handle in t["author"]) for t in threads)

