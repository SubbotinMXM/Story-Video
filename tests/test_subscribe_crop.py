"""Crop хромакея подписки — кнопка не должна оставаться по центру кадра."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import unittest
from pathlib import Path

from engine.compositor.defaults import (
    SUBSCRIBE_BLEND,
    SUBSCRIBE_CHROMA,
    SUBSCRIBE_SIMILARITY,
)
from engine.compositor.utils import probe_chromakey_crop

AAA = Path("/Users/mac/Desktop/AAA.mp4")


class SubscribeCropTests(unittest.TestCase):
    def test_aaa_crop_is_tight_and_not_fullscreen(self) -> None:
        if not AAA.is_file():
            self.skipTest("нет AAA.mp4 на Desktop")
        crop = probe_chromakey_crop(
            AAA,
            chroma=SUBSCRIBE_CHROMA,
            similarity=SUBSCRIBE_SIMILARITY,
            blend=SUBSCRIBE_BLEND,
        )
        self.assertIsNotNone(crop)
        assert crop is not None
        x, y, w, h = crop
        self.assertLess(w, 800)
        self.assertLess(h, 250)
        self.assertGreater(y, 100)
        self.assertGreater(x, 50)
