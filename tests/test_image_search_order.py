"""Поиск картинок: Bing первым (Google ловит капчу), Google — если Bing дал мало, DuckDuckGo — последним."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import asyncio
import unittest
from unittest import mock

from engine.slides import source_google as sg


def urls(prefix: str, n: int) -> list[str]:
    return [f"https://{prefix}{i}.example.org/photo{i}.jpg" for i in range(n)]


def run(bing, google, ddg, *, off_until: float = 0.0):
    client = sg.GoogleImagesClient.__new__(sg.GoogleImagesClient)
    client._captchas, client._google_off_until = 0, off_until
    calls: list[str] = []

    async def fake_bing(page, q, limit):
        calls.append("bing")
        return bing

    async def fake_google(page, q, limit):
        calls.append("google")
        return google

    def fake_ddg(q, limit):
        calls.append("ddg")
        return ddg

    with mock.patch.object(sg, "_bing_with_retry", fake_bing), mock.patch.object(sg, "_scrape_google", fake_google), \
         mock.patch.object(sg, "_scrape_ddg_http", fake_ddg):
        got = asyncio.run(client._scrape(mock.MagicMock(), "q", 12))
    return got, calls, client


class SearchOrderTests(unittest.TestCase):
    def test_enough_from_bing_never_touches_google(self) -> None:
        got, calls, _ = run(urls("b", 8), urls("g", 8), urls("d", 8))
        self.assertEqual(calls, ["bing"])
        self.assertEqual(len(got), 8)

    def test_few_from_bing_adds_google(self) -> None:
        got, calls, _ = run(urls("b", 2), urls("g", 8), [])
        self.assertEqual(calls, ["bing", "google"])
        self.assertEqual(len(got), 10)

    def test_both_poor_goes_to_duckduckgo(self) -> None:
        got, calls, _ = run([], urls("g", 1), urls("d", 6))
        self.assertEqual(calls, ["bing", "google", "ddg"])
        self.assertEqual(len(got), 7)

    def test_google_on_pause_is_skipped(self) -> None:
        import time

        got, calls, _ = run(urls("b", 1), urls("g", 8), urls("d", 6), off_until=time.monotonic() + 600)
        self.assertEqual(calls, ["bing", "ddg"])

    def test_three_captchas_pause_google(self) -> None:
        client = sg.GoogleImagesClient.__new__(sg.GoogleImagesClient)
        client._captchas, client._google_off_until = 0, 0.0

        async def bing(page, q, limit):
            return []

        async def google(page, q, limit):
            sg._CAPTCHA_SEEN[id(page)] = True
            return []

        page = mock.MagicMock()
        with mock.patch.object(sg, "_bing_with_retry", bing), mock.patch.object(sg, "_scrape_google", google), \
             mock.patch.object(sg, "_scrape_ddg_http", lambda q, n: []):
            for _ in range(3):
                asyncio.run(client._scrape(page, "q", 12))
        self.assertGreater(client._google_off_until, 0)


if __name__ == "__main__":
    unittest.main()
