"""Вёрстки Studio: запасные варианты элементов с памятью по каналу; ошибка шага — снимок и понятный текст."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import shutil
import unittest
from unittest import mock

from engine.publish import studio_kit as kit
from engine.publish import youtube
from engine.publish.store import publish_dir, read_json


class FakeLocator:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel
        self.first = self

    def wait_for(self, state="visible", timeout=0):
        self.page.waited.append(self.sel)
        if self.sel not in self.page.present:
            raise TimeoutError(f'Timeout {timeout}ms exceeded waiting for locator("{self.sel}")')


class FakePage:
    url = "https://studio.youtube.com/video/x/edit"

    def __init__(self, present):
        self.present, self.waited = set(present), []

    def locator(self, sel):
        return FakeLocator(self, sel)

    def screenshot(self, path, full_page=True, timeout=None):
        open(path, "wb").write(b"png")

    def content(self):
        return "<html>studio</html>"

    def evaluate(self, js):
        return "en-GB"

    def eval_on_selector_all(self, sel, js):
        return ["ytcp-uploads-dialog#dialog: Details"]


class KitTests(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(publish_dir(), ignore_errors=True)
        self.addCleanup(shutil.rmtree, publish_dir(), True)
        kit.set_channel("@канал")

    def test_other_layout_found_and_remembered(self) -> None:
        main, alt = kit.SELECTORS["title"]
        page = FakePage([alt])
        self.assertEqual(kit.el(page, "title").sel, alt)
        self.assertEqual(read_json(kit.LAYOUTS_FILE, {})["@канал"]["title"], 1)
        page2 = FakePage([alt])
        kit.el(page2, "title")
        self.assertEqual(page2.waited, [alt], "в следующий раз — сразу запомненный вариант")

    def test_unknown_layout_lists_what_was_tried(self) -> None:
        with self.assertRaises(kit.LayoutError) as err:
            kit.el(FakePage([]), "next")
        for sel in kit.SELECTORS["next"]:
            self.assertIn(sel, str(err.exception))

    def test_step_failure_saves_snapshot_and_clear_message(self) -> None:
        page = FakePage([])
        with self.assertRaises(youtube.StudioError) as err:
            with kit.step(page, "название"):
                kit.el(page, "title")
        msg = str(err.exception)
        self.assertIn("шаг «название»", msg)
        folder = msg.split("Снимок и страница: ")[1]
        info = open(f"{folder}/info.txt", encoding="utf-8").read()
        self.assertIn("Шаг: название", info)
        self.assertIn("@канал", info)
        self.assertTrue(open(f"{folder}/screenshot.png", "rb").read())

    def test_diagnose_dead_tab_does_not_touch_page(self) -> None:
        page = FakePage([])

        def dead(*a, **k):
            raise TimeoutError("Timeout 15000ms exceeded")

        page.screenshot = dead
        page.content = lambda: (_ for _ in ()).throw(AssertionError("вкладка не отвечает — код страницы не читаем"))
        page.eval_on_selector_all = lambda *a, **k: (_ for _ in ()).throw(AssertionError("и окна не читаем"))
        folder = kit.diagnose(page, "переключение", RuntimeError("x"))
        info = open(f"{folder}/info.txt", encoding="utf-8").read()
        self.assertIn("вкладка не отвечает", info)

    def test_step_passes_browser_loss_and_needs_user_through(self) -> None:
        page = FakePage([])
        with self.assertRaises(youtube.NeedsUser):
            with kit.step(page, "вход"):
                raise youtube.NeedsUser("войди в аккаунт")

        class TargetClosedError(Exception):
            pass

        with self.assertRaises(TargetClosedError):
            with kit.step(page, "загрузка"):
                raise TargetClosedError("Target page, context or browser has been closed")

    def test_timeout_text_is_short(self) -> None:
        exc = TimeoutError('Locator.click: Timeout 30000ms exceeded.\nCall log:\n  - waiting for locator("#next-button")')
        self.assertEqual(kit._short(exc), "не дождался элемента #next-button (таймаут)")


if __name__ == "__main__":
    unittest.main()
