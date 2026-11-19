"""Источники фото людей без браузера (Яндекс, Википедия) и запасные пути при сбоях поиска."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.slides.stocks_base import StockCandidate


def cand(i: int, source: str = "x", query: str = "q") -> StockCandidate:
    return StockCandidate(provider=source, kind="photo", id=f"{source}{i}", query=query, preview_url="", download_url=f"http://x/{source}{i}.jpg")


PAGE = ('<div>{"origin":{"w":1200,"h":800,"url":"https:\\/\\/a.ru\\/1.jpg"}} {"origin":{"w":200,"h":150,"url":"https:\\/\\/a.ru\\/small.jpg"}} '
        '{"origin":{"w":900,"h":1200,"url":"https:\\/\\/b.ru\\/2.jpg"}}</div>')


class YandexTests(unittest.TestCase):
    def client(self):
        from engine.slides import source_yandex as sy

        c = sy.YandexImagesClient()
        c._http.close()
        return sy, c

    def test_parses_origin_urls_and_drops_small(self) -> None:
        sy, c = self.client()
        with mock.patch.object(c, "_get", return_value=PAGE):
            got = c.search("Валентин Гафт", per_page=10)
        self.assertEqual([x.download_url for x in got], ["https://a.ru/1.jpg", "https://b.ru/2.jpg"])
        self.assertTrue(all(x.provider == "yandex" and x.query == "Валентин Гафт" for x in got))

    def test_captcha_turns_source_off_for_a_while(self) -> None:
        sy, c = self.client()

        class Resp:
            status_code = 200
            url = "https://yandex.ru/showcaptcha?x"
            text = "captcha"

        with mock.patch.object(c._http, "get", return_value=Resp()), mock.patch.object(sy.time, "sleep"):
            self.assertEqual(c.search("a"), [])
            self.assertEqual(c.search("a"), [])
        self.assertFalse(c.available, "после капчи не мучаем Яндекс — ищем в других источниках")
        self.assertEqual(c.search("b"), [])

    def test_no_video(self) -> None:
        _sy, c = self.client()
        self.assertEqual(c.search("a", prefer_video=True), [])


class WikipediaTests(unittest.TestCase):
    def test_lead_photo_is_hero_reference_and_junk_skipped(self) -> None:
        from engine.slides import source_wikipedia as sw

        c = sw.WikipediaPhotosClient()
        c._http.close()
        page = {"title": "Гафт, Валентин Иосифович", "original": {"source": "https://u/Gaft.jpg", "width": 800, "height": 900},
                "images": [{"title": "Файл:Gaft1.jpg"}, {"title": "Файл:Flag of Russia.svg"}, {"title": "Файл:Commons-logo.png"},
                           {"title": "Файл:Gaft2.JPG"}]}
        seen = {}

        def api(lang, params):
            if params.get("generator") == "search":
                return {"query": {"pages": {"1": page}}}
            seen["titles"] = params["titles"]
            return {"query": {"pages": {"2": {"imageinfo": [{"url": "https://u/Gaft1.jpg", "mime": "image/jpeg", "width": 900, "height": 700}]}}}}

        with mock.patch.object(c, "_api", api):
            article, lead = c.photos("Валентин Гафт")
        self.assertTrue(lead.meta["hero_ref"])
        self.assertEqual(seen["titles"], "Файл:Gaft1.jpg|Файл:Gaft2.JPG", "флаги и логотипы не берём")
        self.assertEqual([a.download_url for a in article], ["https://u/Gaft1.jpg"])

    def test_wrong_article_is_ignored(self) -> None:
        from engine.slides import source_wikipedia as sw

        c = sw.WikipediaPhotosClient()
        c._http.close()
        with mock.patch.object(c, "_api", lambda lang, p: {"query": {"pages": {"1": {"title": "Совсем другая статья", "images": []}}}}):
            self.assertEqual(c.photos("Валентин Гафт"), ([], None))


class MergeAndFallbackTests(unittest.TestCase):
    def test_sources_interleave_and_dedupe(self) -> None:
        from engine.slides import person_media as pm

        a = [cand(0, "a"), cand(1, "a")]
        b = [cand(0, "b"), StockCandidate("b", "photo", "dup", "q", "", "http://x/a1.jpg")]
        got = pm._merge([a, b])
        self.assertEqual([c.download_url for c in got], ["http://x/a0.jpg", "http://x/b0.jpg", "http://x/a1.jpg"])

    def test_browser_search_only_when_fast_sources_are_not_enough(self) -> None:
        from engine.slides import person_media as pm
        from engine.slides.story_context import StoryContext
        from engine.slides.theme_plan import ThemePlan

        plan = ThemePlan(summary="s", context=StoryContext(subject="person", characters=["Иван Иванов"]))
        calls: list[str] = []

        def search(clients, source, queries, **kw):
            calls.append(source)
            n = 200 if source == "yandex" else 0
            return [cand(i, source) for i in range(n)]

        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp)
            (pool / "t").mkdir()
            (pool / "f").mkdir()
            with mock.patch.object(pm, "_search", search), mock.patch.object(pm, "_download", lambda *a, **k: False):
                pm.collect_person_pool(None, {"wikipedia": None}, plan, name="Иван Иванов", kind="photo", rng=random.Random(1),
                                       pool=pool, trials=pool / "t", frames=pool / "f", cancel=None, progress=lambda *a: None,
                                       pct_lo=0, pct_hi=1, image_need=10, video_need=0, max_cands=100)
        self.assertNotIn("google", calls, "Яндекс дал достаточно — капризный браузерный поиск не нужен")
        calls.clear()

        def poor(clients, source, queries, **kw):
            calls.append(source)
            return [cand(i, source) for i in range(5)] if source == "google" else []

        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp)
            (pool / "t").mkdir()
            (pool / "f").mkdir()
            with mock.patch.object(pm, "_search", poor), mock.patch.object(pm, "_download", lambda *a, **k: False):
                pm.collect_person_pool(None, {}, plan, name="Иван Иванов", kind="photo", rng=random.Random(1),
                                       pool=pool, trials=pool / "t", frames=pool / "f", cancel=None, progress=lambda *a: None,
                                       pct_lo=0, pct_hi=1, image_need=10, video_need=0, max_cands=100)
        self.assertIn("google", calls, "источники без браузера пусты — идём в Google/Bing")

    def test_topup_round_uses_only_plan_queries_and_keeps_exact_anchors_quoted(self) -> None:
        from engine.slides import person_media as pm
        from engine.slides import stars_media as st
        from engine.slides.theme_plan import ThemePlan

        star = st.Star(1, "Олег Даль", "Oleg Dal", "Олег Даль", "photo", "1941–1981", "советский актёр")
        plan = st._star_plan(ThemePlan(summary="s"), star, star.search, extra=True)
        q, _v = pm.person_query_set(plan, star.search, "photo", limit=30, only_plan=True)
        self.assertIn("Олег Даль 1970-е", q, "без кавычек — шире выдача")
        self.assertNotIn('"Олег Даль" young', q, "стандартные запросы первого прохода не повторяем")


class BuildClientsTests(unittest.TestCase):
    def test_new_sources_are_registered(self) -> None:
        from engine.slides.media_pipeline import MediaSourcesConfig, _build_clients

        clients = _build_clients(MediaSourcesConfig())
        try:
            self.assertIn("yandex", clients)
            self.assertIn("wikipedia", clients)
        finally:
            for c in clients.values():
                try:
                    c.close()
                except Exception:
                    pass


class DownloadLimitsTests(unittest.TestCase):
    """Медленный сервер не должен держать ролик: потолок времени и размера на загрузку."""

    def test_trickling_server_is_cut_off(self) -> None:
        import http.server
        import threading
        import time

        import httpx

        from engine.slides.media_pipeline import _download

        class Slow(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.end_headers()
                try:
                    for _ in range(100):
                        self.wfile.write(b"x" * 1024)
                        self.wfile.flush()
                        time.sleep(0.3)
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                t = time.monotonic()
                ok = _download(httpx.Client(), f"http://127.0.0.1:{srv.server_port}/v.mp4", Path(tmp) / "v.mp4", retries=3, max_seconds=1.0)
                took = time.monotonic() - t
                self.assertFalse(ok)
                self.assertLess(took, 5.0, "без повторов: медленный сервер повтор не ускорит")
                self.assertFalse((Path(tmp) / "v.mp4").exists(), "обрывок файла не остаётся")
        finally:
            srv.shutdown()

    def test_big_file_is_skipped_by_size(self) -> None:
        import http.server
        import threading

        import httpx

        from engine.slides.media_pipeline import _download

        class Big(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", str(50_000_000))
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Big)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                self.assertFalse(_download(httpx.Client(), f"http://127.0.0.1:{srv.server_port}/v.mp4", Path(tmp) / "v.mp4",
                                           retries=1, max_bytes=10_000_000))
        finally:
            srv.shutdown()


class SpeedTests(unittest.TestCase):
    """Сборка быстрее без потери качества: мёртвые сайты, остановка по набору, фоновые видео."""

    def test_dead_host_is_skipped_after_a_few_failures(self) -> None:
        from engine.slides.person_media import HostGuard

        g = HostGuard()
        for _ in range(3):
            g.report("https://dead.ru/a.jpg", False)
        self.assertTrue(g.blocked("https://dead.ru/other.jpg"))
        g.report("https://alive.ru/a.jpg", False)
        g.report("https://alive.ru/b.jpg", True)
        for _ in range(5):
            g.report("https://alive.ru/c.jpg", False)
        self.assertFalse(g.blocked("https://alive.ru/d.jpg"), "сайт, откуда хоть раз скачалось, не блокируем")

    def test_stops_downloading_when_enough_good_photos(self) -> None:
        import numpy as np
        from PIL import Image

        from engine.slides import person_media as pm
        from engine.slides import stars_media as st
        from engine.slides.theme_plan import ThemePlan

        star = st.Star(1, "Иван Иванов", "Ivan Ivanov", "Иван Иванов", "photo", "1930–2000", "актёр")
        anchors = st.anchor_queries(star, "Иван Иванов")
        hero = np.array([1.0, 0.0])
        asked: list[str] = []

        def search(clients, source, queries, **kw):
            if source != "yandex":
                return []
            q = pm.quote_name(anchors[0], "Иван Иванов")
            return [StockCandidate(provider="yandex", kind="photo", id=str(k), query=q, preview_url="", download_url=f"http://x/{k}.jpg",
                                   width=1000, height=800) for k in range(400)]

        def download(http, url, path, **kw):
            asked.append(url)
            rng = random.Random(int(url.rsplit("/", 1)[1].split(".")[0]))
            img = Image.new("RGB", (16, 9))
            img.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(16 * 9)])
            img.resize((900, 1200)).save(path, "JPEG")
            return True

        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp)
            (pool / "t").mkdir()
            (pool / "f").mkdir()
            with mock.patch.object(pm, "_search", search), mock.patch.object(pm, "_download", download), \
                 mock.patch("engine.slides.theme_media._image_short_side", lambda p: 900), \
                 mock.patch.object(pm.faces, "face_embeddings", lambda p: [hero]), \
                 mock.patch.object(pm.faces, "find_hero", lambda per: hero):
                _v, images, _rv, _ri = pm.collect_person_pool(
                    None, {}, st._star_plan(ThemePlan(summary="s"), star, "Иван Иванов"), name="Иван Иванов", kind="photo",
                    rng=random.Random(1), pool=pool, trials=pool / "t", frames=pool / "f", cancel=None,
                    progress=lambda *a: None, pct_lo=0, pct_hi=1, image_need=30, video_need=0, anchors=anchors, max_cands=400)
        self.assertEqual(len(images), 30)
        self.assertLess(len(asked), 150, "набрали 30 — остальные ~250 адресов не качаем")

    def test_broll_runs_in_background_and_extra_clips_are_trimmed(self) -> None:
        from engine.slides import stars_media as st

        self.assertGreaterEqual(st.BROLL_WORKERS, 2)
        self.assertGreaterEqual(st.STAR_WORKERS, 4)
        self.assertGreaterEqual(st.JUDGE_THREADS, 16)


if __name__ == "__main__":
    unittest.main()
