"""Вёрстки YouTube Studio и разбор ошибок.

У YouTube одновременно живут несколько версий вёрстки Studio — на разных каналах элементы могут
называться чуть по-разному. Для каждого элемента — список вариантов (SELECTORS): первый — основной,
дальше — запасные. Какой вариант сработал на канале, запоминается (data/publish/layouts.json):
в следующий раз он пробуется первым. Новая вёрстка, которой нет в списке, — шаг падает со снимком
страницы: по нему вариант добавляется в SELECTORS.

Каждый шаг выкладки пишется в журнал задачи («Studio @канал: шаг…»). Ошибка шага → папка
data/publish/errors/<время>_<канал>_<шаг>/ со снимком экрана (screenshot.png), страницей (page.html)
и описанием (info.txt: шаг, адрес, язык, какие варианты пробовали, ошибка). Путь — в тексте ошибки
и в журнале: папку можно открыть самому или прислать в чат для починки.
"""

from __future__ import annotations

import re
import threading
import traceback
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from engine.core.cancel import CancelledError
from engine.core.logutil import log
from engine.publish.store import LOCK, publish_dir, read_json, write_json

LAYOUTS_FILE = "layouts.json"

# элемент → варианты (основной первым). Метки — внутренние имена Studio, от языка интерфейса не зависят
SELECTORS: dict[str, list[str]] = {
    "file_input": ["ytcp-uploads-dialog input[type=file]", "input[type=file][name=Filedata]"],
    # поля окна загрузки — только внутри окна: при продолжении черновика под окном открыта страница видео
    # с такими же полями (название, описание, плейлист…) — без этого программа жмёт в поле под окном
    "title": ["ytcp-uploads-dialog #title-textarea #textbox", "ytcp-uploads-dialog ytcp-social-suggestions-textbox#title-textarea [contenteditable=true]"],
    "description": ["ytcp-uploads-dialog #description-textarea #textbox",
                    "ytcp-uploads-dialog ytcp-social-suggestions-textbox#description-textarea [contenteditable=true]"],
    "thumbnail_input": ["ytcp-uploads-dialog #file-loader", "ytcp-thumbnail-uploader input[type=file]",
                        "#file-loader"],
    "playlist_trigger": ["ytcp-uploads-dialog ytcp-video-metadata-playlists ytcp-text-dropdown-trigger",
                         "ytcp-uploads-dialog ytcp-video-metadata-playlists ytcp-dropdown-trigger"],
    "playlist_list": ["ytcp-playlist-dialog #playlists-list", "ytcp-playlist-dialog ytcp-checkbox-group"],
    "not_for_kids": ["ytcp-uploads-dialog tp-yt-paper-radio-button[name=VIDEO_MADE_FOR_KIDS_NOT_MFK]", "ytcp-uploads-dialog [name=VIDEO_MADE_FOR_KIDS_NOT_MFK]"],
    "show_more": ["ytcp-uploads-dialog #toggle-button"],
    "altered_no": ["ytcp-uploads-dialog tp-yt-paper-radio-button[name=VIDEO_HAS_ALTERED_CONTENT_NO]", "ytcp-uploads-dialog [name=VIDEO_HAS_ALTERED_CONTENT_NO]"],
    "next": ["ytcp-uploads-dialog #next-button"],
    "private": ["ytcp-uploads-dialog tp-yt-paper-radio-button[name=PRIVATE]", "ytcp-uploads-dialog #private-radio-button"],
    "schedule_expand": ["ytcp-uploads-dialog #second-container-expand-button", "ytcp-uploads-dialog #second-container"],
    "date_trigger": ["ytcp-uploads-dialog #datepicker-trigger", "ytcp-uploads-dialog ytcp-datetime-picker ytcp-text-dropdown-trigger"],
    "date_input": ["ytcp-date-picker input", "ytcp-date-picker tp-yt-paper-input input"],
    "time_input": ["ytcp-uploads-dialog #time-of-day-container input", "ytcp-uploads-dialog ytcp-datetime-picker tp-yt-paper-input input"],
    "done": ["ytcp-uploads-dialog #done-button"],
    "create_button": ["ytcp-button.ytcpAppHeaderCreateIcon", "ytcp-button[icon='yt-sys-icons:video_call']"],
    "create_upload": ["tp-yt-paper-item[test-id=upload]", "#text-item-0"],
    "collab_button": ["#collaboration-button", "ytcp-button#collaboration-button"],
    "collab_search": ["#collaborators-dialog #search-input:visible", "#collaborators-dialog input:visible"],
    "collab_create_link": ["ytcp-video-collaborator-manage-dialog #create-link-button:visible",
                           "#create-link-button:visible"],
    "collab_link_close": ["ytcp-video-collaborator-invite-link-dialog #close-button:visible"],
    "collab_save": ["#collaborators-dialog #save-button:visible"],
    "video_save": ["ytcp-button#save"],
    "accept_confirm": ["ytcp-collaboration-acceptance-dialog #confirm-button"],
    # страница ролика на youtube.com: комментарий от имени канала и закрепление
    "comment_box": ["#simplebox-placeholder", "ytd-comment-simplebox-renderer #placeholder-area"],
    "comment_editor": ["#contenteditable-root", "ytd-commentbox [contenteditable=true]"],
    "comment_submit": ["ytd-commentbox #submit-button", "#submit-button"],
    "comment_threads": ["ytd-comment-thread-renderer", "ytd-comment-view-model"],
    "pin_confirm": ["yt-confirm-dialog-renderer #confirm-button:visible"],
    # вкладка «Posts» канала на youtube.com: текстовый опрос (метки aria — английский и русский интерфейс)
    "post_box": ["ytd-backstage-post-dialog-renderer #commentbox-placeholder:visible", "#commentbox-placeholder:visible"],
    "poll_add": ["ytd-backstage-post-dialog-renderer ytd-button-renderer#poll-button button:visible",
                 "ytd-backstage-post-dialog-renderer button[aria-label='Add a text poll']:visible",
                 "ytd-backstage-post-dialog-renderer button[aria-label*='опрос' i]:visible"],
    "poll_question": ["ytd-backstage-post-dialog-renderer #contenteditable-root[aria-label='Ask your community...']:visible",
                      "ytd-backstage-post-dialog-renderer #contenteditable-root:visible"],
    "poll_add_option": ["ytd-backstage-post-dialog-renderer button[aria-label='Add another option']:visible",
                        "ytd-backstage-post-dialog-renderer button[aria-label*='вариант' i]:visible"],
    "post_menu": ["ytd-backstage-post-dialog-renderer button[aria-label='Action menu']:visible",
                  "ytd-backstage-post-dialog-renderer button[aria-label*='меню' i]:visible"],
    "post_date": ["ytd-backstage-post-dialog-renderer tp-yt-paper-button#date-picker", "tp-yt-paper-button#date-picker"],
    "post_date_input": ["ytd-calendar-date-picker form#date-input input", "ytd-calendar-date-picker input"],
    "post_time": ["ytd-backstage-post-dialog-renderer tp-yt-paper-button#time-picker", "tp-yt-paper-button#time-picker"],
    "post_tz": ["ytd-backstage-post-dialog-renderer tp-yt-paper-button#timezone-picker", "tp-yt-paper-button#timezone-picker"],
}

FAST = 5000  # запасные варианты проверяем быстро: основной уже не нашёлся за полный срок

_local = threading.local()


class LayoutError(RuntimeError):
    """Элемента нет ни в одном известном варианте вёрстки."""


def set_channel(key: str) -> None:
    """Для какого канала работаем (память вариантов вёрстки и подписи в журнале)."""
    _local.channel = key


def set_reporter(fn) -> None:
    """Куда сообщать текущий шаг (живой статус выкладки в очереди): fn(stage: str) или None."""
    _local.reporter = fn


def report(stage: str) -> None:
    fn = getattr(_local, "reporter", None)
    if fn is not None:
        try:
            fn(stage)
        except Exception:
            pass


def current_channel() -> str:
    return getattr(_local, "channel", "") or "?"


def _remembered(key: str) -> int:
    return int((read_json(LAYOUTS_FILE, {}).get(current_channel()) or {}).get(key, 0))


def _remember(key: str, index: int) -> None:
    with LOCK:
        data = read_json(LAYOUTS_FILE, {})
        data.setdefault(current_channel(), {})[key] = index
        write_json(LAYOUTS_FILE, data)


def el(page, key: str, *, timeout: int = 15000, state: str = "visible"):
    """Элемент по ключу из SELECTORS: сначала вариант, который сработал на этом канале раньше, потом остальные.
    Сработал другой вариант — запоминаем и пишем в журнал. Ни один — LayoutError со списком пробованных."""
    variants = SELECTORS[key]
    first = _remembered(key) if _remembered(key) < len(variants) else 0
    order = [first, *[k for k in range(len(variants)) if k != first]]
    tried: list[str] = []
    for n, k in enumerate(order):
        loc = page.locator(variants[k]).first
        try:
            loc.wait_for(state=state, timeout=timeout if n == 0 else FAST)
        except CancelledError:
            raise
        except Exception:
            tried.append(variants[k])
            continue
        if k != first:
            _remember(key, k)
            log(f"Studio {current_channel()}: «{key}» — другая вёрстка, работает вариант {k + 1} ({variants[k]}) — запомнил")
        return loc
    raise LayoutError(f"нет элемента «{key}» ни в одном варианте вёрстки (пробовал: {'; '.join(tried)})")


@contextmanager
def step(page, name: str):
    """Шаг выкладки: в журнал — начало; ошибка — снимок страницы и понятное сообщение с путём к нему.
    Отмену, падение браузера и «нужен человек/лимит» пропускаем как есть (их разбирают выше)."""
    from engine.publish import youtube

    from engine.publish.scan import beat

    beat()
    log(f"Studio {current_channel()}: {name}…")
    report(name)
    try:
        yield
    except (CancelledError, youtube.NeedsUser, youtube.UploadLimit):
        raise
    except Exception as exc:
        if youtube.browser_lost(exc):
            raise
        folder = diagnose(page, name, exc)
        reason = _short(exc)
        log(f"✗ Studio {current_channel()}: шаг «{name}» — {reason}. Снимок и страница: {folder}")
        raise youtube.StudioError(f"шаг «{name}»: {reason}. Снимок и страница: {folder}") from exc


def _short(exc: BaseException) -> str:
    """Суть ошибки одной строкой (без простыни Playwright)."""
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    text = re.sub(r"\s+", " ", text)
    if "Timeout" in type(exc).__name__ or "Timeout" in text:
        m = re.search(r'locator\("([^"]+)"', str(exc))
        text = f"не дождался элемента{f' {m.group(1)}' if m else ''} (таймаут)"
    return text[:300]


def diagnose(page, name: str, exc: BaseException) -> Path:
    """Снимок экрана, страница и описание ошибки → папка в data/publish/errors (путь — в журнал)."""
    safe = re.sub(r"[^\w-]+", "_", f"{current_channel()}_{name}")[:80]
    folder = publish_dir() / "errors" / f"{datetime.now():%Y%m%d-%H%M%S}_{safe}"
    folder.mkdir(parents=True, exist_ok=True)
    info = [f"Шаг: {name}", f"Канал: {current_channel()}", f"Время: {datetime.now():%d.%m.%Y %H:%M:%S}"]
    try:
        info.append(f"Адрес: {page.url}")  # известен без обращения к странице
    except Exception:
        pass
    # всё с ограничением по времени: вкладка могла перестать отвечать — разбор не должен зависнуть вместе с ней
    alive = True
    try:
        page.screenshot(path=str(folder / "screenshot.png"), timeout=15000)
    except Exception as shot_exc:
        alive = False
        info.append(f"(снимок экрана не получился — вкладка не отвечает: {str(shot_exc).splitlines()[0][:150]})")
    if alive:
        try:
            (folder / "page.html").write_text(page.locator("html").first.evaluate("e => e.outerHTML", timeout=10000),
                                              encoding="utf-8")
        except Exception:
            pass
    try:
        if not alive:
            raise RuntimeError("вкладка не отвечает")
        visible = page.eval_on_selector_all(
            "ytcp-dialog, tp-yt-paper-dialog, ytcp-uploads-dialog",
            "els => els.filter(e => e.offsetParent !== null).map(e => e.tagName.toLowerCase() + '#' + e.id + ': '"
            " + (e.innerText || '').replace(/\\s+/g, ' ').slice(0, 300))")
        info.append("Открытые окна Studio:\n  " + ("\n  ".join(v for v in visible if v.strip()) or "—"))
    except Exception:
        pass
    info += ["", f"Ошибка: {type(exc).__name__}: {exc}", "", "".join(traceback.format_exception(exc))[-4000:]]
    (folder / "info.txt").write_text("\n".join(info), encoding="utf-8")
    return folder
