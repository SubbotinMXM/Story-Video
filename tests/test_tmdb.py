"""TMDB: фото людей кино и кадры фильмов — только картинки, ключ токеном или параметром, обход подмены DNS."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import socket
import unittest
from unittest import mock

import httpx

from engine.slides.source_tmdb import TmdbClient
from engine.slides.stocks_base import KeyPool

TOKEN = "eyJ-test-token"
SHORT = "abc123shortkey"


def client(handler, *keys: str) -> TmdbClient:
    c = TmdbClient(KeyPool(list(keys) or [TOKEN]))
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    return c


def _img(path: str, w: int = 1920, h: int = 1080, vote: float = 5.0) -> dict:
    return {"file_path": path, "width": w, "height": h, "vote_average": vote, "vote_count": 3}


class TmdbTests(unittest.TestCase):
    def test_token_goes_in_header_and_short_key_in_params(self) -> None:
        seen = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append((req.headers.get("authorization"), req.url.params.get("api_key")))
            return httpx.Response(200, json={"results": []})

        client(handler, TOKEN).search("Keanu Reeves")
        client(handler, SHORT).search("Keanu Reeves")
        self.assertEqual(seen[0], (f"Bearer {TOKEN}", None))
        self.assertEqual(seen[1], (None, SHORT))

    def test_video_is_never_returned(self) -> None:
        called = []
        c = client(lambda r: called.append(r) or httpx.Response(200, json={"results": []}))
        self.assertEqual(c.search("anything", prefer_video=True), [])
        self.assertEqual(called, [], "видео нет — запросов к сервису тоже")

    def test_person_photos_take_the_most_popular_exact_namesake(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            p = req.url.path
            if p.endswith("/search/person"):
                return httpx.Response(200, json={"results": [
                    {"id": 1, "name": "Anna Lee", "popularity": 0.2},
                    {"id": 2, "name": "Anna Lee", "popularity": 9.0},
                    {"id": 3, "name": "Anna Leeson", "popularity": 50.0}]})
            if p.endswith("/person/2/images"):
                return httpx.Response(200, json={"profiles": [_img("/p1.jpg", 1000, 1500), _img("/tiny.jpg", 200, 300)]})
            if p.endswith("/person/2/tagged_images"):
                return httpx.Response(200, json={"results": [_img("/s1.jpg")]})
            return httpx.Response(404)

        got = client(handler).person_photos("Anna Lee")
        self.assertEqual([c.meta["role"] for c in got], ["profile", "still"], "мелкий снимок отсеян, тёзка с другим именем не взят")
        self.assertTrue(all(c.provider == "tmdb" and c.kind == "photo" for c in got))
        self.assertTrue(got[0].download_url.startswith("https://image.tmdb.org/t/p/original/"))

    def test_unknown_person_returns_nothing(self) -> None:
        c = client(lambda r: httpx.Response(200, json={"results": [{"id": 7, "name": "Completely Different", "popularity": 5}]}))
        self.assertEqual(c.person_photos("Anna Lee"), [])

    def test_film_gives_backdrops_best_voted_first(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/search/multi"):
                return httpx.Response(200, json={"results": [{"media_type": "movie", "id": 9, "title": "Blade", "popularity": 3}]})
            if req.url.path.endswith("/movie/9/images"):
                return httpx.Response(200, json={"backdrops": [_img("/a.jpg", vote=1), _img("/b.jpg", vote=8)]})
            return httpx.Response(404)

        got = client(handler).search("Blade")
        self.assertEqual([c.download_url.rsplit("/", 1)[-1] for c in got], ["b.jpg", "a.jpg"])
        self.assertEqual(got[0].meta["title"], "Blade")

    def test_rejected_key_is_dropped_and_next_one_used(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            if req.headers.get("authorization") == "Bearer eyJ-bad":
                return httpx.Response(401)
            return httpx.Response(200, json={"results": []})

        c = client(handler, "eyJ-bad", TOKEN)
        self.assertEqual(c.search("x"), [])
        self.assertEqual(c.keys.next(), TOKEN, "недействительный ключ больше не берётся")

    def test_works_without_keys_as_empty(self) -> None:
        self.assertEqual(TmdbClient(KeyPool([])).search("Keanu Reeves"), [])


class DnsFixTests(unittest.TestCase):
    def setUp(self) -> None:
        from engine.core import dnsfix

        self.dns = dnsfix
        dnsfix._cache.clear()

    def test_poisoned_answer_is_replaced_by_doh(self) -> None:
        loop = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with mock.patch.object(self.dns, "_orig", return_value=loop), \
             mock.patch.object(self.dns, "doh", return_value=["18.244.87.28"]):
            res = self.dns._getaddrinfo("api.themoviedb.org", 443)
        self.assertEqual(res[0][4], ("18.244.87.28", 443))

    def test_normal_answer_is_kept(self) -> None:
        good = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("18.244.1.1", 443))]
        with mock.patch.object(self.dns, "_orig", return_value=good), mock.patch.object(self.dns, "doh") as doh:
            self.assertEqual(self.dns._getaddrinfo("image.tmdb.org", 443), good)
        doh.assert_not_called()

    def test_other_hosts_are_untouched(self) -> None:
        loop = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        with mock.patch.object(self.dns, "_orig", return_value=loop), mock.patch.object(self.dns, "doh") as doh:
            self.assertEqual(self.dns._getaddrinfo("localhost", 80), loop)
        doh.assert_not_called()

    def test_covers_subdomains_only_of_listed_domains(self) -> None:
        self.assertTrue(self.dns._covered("api.themoviedb.org"))
        self.assertTrue(self.dns._covered("image.tmdb.org"))
        self.assertFalse(self.dns._covered("nottmdb.org.evil.com"))


class WeightsTests(unittest.TestCase):
    def test_tmdb_takes_part_next_to_other_sources(self) -> None:
        from engine.slides.source_mix import allot, base_weights

        w = base_weights("person", video=False)
        self.assertGreaterEqual(w["tmdb"], 2)
        self.assertNotIn("tmdb", base_weights("person", video=True), "видео у TMDB нет")
        self.assertNotIn("tmdb", base_weights("nature", video=False))
        quotas = allot(base_weights("other", video=False), ["google", "stocks", "tmdb"], 6)
        self.assertGreaterEqual(quotas.get("tmdb", 0), 1, "у доступного источника всегда есть место")
        no_key = allot(base_weights("other", video=False), ["google", "stocks"], 6)
        self.assertNotIn("tmdb", no_key, "ключа нет — источник не участвует")


if __name__ == "__main__":
    unittest.main()
