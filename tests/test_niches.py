"""Поиск ниш: каналы-ориентиры — небольшие (потолок подписчиков + 15%), просмотры стабильно в 3+ раза больше
подписчиков или ролики «стреляют»; возраст канала и формат не важны; 40 вечнозелёных ниш; запросы; история."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import shutil
import time
import unittest
from unittest import mock

from engine.publish.store import publish_dir
from engine.research import niches

DAY = 86400


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def every(n: int, step_days: float, views: int, minutes: float = 20, start_days: float = 4) -> list[tuple]:
    """n роликов: самый свежий start_days назад, дальше каждые step_days (дней назад, просмотры, минуты)."""
    return [(start_days + k * step_days, views, minutes) for k in range(n)]


class FakeApi:
    """YouTube API: у каждого канала — найденный по запросу ролик и список загрузок (с пагинацией по 50)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.channels = {
            # молодой: создан 40 дней назад, 900 подписчиков, ролики по 150 тыс. — подходит
            "UCyoung": {"subs": 900, "created": 40, "found": (150_000, 25), "uploads": every(12, 3, 150_000)},
            # создан давно, 2 года молчал, 2 месяца назад ожил — подходит
            "UCrevived": {"subs": 5000, "created": 3000, "found": (300_000, 30),
                          "uploads": every(8, 7, 200_000) + every(4, 30, 3000, start_days=800)},
            # много подписчиков, просмотры ниже их — перекос
            "UCbig": {"subs": 100_000, "created": 30, "found": (120_000, 30), "uploads": every(10, 3, 20_000)},
            # стабильно ведётся годами — не «молодой»
            "UCsteady": {"subs": 2000, "created": 2000, "found": (400_000, 20), "uploads": every(50, 7, 300_000)},
            # одни Shorts
            "UCshort": {"subs": 300, "created": 10, "found": (500_000, 0.75), "uploads": every(5, 2, 500_000, 0.75)},
            # молодой, но просмотры нестабильные: один залёт, остальное — 5 тыс.
            "UCluck": {"subs": 800, "created": 50, "found": (900_000, 20),
                       "uploads": [(5, 900_000, 20)] + every(9, 4, 5000, start_days=9)},
        }
        self.extra_uploads = 0  # сколько роликов в загрузках «за кадром» (на канале больше, чем вернули)

    def close(self) -> None:
        pass

    def _video(self, vid: str) -> dict | None:
        cid, _, k = vid.partition(":")
        ch = self.channels.get(cid)
        if not ch:
            return None
        if k == "found":
            views, minutes, ago = *ch["found"], 5
        else:
            ago, views, minutes = ch["uploads"][int(k)]
        return {"id": vid, "snippet": {"title": f"Ролик {vid}", "channelId": cid, "publishedAt": iso(time.time() - ago * DAY)},
                "statistics": {"viewCount": str(views)}, "contentDetails": {"duration": f"PT{int(minutes * 60)}S"}}

    def get(self, path: str, params: dict) -> dict:
        self.calls.append((path, params))
        if path == "search":
            return {"items": [{"id": {"videoId": f"{cid}:found"}} for cid in self.channels]}
        if path == "videos":
            return {"items": [v for v in (self._video(i) for i in params["id"].split(",")) if v]}
        if path == "channels":
            return {"items": [{"id": cid, "snippet": {"title": cid, "publishedAt": iso(time.time() - c["created"] * DAY)},
                               "statistics": {"subscriberCount": str(c["subs"]), "videoCount": str(len(c["uploads"]) + self.extra_uploads)},
                               "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid}}}
                              for cid, c in self.channels.items() if cid in params["id"].split(",")]}
        if path == "playlistItems":
            cid = params["playlistId"][2:]
            ups = self.channels[cid]["uploads"]
            start = int(params.get("pageToken") or 0)
            page = ups[start:start + params["maxResults"]]
            out = {"items": [{"contentDetails": {"videoId": f"{cid}:{start + k}", "videoPublishedAt": iso(time.time() - ago * DAY)}}
                             for k, (ago, _v, _m) in enumerate(page)]}
            if start + len(page) < len(ups):
                out["nextPageToken"] = str(start + len(page))
            return out
        raise AssertionError(path)


class NicheTests(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(publish_dir(), ignore_errors=True)
        self.addCleanup(shutil.rmtree, publish_dir(), True)

    def _find(self, api: FakeApi, keywords=("истории о свекрови", "рассказы"), **flt):
        f = niches.Filters.of(flt)
        with mock.patch("engine.rewrite.channels._Api", lambda keys: api), \
                mock.patch("engine.publish.polls.ask", lambda *a, **k: "{}"):
            return niches.find("Жизненные истории", list(keywords), f, ["key"], None)

    def test_steady_views_far_above_subscribers_pass_any_age(self) -> None:
        res = self._find(FakeApi())
        got = {c["id"]: c for c in res["channels"]}
        self.assertEqual(set(got), {"UCyoung", "UCrevived", "UCsteady"})  # UCsteady ведётся годами — неважно
        self.assertEqual(got["UCyoung"]["recent_median"], 150_000)
        self.assertEqual(got["UCyoung"]["hit_share"], 1.0)
        self.assertTrue(got["UCyoung"]["anomaly"])  # 150 тыс. на ролик при 900 подписчиках
        self.assertNotIn("uploads", got["UCyoung"])
        self.assertNotIn("fit", got["UCyoung"])  # формат не оцениваем
        self.assertEqual(niches.runs()[0]["niche"], "Жизненные истории")

    def test_dropped_reasons(self) -> None:
        d = self._find(FakeApi())["stats"]["dropped"]
        self.assertEqual(d["short"], 1)  # шортс не берём вовсе
        self.assertNotIn("old", d)
        self.assertEqual(d["big"], 1)  # UCbig — 100 тыс. подписчиков при потолке 10 тыс.
        self.assertGreaterEqual(d["unstable"], 1)  # UCluck: один залёт, остальное по 5 тыс.
        d = self._find(FakeApi(), max_subs=0)["stats"]["dropped"]  # без потолка — отсеивается по просмотрам
        self.assertNotIn("big", d)
        self.assertGreaterEqual(d["low_ratio"], 1)

    def test_views_must_beat_subscribers_three_times(self) -> None:
        api = FakeApi()
        api.channels = {"UCx": {"subs": 9_000, "created": 30, "found": (300_000, 20), "uploads": every(10, 3, 25_000)}}
        self.assertEqual(self._find(api)["channels"], [])  # 25 тыс. при 9 тыс. подписчиков — всего ×2,8
        api.channels["UCx"]["uploads"] = every(10, 3, 30_000)  # ×3,3 и 30 тыс. — подходит
        self.assertEqual([c["id"] for c in self._find(api)["channels"]], ["UCx"])

    def test_subscriber_ceiling_with_small_slack(self) -> None:
        def chan(subs):
            return {"subs": subs, "created": 30, "found": (900_000, 20), "uploads": every(10, 3, 400_000)}

        api = FakeApi()
        api.channels = {"UC10k": chan(10_000), "UC11k": chan(11_000), "UC20k": chan(20_000)}
        self.assertEqual({c["id"] for c in self._find(api)["channels"]}, {"UC10k", "UC11k"})  # 11 тыс. — ещё да
        self.assertEqual({c["id"] for c in self._find(api, max_subs=20_000)["channels"]}, {"UC10k", "UC11k", "UC20k"})
        self.assertEqual(len(self._find(api, max_subs=0)["channels"]), 3)  # 0 — без потолка

    def test_breakout_videos_pass_with_modest_median(self) -> None:
        # как @TrofeiniyArhiv: 2,7 тыс. подписчиков, медиана ~26 тыс., но ролики по 280 и 270 тыс.
        api = FakeApi()
        ups = every(14, 4, 20_000)
        ups[3], ups[9] = (16, 280_000, 100), (40, 273_000, 120)
        api.channels = {"UCburst": {"subs": 2_700, "created": 5000, "found": (280_000, 100), "uploads": ups}}
        got = self._find(api)["channels"]
        self.assertEqual([c["id"] for c in got], ["UCburst"])
        self.assertEqual((got[0]["hit_count"], got[0]["top_views"]), (2, 280_000))
        ups[9] = (40, 40_000, 120)  # залёт один — это случайность, не ниша
        self.assertEqual(self._find(api)["channels"], [])

    def test_shorts_in_uploads_ignored(self) -> None:
        api = FakeApi()
        api.channels = {"UCx": {"subs": 1000, "created": 30, "found": (300_000, 20),
                                "uploads": every(10, 1, 900_000, 0.5) + every(5, 3, 200_000, start_days=20)}}
        c = self._find(api)["channels"][0]
        self.assertEqual((c["recent_median"], c["recent_count"]), (200_000, 5))

    def test_fewer_videos_rank_higher(self) -> None:
        a = niches.Found("a", "A", subs=1000, videos=20, recent_median=200_000, hit_share=1.0)
        b = niches.Found("b", "B", subs=1000, videos=900, recent_median=200_000, hit_share=1.0)
        self.assertGreater(niches.score(a), niches.score(b))

    def test_results_capped_at_15(self) -> None:
        api = FakeApi()
        api.channels = {f"UC{k}": {"subs": 500, "created": 20, "found": (200_000, 20), "uploads": every(6, 3, 200_000)}
                        for k in range(20)}
        res = self._find(api)
        self.assertEqual(len(res["channels"]), 15)
        self.assertEqual(res["stats"]["passed"], 20)

    def test_search_uses_language_region_window_and_both_orders(self) -> None:
        api = FakeApi()
        with mock.patch("engine.publish.polls.ask", return_value='{"queries": ["mother in law stories"]}'):
            f = niches.Filters.of({"language": "es"})
            with mock.patch("engine.rewrite.channels._Api", lambda keys: api):
                niches.find("Свекровь", ["истории о свекрови"], f, ["key"], None)
        searches = [p for path, p in api.calls if path == "search"]
        self.assertEqual({(p["order"], p["videoDuration"]) for p in searches},
                         {("relevance", "medium"), ("relevance", "long"), ("viewCount", "long")})
        self.assertEqual({p["q"] for p in searches}, {"mother in law stories"})  # русские слова для испанцев не ищем
        self.assertEqual((searches[0]["relevanceLanguage"], searches[0]["regionCode"]), ("es", "ES"))
        self.assertIn("publishedAfter", searches[0])

    def test_hidden_subscribers_never_pass(self) -> None:
        api = FakeApi()
        orig = api.get

        def get(path, params):
            data = orig(path, params)
            if path == "channels":
                for ch in data["items"]:
                    ch["statistics"] = {"hiddenSubscriberCount": True, "videoCount": "20"}
            return data

        api.get = get
        res = self._find(api)
        self.assertEqual(res["channels"], [])
        self.assertEqual(res["stats"]["dropped"]["hidden"], 5)

    def test_evergreen_has_40_niches_with_keywords(self) -> None:
        self.assertEqual(len(niches.EVERGREEN), 40)
        self.assertEqual(len({n["name"] for n in niches.EVERGREEN}), 40)
        self.assertTrue(all(len(n["keywords"]) >= 3 and n["why"] for n in niches.EVERGREEN))

    def test_queries_keep_keywords_and_ai_adds_more(self) -> None:
        with mock.patch("engine.publish.polls.ask", return_value='{"queries": ["доп запрос"]}'):
            q = niches.queries_for(["а", "б"], "ru", None, None)
        self.assertEqual(q, ["а", "б", "доп запрос"])
        with mock.patch("engine.publish.polls.ask", side_effect=RuntimeError("нет")):
            self.assertEqual(niches.queries_for(["а", "б", "а"], "en", None, None), ["а", "б"])  # без нейросети — как есть

    def test_history_keeps_last_20_and_remove(self) -> None:
        for k in range(25):
            niches.save({"id": str(k), "niche": str(k), "channels": [{"id": "UC1"}, {"id": "UC2"}]})
        self.assertEqual(len(niches.runs()), 20)
        self.assertEqual(niches.runs()[0]["niche"], "24")
        self.assertTrue(niches.remove("24", "UC1"))
        self.assertEqual([c["id"] for c in niches.runs()[0]["channels"]], ["UC2"])
        self.assertFalse(niches.remove("24", "UC1"))
        self.assertTrue(niches.remove("24"))
        self.assertEqual(niches.runs()[0]["niche"], "23")
        self.assertFalse(niches.remove("nope"))

    def test_old_runs_get_id_from_time(self) -> None:
        niches.save({"niche": "старый", "at": 1794817319.895344, "channels": []})
        rid = niches.runs()[0]["id"]
        self.assertEqual(rid, "1794817319.895344")
        self.assertTrue(niches.remove(rid))

    def test_filters_tolerate_old_and_bad_values(self) -> None:
        f = niches.Filters.of({"max_subs": "5000", "results": 99, "judge": "false", "language": "en"})
        self.assertEqual((f.results, f.language, f.max_subs), (30, "en", 5000))  # лишние поля просто игнорируются
        self.assertEqual(f.subs_limit, 5750)
        self.assertEqual((niches.Filters().results, niches.Filters().max_subs), (15, 10_000))
        self.assertEqual(niches.Filters.of({"max_subs": -5}).subs_limit, 0)

    def test_new_search_languages_have_regions(self) -> None:
        for lang, region in (("ro", "RO"), ("nl", "NL"), ("uk", "UA")):
            api = FakeApi()
            with mock.patch("engine.publish.polls.ask", return_value='{"queries": ["q"]}'), \
                    mock.patch("engine.rewrite.channels._Api", lambda keys: api):
                niches.find("Н", ["слово"], niches.Filters.of({"language": lang}), ["key"], None)
            first = next(p for path, p in api.calls if path == "search")
            self.assertEqual((first["relevanceLanguage"], first["regionCode"]), (lang, region))

    def test_foreign_language_without_translation_warns(self) -> None:
        api = FakeApi()
        with mock.patch("engine.publish.polls.ask", side_effect=RuntimeError("нет нейросети")), \
                mock.patch("engine.rewrite.channels._Api", lambda keys: api):
            res = niches.find("Н", ["слово"], niches.Filters.of({"language": "ro"}), ["key"], None)
        self.assertTrue(res["notes"] and "ro" in res["notes"][0], "видно, что слова не переведены")
        self.assertEqual(res["queries"], ["слово"])
        with mock.patch("engine.publish.polls.ask", side_effect=RuntimeError("нет")), \
                mock.patch("engine.rewrite.channels._Api", lambda keys: FakeApi()):
            ru = niches.find("Н", ["слово"], niches.Filters.of({"language": "ru"}), ["key"], None)
        self.assertNotIn("notes", ru, "русский — перевод не нужен, предупреждения нет")

    def test_no_keys_is_error(self) -> None:
        with self.assertRaises(ValueError):
            niches.find("Ниша", [], niches.Filters(), [], None)


class QuotaSavingTests(unittest.TestCase):
    """Квота поиска: кэш и бесплатный поиск, когда она кончилась."""

    def _find(self, api, **flt):
        f = niches.Filters.of(flt)
        with mock.patch("engine.rewrite.channels._Api", lambda keys: api), \
                mock.patch("engine.publish.polls.ask", lambda *a, **k: "{}"):
            return niches.find("Жизненные истории", ["истории"], f, ["key"], None)

    def test_exhausted_quota_switches_to_free_search(self) -> None:
        from engine.rewrite.channels import KeysExhausted

        api = FakeApi()
        orig = api.get

        def get(path, params):
            if path == "search":
                raise KeysExhausted("ключи YouTube API не работают — …AAAA: суточная квота исчерпана")
            return orig(path, params)

        api.get = get
        asked: list[tuple] = []

        def free(q, order, duration, **kw):
            asked.append((q, order, duration))
            return [f"{cid}:found" for cid in api.channels]

        with mock.patch("engine.research.free_search.search_ids", free):
            res = self._find(api)
        self.assertEqual(len(asked), 3, "по одному запросу — три прохода, без обращений к API-поиску")
        self.assertTrue(res["channels"], "каналы найдены без квоты")

    def test_free_search_empty_reports_keys_problem(self) -> None:
        from engine.rewrite.channels import KeysExhausted

        api = FakeApi()
        api.get = lambda path, params: (_ for _ in ()).throw(KeysExhausted("квота")) if path == "search" else {}
        with mock.patch("engine.research.free_search.search_ids", lambda *a, **k: []):
            with self.assertRaises(KeysExhausted):
                self._find(api)

    def test_free_search_drops_videos_older_than_window(self) -> None:
        from engine.rewrite.channels import KeysExhausted

        api = FakeApi()
        orig = api.get

        def get(path, params):
            if path == "search":
                raise KeysExhausted("квота")
            data = orig(path, params)
            if path == "videos":  # у «found»-ролика UCyoung дата — год назад: за окном в полгода
                for v in data["items"]:
                    if v["id"] == "UCyoung:found":
                        v["snippet"]["publishedAt"] = iso(time.time() - 400 * DAY)
            return data

        api.get = get
        with mock.patch("engine.research.free_search.search_ids", lambda *a, **k: [f"{c}:found" for c in api.channels]):
            res = self._find(api)
        self.assertNotIn("UCyoung", {c["id"] for c in res["channels"]})

    def test_sp_param_filters(self) -> None:
        from engine.research import free_search

        self.assertEqual(free_search.sp_param("relevance", "medium"), "EgYIBRABGAM%3D")
        self.assertEqual(free_search.sp_param("relevance", "long"), "EgYIBRABGAI%3D")
        self.assertEqual(free_search.sp_param("viewCount", "long"), "CAMSBggFEAEYAg%3D%3D")

    def test_search_cache_saves_repeat(self) -> None:
        import httpx

        from engine.rewrite.channels import _Api

        env = mock.patch.dict("os.environ", {"STORY_VIDEO_YT_CACHE_OFF": "0"})
        env.start()
        self.addCleanup(env.stop)
        api = _Api(["KEY1"])
        calls = []

        def fake_get(url, params):
            calls.append(params.get("q"))
            return httpx.Response(200, json={"items": [{"id": {"videoId": "x"}}]})

        try:
            with mock.patch.object(api.http, "get", side_effect=fake_get):
                p = {"part": "snippet", "q": "кэш-тест-уникальный", "order": "relevance"}
                a = api.get("search", p)
                b = api.get("search", dict(p))
                api.get("channels", {"id": "1"})
                api.get("channels", {"id": "1"})
        finally:
            api.close()
        self.assertEqual(a, b)
        self.assertEqual(calls.count("кэш-тест-уникальный"), 1)
        self.assertEqual(len(calls), 3, "channels кэшем не пользуется")

    def test_search_quota_does_not_kill_keys_for_other_requests(self) -> None:
        import httpx

        from engine.rewrite.channels import KeysExhausted, _Api

        api = _Api(["KEY1"])

        def fake_get(url, params):
            if url.endswith("/search"):
                return httpx.Response(429, json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}})
            return httpx.Response(200, json={"items": [1]})

        try:
            with mock.patch.object(api.http, "get", side_effect=fake_get):
                with self.assertRaises(KeysExhausted):
                    api.get("search", {"q": "ключ-тест-2"})
                self.assertEqual(api.get("videos", {"id": "x"}), {"items": [1]}, "videos.list на этом ключе ещё работает")
        finally:
            api.close()


class SourceApi(FakeApi):
    """FakeApi + канал-образец UCsource: его читают по @handle, id или ссылке на ролик."""

    SRC_ID = "UC" + "s" * 22

    def __init__(self) -> None:
        super().__init__()
        self.channels["UCsource"] = {"subs": 800, "created": 60, "found": (150_000, 25), "uploads": every(10, 3, 150_000)}
        self.source_found = True

    def _src(self) -> dict:
        return {"id": self.SRC_ID, "snippet": {"title": "Образец", "description": "Судьбы актрис", "defaultLanguage": "ru"},
                "statistics": {"subscriberCount": "800", "videoCount": "10"},
                "brandingSettings": {"channel": {"keywords": 'актрисы "судьбы звёзд" кино'}},
                "contentDetails": {"relatedPlaylists": {"uploads": "UUUCsource"}}}

    def get(self, path: str, params: dict) -> dict:
        if path == "channels" and ("forHandle" in params or params.get("id") == self.SRC_ID):
            self.calls.append((path, params))
            return {"items": [self._src()] if self.source_found else []}
        if path == "videos" and params["id"] == "abcdefghijk":
            self.calls.append((path, params))
            return {"items": [{"id": "abcdefghijk", "snippet": {"channelId": self.SRC_ID}}]}
        return super().get(path, params)


class SimilarTests(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(publish_dir(), ignore_errors=True)
        self.addCleanup(shutil.rmtree, publish_dir(), True)

    def _similar(self, api, url="https://www.youtube.com/@obrazets", answer=None, **kw):
        # исходные ролики образца: у FakeApi канал UCsource — id «UCsource», а образец отдаётся как SRC_ID
        orig = api._video

        def video(vid):
            return orig(vid.replace(api.SRC_ID, "UCsource"))

        api._video = video
        api.channels[api.SRC_ID] = api.channels["UCsource"]
        ask = mock.MagicMock(return_value=answer if answer is not None else
                             json.dumps({"topic": "судьбы актрис", "language": "ru", "queries": ["судьба актрисы", "актрисы СССР"]},
                                        ensure_ascii=False))
        with mock.patch("engine.rewrite.channels._Api", lambda keys: api), mock.patch("engine.publish.polls.ask", ask):
            res = niches.find_similar(url, niches.Filters.of(kw.pop("flt", {})), ["key"], None, **kw)
        return res, ask

    def test_same_criteria_and_source_excluded(self) -> None:
        res, _ask = self._similar(SourceApi())
        ids = [c["id"] for c in res["channels"]]
        self.assertNotIn(SourceApi.SRC_ID, ids, "образец в выдаче быть не должен")
        self.assertIn("UCyoung", ids)
        self.assertNotIn("UCbig", ids, "критерии те же: просмотры ниже подписчиков — мимо")
        self.assertNotIn("UCshort", ids)
        self.assertEqual(res["similar"]["title"], "Образец")
        self.assertEqual(res["similar"]["topic"], "судьбы актрис")
        self.assertEqual(res["niche"], "Похожие на «Образец»")
        self.assertEqual(niches.runs()[0]["similar"]["id"], SourceApi.SRC_ID, "сохранено в историю")

    def test_queries_are_model_queries_plus_best_video_titles(self) -> None:
        api = SourceApi()
        res, ask = self._similar(api)
        self.assertEqual(res["queries"][:2], ["судьба актрисы", "актрисы СССР"])
        self.assertEqual(len(res["queries"]), 2 + niches.TITLE_QUERIES)
        self.assertTrue(all(q.startswith("Ролик ") for q in res["queries"][2:]), "названия лучших роликов образца")
        self.assertLessEqual(len(res["queries"]), niches.MAX_QUERIES)
        self.assertIn("судьбы звёзд", ask.call_args.args[1], "ключевые слова канала уходят нейросети")

    def test_language_from_source_unless_chosen(self) -> None:
        answer = json.dumps({"topic": "t", "language": "es", "queries": ["historias"]})
        api = SourceApi()
        res, _ = self._similar(api, answer=answer)
        searches = [p for path, p in api.calls if path == "search"]
        self.assertEqual((searches[0]["relevanceLanguage"], searches[0]["regionCode"]), ("es", "ES"))
        self.assertEqual(res["similar"]["language"], "es")
        api2 = SourceApi()
        res2, _ = self._similar(api2, answer=answer, auto_language=False, flt={"language": "de"})
        self.assertEqual(res2["similar"]["language"], "de", "язык выбрал пользователь — он главнее")

    def test_chosen_language_gets_queries_and_titles_in_that_language(self) -> None:
        """Образец на одном языке, человек выбрал другой: названия роликов образца на его языке в поиск не идут."""
        answer = json.dumps({"topic": "t", "language": "en", "queries": ["vidas de actrices", "actrices clasicas"],
                             "titles": ["Titulo uno", "Titulo dos", "Titulo tres"]})
        api = SourceApi()
        res, ask = self._similar(api, answer=answer, auto_language=False, flt={"language": "es"})
        self.assertEqual(res["queries"], ["vidas de actrices", "actrices clasicas", "Titulo uno", "Titulo dos", "Titulo tres"])
        self.assertFalse(any(q.startswith("Ролик ") for q in res["queries"]), "оригиналы названий — другой язык")
        self.assertIn("Язык поиска (ЗАДАН): es", ask.call_args.args[1])
        searches = [p for path, p in api.calls if path == "search"]
        self.assertEqual(searches[0]["relevanceLanguage"], "es")

    def test_chosen_language_without_translation_is_an_honest_error(self) -> None:
        with self.assertRaisesRegex(niches.SimilarError, "перевести запросы"):
            self._similar(SourceApi(), answer="не json", auto_language=False, flt={"language": "es"})

    def test_url_forms(self) -> None:
        for url in ("https://www.youtube.com/@obrazets", f"https://www.youtube.com/channel/{SourceApi.SRC_ID}",
                    "https://youtu.be/abcdefghijk", "https://www.youtube.com/watch?v=abcdefghijk&t=5s"):
            res, _ = self._similar(SourceApi(), url=url)
            self.assertEqual(res["similar"]["id"], SourceApi.SRC_ID, url)

    def test_bad_input_errors_are_human(self) -> None:
        api = SourceApi()
        with mock.patch("engine.rewrite.channels._Api", lambda keys: api):
            with self.assertRaisesRegex(niches.SimilarError, "Вставь ссылку"):
                niches.find_similar("  ", niches.Filters(), ["k"], None)
            with self.assertRaisesRegex(niches.SimilarError, "не понял ссылку"):
                niches.find_similar("https://example.com/x", niches.Filters(), ["k"], None)
            api.source_found = False
            with self.assertRaisesRegex(niches.SimilarError, "не найден"):
                niches.find_similar("https://www.youtube.com/@net", niches.Filters(), ["k"], None)
        with self.assertRaises(ValueError):
            niches.find_similar("https://www.youtube.com/@x", niches.Filters(), [], None)

    def test_without_llm_uses_titles_and_keywords(self) -> None:
        api = SourceApi()
        res, _ = self._similar(api, answer="")  # нейросеть вернула пустое
        self.assertTrue(res["queries"], "запросы есть и без нейросети")
        self.assertTrue(any(q.startswith("Ролик ") for q in res["queries"]))
        self.assertIn("судьбы звёзд", res["queries"], "фразы из ключевых слов канала")

    def test_source_without_long_videos_is_error(self) -> None:
        api = SourceApi()
        api.channels["UCsource"]["uploads"] = every(5, 2, 100_000, 0.5)  # одни Shorts
        with self.assertRaisesRegex(niches.SimilarError, "нет длинных роликов"):
            self._similar(api)

    def test_job_runner_wires_similar_mode(self) -> None:
        from engine.core.cancel import CancelToken
        from server import runners

        with self.assertRaisesRegex(ValueError, "YouTube Data API"):
            runners._niches_job({}, {"similar_url": "https://www.youtube.com/@x"}, {})
        seen = {}

        def fake(url, f, keys, llm, **kw):
            seen.update(url=url, lang=f.language, max_subs=f.max_subs, auto=kw["auto_language"])
            return {"channels": [], "stats": {"videos": 1, "channels": 1, "checked": 0, "passed": 0},
                    "similar": {"title": "Образец"}}

        cfg = {"youtube_api_keys": "k1"}
        with mock.patch.object(niches, "find_similar", fake):
            title, run = runners._niches_job(cfg, {"similar_url": "https://www.youtube.com/@x",
                                                   "filters": {"language": "auto", "max_subs": 5000}}, {})
            out = run(None, lambda *a: None, CancelToken())
            self.assertTrue(title.startswith("Поиск ниш: похожие"))
            self.assertEqual((seen["auto"], seen["max_subs"]), (True, 5000))
            self.assertIn("Образец", out["summary"])
            _t, run2 = runners._niches_job(cfg, {"similar_url": "u", "filters": {"language": "de"}}, {})
            run2(None, lambda *a: None, CancelToken())
            self.assertEqual((seen["auto"], seen["lang"]), (False, "de"), "язык выбран вручную — авто выключено")
        with mock.patch.object(niches, "find_similar", side_effect=niches.SimilarError("Канал не найден: u")):
            _t, run3 = runners._niches_job(cfg, {"similar_url": "u"}, {})
            with self.assertRaisesRegex(ValueError, "Канал не найден"):
                run3(None, lambda *a: None, CancelToken())


class FavoritesTests(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(publish_dir(), ignore_errors=True)
        self.addCleanup(shutil.rmtree, publish_dir(), True)
        niches.save({"id": "r1", "at": 1000.0, "niche": "Судьбы", "filters": {"language": "es"}, "queries": [], "stats": {},
                     "channels": [{"id": "UC1", "title": "Один", "subs": 100}, {"id": "UC2", "title": "Два", "subs": 200}]})

    def test_added_channel_survives_history_removal_until_deleted_by_hand(self) -> None:
        self.assertTrue(niches.add_favorite("r1", "UC1"))
        niches.remove("r1")  # поиск ушёл из истории
        favs = niches.favorites()
        self.assertEqual([f["id"] for f in favs], ["UC1"], "в избранном остался")
        self.assertEqual((favs[0]["title"], favs[0]["found_in"], favs[0]["language"]), ("Один", "Судьбы", "es"))
        self.assertTrue(niches.remove_favorite("UC1"))
        self.assertEqual(niches.favorites(), [])
        self.assertFalse(niches.remove_favorite("UC1"))

    def test_twice_keeps_one_record_and_unknown_channel_is_refused(self) -> None:
        self.assertTrue(niches.add_favorite("r1", "UC1"))
        first = niches.favorites()[0]["saved_at"]
        self.assertTrue(niches.add_favorite("r1", "UC1"))
        self.assertEqual(len(niches.favorites()), 1)
        self.assertEqual(niches.favorites()[0]["saved_at"], first, "дата добавления не сбивается")
        self.assertFalse(niches.add_favorite("r1", "нет"))
        self.assertFalse(niches.add_favorite("нет", "UC1"))

    def test_newest_first_and_api(self) -> None:
        from fastapi.testclient import TestClient

        from server.app import app

        client = TestClient(app)
        self.assertEqual(client.post("/api/niches/favorites", json={"run_id": "r1", "channel_id": "UC1"}).status_code, 200)
        out = client.post("/api/niches/favorites", json={"run_id": "r1", "channel_id": "UC2"}).json()["favorites"]
        self.assertEqual([f["id"] for f in out], ["UC2", "UC1"])
        self.assertEqual(client.post("/api/niches/favorites", json={"run_id": "r1", "channel_id": "x"}).status_code, 404)
        self.assertEqual(client.delete("/api/niches/favorites/UC2").json()["favorites"][0]["id"], "UC1")
        self.assertEqual(client.delete("/api/niches/favorites/UC2").status_code, 404)


if __name__ == "__main__":
    unittest.main()
