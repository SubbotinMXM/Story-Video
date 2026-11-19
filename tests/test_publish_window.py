"""Маленькое окно браузера профиля (800×513) не должно ронять выкладку: Studio рассчитан на окно побольше."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import unittest
from unittest import mock

from engine.publish import youtube


class FakePage:
    """Страница: размер области меняется только если окно удалось увеличить (или задан размер страницы)."""

    def __init__(self, w: int, h: int, *, window_works: bool) -> None:
        self.w, self.h, self.window_works = w, h, window_works
        self.viewport_calls: list[dict] = []
        self.cdp_calls: list[dict] = []
        outer = self

        class Ctx:
            def new_cdp_session(self, page):
                class Cdp:
                    def send(self, method, params=None):
                        outer.cdp_calls.append({"m": method, **(params or {})})
                        if method == "Browser.getWindowForTarget":
                            return {"windowId": 1}
                        b = (params or {}).get("bounds") or {}
                        if outer.window_works and "width" in b:
                            outer.w, outer.h = b["width"] - 16, b["height"] - 110
                        elif not outer.window_works:
                            pass
                        return {}

                return Cdp()

        self.context = Ctx()

    def evaluate(self, _js):
        return [self.w, self.h]

    def wait_for_timeout(self, _ms):
        pass

    def set_viewport_size(self, vs):
        self.viewport_calls.append(vs)
        self.w, self.h = vs["width"], vs["height"]


class FitWindowTests(unittest.TestCase):
    def test_big_window_is_left_alone(self) -> None:
        page = FakePage(1920, 1000, window_works=True)
        self.assertEqual(youtube.fit_window(page), (1920, 1000))
        self.assertEqual(page.cdp_calls, [])
        self.assertEqual(page.viewport_calls, [])

    def test_working_laptop_window_is_not_touched(self) -> None:
        page = FakePage(1280, 713, window_works=True)
        self.assertEqual(youtube.fit_window(page), (1280, 713))
        self.assertEqual(page.cdp_calls, [])
        self.assertEqual(page.viewport_calls, [])

    def test_small_window_is_enlarged_through_the_window(self) -> None:
        page = FakePage(800, 513, window_works=True)
        w, h = youtube.fit_window(page)
        self.assertGreaterEqual(w, youtube.TARGET_WINDOW[0])
        self.assertGreaterEqual(h, youtube.TARGET_WINDOW[1])
        self.assertEqual(page.viewport_calls, [], "окно выросло само — размер страницы не подменяем")

    def test_window_that_will_not_grow_falls_back_to_page_size(self) -> None:
        """Профиль AdsPower с экраном 400×300: окно не растёт — задаём размер области страницы (так сработало вживую)."""
        page = FakePage(800, 513, window_works=False)
        w, h = youtube.fit_window(page)
        self.assertGreaterEqual(w, youtube.TARGET_WINDOW[0])
        self.assertGreaterEqual(h, youtube.TARGET_WINDOW[1])
        self.assertTrue(page.viewport_calls)

    def test_broken_page_does_not_raise(self) -> None:
        page = mock.MagicMock()
        page.evaluate.side_effect = RuntimeError("closed")
        self.assertEqual(youtube.fit_window(page), (0, 0))

    def test_studio_page_fits_window_for_every_profile(self) -> None:
        """Все работы в Studio (выкладка, комментарии, аналитика…) идут через studio_page — окно увеличивается там."""
        import inspect

        self.assertIn("fit_window(page)", inspect.getsource(youtube.studio_page))


class SaveRetryTests(unittest.TestCase):
    def test_save_outside_viewport_enlarges_and_retries(self) -> None:
        from engine.publish import elements

        page = mock.MagicMock()
        save = page.locator.return_value.first
        save.get_attribute.return_value = "false"
        page.locator.return_value.first.is_visible.return_value = False  # редактор закрылся
        calls = []

        def click(p, loc):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("Locator.click: element is outside of the viewport")

        with mock.patch.object(elements.human, "click", click), mock.patch.object(youtube, "fit_window") as fit:
            elements._save(page, lambda _c: None, "конечная заставка")
        self.assertEqual(len(calls), 2)
        fit.assert_called_once()
        self.assertTrue(fit.call_args.kwargs.get("force"))

    def test_other_click_errors_are_not_hidden(self) -> None:
        from engine.publish import elements

        page = mock.MagicMock()
        page.locator.return_value.first.get_attribute.return_value = "false"
        with mock.patch.object(elements.human, "click", side_effect=RuntimeError("detached")), \
             mock.patch.object(youtube, "fit_window") as fit:
            with self.assertRaises(RuntimeError):
                elements._save(page, lambda _c: None, "конечная заставка")
        fit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
