"""Проверка YouTube «вы не робот»: ролик открывается через запасной клиент, обложка качается и без страницы."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.publish import cover as cv
from engine.rewrite import source as src

BOT = "ERROR: [youtube] X: Sign in to confirm you’re not a bot."


class _FakeYDL:
    """yt-dlp, у которого обычный клиент упирается в проверку, а запасной проходит."""

    seen: list = []

    def __init__(self, opts):
        self.clients = (opts.get("extractor_args") or {}).get("youtube", {}).get("player_client")
        _FakeYDL.seen.append(self.clients)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        if not self.clients:
            raise RuntimeError(BOT)
        return {"id": "abcdefghijk", "title": "T"}


class VideoInfoFallback(unittest.TestCase):
    def test_fallback_client_opens_page(self) -> None:
        _FakeYDL.seen = []
        with mock.patch.dict(sys.modules, {"yt_dlp": mock.Mock(YoutubeDL=_FakeYDL)}):
            info = src.video_info("https://www.youtube.com/watch?v=abcdefghijk")
        self.assertEqual(info["title"], "T")
        self.assertEqual(_FakeYDL.seen[0], None)
        self.assertEqual(_FakeYDL.seen[1], ["tv_embedded"])

    def test_other_errors_do_not_try_other_clients(self) -> None:
        class Gone(_FakeYDL):
            def extract_info(self, url, download=False):
                raise RuntimeError("Video unavailable")

        _FakeYDL.seen = []
        with mock.patch.dict(sys.modules, {"yt_dlp": mock.Mock(YoutubeDL=Gone)}):
            with self.assertRaises(src.SourceError):
                src.video_info("https://www.youtube.com/watch?v=abcdefghijk")
        self.assertEqual(len(_FakeYDL.seen), 1)


class CoverWithoutPage(unittest.TestCase):
    def test_thumbnail_by_id_when_page_blocked(self) -> None:
        class Resp:
            status_code = 200
            content = b"x" * 9000

        class Http:
            urls: list = []

            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, u):
                Http.urls.append(u)
                return Resp()

        with mock.patch("engine.rewrite.source.video_info", side_effect=src.SourceError("bot")), \
                mock.patch.object(cv.httpx, "Client", Http):
            title, image, _ = cv.youtube_original("https://www.youtube.com/watch?v=abcdefghijk")
        self.assertTrue(image)
        self.assertIn("abcdefghijk", Http.urls[0])

    def test_without_id_still_raises(self) -> None:
        with mock.patch("engine.rewrite.source.video_info", side_effect=src.SourceError("bot")):
            with self.assertRaises(src.SourceError):
                cv.youtube_original("https://example.com/none")


if __name__ == "__main__":
    unittest.main()
