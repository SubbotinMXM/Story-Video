"""Парсинг ключей стоков."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import unittest

from engine.slides.media_pipeline import stock_keys_from_config


class ConfigParseTests(unittest.TestCase):
    def test_stock_keys_from_config(self) -> None:
        cfg = stock_keys_from_config(
            {
                "pexels_api_key": "a,b",
                "pixabay_api_keys": "p1",
                "unsplash_api_key": "u1",
            }
        )
        self.assertEqual(cfg.pexels, ["a", "b"])
        self.assertEqual(cfg.pixabay, ["p1"])
        self.assertEqual(cfg.unsplash, ["u1"])
        self.assertTrue(cfg.google)

    def test_google_always_on(self) -> None:
        # переключателя больше нет: старое значение в настройках игнорируется
        self.assertTrue(stock_keys_from_config({"google_images": False}).google)


if __name__ == "__main__":
    unittest.main()
