"""AI-кадры RoyalTechno: клиент по правилам API, выбор слабых кадров, лимит доли, без двойной оплаты."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.slides import ai_gen
from engine.slides.ai_fill import fill_pool, fill_slots
from engine.slides.ai_gen import AiConfig, GenTask
from engine.slides.story_context import StoryContext
from engine.slides.theme_plan import ThemePlan

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 4000
MP4 = b"\0\0\0\x18ftypmp42" + b"\0" * 4000


class FakeRoyal:
    def __init__(self, *, fail_kind: str = "", status_on_post: int = 201, polls_until_done: int = 2) -> None:
        self.posts: list[dict] = []
        self.idem: list[str] = []
        self.jobs: dict[str, dict] = {}
        self.fail_kind = fail_kind
        self.status_on_post = status_on_post
        self.polls_until_done = polls_until_done
        self.conditional = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.url.host == "cdn.example":
            return httpx.Response(200, content=MP4 if path.endswith(".mp4") else PNG)
        if path == "/v1/account":
            return httpx.Response(200, json={"balance_usd": "$5.00", "limits": {"recommended_max_in_flight": 3}})
        if path == "/v1/public/pricing":
            return httpx.Response(200, json={"models": ai_gen.FALLBACK_MODELS})
        if path == "/v1/jobs" and req.method == "POST":
            if self.status_on_post != 201:
                return httpx.Response(self.status_on_post, json={"error": {"code": "insufficient_balance", "message": "no money"}})
            body = json.loads(req.content)
            self.posts.append(body)
            self.idem.append(req.headers.get("Idempotency-Key", ""))
            jid = f"job_{len(self.jobs)}"
            kind = "video" if body["model"] in ("veo-3.1", "seedance-2.0-fast") else "image"
            self.jobs[jid] = {"id": jid, "status": "queued", "polls": 0, "kind": kind}
            return httpx.Response(201, json={"id": jid, "status": "queued"})
        if path.startswith("/v1/jobs/") and req.method == "GET":
            jid = path.rsplit("/", 1)[1]
            job = self.jobs[jid]
            if req.headers.get("If-None-Match") == f'"{jid}-{job["polls"]}"' and job["polls"] < 1:
                self.conditional += 1
                return httpx.Response(304)
            job["polls"] += 1
            if job["polls"] >= self.polls_until_done:
                if self.fail_kind == job["kind"]:
                    job["status"] = "failed"
                    return httpx.Response(200, json={"id": jid, "status": "failed", "error": {"message": "content policy"}})
                ext = "mp4" if job["kind"] == "video" else "png"
                return httpx.Response(200, json={"id": jid, "status": "succeeded", "cost_usd_cents": 2,
                                                 "output": {"type": job["kind"], "url": f"https://cdn.example/{jid}.{ext}"}},
                                      headers={"ETag": f'"{jid}-{job["polls"]}"'})
            return httpx.Response(200, json={"id": jid, "status": "processing"}, headers={"ETag": f'"{jid}-{job["polls"]}"'})
        if path.endswith("/cancel"):
            return httpx.Response(200, json={})
        return httpx.Response(404)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeRoyal()
        self._use(self.fake)
        ai_gen._catalog_cache = None
        fast = mock.patch.dict(ai_gen.POLL_SEC, {"image": 0.01, "video": 0.01})
        fast.start()
        self.addCleanup(fast.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "pool").mkdir()

    def _use(self, fake) -> None:
        p = mock.patch.object(ai_gen, "_TRANSPORT", httpx.MockTransport(fake))
        p.start()
        self.addCleanup(p.stop)

    def gen(self, tasks, cfg=None, **kw):
        cfg = cfg or AiConfig(api_key="rt_live_x", image_model="nano-banana-pro-2k", video_model="veo-3.1-1080p")
        return ai_gen.generate(cfg, tasks, state_file=self.dir / "ai_jobs.json", poll_scale=0.001, **kw)


class CatalogTests(Base):
    def test_options_have_prices_and_variants(self) -> None:
        opts = ai_gen.model_options()
        self.assertTrue(any("2K" in o["label"] and "$0.04" in o["label"] for o in opts["image"]))
        self.assertTrue(any("Veo 3.1 1080p" in o["label"] for o in opts["video"]))
        grok = next(o for o in opts["video"] if o["id"] == "grok-imagine-1.5")
        self.assertTrue(grok["low_res"])

    def test_variant_to_request_fields(self) -> None:
        e = {m["model_id"]: m for m in ai_gen.FALLBACK_MODELS}
        self.assertEqual(ai_gen.resolution_of(e["veo-3.1-1080p"]), "1080p")
        self.assertEqual(ai_gen.resolution_of(e["nano-banana-pro-4k"]), "4k")
        self.assertEqual(ai_gen.resolution_of(e["grok-imagine-1.5-720p"]), "", "720p — отдельная модель, не resolution")
        self.assertIsNone(ai_gen.clip_seconds(e["veo-3.1-lite"]), "Veo — фиксированные 8 с")
        self.assertEqual(ai_gen.clip_seconds(e["omni-flash"]), 6, "omni: 4/6/8/10 — берём 6")
        self.assertEqual(ai_gen.price_of(e["seedance-fast"]), 45)


class GenerateTests(Base):
    def _tasks(self, n=4):
        return [GenTask(key=f"slot:{i}", kind="video" if i % 2 else "image", prompt=f"steppe {i}",
                        dest=self.dir / "pool" / f"ai_{i}.{'mp4' if i % 2 else 'png'}") for i in range(n)]

    def test_generates_downloads_and_respects_window(self) -> None:
        tasks = self.gen(self._tasks(5))
        self.assertTrue(all(t.result and t.result.stat().st_size > 1000 for t in tasks))
        body = self.fake.posts[0]
        self.assertEqual(body["model"], "nano-banana-pro")
        self.assertEqual(body["input"]["resolution"], "2k")
        self.assertEqual(body["input"]["aspect_ratio"], "landscape")
        vid = self.fake.posts[1]
        self.assertEqual((vid["model"], vid["input"]["resolution"]), ("veo-3.1", "1080p"))
        self.assertNotIn("duration_sec", vid["input"], "Veo: фиксированная длина")
        self.assertEqual(len(set(self.fake.idem)), 5, "свой Idempotency-Key на каждую задачу")

    def test_resume_does_not_pay_twice(self) -> None:
        tasks = self.gen(self._tasks(3))
        self.assertEqual(len(self.fake.posts), 3)
        again = self.gen(self._tasks(3))
        self.assertEqual(len(self.fake.posts), 3, "после сбоя/повтора — те же задачи, без новых отправок")
        self.assertTrue(all(t.result for t in again))

    def test_failed_job_keeps_live_frame(self) -> None:
        fake = FakeRoyal(fail_kind="video")
        self._use(fake)
        tasks = self.gen(self._tasks(2))
        self.assertIsNotNone(tasks[0].result)
        self.assertIsNone(tasks[1].result)
        self.assertIn("content policy", tasks[1].error)

    def test_no_balance_stops_submitting(self) -> None:
        fake = FakeRoyal(status_on_post=402)
        self._use(fake)
        tasks = self.gen(self._tasks(4))
        self.assertTrue(all(t.result is None and "402" in t.error for t in tasks))


PLAN = ThemePlan(summary="Mongolia documentary", context=StoryContext(era="modern day", place="Mongolia", subject="place"),
                 video_queries=["Mongolian steppe horses"], image_queries=["yurt in the steppe"])


class FillTests(Base):
    def test_slots_only_weak_and_holes_within_share(self) -> None:
        slots = [{"kind": "video", "path": f"pool/v{i}.mp4", "fit": 8, "query": f"q{i}", "start": i * 4, "end": i * 4 + 4, "duration": 4}
                 for i in range(50)]
        slots[3].update(fit=2, weak=True)
        slots[7].update(fallback=True, fit=8)
        slots[10].update(fit=5)
        for i in (20, 21, 22, 23, 24, 25, 26, 27):
            slots[i].update(fit=4)
        cfg = AiConfig(api_key="k", image_model="nano-banana-pro", video_model="")
        out = fill_slots(slots, PLAN, cfg, pool=self.dir / "pool")
        ai = [i for i, s in enumerate(out) if s.get("provider") == "ai"]
        self.assertEqual(len(ai), 6, "12% от 50 кадров")
        self.assertIn(7, ai, "дыра — первой")
        self.assertIn(3, ai, "самый слабый")
        self.assertTrue(all(out[i]["kind"] == "image" for i in ai), "видео-модель не выбрана — фото")
        self.assertEqual(out[0]["path"], "pool/v0.mp4", "хорошие живые кадры не трогаем")
        self.assertIn("Mongolia", self.fake.posts[0]["input"]["prompt"])
        self.assertIn("No text", self.fake.posts[0]["input"]["prompt"])

    def test_zero_balance_video_still_built_from_live_frames(self) -> None:
        """Ноль на балансе (402): кадры остаются живыми, подбор не падает."""
        self._use(FakeRoyal(status_on_post=402))
        slots = [{"kind": "image", "path": f"pool/i{i}.jpg", "fit": 3 if i % 3 == 0 else 8, "text": "ВВП 12%"} for i in range(30)]
        slots[2]["data"] = "chart"
        out = fill_slots(slots, PLAN, AiConfig(api_key="k", image_model="nano-banana-pro"), pool=self.dir / "pool")
        self.assertEqual(out, slots)
        new_v, new_i = fill_pool([], slots[:10], PLAN, AiConfig(api_key="k", image_model="nano-banana-pro"),
                                 pool=self.dir / "pool", image_target=20, video_target=0)
        self.assertEqual(new_i, slots[:10])

    def test_network_down_does_not_break_video(self) -> None:
        def down(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("нет сети", request=req)

        self._use(down)
        slots = [{"kind": "image", "path": f"pool/i{i}.jpg", "fit": 2} for i in range(20)]
        out = fill_slots(slots, PLAN, AiConfig(api_key="k", image_model="nano-banana-pro"), pool=self.dir / "pool")
        self.assertEqual(out, slots)

    def test_bad_key_does_not_break_video(self) -> None:
        self._use(lambda req: httpx.Response(401, json={"error": {"message": "invalid key"}}))
        slots = [{"kind": "image", "path": f"pool/i{i}.jpg", "fit": 2} for i in range(20)]
        self.assertEqual(fill_slots(slots, PLAN, AiConfig(api_key="k", image_model="nano-banana-pro"), pool=self.dir / "pool"), slots)

    def test_no_weak_frames_nothing_generated(self) -> None:
        slots = [{"kind": "image", "path": f"pool/i{i}.jpg", "fit": 8} for i in range(30)]
        out = fill_slots(slots, PLAN, AiConfig(api_key="k", image_model="nano-banana-pro"), pool=self.dir / "pool")
        self.assertEqual(out, slots)
        self.assertEqual(self.fake.posts, [])

    def test_pool_fills_shortage_and_weak(self) -> None:
        images = [{"path": f"pool/i{i}.jpg", "score": 0.8, "query": "q"} for i in range(70)]
        images[5]["score"] = 0.3
        videos = [{"path": f"pool/v{i}.mp4", "score": 0.8, "duration": 6} for i in range(10)]
        cfg = AiConfig(api_key="k", image_model="nano-banana-pro", video_model="seedance-fast")
        new_v, new_i = fill_pool(videos, images, PLAN, cfg, pool=self.dir / "pool", image_target=80, video_target=12)
        ai_i = [x for x in new_i if x.get("provider") == "ai"]
        ai_v = [x for x in new_v if x.get("provider") == "ai"]
        self.assertLessEqual(len(ai_i) + len(ai_v), int(92 * 0.12))
        self.assertEqual(new_i[5].get("provider"), "ai", "слабое фото заменено")
        self.assertGreater(len(new_i), 70, "нехватка фото добрана")

    def test_runner_validation(self) -> None:
        from server.runners import _ai

        self.assertIsNone(_ai({"ai_gen_enabled": False}))
        with self.assertRaisesRegex(ValueError, "ключа"):
            _ai({"ai_gen_enabled": True, "ai_image_model": "nano-banana-pro"})
        with self.assertRaisesRegex(ValueError, "модель"):
            _ai({"ai_gen_enabled": True, "royaltechno_api_key": "k"})
        ai = _ai({"ai_gen_enabled": True, "royaltechno_api_key": "k", "ai_video_model": "veo-3.1-lite"})
        self.assertTrue(ai.active)


class DataBeatTests(Base):
    """Куски про цифры и графики: при AI-кадрах — обязательно инфографика."""

    def test_detect_numbers_not_years(self) -> None:
        from engine.slides.strict_media import is_data

        self.assertTrue(is_data("ВВП Монголии вырос на 12% за 10 лет"))
        self.assertTrue(is_data("Средняя зарплата — 500 долларов"))
        self.assertTrue(is_data("Population reached 3.4 million people"))
        self.assertFalse(is_data("В 1919 году он подписал договор"))
        self.assertFalse(is_data("Степь тянется до горизонта"))

    def test_queries_marked_by_llm_or_numbers(self) -> None:
        from engine.slides import strict_media as sm

        beats = [sm.Beat(0, 4, "Степь до горизонта"), sm.Beat(4, 8, "ВВП вырос на 12%"), sm.Beat(8, 12, "Экономика менялась")]
        client = mock.Mock()
        client.complete.return_value = json.dumps({"queries": ["steppe horizon", "Mongolia economy growth", "DATA: line chart of mining exports"]})
        q = sm._plan_queries(client, PLAN, beats, None, all_beats=True)
        self.assertEqual(q[0], "steppe horizon")
        self.assertEqual(q[1], sm.DATA_MARK + "Mongolia economy growth", "цифры в тексте")
        self.assertEqual(q[2], sm.DATA_MARK + "line chart of mining exports", "LLM ответила DATA")

    def test_data_slots_forced_first_even_if_live_frame_ok(self) -> None:
        slots = [{"kind": "image", "path": f"pool/i{i}.jpg", "fit": 8, "query": f"q{i}", "text": f"t{i}"} for i in range(20)]
        slots[4].update(data="bar chart of GDP growth", text="ВВП вырос на 12% и достиг 15,3 млрд")
        slots[9].update(data="pie chart of population", text="3.4 million people")
        slots[15].update(fit=2, weak=True)
        cfg = AiConfig(api_key="k", image_model="nano-banana-pro")
        out = fill_slots(slots, PLAN, cfg, pool=self.dir / "pool")
        ai = [i for i, s in enumerate(out) if s.get("provider") == "ai"]
        self.assertEqual(sorted(ai), [4, 9, 15][: len(ai)], ai)
        self.assertIn(4, ai)
        self.assertIn(9, ai)
        prompt = self.fake.posts[0]["input"]["prompt"]
        self.assertIn("infographic", prompt)
        self.assertIn("12%", prompt)
        self.assertIn("15,3B", prompt)
        self.assertNotIn("млрд", prompt)

    def test_data_beat_searches_live_and_keeps_mark(self) -> None:
        from engine.slides import strict_media as sm

        seen = []

        def fake_mixed(*a, **kw):
            seen.append(kw["video_query"])
            return {"kind": "image", "fit": 7, "weak": False}, 0, False

        with mock.patch.object(sm, "_pick_mixed", fake_mixed):
            hit, _r, _w = sm._pick_beat(None, {}, PLAN, sm.Beat(4, 8, "ВВП вырос на 12%"), sm.DATA_MARK + "bar chart of GDP",
                                        index=1, trials=Path("."), frames=Path("."), pool=Path("."), mix=mock.Mock())
        self.assertEqual(seen, ["bar chart of GDP"], "живой кадр ищется без пометки — на случай выключенных AI-кадров")
        self.assertEqual(hit["data"], "bar chart of GDP")

    def test_data_raises_cap_to_15_percent(self) -> None:
        slots = [{"kind": "image", "path": f"pool/i{i}.jpg", "fit": 8} for i in range(40)]
        for i in range(0, 40, 5):  # 8 кусков с цифрами при обычном лимите 12% = 4
            slots[i]["data"] = "chart"
        out = fill_slots(slots, PLAN, AiConfig(api_key="k", image_model="nano-banana-pro"), pool=self.dir / "pool")
        self.assertEqual(sum(1 for s in out if s.get("provider") == "ai"), 6, "15% от 40")


class ReportTests(Base):
    """Оповещение: AI-кадры не получились — сколько и почему, ролик при этом собран."""

    def _slots(self, n=20):
        return [{"kind": "image", "path": f"pool/i{i}.jpg", "fit": 2} for i in range(n)]

    def test_zero_balance_reported(self) -> None:
        from engine.slides.ai_fill import report_lines

        self._use(FakeRoyal(status_on_post=402))
        fill_slots(self._slots(), PLAN, AiConfig(api_key="k", image_model="nano-banana-pro"), pool=self.dir / "pool")
        lines, warnings = report_lines(self.dir)
        self.assertEqual(lines, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("сгенерировано 0 из 2", warnings[0])
        self.assertIn("не хватает денег", warnings[0])

    def test_service_down_reported(self) -> None:
        from engine.slides.ai_fill import report_lines

        def down(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused", request=req)

        self._use(down)
        fill_slots(self._slots(), PLAN, AiConfig(api_key="k", image_model="nano-banana-pro"), pool=self.dir / "pool")
        _lines, warnings = report_lines(self.dir)
        self.assertIn("сервис RoyalTechno недоступен", warnings[0])

    def test_partial_failure_and_success_lines(self) -> None:
        from engine.slides.ai_fill import report_lines

        self._use(FakeRoyal(fail_kind="video"))
        slots = [{"kind": "video" if i % 2 else "image", "path": f"pool/x{i}", "fit": 2} for i in range(30)]
        fill_slots(slots, PLAN, AiConfig(api_key="k", image_model="nano-banana-pro", video_model="veo-3.1-lite"), pool=self.dir / "pool")
        _lines, warnings = report_lines(self.dir)
        self.assertIn("из 3", warnings[0])
        self.assertIn("контент-политика", warnings[0])

    def test_all_ok_is_info_not_warning(self) -> None:
        from engine.slides.ai_fill import report_lines

        fill_slots(self._slots(), PLAN, AiConfig(api_key="k", image_model="nano-banana-pro"), pool=self.dir / "pool")
        self.assertEqual(report_lines(self.dir), (["AI-кадры: 2"], []))

    def test_job_result_carries_warning(self) -> None:
        from server.runners import _slides_result

        self._use(FakeRoyal(status_on_post=402))
        fill_slots(self._slots(), PLAN, AiConfig(api_key="k", image_model="nano-banana-pro"), pool=self.dir / "pool")
        render = mock.Mock(run_dir=self.dir, desktop_video=None, size_bytes=1e9, duration=600.0, video_file=self.dir / "final.mp4")
        res = _slides_result(render)
        self.assertIn("AI-кадры не все", res["summary"])
        self.assertIn("не хватает денег", res["warnings"][0])


class PipelineTests(unittest.TestCase):
    def test_theme_person_video_never_uses_ai(self) -> None:
        """Ролик про конкретного человека: только его настоящие кадры, AI не подмешиваем."""
        from engine.slides import theme_media as tm
        from engine.slides.media_pipeline import MediaRequest, MediaSourcesConfig

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "script.txt").write_text("текст", encoding="utf-8")
            plan = ThemePlan(summary="s", context=StoryContext(subject="person", characters=["Anda Călugăreanu"], era="1946-1992", person_era="photo"))
            req = MediaRequest(run_dir=run, keys=MediaSourcesConfig(), copy_preview_to_desktop=False, llm_api_key="k",
                               ai=AiConfig(api_key="k", image_model="nano-banana-pro"))
            photo = {"path": "pool/photo_00_google.jpg", "score": 1.0, "provider": "google", "id": "1", "query": "q"}
            with mock.patch.object(tm, "_llm", lambda r: mock.MagicMock()), \
                 mock.patch.object(tm, "ensure_theme_plan", lambda *a: plan), \
                 mock.patch.object(tm, "_build_clients", lambda keys: {}), \
                 mock.patch("engine.slides.person_media.collect_person_pool", return_value=([], [photo], 0, 0)), \
                 mock.patch("engine.slides.ai_fill.fill_pool", side_effect=AssertionError("AI в ролике про человека")):
                tm.collect_theme_pool(req)

    def test_theme_place_video_uses_ai(self) -> None:
        from engine.slides import theme_media as tm
        from engine.slides.media_pipeline import MediaRequest, MediaSourcesConfig

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "script.txt").write_text("текст", encoding="utf-8")
            req = MediaRequest(run_dir=run, keys=MediaSourcesConfig(), copy_preview_to_desktop=False, llm_api_key="k",
                               ai=AiConfig(api_key="k", image_model="nano-banana-pro"))
            img = {"path": "pool/i.jpg", "score": 0.4, "provider": "pexels", "id": "1", "query": "q"}
            called = []
            with mock.patch.object(tm, "_llm", lambda r: mock.MagicMock()), \
                 mock.patch.object(tm, "ensure_theme_plan", lambda *a: PLAN), \
                 mock.patch.object(tm, "_build_clients", lambda keys: {}), \
                 mock.patch.object(tm, "gather_pool", return_value=([], [img], 0, 0)), \
                 mock.patch("engine.slides.ai_fill.fill_pool", side_effect=lambda v, i, *a, **k: (called.append(1), (v, i))[1]):
                tm.collect_theme_pool(req)
            self.assertEqual(called, [1])


if __name__ == "__main__":
    unittest.main()
