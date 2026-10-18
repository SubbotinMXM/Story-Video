"""Тесты парсера URL Google Images (без браузера)."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import unittest

from engine.slides.source_google import _extract_urls, _uniq_good, _url_id


class GoogleUrlParseTests(unittest.TestCase):
    def test_extract_and_filter(self) -> None:
        html = (
            '["https://cdn.example.com/a.jpg"],'
            'https://encrypted-tbn0.gstatic.com/images?q=tbn:x,'
            'https://www.google.com/logos/x.png,'
            "https://images.example.org/photo.png?w=800"
        )
        urls = _uniq_good(_extract_urls(html))
        self.assertIn("https://cdn.example.com/a.jpg", urls)
        self.assertIn("https://images.example.org/photo.png?w=800", urls)
        self.assertTrue(all("gstatic" not in u and "google.com" not in u for u in urls))

    def test_url_id_stable(self) -> None:
        self.assertEqual(_url_id("http://a/b"), _url_id("http://a/b"))
        self.assertNotEqual(_url_id("http://a/b"), _url_id("http://a/c"))


if __name__ == "__main__":
    unittest.main()
