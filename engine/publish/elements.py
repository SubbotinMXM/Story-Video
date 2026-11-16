"""Конечная заставка и подсказки ролика (End screen, Cards) — в каждом выложенном ролике.

- Конечная заставка: шаблон «1 видео, 1 плейлист, 1 подписка» — рекомендованное видео канала («Best for
  viewer»), плейлист канала (тот единственный, куда идёт ролик) и кнопка подписки на канал.
- Подсказка: плейлист канала в самом начале ролика (всплывает, кому интересно — переходит).

Делается после сохранения ролика, на странице видео в Studio (/video/<id>/edit, справа «End screen» и
«Cards»): к этому времени видео обработано и редакторы доступны. Уже есть элементы — ничего не добавляем
(повтор после сбоя не создаёт дублей). Не вышло — StudioError: выкладка повторит сама (ролик уже сохранён,
продолжение пойдёт с этого шага).
"""

from __future__ import annotations

import time

from engine.core.cancel import CancelToken
from engine.core.logutil import log
from engine.publish import human

ENDSCREEN_LINK = "#endscreen-editor-link"
CARDS_LINK = "#info-cards-editor-link"
# на странице несколько скрытых окон редактора — работаем только с открытым (видимым)
MODAL = "ytve-modal-host #video-editor-modal-host-title:visible"  # заголовок открытого окна
SAVE = "ytve-modal-host ytcp-button#save-button:visible"
DISCARD = "ytve-modal-host ytcp-button#discard-button:visible"
TEMPLATES = "ytve-modal-host #cards-row > div"
TEMPLATE_TEXT = "1 video, 1 playlist, 1 subscribe"  # английский интерфейс Studio
TEMPLATE_INDEX = 5  # тот же шаблон по порядку в списке (на случай другого языка интерфейса)
ES_ROWS = "ytve-endscreen-row"
ES_ERROR_ITEM = "ytve-endscreen-editor-preview-overlay-item.error"
PLAYLIST_SELECT = "ytve-modal-host #playlist-select"
PLAYLIST_CARDS = "ytcp-playlist-pick-dialog ytcp-entity-card"
CARD_TYPES = "ytve-modal-host .info-card-type-option-container"  # Video, Playlist, Channel, Link
CARD_TYPES_HEADER = "ytve-modal-host #info-cards-editor-default-options-header"
WAIT_READY_SEC = 10 * 60  # видео обрабатывается — редакторы ждут столько


class ElementsError(RuntimeError):
    pass


def add_video_elements(page, video_url: str, playlists: list[str], *, cancel: CancelToken | None = None,
                       act=None) -> list[str]:
    """Заставка и подсказка-плейлист у ролика. → пометки для итогов. Ошибка — StudioError (повтор позже)."""
    from engine.publish import youtube

    act = act or youtube._act
    vid = youtube._video_id(video_url)
    if not vid:
        raise youtube.StudioError("нет ссылки на ролик — заставку и подсказку добавить некуда")
    notes: list[str] = []
    page.goto(f"{youtube.STUDIO}/video/{vid}/edit", wait_until="domcontentloaded")
    _wait_ready(page, cancel)
    notes.append(_end_screen(page, playlists, cancel, act))
    notes.append(_card(page, playlists, cancel, act))
    return [n for n in notes if n]


def _wait_ready(page, cancel) -> None:
    from engine.publish import youtube

    end = time.monotonic() + WAIT_READY_SEC
    while time.monotonic() < end:
        if cancel:
            cancel.check()
        link = page.locator(ENDSCREEN_LINK).first
        if link.count() and link.is_visible() and link.get_attribute("aria-disabled") != "true" \
                and link.get_attribute("disabled") is None:
            return
        page.wait_for_timeout(15000)
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
    raise youtube.StudioError("заставку и подсказку пока не добавить — видео ещё обрабатывается; повторю позже")


def _open(page, link: str, act) -> None:
    from engine.publish import youtube

    act(None)
    human.click(page, page.locator(link).first)
    try:
        page.locator(MODAL).first.wait_for(state="visible", timeout=45000)
    except Exception:
        raise youtube.StudioError("редактор заставки/подсказок не открылся (ролик ещё черновик или обрабатывается) — "
                                  "повторю позже") from None
    page.wait_for_timeout(4000)


def _close_unchanged(page, act) -> None:
    btn = page.locator(DISCARD).first
    if btn.count() and btn.is_visible():
        act(None)
        human.click(page, btn)
        page.wait_for_timeout(2500)
        confirm = page.locator("ytcp-confirmation-dialog #confirm-button:visible").first
        if confirm.count():
            human.click(page, confirm)
            page.wait_for_timeout(2000)


def _save(page, act, what: str) -> None:
    from engine.publish import youtube

    save = page.locator(SAVE).first
    if save.get_attribute("aria-disabled") == "true":
        msg = " ".join(page.locator("ytve-modal-host #plugin-message").first.inner_text().split())
        _close_unchanged(page, act)
        raise youtube.StudioError(f"{what}: Studio не даёт сохранить ({msg or 'кнопка Save недоступна'})")
    act(None)
    human.click(page, save)
    for _ in range(30):
        page.wait_for_timeout(1000)
        if not page.locator(MODAL).first.is_visible():
            return
    raise youtube.StudioError(f"{what}: редактор не закрылся после «Save»")


def _pick_playlist(page, playlists: list[str], act) -> str:
    """Окно выбора плейлиста: единственный плейлист канала (или тот, что совпадает по названию)."""
    from engine.publish import youtube

    cards = page.locator(PLAYLIST_CARDS)
    for _ in range(15):
        if cards.count():
            break
        page.wait_for_timeout(1000)
    n = cards.count()
    if not n:
        raise youtube.StudioError("в окне выбора нет ни одного плейлиста канала — создай плейлист")
    texts = [" ".join(t.split()) for t in cards.all_inner_texts()]
    want = playlists[0].strip().lower() if len(playlists) == 1 else ""
    k = next((i for i, t in enumerate(texts) if want and t.lower().startswith(want)), 0)
    act(None)
    human.click(page, cards.nth(k))
    page.wait_for_timeout(3000)
    note = "" if n == 1 else f"плейлистов на канале {n} — в заставку и подсказку взял «{texts[k].split(' ')[0]}»"
    return note


def _end_screen(page, playlists: list[str], cancel, act) -> str:
    from engine.publish import youtube

    _open(page, ENDSCREEN_LINK, act)
    if page.locator(ES_ROWS).count():
        log("Studio: конечная заставка уже есть — не трогаю")
        _close_unchanged(page, act)
        return ""
    tpl = page.locator(TEMPLATES).filter(has_text=TEMPLATE_TEXT).first
    if not tpl.count():
        tpl = page.locator(TEMPLATES).nth(TEMPLATE_INDEX)
    act(cancel)
    human.click(page, tpl)
    page.wait_for_timeout(5000)
    if page.locator(ES_ROWS).count() != 3:
        _close_unchanged(page, act)
        raise youtube.StudioError("конечная заставка: шаблон «видео + плейлист + подписка» не применился")
    note = ""
    bad = page.locator(ES_ERROR_ITEM).first
    if bad.count():  # элемент плейлиста ждёт выбора
        act(cancel)
        human.click(page, bad)
        page.wait_for_timeout(2500)
        act(cancel)
        human.click(page, page.locator(PLAYLIST_SELECT).first)
        page.wait_for_timeout(2500)
        note = _pick_playlist(page, playlists, act)
    if page.locator(f"{ES_ROWS}.error").count():
        _close_unchanged(page, act)
        raise youtube.StudioError("конечная заставка: элемент плейлиста не заполнился")
    _save(page, act, "конечная заставка")
    log("Studio: конечная заставка добавлена (видео + плейлист + подписка)")
    return note


def _card(page, playlists: list[str], cancel, act) -> str:
    from engine.publish import youtube

    _open(page, CARDS_LINK, act)
    header = page.locator(CARD_TYPES_HEADER).first
    if not (header.count() and header.is_visible()):
        log("Studio: подсказки уже есть — не трогаю")
        _close_unchanged(page, act)
        return ""
    types = page.locator(CARD_TYPES)
    if types.count() < 2:
        _close_unchanged(page, act)
        raise youtube.StudioError("подсказки: нет выбора типа подсказки")
    act(cancel)
    human.click(page, types.nth(1))  # Video, Playlist, Channel, Link → «Playlist»
    page.wait_for_timeout(3500)
    note = ""
    if page.locator(PLAYLIST_CARDS).count() or page.locator("ytcp-playlist-pick-dialog").count():
        note = _pick_playlist(page, playlists, act)
    _save(page, act, "подсказка-плейлист")
    log("Studio: подсказка-плейлист добавлена в начало ролика")
    return note
