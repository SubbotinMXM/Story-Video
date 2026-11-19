"""Аналитика каналов из профилей AdsPower: подписчики, часы просмотра за всё время, просмотры за 48 часов.

Где берём (проверено вживую в Studio, ноябрь 2026):
- главная аналитики канала (/channel/<UC>/analytics): карточка «в реальном времени» — просмотры за 48 часов,
  соседняя — всего подписчиков (точные числа, без задержки);
- расширенный режим за всё время (explore, time_period=lifetime): строка «Итого» — часы просмотра, просмотры
  и подписчики за всю жизнь канала, в порядке метрик из адреса (от языка интерфейса не зависит).
  Данные YouTube Analytics приходят с задержкой до 1–2 суток.

Браузер профиля открывается скрыто (headless) и только читает страницы: никаких кликов, кроме переключения
канала в профиле с несколькими каналами — оно, как и при выкладке, делается живой мышью с паузами.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field

from engine.core.cancel import CancelToken
from engine.core.logutil import log
from engine.publish import pause
from engine.publish.store import LOCK, read_json, write_json

FILE = "analytics.json"
HISTORY_MAX = 400  # снимков на канал (синхронизация раз в день — больше года)
WAIT_SEC = 60  # сколько ждать, пока Studio дорисует цифры
CHANNEL_TIMEOUT = 5 * 60  # на канал (переключение + две страницы); профиль — столько × число каналов

HUB_GRID = 'ul[data-target-id="key_metrics_grid"]'
REALTIME_CARD = ".ytcpAdaptiveCardGridViewModelRealtimeCard"
TOTAL_VALUES = "yta-explore-table-row.total-row .debug-metric-value"
# порядок метрик задаём сами — так же идут столбцы строки «Итого»
EXPLORE_METRICS = ("WATCH_TIME", "VIEWS", "SUBSCRIBERS_NET_CHANGE")
_SUBS_WORDS = ("subscri", "подписч", "suscript", "abonn", "abonne", "inscrit", "iscritt")


def day_before(history: list[dict], now_t: float) -> dict | None:
    """С чем сравнивать текущие цифры: последний снимок прошлого дня (по МСК) — «вчера».
    Вчера не синхронизировали — последний снимок раньше (дату показываем); раньше сегодня снимков нет — None."""
    from datetime import datetime

    from engine.publish.schedule import MSK

    today = datetime.fromtimestamp(now_t, MSK).date()
    older = [h for h in history if h.get("t") and datetime.fromtimestamp(float(h["t"]), MSK).date() < today]
    return max(older, key=lambda h: float(h["t"])) if older else None


class AnalyticsError(RuntimeError):
    pass


@dataclass
class Stats:
    channel_id: str
    name: str = ""
    handle: str = ""
    profile_id: str = ""
    profile: str = ""  # «№6 Страны РУ»
    subscribers: int | None = None
    views_48h: int | None = None
    watch_hours: float | None = None  # за всё время
    views_total: int | None = None  # за всё время
    scheduled: int | None = None  # запланировано, но ещё не вышло (страница «Контент»)
    synced_at: float = 0.0
    error: str = ""
    history: list[dict] = field(default_factory=list)


# ---------- числа со страницы ----------

_MULT = (("млрд", 1e9), ("тыс", 1e3), ("млн", 1e6), ("mil", 1e3), ("k", 1e3), ("к", 1e3), ("m", 1e6), ("м", 1e6),
         ("b", 1e9))


def parse_number(text: str) -> float | None:
    """Число как его пишет Studio на любом языке: «9,981», «777,373.7», «113.0K», «9 981», «777 373,7»,
    «1,2 тыс.», «3,4 млн». Нет числа («—») — None."""
    t = str(text or "").strip().lower().replace(" ", " ").replace(" ", " ").replace(" ", " ")
    t = t.replace("‎", "").replace("⁨", "").replace("⁩", "")
    m = re.search(r"[-−+]?\d[\d\s.,']*", t)
    if not m:
        return None
    core = m.group(0).strip().replace("−", "-").replace(" ", "").replace("'", "")
    rest = t[m.end():].strip()
    mult = next((k for word, k in _MULT if rest.startswith(word)), 1.0)
    core = core.rstrip(".,")
    if "," in core and "." in core:
        dec = "," if core.rfind(",") > core.rfind(".") else "."
        core = core.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in core or "." in core:
        sep = "," if "," in core else "."
        if mult == 1.0 and re.fullmatch(rf"[-+]?\d{{1,3}}(\{sep}\d{{3}})+", core):
            core = core.replace(sep, "")  # разделитель тысяч: 9,981 / 1.832.282
        else:
            core = core.replace(sep, ".")
    try:
        return float(core) * mult
    except ValueError:
        return None


def _first_number(lines: list[str]) -> float | None:
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith(("+", "−", "-")):
            continue
        n = parse_number(s)
        if n is not None and re.fullmatch(r"[\d\s.,'  kKmMкКмМ]+(тыс\.?|млн\.?)?", s):
            return n
    return None


def _card_lines(text: str) -> tuple[str, list[str]]:
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    return (lines[0] if lines else ""), lines[1:]


def read_hub(cards: list[tuple[str, bool]]) -> tuple[int | None, int | None]:
    """Карточки главной аналитики [(текст, это карточка реального времени)] → (подписчики, просмотры за 48 ч).
    48 ч — карточка реального времени (в названии «48»); подписчики — карточка с «подписчиками» в названии,
    иначе соседняя после карточки реального времени (так она стоит в Studio)."""
    views = subs = None
    rt = next((i for i, (_t, live) in enumerate(cards) if live), None)
    if rt is None:
        rt = next((i for i, (t, _l) in enumerate(cards) if "48" in _card_lines(t)[0]), None)
    if rt is not None:
        views = _first_number(_card_lines(cards[rt][0])[1])
    for i, (text, _live) in enumerate(cards):
        title, rest = _card_lines(text)
        if i != rt and any(w in title.lower() for w in _SUBS_WORDS):
            subs = _first_number(rest)
            break
    if subs is None and rt is not None and rt + 1 < len(cards):
        subs = _first_number(_card_lines(cards[rt + 1][0])[1])
    return (int(subs) if subs is not None else None), (int(views) if views is not None else None)


def read_totals(values: list[str]) -> tuple[float | None, int | None, int | None]:
    """Строка «Итого» расширенного режима → (часы, просмотры, подписчики) за всё время."""
    nums = [parse_number(v) for v in values[:len(EXPLORE_METRICS)]]
    nums += [None] * (len(EXPLORE_METRICS) - len(nums))
    hours, views, subs = nums
    return hours, (int(views) if views is not None else None), (int(subs) if subs is not None else None)


# ---------- Studio ----------


def hub_url(cid: str) -> str:
    from engine.publish.youtube import STUDIO

    return f"{STUDIO}/channel/{cid}/analytics"


def explore_url(cid: str) -> str:
    from engine.publish.youtube import STUDIO

    metrics = "".join(f"&t_metrics={m}" for m in EXPLORE_METRICS)
    return (f"{STUDIO}/channel/{cid}/analytics/tab-overview/period-default/explore?entity_type=CHANNEL"
            f"&entity_id={cid}&time_period=lifetime&explore_type=TABLE_AND_CHART&metric={EXPLORE_METRICS[0]}"
            f"&granularity=DAY{metrics}&dimension=VIDEO&o_column={EXPLORE_METRICS[0]}"
            f"&o_direction=ANALYTICS_ORDER_DIRECTION_DESC")


def _wait(page, selector: str, count: int, cancel: CancelToken | None) -> bool:
    end = time.monotonic() + WAIT_SEC
    while time.monotonic() < end:
        if cancel:
            cancel.check()
        try:
            if page.locator(selector).count() >= count:
                page.wait_for_timeout(2500)  # цифры дорисовываются
                return True
        except Exception:
            pass
        page.wait_for_timeout(1000)
    return False


def read_channel(page, channel, cancel: CancelToken | None = None) -> Stats:
    """Цифры активного канала (переключение — до вызова). Только переходы по адресам и чтение текста."""
    from engine.publish import youtube

    st = Stats(channel_id=channel.id, name=channel.name, handle=channel.handle)
    pause(cancel)
    if youtube.studio_channel(page, hub_url(channel.id)) != channel.id:
        raise AnalyticsError("Studio открылась не от имени этого канала")
    if not _wait(page, f"{HUB_GRID} > li", 2, cancel):
        raise AnalyticsError("главная аналитики канала не загрузилась")
    items = page.locator(f"{HUB_GRID} > li")
    cards = [(items.nth(i).inner_text(), items.nth(i).locator(REALTIME_CARD).count() > 0)
             for i in range(items.count())]
    st.subscribers, st.views_48h = read_hub(cards)
    pause(cancel)
    youtube.goto(page, explore_url(channel.id), timeout=60000)
    if _wait(page, TOTAL_VALUES, len(EXPLORE_METRICS), cancel):
        st.watch_hours, st.views_total, _subs = read_totals(page.locator(TOTAL_VALUES).all_inner_texts())
    else:
        log(f"Аналитика {channel.handle}: таблица «за всё время» не загрузилась — часы просмотра не обновлены")
    if st.subscribers is None and st.views_48h is None and st.watch_hours is None:
        raise AnalyticsError("цифры на страницах аналитики не нашлись (Studio поменяла вёрстку?)")
    pause(cancel)
    try:  # запланированные ролики — отдельным чтением; не вышло — столбец остаётся прежним, остальное не страдает
        st.scheduled = youtube.count_scheduled(page, channel.id)
    except Exception as exc:
        log(f"Аналитика {channel.handle}: запланированные ролики не прочитались ({str(exc)[:100]})")
    return st


def collect(cdp_url: str, channels: list, *, cancel: CancelToken | None = None, on_channel=None,
            pulse=None) -> list[Stats]:
    """Все каналы одного профиля: переключиться (если нужно) → прочитать. Ошибка канала — в его Stats.error."""
    from engine.publish import youtube
    from engine.publish.studio_kit import set_channel

    out: list[Stats] = []
    with youtube.studio_page(cdp_url) as page:
        for ch in channels:
            if cancel:
                cancel.check()
            set_channel(ch.handle or ch.id)
            if pulse is not None:
                pulse.touch()
            try:
                youtube.switch_to(page, ch, cancel=cancel)
                st = read_channel(page, ch, cancel)
            except (youtube.StudioError, youtube.NeedsUser, AnalyticsError) as exc:
                st = Stats(channel_id=ch.id, name=ch.name, handle=ch.handle, error=str(exc)[:300])
            log(f"Аналитика {ch.name} ({ch.handle}): подписчиков {st.subscribers}, часов {st.watch_hours}, "
                f"за 48 ч {st.views_48h}, запланировано {st.scheduled}" + (f" — {st.error}" if st.error else ""))
            out.append(st)
            if on_channel:
                on_channel(st)
    return out


# ---------- хранение ----------


def load() -> dict[str, Stats]:
    raw = read_json(FILE, {}).get("channels") or {}
    out: dict[str, Stats] = {}
    for cid, d in raw.items():
        if isinstance(d, dict):
            known = {k: v for k, v in d.items() if k in Stats.__dataclass_fields__}
            out[cid] = Stats(**{**known, "channel_id": cid})
    return out


def save_one(st: Stats) -> Stats:
    """Записать свежие цифры канала (и снимок в историю). Ошибка — прежние цифры остаются, видна ошибка."""
    with LOCK:
        data = read_json(FILE, {})
        chans = data.setdefault("channels", {})
        old = chans.get(st.channel_id) or {}
        cur = dict(old)
        cur.update({k: v for k, v in asdict(st).items() if k not in ("history", "channel_id")})
        if st.error:  # не затираем последние известные цифры
            for k in ("subscribers", "views_48h", "watch_hours", "views_total", "scheduled"):
                if getattr(st, k) is None:
                    cur[k] = old.get(k)
            cur["synced_at"] = old.get("synced_at", 0.0)
            cur["error_at"] = time.time()
        else:
            if st.scheduled is None:  # не прочиталось в этот раз — оставляем прежнее
                cur["scheduled"] = old.get("scheduled")
            cur["synced_at"] = st.synced_at or time.time()
            hist = list(old.get("history") or [])
            hist.append({"t": cur["synced_at"], "subscribers": st.subscribers, "views_48h": st.views_48h,
                         "watch_hours": st.watch_hours, "views_total": st.views_total})
            cur["history"] = hist[-HISTORY_MAX:]
        chans[st.channel_id] = cur
        write_json(FILE, data)
    return st


def _refresh_profiles(api_key: str, url: str) -> str:
    """Список профилей из AdsPower (новые, переименованные, удалённые) — как «Синхронизировать с AdsPower».
    → пометка о сбое (пусто — всё хорошо)."""
    from engine.publish import registry
    from engine.publish.adspower import AdsPower, AdsPowerError

    try:
        with AdsPower(api_key, url) as ap:
            registry.sync(ap.profiles())
        return ""
    except AdsPowerError as exc:
        return f"список профилей из AdsPower не обновился ({exc}) — беру известные"


def sync(api_key: str, url: str, *, profile_ids: list[str] | None = None, cancel: CancelToken | None = None,
         progress=None, discover: bool = True, channel_ids: list[str] | None = None) -> dict:
    """Синхронизация аналитики: по очереди профили AdsPower, браузеры — скрыто. discover — заодно найти новое:
    сначала свежий список профилей из AdsPower, потом в каждом профиле (за тот же заход, профиль открывается
    один раз) перечитать каналы — новые аккаунты и каналы появятся в списке и сразу получат цифры. → итог."""
    from engine.publish import registry, youtube
    from engine.publish.adspower import AdsPowerError
    from engine.publish.scan import SCAN_TIMEOUT, in_profile

    notes: list[str] = []
    only = set(channel_ids or [])
    # только эти каналы (группа «Аналитики»): профили без них не открываем, но в открытых каналы перечитываем —
    # новый аккаунт или канал в таком профиле сразу появится во всех разделах (выкладка, комментарии, опросы)
    rescan = discover or bool(only)
    if only:
        discover = False
    if rescan and not profile_ids:
        if progress:
            progress(0.0, "Аналитика: обновляю список профилей из AdsPower")
        note = _refresh_profiles(api_key, url)
        if note:
            notes.append(note)
    profiles = [p for p in registry.load().values() if not p.missing and (p.channels or discover)
                and (not profile_ids or p.id in profile_ids)]
    if only:
        profiles = [p for p in profiles if any(c.id in only for c in p.channels)]
        if not profiles:
            raise AnalyticsError("каналов группы нет в профилях AdsPower — нажми «Синхронизировать всё»")
    if not profiles:
        raise AnalyticsError("нет каналов: в «Выкладке» нажми «Синхронизировать с AdsPower» и «Обновить каналы»")
    ok: list[str] = []
    failed: list[str] = []
    added: list[str] = []
    total = 0

    for n, p in enumerate(profiles):
        if cancel:
            cancel.check()
        share = 1 / len(profiles)
        chans = [c for c in p.channels if not only or c.id in only]
        state = {"channels": chans, "done": 0, "scanned": not rescan}

        def tick(msg: str, frac: float = 0.0, n=n, share=share) -> None:
            if progress:
                progress(min(1.0, n * share + share * frac), msg)

        def keep(st: Stats, p=p, state=state, tick=tick) -> None:
            nonlocal total
            st.profile_id, st.profile = p.id, p.label()
            st.synced_at = time.time()
            save_one(st)
            (failed if st.error else ok).append(f"{st.name}: {st.error}" if st.error else st.name)
            state["done"] += 1
            total += 1
            count = max(1, len(state["channels"]))
            tick(f"Аналитика: {p.label()} — {state['done']} из {count}, {st.name}", 0.2 + 0.8 * state["done"] / count)

        def work(cdp, pulse, p=p, state=state, tick=tick, keep=keep) -> None:
            if not state["scanned"]:
                tick(f"Аналитика: {p.label()} — ищу новые каналы", 0.02)
                env: dict = {}
                try:
                    found = youtube.scan_channels(cdp, cancel=cancel, pulse=pulse, env=env)
                except Exception as exc:  # noqa: BLE001 — каналы не перечитались: цифры по известным всё равно
                    if youtube.browser_lost(exc) or (cancel is not None and cancel.is_cancelled()):
                        raise
                    found = None
                    notes.append(f"{p.label()}: каналы не перечитались ({str(exc).splitlines()[0][:150]}) — "
                                 "цифры по известным")
                if found:
                    before = {c.id for c in p.channels}
                    entry = registry.set_channels(p.id, found, studio=env or None)
                    state["channels"] = [c for c in entry.channels if not only or c.id in only]
                    added.extend(f"{c.name} ({c.handle}) — {p.label()}" for c in entry.channels if c.id not in before)
                elif found is not None and p.channels:
                    # пусто, а раньше каналы были — скорее сбой чтения, чем удаление: список не трогаем
                    notes.append(f"{p.label()}: каналов не нашлось — оставил прежний список, проверь «Обновить каналы»")
                state["scanned"] = True
                tick(f"Аналитика: {p.label()} — каналов {len(state['channels'])}", 0.2)
            # повтор после закрывшегося браузера — только каналы, которые ещё не прочитаны
            collect(cdp, state["channels"][state["done"]:], cancel=cancel,
                    on_channel=lambda st: (keep(st), pulse.touch()), pulse=pulse)

        tick(f"Аналитика: открываю профиль {p.label()}")
        timeout = CHANNEL_TIMEOUT * max(1, len(state["channels"])) + 120 + (SCAN_TIMEOUT if rescan else 0)
        try:
            in_profile(api_key, url, p.id, work, headless=True, timeout=timeout, cancel=cancel, what="аналитика",
                       again="запусти синхронизацию ещё раз")
        except (AdsPowerError, AnalyticsError) as exc:
            for ch in state["channels"][state["done"]:]:
                keep(Stats(channel_id=ch.id, name=ch.name, handle=ch.handle, error=f"профиль {p.label()}: {exc}"))
            if not state["channels"]:
                notes.append(f"{p.label()}: профиль не открылся ({exc})")
        except Exception as exc:
            if not youtube.browser_lost(exc):
                raise
            for ch in state["channels"][state["done"]:]:
                keep(Stats(channel_id=ch.id, name=ch.name, handle=ch.handle,
                           error=f"браузер профиля {p.label()} закрылся — профиль освобождён"))
    if progress:
        progress(1.0, "Аналитика готова")
    return {"ok": ok, "failed": failed, "total": total, "added": added, "notes": notes}
