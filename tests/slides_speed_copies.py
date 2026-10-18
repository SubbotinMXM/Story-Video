"""Сдвиг скорости копий."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import unittest

from engine.slides.speed_copies import copy_filename, copy_percents, percent_phrase


class SpeedCopyTests(unittest.TestCase):
    def test_pairs(self) -> None:
        self.assertEqual(copy_percents(1), [99])
        self.assertEqual(copy_percents(2), [99, 101])
        self.assertEqual(copy_percents(3), [99, 101, 98])
        self.assertEqual(copy_percents(4), [99, 101, 98, 102])

    def test_names(self) -> None:
        self.assertEqual(percent_phrase(99), "99 процентов")
        self.assertEqual(percent_phrase(101), "101 процент")
        self.assertEqual(percent_phrase(102), "102 процента")
        self.assertEqual(percent_phrase(98), "98 процентов")
        self.assertEqual(copy_filename(99), "99 процентов ролик.mp4")
        self.assertEqual(copy_filename(101), "101 процент ролик.mp4")


if __name__ == "__main__":
    unittest.main()
