"""Действия в браузере «как руками»: мышь, колесо, клавиатура, кнопка выбора файла.

Правило выкладки: всё, что меняет страницу YouTube, делается так же, как это сделал бы человек, —
настоящими событиями мыши и клавиатуры, с паузами. JS программа использует только чтобы ПРОЧИТАТЬ
страницу (ID канала, даты, список роликов) — ничего на странице через JS не нажимается и не меняется.

- клик: доводим кнопку до экрана колесом мыши, ведём мышь плавно (по шагам) в случайную точку кнопки,
  нажимаем и отпускаем с небольшой задержкой;
- набор текста: по буквам, с неровным темпом и паузами между словами; длинное описание — вставкой
  (как человек вставляет заранее подготовленный текст из буфера);
- файл: нажимаем кнопку «Выбрать файл» на странице — открывается окно выбора файла, в него подставляется
  файл (вместо того, чтобы человек выбирал его в окне Finder).
"""

from __future__ import annotations

import random
import time
from pathlib import Path

from engine.core.cancel import CancelToken
from engine.core.logutil import log

CHOOSER_WAIT_SEC = 8  # сколько ждать окно выбора файла после нажатия кнопки
PASTE_FROM = 400  # текст длиннее — вставляем (как из буфера), короче — печатаем по буквам


def _sleep(page, lo: float, hi: float) -> None:
    page.wait_for_timeout(int(random.uniform(lo, hi) * 1000))


def _viewport(page) -> tuple[float, float]:
    vs = page.viewport_size
    if vs:
        return float(vs["width"]), float(vs["height"])
    w, h = page.evaluate("() => [window.innerWidth, window.innerHeight]")  # только чтение
    return float(w), float(h)


_SCROLL_AREA = """e => { let p = e.parentElement || (e.getRootNode && e.getRootNode().host);
    while (p && p !== document.body && p !== document.documentElement) {
        const st = getComputedStyle(p);
        if (/(auto|scroll)/.test(st.overflowY) && p.scrollHeight > p.clientHeight + 4) {
            const r = p.getBoundingClientRect(); return {x: r.x, y: r.y, width: r.width, height: r.height}; }
        p = p.parentElement || (p.getRootNode && p.getRootNode().host);
    } return null; }"""

# под точкой нажатия — сам элемент (или его часть), а не перекрывающая его панель/подложка. Только чтение
_HIT = """(e, pt) => { let t = document.elementFromPoint(pt[0], pt[1]);
    while (t && t.shadowRoot) { const u = t.shadowRoot.elementFromPoint(pt[0], pt[1]); if (!u || u === t) break; t = u; }
    for (let n = t; n; n = n.parentElement || (n.getRootNode && n.getRootNode().host)) if (n === e) return true;
    return false; }"""


def reach(page, locator) -> dict | None:
    """Довести элемент до видимой части колесом мыши — как человек: мышь над областью, которая прокручивается
    (окно Studio прокручивается внутри себя, а не страница), колесо до тех пор, пока элемент не окажется
    посередине. → рамка элемента или None."""
    try:
        box = locator.bounding_box(timeout=5000)
    except Exception:
        return None
    if box is None:
        return None
    w, h = _viewport(page)
    try:
        area = locator.evaluate(_SCROLL_AREA)  # только чтение: где у элемента прокрутка
    except Exception:
        area = None
    top_lim, bottom_lim = 60, h - 40
    if area:
        top_lim, bottom_lim = max(top_lim, area["y"] + 40), min(bottom_lim, area["y"] + area["height"] - 40)
    for _ in range(25):
        top, bottom = box["y"], box["y"] + box["height"]
        if top >= top_lim and bottom <= bottom_lim:
            return box
        if area:  # мышь — в середину прокручиваемой области (над ней колесо её и крутит)
            x = area["x"] + area["width"] * random.uniform(0.35, 0.65)
            y = area["y"] + area["height"] * random.uniform(0.35, 0.65)
        else:
            x = min(max(box["x"] + box["width"] / 2, 10), w - 10)
            y = min(max(box["y"] + box["height"] / 2, 70), h - 50)
        page.mouse.move(min(max(x, 5), w - 5), min(max(y, 5), h - 5), steps=random.randint(4, 9))
        mid = (top_lim + bottom_lim) / 2
        delta = (top + bottom) / 2 - mid
        page.mouse.wheel(0, max(-700, min(700, delta + random.uniform(-30, 30))))
        _sleep(page, 0.25, 0.6)
        try:
            new = locator.bounding_box(timeout=3000)
        except Exception:
            return None
        if new is None:
            return None
        if abs(new["y"] - box["y"]) < 2:  # колесо не двигает — дальше решит click()
            return new
        box = new
    return box


def center(page, locator) -> dict | None:
    """Колесом поставить элемент в середину его прокручиваемой области (подальше от шапки и нижней панели)."""
    try:
        area = locator.evaluate(_SCROLL_AREA)
        box = locator.bounding_box(timeout=5000)
    except Exception:
        return None
    if not box:
        return None
    w, h = _viewport(page)
    a = area or {"x": 0, "y": 0, "width": w, "height": h}
    for _ in range(12):
        mid = a["y"] + a["height"] / 2
        delta = box["y"] + box["height"] / 2 - mid
        if abs(delta) < 40:
            return box
        page.mouse.move(min(max(a["x"] + a["width"] * random.uniform(0.35, 0.65), 5), w - 5),
                        min(max(a["y"] + a["height"] * random.uniform(0.35, 0.65), 5), h - 5),
                        steps=random.randint(4, 9))
        page.mouse.wheel(0, max(-700, min(700, delta)))
        _sleep(page, 0.25, 0.6)
        try:
            new = locator.bounding_box(timeout=3000)
        except Exception:
            return box
        if not new or abs(new["y"] - box["y"]) < 2:
            return new or box
        box = new
    return box


def _hits(locator, x: float, y: float) -> bool:
    try:
        return bool(locator.evaluate(_HIT, [x, y]))
    except Exception:
        return False


def click(page, locator, *, timeout: int = 15000) -> None:
    """Клик как человек: элемент на экране (колесом), мышь плавно к случайной точке внутри, нажать-отпустить.
    Перед нажатием — проверка, что под мышью именно этот элемент (не перекрывающая его панель окна):
    перекрыт — докручиваем его как браузер; всё равно перекрыт — обычный клик Playwright (тоже настоящая мышь,
    со своей проверкой). Элемент без рамки — обычный клик Playwright."""
    locator.wait_for(state="visible", timeout=timeout)
    box = reach(page, locator)
    point = _free_point(locator, box)
    if point is None:
        # перекрыт (закреплённая шапка окна, нижняя панель) — колесом в середину прокручиваемой области
        box = center(page, locator)
        point = _free_point(locator, box)
    if point is None:
        try:  # докрутить как браузер (к краю) и отступить колесом из-под закреплённой шапки
            locator.scroll_into_view_if_needed(timeout=timeout)
            _sleep(page, 0.2, 0.5)
            page.mouse.wheel(0, -220)
            _sleep(page, 0.3, 0.6)
            box = locator.bounding_box(timeout=timeout)
        except Exception:
            box = None
        point = _free_point(locator, box)
    if point is None:
        locator.click(timeout=timeout)
        return
    page.mouse.move(point[0], point[1], steps=random.randint(8, 22))
    _sleep(page, 0.08, 0.3)
    page.mouse.down()
    _sleep(page, 0.05, 0.14)
    page.mouse.up()


def _free_point(locator, box) -> tuple[float, float] | None:
    """Случайная точка внутри элемента, где под мышью он сам (не перекрыт). Нет такой — None."""
    if not box or box["width"] < 2 or box["height"] < 2:
        return None
    for _ in range(6):
        x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
        if _hits(locator, x, y):
            return x, y
    return None


def type_text(page, text: str, cancel: CancelToken | None = None) -> None:
    """Набрать текст в поле, где стоит курсор. Короткий — по буквам с живым темпом; длинный — вставкой.
    Перевод строки — Shift+Enter (в полях Studio Enter отправляет/закрывает)."""
    lines = text.split("\n")
    paste = len(text) > PASTE_FROM
    for k, line in enumerate(lines):
        if cancel:
            cancel.check()
        if line:
            if paste:
                page.keyboard.insert_text(line)
                _sleep(page, 0.05, 0.2)
            else:
                for word_i, word in enumerate(line.split(" ")):
                    if word_i:
                        page.keyboard.type(" ", delay=random.randint(60, 160))
                    if word:
                        page.keyboard.type(word, delay=random.randint(45, 150))
                    if random.random() < 0.12:
                        _sleep(page, 0.3, 0.9)  # задумался
        if k < len(lines) - 1:
            page.keyboard.press("Shift+Enter")
            _sleep(page, 0.08, 0.3)


def select_all(page) -> None:
    """«Выделить всё» (⌘A) — настоящее нажатие клавиш с явной командой выделения. Обычный press("Meta+A")
    Playwright сопровождает этой командой, только если браузер «маковский» по User-Agent; у профиля AdsPower
    с отпечатком Windows на настоящем маковском Chrome ⌘A тогда не выделяет ничего (13.11: новое название
    вписалось в середину старого)."""
    cdp = page.context.new_cdp_session(page)
    try:
        base = {"key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65, "modifiers": 4}
        cdp.send("Input.dispatchKeyEvent", {**base, "type": "rawKeyDown", "commands": ["selectAll"]})
        cdp.send("Input.dispatchKeyEvent", {**base, "type": "keyUp"})
    finally:
        cdp.detach()


def field_text(locator) -> str:
    """Что сейчас в поле (только чтение): у input — значение, у редактируемого блока Studio — текст."""
    try:
        return str(locator.evaluate("e => (e.value !== undefined ? e.value : e.innerText) || ''", timeout=5000))
    except Exception:
        return ""


def _norm(text: str) -> str:
    return " ".join(str(text or "").split())


def clear_field(page, locator=None) -> None:
    """Очистить поле с курсором: выделить всё и стереть (клавиатурой). locator — проверить, что пусто;
    не пусто — запасной способ: курсор в конец и Backspace по буквам."""
    select_all(page)
    _sleep(page, 0.1, 0.3)
    page.keyboard.press("Backspace")
    _sleep(page, 0.1, 0.3)
    if locator is None:
        return
    left = _norm(field_text(locator))
    if not left:
        return
    log(f"Поле не очистилось выделением (осталось {len(left)} симв.) — стираю по буквам")
    # курсор может стоять в любой строке многострочного описания: стираем и назад, и вперёд
    n = len(field_text(locator)) + 3
    page.keyboard.press("End")
    for _ in range(n):
        page.keyboard.press("Backspace", delay=random.randint(10, 35))
    for _ in range(len(field_text(locator)) + 3 if _norm(field_text(locator)) else 0):
        page.keyboard.press("Delete", delay=random.randint(10, 35))
    _sleep(page, 0.1, 0.3)
    if _norm(field_text(locator)):
        raise RuntimeError("поле не очищается — в нём остаётся старый текст")


def press(page, key: str) -> None:
    _sleep(page, 0.15, 0.5)
    page.keyboard.press(key)


def choose_file(page, buttons: list[str], path: Path, *, fallback_input=None, what: str = "файл") -> str:
    """Выбрать файл, как человек: нажать кнопку выбора файла на странице — браузер открывает окно выбора,
    программа подставляет в него файл (окно Finder не показывается). Путь отдаётся браузеру напрямую
    (DOM.setFileInputFiles): так работает с файлами любого размера.
    Кнопку не нашли / окно не открылось — запасной способ: файл прямо в поле выбора (fallback_input).
    → как выбрали: «кнопкой» или «запасным способом»."""
    path_s = str(Path(path).resolve())
    cdp = page.context.new_cdp_session(page)
    got: dict = {}
    try:
        cdp.send("Page.enable")
        cdp.send("Page.setInterceptFileChooserDialog", {"enabled": True})
        cdp.on("Page.fileChooserOpened", lambda ev: got.update(ev))
        for sel in buttons:
            loc = page.locator(sel).first
            try:
                if not loc.count() or not loc.is_visible():
                    continue
                click(page, loc)
            except Exception:
                continue
            end = time.monotonic() + CHOOSER_WAIT_SEC
            while not got and time.monotonic() < end:
                page.wait_for_timeout(200)
            if got:
                break
        if got.get("backendNodeId"):
            _sleep(page, 0.8, 2.0)  # «нашёл файл в окне»
            cdp.send("DOM.setFileInputFiles", {"files": [path_s], "backendNodeId": got["backendNodeId"]})
            return "кнопкой"
    finally:
        try:
            cdp.send("Page.setInterceptFileChooserDialog", {"enabled": False})
        except Exception:
            pass
        try:
            cdp.detach()
        except Exception:
            pass
    if fallback_input is None:
        from engine.publish.youtube import StudioError

        raise StudioError(f"не открылось окно выбора файла ({what}) — кнопка не нашлась")
    log(f"Studio: {what} — кнопка выбора файла не сработала, подаю файл запасным способом")
    from engine.publish.youtube import set_file

    set_file(page, fallback_input, path_s)
    return "запасным способом"
