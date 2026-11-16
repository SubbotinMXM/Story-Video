"""Переключение каналов профиля: одинаковые названия, проверка по ID на youtube.com и в Studio, поиск каналов."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)
from _human import setUpModule, tearDownModule  # noqa: F401  (простые клики вместо «человеческих»)

import unittest
from contextlib import contextmanager, nullcontext
from unittest import mock

from engine.publish import registry, youtube

A = registry.Channel("UC" + "a" * 22, "Точки на карте", handle="@Точкинакарте-р3ш", authuser=0)
B = registry.Channel("UC" + "b" * 22, "Точки на карте", handle="@Точкинакарте-ш4ь", authuser=0)
ITEMS = [{"name": "Точки на карте", "handle": A.handle, "gaia": "1", "selected": True},
         {"name": "Точки на карте", "handle": B.handle, "gaia": "2", "selected": False}]


class FakeYouTube:
    """Профиль с двумя каналами одного аккаунта. Клик по пункту списка делает канал активным на youtube.com
    сразу, а Studio — только после захода на её главную (как в жизни). Прямой адрес неактивного канала
    в Studio «открывается» (как в жизни — поэтому по нему проверять нельзя)."""

    def __init__(self, active: registry.Channel) -> None:
        self.yt = active.id
        self.studio = active.id
        self.clicked: list[int] = []
        page = mock.MagicMock()
        page.expect_navigation.side_effect = lambda **k: nullcontext()
        page.locator.return_value.nth.side_effect = self.item
        self.page = page

    def item(self, i: int):
        el = mock.MagicMock()
        el.click.side_effect = lambda **k: self.click(i)
        return el

    def click(self, i: int) -> None:
        self.clicked.append(i)
        self.yt = [A, B][i].id

    def studio_channel(self, page, url: str) -> str:
        if url == youtube.STUDIO:
            self.studio = self.yt  # главная Studio подхватывает переключение
            return self.studio
        return url.split("/channel/")[1].split("/")[0]  # прямой адрес «открывается» у любого канала аккаунта

    def patches(self):
        return (mock.patch.object(youtube, "_switcher_items", lambda page, au: ITEMS),
                mock.patch.object(youtube, "active_channel", lambda page: self.yt),
                mock.patch.object(youtube, "studio_channel", self.studio_channel),
                mock.patch.object(youtube, "pause", lambda c: None))


@contextmanager
def applied(patches):
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in patches:
            p.stop()


class SwitchTest(unittest.TestCase):
    def test_same_names_switches_by_handle(self) -> None:
        yt = FakeYouTube(active=A)
        with applied(yt.patches()):
            youtube.switch_to(yt.page, B)
        self.assertEqual(yt.clicked, [1], "нажат второй пункт — по @-имени, не по названию")
        self.assertEqual((yt.yt, yt.studio), (B.id, B.id))

    def test_studio_direct_url_does_not_fool_check(self) -> None:
        """Прямой адрес B в Studio открывается и при активном A — но комментарии пошли бы от A: нужен клик."""
        yt = FakeYouTube(active=A)
        with applied(yt.patches()):
            youtube.switch_to(yt.page, B)
        self.assertTrue(yt.clicked)

    def test_already_active_no_clicks(self) -> None:
        yt = FakeYouTube(active=B)
        with applied(yt.patches()):
            youtube.switch_to(yt.page, B)
        self.assertEqual(yt.clicked, [])

    def test_back_and_forth(self) -> None:
        yt = FakeYouTube(active=A)
        with applied(yt.patches()):
            for ch in (B, A, B):
                youtube.switch_to(yt.page, ch)
        self.assertEqual(yt.clicked, [1, 0, 1])

    def test_click_without_effect_is_error(self) -> None:
        yt = FakeYouTube(active=A)
        yt.click = lambda i: yt.clicked.append(i)  # YouTube не переключил
        with applied(yt.patches()), mock.patch.object(youtube, "SWITCH_WAIT_SEC", 0):
            with self.assertRaisesRegex(youtube.StudioError, "не переключился"):
                youtube.switch_to(yt.page, B)

    def test_missing_handle(self) -> None:
        yt = FakeYouTube(active=A)
        other = registry.Channel("UC" + "c" * 22, "Точки на карте", handle="@другой", authuser=0)
        with applied(yt.patches()):
            with self.assertRaisesRegex(youtube.StudioError, "нет в списке"):
                youtube.switch_to(yt.page, other)


class ScanTest(unittest.TestCase):
    def run_scan(self, switch):
        @contextmanager
        def page(cdp):
            yield mock.MagicMock()

        ids = {A.handle: A.id, B.handle: B.id}
        lists = {0: ITEMS, 1: ITEMS}  # второй «аккаунт» Google вернул те же каналы
        with mock.patch.object(youtube, "studio_page", page), \
             mock.patch.object(youtube, "_switcher_items", lambda p, au: [dict(i, gaia=str(au)) for i in lists.get(au, [])]), \
             mock.patch.object(youtube, "channel_id", lambda p, h: ids[h]), \
             mock.patch.object(youtube, "switch_to", switch), \
             mock.patch.object(youtube, "_playlists", lambda p, cid: ["один"]), \
             mock.patch.object(youtube, "_last_date", lambda p, cid: "2026-11-10"), \
             mock.patch("engine.publish.studio_kit.diagnose", lambda *a: "/tmp/x"):
            return youtube.scan_channels("ws://x")

    def test_same_names_both_found_once(self) -> None:
        got = self.run_scan(lambda *a, **k: None)
        self.assertEqual([c.id for c in got], [A.id, B.id], "оба канала, без повторов из второго аккаунта")
        self.assertTrue(all(c.playlists == ["один"] for c in got))

    def test_one_channel_failing_does_not_stop_scan(self) -> None:
        def switch(page, ch, **k):
            if ch.id == A.id:
                raise youtube.StudioError("не переключился")

        got = self.run_scan(switch)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[1].playlists, ["один"], "второй канал прочитан, хоть первый и не открылся")

    def test_all_failing_is_error(self) -> None:
        def switch(page, ch, **k):
            raise youtube.StudioError("не переключился")

        with self.assertRaises(youtube.StudioError):
            self.run_scan(switch)


if __name__ == "__main__":
    unittest.main()
