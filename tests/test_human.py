"""Действия «как руками» (engine.publish.human): мышь, колесо, клавиатура, кнопка выбора файла."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import unittest
from pathlib import Path
from unittest import mock

from engine.publish import human


class FakeMouse:
    def __init__(self, page) -> None:
        self.page = page
        self.events: list[tuple] = []

    def move(self, x, y, steps=1):
        self.events.append(("move", round(x), round(y), steps))

    def down(self):
        self.events.append(("down",))

    def up(self):
        self.events.append(("up",))

    def wheel(self, dx, dy):
        self.events.append(("wheel", dy))
        self.page.scroll += dy


class FakeLocator:
    def __init__(self, page, y: float, visible: bool = True) -> None:
        self.page, self.y0, self.visible = page, y, visible
        self.clicked = 0

    def wait_for(self, **k):
        pass

    def bounding_box(self, **k):
        return {"x": 100, "y": self.y0 - self.page.scroll, "width": 120, "height": 40}

    def click(self, **k):
        self.clicked += 1

    def scroll_into_view_if_needed(self, **k):
        self.page.scroll = self.y0 - 300

    covered_below = 10**9  # всё, что ниже этой линии экрана, перекрыто панелью (как нижняя полоса окна Studio)

    def evaluate(self, js, arg=None):
        if arg is None:
            return None  # прокручиваемой области нет — крутим страницу
        return arg[1] < self.covered_below  # под точкой — сам элемент, если не под панелью

    @property
    def first(self):
        return self

    def count(self):
        return 1

    def is_visible(self):
        return self.visible


class FakeKeyboard:
    def __init__(self) -> None:
        self.typed: list[tuple] = []

    def type(self, text, delay=0):
        self.typed.append(("type", text, delay))

    def insert_text(self, text):
        self.typed.append(("insert", text))

    def press(self, key):
        self.typed.append(("press", key))


class FakePage:
    viewport_size = {"width": 1280, "height": 800}

    def __init__(self) -> None:
        self.scroll = 0
        self.mouse = FakeMouse(self)
        self.keyboard = FakeKeyboard()
        self.waited = 0

    def wait_for_timeout(self, ms):
        self.waited += ms


class ClickTest(unittest.TestCase):
    def test_visible_button_real_mouse(self) -> None:
        page = FakePage()
        loc = FakeLocator(page, 300)
        human.click(page, loc)
        kinds = [e[0] for e in page.mouse.events]
        self.assertEqual(kinds[-3:], ["move", "down", "up"], "нажать и отпустить мышью")
        self.assertEqual(loc.clicked, 0, "без программного click()")
        move = page.mouse.events[-3]
        self.assertGreaterEqual(move[3], 8, "мышь идёт плавно, по шагам")
        self.assertTrue(100 <= move[1] <= 220 and 300 <= move[2] <= 340, "точка внутри кнопки")

    def test_offscreen_scrolled_by_wheel(self) -> None:
        page = FakePage()
        loc = FakeLocator(page, 2000)
        human.click(page, loc)
        self.assertTrue(any(e[0] == "wheel" for e in page.mouse.events), "до кнопки — колесом")
        self.assertEqual(page.mouse.events[-1], ("up",))
        self.assertLess(loc.bounding_box()["y"], 800)

    def test_random_point_varies(self) -> None:
        points = set()
        for _ in range(10):
            page = FakePage()
            human.click(page, FakeLocator(page, 300))
            points.add(page.mouse.events[-3][1:3])
        self.assertGreater(len(points), 3, "не в одну и ту же точку")


    def test_covered_by_panel_scrolls_before_click(self) -> None:
        page = FakePage()
        loc = FakeLocator(page, 700)  # видна, но под нижней панелью
        loc.covered_below = 650
        page.mouse.wheel = lambda dx, dy: None  # колесо над панелью ничего не крутит
        human.click(page, loc)
        self.assertLess(loc.bounding_box()["y"], 650, "докрутили, чтобы кнопка вышла из-под панели")
        self.assertEqual(page.mouse.events[-1], ("up",))
        self.assertEqual(loc.clicked, 0)

    def test_under_sticky_header_centered_by_wheel(self) -> None:
        page = FakePage()
        loc = FakeLocator(page, 100)  # на экране, но под закреплённой шапкой окна (выше линии 200)
        orig = loc.evaluate
        loc.evaluate = lambda js, arg=None: orig(js, arg) if arg is None else arg[1] > 200
        human.click(page, loc)
        self.assertTrue(any(e[0] == "wheel" for e in page.mouse.events), "колесом в середину")
        self.assertEqual(page.mouse.events[-1], ("up",))
        self.assertEqual(loc.clicked, 0)

    def test_always_covered_falls_back_to_playwright_click(self) -> None:
        page = FakePage()
        loc = FakeLocator(page, 300)
        loc.covered_below = -1
        human.click(page, loc)
        self.assertEqual(loc.clicked, 1, "Playwright сам проверит, куда жмёт")


class CoverTest(unittest.TestCase):
    """Кнопку перекрыла всплывашка Studio (календарь остался открытым): закрываем Escape и жмём снова."""

    def test_popup_over_button_is_closed_with_escape_then_clicked(self) -> None:
        page = FakePage()
        loc = FakeLocator(page, 300)
        state = {"open": True}
        orig = loc.evaluate

        def evaluate(js, arg=None):
            if arg is None and "elementFromPoint" in str(js):
                return "ytcp-date-picker" if state["open"] else ""
            if arg is not None:
                return not state["open"]  # пока панель открыта, под мышью не кнопка
            return orig(js, arg)

        loc.evaluate = evaluate
        page.keyboard.press = lambda key: (page.keyboard.typed.append(("press", key)), state.update(open=False))
        human.click(page, loc)
        self.assertIn(("press", "Escape"), page.keyboard.typed)
        self.assertEqual(page.mouse.events[-1], ("up",), "после закрытия панели — обычный клик мышью")
        self.assertEqual(loc.clicked, 0)

    def test_notice_window_got_it_is_pressed(self) -> None:
        """Окно YouTube «Remember that anyone can see your posts» с кнопкой «Got it» закрывается мышью по кнопке."""
        page = FakePage()
        shown = [[640.0, 500.0, "Got it"], None]
        page.evaluate = lambda js, arg=None: shown.pop(0) if shown else None
        self.assertTrue(human.dismiss_notice(page))
        self.assertEqual(page.mouse.events[-1], ("up",))
        page2 = FakePage()
        page2.evaluate = lambda js, arg=None: None
        self.assertFalse(human.dismiss_notice(page2), "окна нет — ничего не нажимаем")
        self.assertEqual(page2.mouse.events, [])

    def test_sticky_header_is_not_escaped(self) -> None:
        """Закреплённая шапка — не всплывашка: Escape не нажимаем (он мог бы закрыть окно загрузки)."""
        page = FakePage()
        loc = FakeLocator(page, 300)
        loc.covered_below = -1  # всегда перекрыта, но evaluate(_COVER_JS) вернёт None
        human.click(page, loc)
        self.assertNotIn(("press", "Escape"), page.keyboard.typed)
        self.assertEqual(loc.clicked, 1)


class TypeTest(unittest.TestCase):
    def test_short_text_by_letters_with_delay(self) -> None:
        page = FakePage()
        human.type_text(page, "Спасибо за отзыв")
        typed = [t for t in page.keyboard.typed if t[0] == "type"]
        self.assertEqual("".join(t[1] for t in typed), "Спасибо за отзыв")
        self.assertTrue(all(t[2] >= 45 for t in typed), "по буквам, с задержкой")
        self.assertFalse(any(t[0] == "insert" for t in page.keyboard.typed))

    def test_long_text_pasted_lines_by_shift_enter(self) -> None:
        page = FakePage()
        text = "\n".join(f"{i}:00 глава номер {i} " + "x" * 30 for i in range(20))
        human.type_text(page, text)
        inserted = [t[1] for t in page.keyboard.typed if t[0] == "insert"]
        self.assertEqual(len(inserted), 20)
        self.assertEqual(sum(1 for t in page.keyboard.typed if t == ("press", "Shift+Enter")), 19)


class ChooseFileTest(unittest.TestCase):
    def make(self, fire: bool):
        page = FakePage()
        sent: list[tuple] = []
        handlers = {}

        class Cdp:
            def send(self, method, params=None):
                sent.append((method, params))

            def on(self, event, fn):
                handlers[event] = fn

            def detach(self):
                pass

        button = FakeLocator(page, 300)
        orig_up = page.mouse.up

        def up():
            orig_up()
            if fire:
                handlers["Page.fileChooserOpened"]({"backendNodeId": 847, "mode": "selectMultiple"})

        page.mouse.up = up
        page.context = mock.MagicMock()
        page.context.new_cdp_session.return_value = Cdp()
        page.locator = lambda sel: button
        return page, sent

    def test_button_click_opens_chooser(self) -> None:
        page, sent = self.make(fire=True)
        how = human.choose_file(page, ["#select-files-button"], Path("/tmp/v.mp4"), fallback_input=mock.MagicMock())
        self.assertEqual(how, "кнопкой")
        self.assertIn(("DOM.setFileInputFiles", {"files": [str(Path("/tmp/v.mp4").resolve())], "backendNodeId": 847}), sent)
        self.assertIn(("Page.setInterceptFileChooserDialog", {"enabled": False}), sent, "перехват окна выключен после")

    def test_no_chooser_fallback(self) -> None:
        page, sent = self.make(fire=False)
        with mock.patch("engine.publish.youtube.set_file") as set_file, mock.patch.object(human, "CHOOSER_WAIT_SEC", 0):
            how = human.choose_file(page, ["#x"], Path("/tmp/v.mp4"), fallback_input="INPUT")
        self.assertEqual(how, "запасным способом")
        set_file.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class StubbornFieldTest(unittest.TestCase):
    """⌘A в профиле с отпечатком Windows не выделяет текст (13.11): поле всё равно очищается, название —
    ровно то, что нужно; не вышло — понятная ошибка, а не кривое название."""

    class Field:
        def __init__(self, text: str) -> None:
            self.text = text

        def evaluate(self, js, timeout=None):
            return self.text

    class Keyboard:
        def __init__(self, field, select_works: bool) -> None:
            self.field, self.select_works, self.selected = field, select_works, False

        def press(self, key, delay=None):
            if key == "Backspace":
                if self.selected:
                    self.field.text, self.selected = "", False
                else:
                    self.field.text = self.field.text[:-1]

        def type(self, text, delay=None):
            self.field.text += text

        def insert_text(self, text):
            self.field.text += text

    class Page:
        def __init__(self, kb) -> None:
            self.keyboard = kb
            self.context = self

        def new_cdp_session(self, page):
            kb = self.keyboard

            class S:
                def send(self, method, params):
                    if params.get("commands") == ["selectAll"] and kb.select_works:
                        kb.selected = True

                def detach(self):
                    pass

            return S()

        def wait_for_timeout(self, ms):
            pass

    def _run(self, select_works: bool) -> str:
        from engine.publish import human, youtube

        field = self.Field("Последняя капля . Я плакала когда слушала эту историю")
        page = self.Page(self.Keyboard(field, select_works))
        with mock.patch.object(human, "click", lambda *a, **k: None), mock.patch.object(youtube, "_act", lambda c: None):
            youtube._type_into(page, field, "ПОГРУЗИТЕСЬ В МИР СПОКОЙНОГО СНА!", None)
        return field.text

    def test_select_all_works(self) -> None:
        self.assertEqual(self._run(True), "ПОГРУЗИТЕСЬ В МИР СПОКОЙНОГО СНА!")

    def test_select_all_ignored_falls_back_to_backspace(self) -> None:
        self.assertEqual(self._run(False), "ПОГРУЗИТЕСЬ В МИР СПОКОЙНОГО СНА!")
