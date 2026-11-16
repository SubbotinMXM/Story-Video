"""Несколько ключей YouTube: кончилась квота у одного — работает следующий."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.core.config import _normalize
from engine.slides.media_pipeline import stock_keys_from_config
from engine.slides.source_youtube import YouTubeClient

ITEM = {"items": [{"id": {"videoId": "abc"}, "snippet": {"title": "Mongolia steppe", "channelTitle": "Nature"}}]}
# проверка найденного видео (videos.list): CC, без правообладателя в Content ID
CHECKED = {"items": [{"id": "abc", "status": {"license": "creativeCommon"}, "contentDetails": {"licensedContent": False, "duration": "PT2M"}, "snippet": {"description": "Shot by me"}}]}


class YouTubeKeysTests(unittest.TestCase):
    def test_config_keeps_several_keys_and_migrates_old_one(self) -> None:
        self.assertEqual(_normalize({"youtube_api_key": "OLD"})["youtube_api_keys"], "OLD")
        cfg = _normalize({"youtube_api_keys": "K1\nK2, K3", "youtube_api_key": "OLD"})
        self.assertEqual(stock_keys_from_config(cfg).youtube, ["K1", "K2", "K3"])
        self.assertNotIn("youtube_api_key", cfg)

    def test_quota_exhausted_key_switches_to_next(self) -> None:
        used: list[str] = []

        def fake_get(url, params):
            if url.endswith("/videos"):
                return httpx.Response(200, json=CHECKED)
            used.append(params["key"])
            if params["key"] == "K1":
                return httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}})
            return httpx.Response(200, json=ITEM)

        yt = YouTubeClient(["K1", "K2"])
        with mock.patch.object(yt._http, "get", side_effect=fake_get):
            first = yt.search("mongolia")
            second = yt.search("steppe")
        self.assertTrue(first and second)
        self.assertEqual(used, ["K1", "K2", "K2"], "исчерпанный ключ больше не дёргаем")

    def test_all_keys_exhausted_returns_empty(self) -> None:
        yt = YouTubeClient(["K1", "K2"])
        with mock.patch.object(yt._http, "get", return_value=httpx.Response(429)):
            self.assertEqual(yt.search("x"), [])

    def test_ping_reports_quota(self) -> None:
        from engine.slides import source_ping

        def handler(req: httpx.Request) -> httpx.Response:
            key = req.url.params.get("key")
            return httpx.Response(200, json={"items": []}) if key == "GOOD" else httpx.Response(403, text="quotaExceeded")

        real = httpx.Client
        with mock.patch.object(source_ping.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler))):
            msg = source_ping.ping_stocks(youtube="GOOD\nSPENT")
        self.assertIn("YouTube: 1/2 OK", msg)
        self.assertIn("квота на сегодня исчерпана", msg)


if __name__ == "__main__":
    unittest.main()
