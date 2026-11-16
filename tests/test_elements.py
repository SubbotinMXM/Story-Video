"""Конечная заставка и подсказка-плейлист: уже есть — не трогаем; нет — добавляем."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)
from _human import setUpModule, tearDownModule  # noqa: F401

import unittest
from unittest import mock

from engine.publish import elements as E
from engine.publish import youtube


def page_with(counts: dict, visible: dict | None = None):
    """Поддельная страница: locator(sel).count() — из counts, is_visible — из visible."""
    page = mock.MagicMock()
    visible = visible or {}

    def locator(sel):
        loc = mock.MagicMock()
        loc.count.return_value = counts.get(sel, 0)
        loc.first = loc
        loc.is_visible.return_value = visible.get(sel, counts.get(sel, 0) > 0)
        loc.filter.return_value = loc
        loc.nth.return_value = loc
        loc.get_attribute.return_value = None
        loc.all_inner_texts.return_value = ["Для вас\n18 videos"]
        return loc

    page.locator.side_effect = locator
    return page


class ElementsTest(unittest.TestCase):
    def setUp(self) -> None:
        p = mock.patch.object(E, "_open", lambda page, link, act: None)
        p.start()
        self.addCleanup(p.stop)
        self.act = lambda c: None

    def test_existing_end_screen_untouched(self) -> None:
        page = page_with({E.ES_ROWS: 3, E.DISCARD: 1})
        with mock.patch.object(E, "_save", side_effect=AssertionError("не сохраняем")):
            self.assertEqual(E._end_screen(page, ["Для вас"], None, self.act), "")

    def test_existing_card_untouched(self) -> None:
        page = page_with({E.CARD_TYPES_HEADER: 0, E.DISCARD: 1})
        with mock.patch.object(E, "_save", side_effect=AssertionError("не сохраняем")):
            self.assertEqual(E._card(page, ["Для вас"], None, self.act), "")

    def test_new_card_is_playlist_and_saved(self) -> None:
        page = page_with({E.CARD_TYPES_HEADER: 1, E.CARD_TYPES: 4, E.PLAYLIST_CARDS: 1})
        saved = []
        with mock.patch.object(E, "_save", lambda page, act, what: saved.append(what)):
            E._card(page, ["Для вас"], None, self.act)
        self.assertEqual(saved, ["подсказка-плейлист"])

    def test_template_not_applied_is_error(self) -> None:
        page = page_with({E.ES_ROWS: 0, E.TEMPLATES: 6})
        with self.assertRaises(youtube.StudioError):
            E._end_screen(page, ["Для вас"], None, self.act)


if __name__ == "__main__":
    unittest.main()
