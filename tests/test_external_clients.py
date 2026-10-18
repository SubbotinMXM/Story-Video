"""Клиенты внешних сервисов на подменённой сети: разбор ответов, ключи, повторы.

И оркестрация на имитациях: тема от LLM, запросы под куски, полный цикл и его продолжение.
"""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx

from engine.slides.stocks_base import KeyPool


def mock_http(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class StockClientsTests(unittest.TestCase):
    def test_pexels_next_key_on_limit_same_search(self) -> None:
        from engine.slides.stocks_pexels import PexelsClient

        used: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            key = req.headers["Authorization"]
            used.append(key)
            if key == "k1":
                return httpx.Response(429)
            return httpx.Response(200, json={"videos": [{
                "id": 7, "width": 1920, "height": 1080, "duration": 9, "image": "https://p/1.jpg",
                "video_files": [
                    {"link": "https://v/360.mp4", "width": 640, "height": 360, "file_type": "video/mp4"},
                    {"link": "https://v/1080.mp4", "width": 1920, "height": 1080, "file_type": "video/mp4"},
                    {"link": "https://v/4k.mp4", "width": 3840, "height": 2160, "file_type": "video/mp4"},
                ],
            }]})

        c = PexelsClient(KeyPool(["k1", "k2"]))
        c._http = mock_http(handler)
        got = c.search("city", prefer_video=True, per_page=5)
        self.assertEqual(used[:2], ["k1", "k2"])
        self.assertTrue(got, "на лимите ключа поиск должен повториться со следующим ключом")
        self.assertEqual(got[0].download_url, "https://v/1080.mp4")

    def test_pixabay_and_unsplash_next_key_on_limit(self) -> None:
        from engine.slides.stocks_pixabay import PixabayClient
        from engine.slides.stocks_unsplash import UnsplashClient

        def pix(req: httpx.Request) -> httpx.Response:
            if req.url.params.get("key") == "k1":
                return httpx.Response(429)
            return httpx.Response(200, json={"hits": [{"id": 1, "picture_id": "p", "videos": {"large": {"url": "https://v/l.mp4", "width": 1920, "height": 1080}}}]})

        def uns(req: httpx.Request) -> httpx.Response:
            if req.headers["Authorization"] == "Client-ID k1":
                return httpx.Response(401)
            return httpx.Response(200, json={"results": [{"id": "u", "width": 3000, "height": 2000, "urls": {"small": "https://s", "regular": "https://r"}}]})

        p = PixabayClient(KeyPool(["k1", "k2"]))
        p._http = mock_http(pix)
        self.assertTrue(p.search("x", prefer_video=True))
        u = UnsplashClient(KeyPool(["k1", "k2"]))
        u._http = mock_http(uns)
        self.assertTrue(u.search("x", prefer_video=False))

    def test_all_keys_limited_gives_empty_not_loop(self) -> None:
        from engine.slides.stocks_pexels import PexelsClient

        hits = []
        c = PexelsClient(KeyPool(["a", "b", "c"]))
        c._http = mock_http(lambda req: (hits.append(1), httpx.Response(429))[1])
        self.assertEqual(c.search("x", prefer_video=True), [])
        self.assertLessEqual(len(hits), 3 * 2, "не больше одного круга ключей на запрос")

    def test_pexels_bad_key_dropped(self) -> None:
        from engine.slides.stocks_pexels import PexelsClient

        pool = KeyPool(["bad"])
        c = PexelsClient(pool)
        c._http = mock_http(lambda req: httpx.Response(401))
        self.assertEqual(c.search("x", prefer_video=True), [])
        self.assertFalse(pool.available)

    def test_pixabay_prefers_1080(self) -> None:
        from engine.slides.stocks_pixabay import PixabayClient

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"hits": [{
                "id": 5, "duration": 10, "picture_id": "pic",
                "videos": {
                    "large": {"url": "https://v/l.mp4", "width": 1920, "height": 1080},
                    "medium": {"url": "https://v/m.mp4", "width": 1280, "height": 720},
                    "small": {"url": "https://v/s.mp4", "width": 960, "height": 540},
                },
            }]})

        c = PixabayClient(KeyPool(["k"]))
        c._http = mock_http(handler)
        got = [x for x in c.search("x", prefer_video=True) if x.kind == "video"]
        self.assertEqual(got[0].download_url, "https://v/l.mp4")

    def test_youtube_search_filters_and_license(self) -> None:
        from engine.slides.source_youtube import YouTubeClient

        seen: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen.update(dict(req.url.params))
            items = [
                {"id": {"videoId": "ok1"}, "snippet": {"title": "City &amp; lights", "channelTitle": "Drone Guy", "thumbnails": {}}},
                {"id": {"videoId": "bad"}, "snippet": {"title": "Official Trailer", "channelTitle": "Studio", "thumbnails": {}}},
            ]
            return httpx.Response(200, json={"items": items})

        yt = YouTubeClient("key")
        yt._http = mock_http(handler)
        got = yt.search("city", prefer_video=True)
        self.assertEqual(seen.get("videoLicense"), "creativeCommon")
        self.assertEqual([c.id for c in got], ["ok1"])
        self.assertEqual(got[0].meta["title"], "City & lights")

    def test_youtube_quota_error_returns_empty(self) -> None:
        from engine.slides.source_youtube import YouTubeClient

        yt = YouTubeClient("key")
        yt._http = mock_http(lambda req: httpx.Response(403, json={}))
        self.assertEqual(yt.search("x", prefer_video=True), [])


class LumeanClientTests(unittest.TestCase):
    def _client(self, handler):
        from engine.core.lumean import LumeanClient

        c = LumeanClient("key")
        c._http = httpx.Client(base_url=c.base_url, transport=httpx.MockTransport(handler))
        return c

    def test_order_body_overrides_voice_and_speed(self) -> None:
        bodies: list[dict] = []

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "GET":
                return httpx.Response(200, json={"id": "t", "config": {"tts_settings": {
                    "model_id": "eleven_v3", "voice_id": "TEMPLATE_VOICE", "voice_settings": {"speed": 1, "stability": 0.5}}}})
            bodies.append(json.loads(req.content))
            return httpx.Response(200, json={"id": "order-1", "status": "pending"})

        c = self._client(handler)
        self.assertEqual(c.create_tts_order(template_id="t", input_text="текст", voice_id="MY_VOICE", speed=1.1), "order-1")
        tts = bodies[0]["config_override"]["tts_settings"]
        self.assertEqual(tts["voice_id"], "MY_VOICE")
        self.assertEqual(tts["voice_settings"]["speed"], 1.1)
        self.assertTrue(tts["advanced_voice_settings"])
        self.assertEqual((c._last_tts_speed, c._last_tts_model), (1.1, "eleven_v3"))

    def test_speed_clamped(self) -> None:
        from engine.core.lumean import clamp_voice_speed

        self.assertEqual(clamp_voice_speed(5), 1.2)
        self.assertEqual(clamp_voice_speed(0.1), 0.7)
        self.assertEqual(clamp_voice_speed(None), 1.0)

    def test_wait_order_failed_and_ok(self) -> None:
        from engine.core.lumean import LumeanOrderFailed

        statuses = iter(["pending", "completed"])
        c = self._client(lambda req: httpx.Response(200, json={"id": "o", "status": next(statuses)}))
        self.assertEqual(c.wait_order("o", poll_sec=0.01)["status"], "completed")
        c2 = self._client(lambda req: httpx.Response(200, json={"id": "o", "status": "failed"}))
        with self.assertRaises(LumeanOrderFailed):
            c2.wait_order("o", poll_sec=0.01)

    def test_wait_timeout_grows_with_text(self) -> None:
        from engine.core.lumean import tts_wait_timeout_sec

        short, long = tts_wait_timeout_sec(500), tts_wait_timeout_sec(60000)
        self.assertGreaterEqual(short, 20 * 60)
        self.assertGreater(long, short)
        self.assertLessEqual(long, 90 * 60)


class FakeLLM:
    def __init__(self, answers: dict[str, str] | None = None, default: str = "{}") -> None:
        self.answers = answers or {}
        self.default = default
        self.labels: list[str] = []

    def complete(self, *, label: str, system: str, user: str, model=None) -> str:
        self.labels.append(label)
        for prefix, ans in self.answers.items():
            if label.startswith(prefix):
                return ans(user) if callable(ans) else ans
        return self.default

    def close(self) -> None:
        pass


class LlmPlanningTests(unittest.TestCase):
    CONTEXT = json.dumps({
        "era": "modern day", "place": "Iceland", "title": "Исландия", "characters": [],
        "visual_style": "nature", "subject": "place", "must_include": ["Iceland"], "avoid": [], "historical": False,
    })

    def test_theme_plan_parses_subject(self) -> None:
        from engine.slides.theme_plan import build_theme_plan

        llm = FakeLLM({
            "theme_context": "```json\n" + self.CONTEXT + "\n```",
            "theme_queries": json.dumps({"summary": "s", "video_queries": ["v1", "v2", "v3", "v4"], "image_queries": ["i1", "i2", "i3", "i4", "i5", "i6"]}),
        })
        plan = build_theme_plan("Текст про Исландию", llm)
        self.assertEqual(plan.context.subject, "place")
        self.assertEqual(plan.context.place, "Iceland")
        self.assertIn("v1", plan.video_queries)

    def test_garbage_llm_does_not_crash(self) -> None:
        from engine.slides.theme_plan import build_theme_plan

        plan = build_theme_plan("Текст", FakeLLM(default="не json вообще"))
        self.assertTrue(plan.video_queries or plan.image_queries, "должны быть запасные запросы")

    def test_beat_queries_parallel_batches_keep_order(self) -> None:
        from engine.slides import strict_media as sm
        from engine.slides.story_context import StoryContext
        from engine.slides.theme_plan import ThemePlan

        def per_batch(user: str) -> str:
            n = sum(1 for line in user.splitlines() if line[:1].isdigit())
            first = int(next(line for line in user.splitlines() if line[:1].isdigit()).split(".")[1].strip().split()[0][1:])
            return json.dumps({"queries": [f"q{first + i}" for i in range(n)]})

        beats = [sm.Beat(i * 6.0, i * 6.0 + 6, f"b{i} text") for i in range(40)]  # 20 в начале (2 мин), 20 после
        plan = ThemePlan(summary="s", context=StoryContext(), video_queries=["tv"], image_queries=["ti"])
        llm = FakeLLM({"beat_queries": per_batch})
        q = sm._plan_queries(llm, plan, beats, None)
        self.assertEqual(q[0], "q0")
        self.assertEqual(q[19], "q19")
        self.assertEqual(q[35], "ti", "без «весь ролик» после 2 мин — запрос темы")
        q_full = sm._plan_queries(llm, plan, beats, None, all_beats=True)
        self.assertEqual(q_full[35], "q35")
        self.assertEqual(sum(1 for l in llm.labels if l.startswith("beat_queries")), 2 + 3)


class FullPipelineOrchestrationTests(unittest.TestCase):
    """Какие стадии запускаются при новом ролике и при продолжении."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.run = Path(self.tmp.name) / "run"
        self.run.mkdir()
        self.addCleanup(self.tmp.cleanup)

    def _req(self, **kw):
        from engine.slides.full_pipeline import FullRunRequest
        from engine.slides.media_pipeline import MediaSourcesConfig
        from engine.slides.tts_pipeline import TtsRequest

        base = dict(tts=TtsRequest(text="текст"), llm_api_key="k", stock_keys=MediaSourcesConfig(), resume_dir=self.run)
        base.update(kw)
        return FullRunRequest(**base)

    def _run(self, req, *, files: list[str]):
        from engine.slides import full_pipeline as fp

        for name in files:
            p = self.run / name
            if name == "timeline.json":
                p.write_text(json.dumps({"mode": "strict_sync", "variant": "3min", "slots": [{}]}), encoding="utf-8")
            elif name == "final.mp4":
                p.write_bytes(b"x" * 2000)
            else:
                p.write_text("{}", encoding="utf-8")
        called: list[str] = []

        def stage(name):
            def fn(*a, **k):
                called.append(name)
                return mock.MagicMock(run_dir=self.run, language="ru", word_count=1, chunk_count=1, selected=1)
            return fn

        with mock.patch.object(fp, "run_tts_stage", stage("tts")), \
             mock.patch.object(fp, "run_whisper_stage", stage("whisper")), \
             mock.patch.object(fp, "run_chunks_stage", stage("chunks")), \
             mock.patch.object(fp, "run_media_stage", stage("media")), \
             mock.patch.object(fp, "run_render_stage", stage("render")), \
             mock.patch.object(fp, "load_tts_result", lambda d: mock.MagicMock(run_dir=self.run, language="ru", audio_file=self.run / "voice.mp3")), \
             mock.patch.object(fp, "load_whisper_result", lambda d: mock.MagicMock(word_count=1)), \
             mock.patch.object(fp, "load_chunks_result", lambda d: mock.MagicMock(chunk_count=1)), \
             mock.patch.object(fp, "load_media_result", lambda d: mock.MagicMock(selected=1)), \
             mock.patch("engine.slides.ffprobe_util.probe_duration", lambda p: 9.0):
            fp.run_full_pipeline(req)
        return called

    def test_new_run_runs_everything(self) -> None:
        self.assertEqual(self._run(self._req(), files=[]), ["tts", "whisper", "chunks", "media", "render"])

    def test_resume_after_media(self) -> None:
        got = self._run(self._req(strict_sync=True), files=["voice.mp3", "timings.json", "chunks.json", "timeline.json"])
        self.assertEqual(got, ["render"])

    def test_mode_change_redoes_media_only(self) -> None:
        got = self._run(self._req(strict_sync=True, strict_full=True), files=["voice.mp3", "timings.json", "chunks.json", "timeline.json"])
        self.assertEqual(got, ["media", "render"])

    def test_voice_only_stops_after_chunks(self) -> None:
        self.assertEqual(self._run(self._req(stop_after_chunks=True), files=[]), ["tts", "whisper", "chunks"])

    def test_done_run_renders_nothing(self) -> None:
        got = self._run(self._req(), files=["voice.mp3", "timings.json", "chunks.json", "timeline.json", "final.mp4"])
        self.assertEqual(got, [])


class TtsStageTests(unittest.TestCase):
    def test_tts_stage_with_provider_and_language_left_auto(self) -> None:
        from engine.slides import checkpoint as cp
        from engine.slides.tts_pipeline import TtsRequest, run_tts_stage
        from engine.tts import PROVIDERS

        calls = {}

        def fake_synth(self, text, dest, opts, *, state_file, cancel=None, on_progress=None):
            calls["state"] = state_file
            dest.write_bytes(b"x" * 3000)
            return dest

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(type(PROVIDERS["lumean"]), "synthesize", fake_synth):
            run = Path(tmp) / "run"
            res = run_tts_stage(TtsRequest(text="Привет", options={"api_key": "k"}, run_dir=run, copy_audio_to_desktop=False))
            self.assertEqual(res.language, "", "язык не задаём — его определит Whisper")
            self.assertEqual(calls["state"], run / "tts_order.json")
            self.assertEqual(cp.load(run).stage, "tts_done")

    def test_tts_stage_requires_key(self) -> None:
        from engine.slides.tts_pipeline import TtsRequest, run_tts_stage

        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, "API key"):
            run_tts_stage(TtsRequest(text="Привет", options={}, run_dir=Path(tmp) / "r", copy_audio_to_desktop=False))




class WatermarkStockFilterTests(unittest.TestCase):
    def test_paid_stock_previews_go_last(self) -> None:
        from engine.slides.source_google import _uniq_good, is_watermarked

        clean = "https://upload.wikimedia.org/wikipedia/commons/a/ab/Mongolian_horse.jpg"
        news = "https://www.bbc.co.uk/news/images/mongolia-herders-2024.jpg"
        marked = [
            "https://c8.alamy.com/comp/2B1X0YJ/mongolian-herder-with-horses.jpg",
            "https://www.shutterstock.com/image-photo/mongolia-steppe-260nw-123456789.jpg",
            "https://media.istockphoto.com/id/12345/photo/yurt.jpg?s=612x612",
            "https://t3.ftcdn.net/jpg/01/23/45/67/360_F_123456789_abc.jpg",
        ]
        out = _uniq_good([marked[0], clean, marked[1], news, *marked[2:]])
        self.assertEqual(out[:2], [clean, news], "чистые картинки — первыми")
        self.assertEqual(sorted(out[2:]), sorted(marked), "с водяным знаком не выброшены — на случай нехватки")
        self.assertTrue(all(is_watermarked(u) for u in marked))
        self.assertFalse(is_watermarked(clean))


if __name__ == "__main__":
    unittest.main()
