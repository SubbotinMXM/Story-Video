"""Тесты стоков / ключей."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import unittest

from engine.slides.stocks_base import KeyPool, parse_keys
from engine.slides.stocks_pexels import _pick_video_file


class KeysTests(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertEqual(parse_keys("a, b\nc"), ["a", "b", "c"])
        self.assertEqual(parse_keys(["x", "x", "y"]), ["x", "y"])

    def test_pool_rotation(self) -> None:
        pool = KeyPool(["k1", "k2", "", "k1"])
        self.assertEqual(pool.next(), "k1")
        self.assertEqual(pool.next(), "k1")
        pool.rotate("k1")
        self.assertEqual(pool.next(), "k2")
        pool.rotate("k2")
        self.assertEqual(pool.next(), "k1")
        pool.mark_bad("k1")
        pool.mark_bad("k2")
        self.assertIsNone(pool.next())


class PexelsPickTests(unittest.TestCase):
    def test_prefers_1080p(self) -> None:
        files = [
            {"link": "https://x/a.mp4", "width": 3840, "height": 2160},
            {"link": "https://x/b.mp4", "width": 1280, "height": 720},
            {"link": "https://x/d.mp4", "width": 1920, "height": 1080},
            {"link": "https://x/c.mp4", "width": 640, "height": 360},
        ]
        link, w, _h = _pick_video_file(files)
        self.assertEqual(link, "https://x/d.mp4")
        self.assertEqual(w, 1920)

    def test_720p_over_4k_and_360p(self) -> None:
        files = [
            {"link": "https://x/a.mp4", "width": 3840, "height": 2160},
            {"link": "https://x/b.mp4", "width": 1280, "height": 720},
            {"link": "https://x/c.mp4", "width": 640, "height": 360},
        ]
        link, _w, _h = _pick_video_file(files)
        self.assertEqual(link, "https://x/b.mp4")



class HeightFilterTests(unittest.TestCase):
    def test_candidate_height(self) -> None:
        from engine.slides.ffprobe_util import candidate_height_ok

        self.assertFalse(candidate_height_ok(640, 360, 720))
        self.assertTrue(candidate_height_ok(1280, 720, 720))
        self.assertTrue(candidate_height_ok(0, 0, 720))  # неизвестно — проверим файл
        self.assertTrue(candidate_height_ok(640, 360, 0))

    def test_youtube_format_is_video_only_hd(self) -> None:
        from engine.slides.source_youtube import _format

        fmt = _format(720)
        self.assertIn("[height>=720]", fmt)
        self.assertNotIn("+ba", fmt)
        self.assertNotIn("best[", fmt)


if __name__ == "__main__":
    unittest.main()
