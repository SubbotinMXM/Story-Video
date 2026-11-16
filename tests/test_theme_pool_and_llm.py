"""Режим «по смыслу» целиком на подменённых источниках + повторы LLM при сбоях связи."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx

from engine.slides.ffmpeg_util import find_ffmpeg
from engine.slides.stocks_base import StockCandidate

FF = find_ffmpeg()


def ff(*args: str) -> None:
    subprocess.run([FF, "-v", "error", "-y", *args], check=True)


class FakeSource:
    def __init__(self, name: str, cands: list[StockCandidate]) -> None:
        self.name = name
        self.cands = cands

    def search(self, query, *, prefer_video=True, per_page=12):
        return [c for c in self.cands if (c.kind == "video") == prefer_video]

    def close(self) -> None:
        pass


class ThemePoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        media = self.root / "media"
        media.mkdir()
        # сток: видео 1080p и 360p (последнее должно отсеяться), картинки большие и мелкие
        ff("-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30", "-t", "4", "-pix_fmt", "yuv420p", str(media / "hd.mp4"))
        ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-t", "4", "-pix_fmt", "yuv420p", str(media / "sd.mp4"))
        for i in range(8):
            ff("-f", "lavfi", "-i", f"mandelbrot=size=1600x1000:start_scale={1 + i}", "-frames:v", "1", str(media / f"big{i}.jpg"))
        ff("-f", "lavfi", "-i", "smptebars=size=300x200", "-frames:v", "1", str(media / "tiny.jpg"))
        run = self.root / "run"
        run.mkdir()
        (run / "script.txt").write_text("Исландия, водопады и вулканы.", encoding="utf-8")
        (run / "theme.json").write_text(json.dumps({
            "summary": "Iceland nature", "video_queries": ["iceland waterfall"] * 4, "image_queries": ["iceland volcano"] * 6,
            "story_context": {"place": "Iceland", "subject": "nature", "visual_style": "nature"},
        }), encoding="utf-8")
        self.run = run
        self.media = media

    def _cand(self, provider, cid, kind, file, w, h):
        return StockCandidate(provider=provider, kind=kind, id=cid, query="", preview_url="", download_url=f"file://{file}", width=w, height=h)

    def test_pool_quality_and_sources(self) -> None:
        from engine.slides import theme_media as tm
        from engine.slides.media_pipeline import MediaRequest, MediaSourcesConfig

        m = self.media
        clients = {
            "pexels": FakeSource("pexels", [
                self._cand("pexels", "v_hd", "video", m / "hd.mp4", 1920, 1080),
                self._cand("pexels", "v_sd", "video", m / "sd.mp4", 0, 0),  # размер неизвестен — отсеет проверка файла
            ] + [self._cand("pexels", f"p{i}", "photo", m / f"big{i}.jpg", 1600, 1000) for i in range(4)]),
            "google": FakeSource("google", [self._cand("google", f"g{i}", "photo", m / f"big{i + 4}.jpg", 0, 0) for i in range(4)]
                                 + [self._cand("google", "tiny", "photo", m / "tiny.jpg", 0, 0)]),
        }

        def fake_download(http, url, dest, **kw):
            shutil.copy(url.removeprefix("file://"), dest)
            return True

        req = MediaRequest(run_dir=self.run, keys=MediaSourcesConfig(), copy_preview_to_desktop=False, llm_api_key="k")
        with mock.patch.object(tm, "_build_clients", lambda keys: clients), \
             mock.patch.object(tm, "_download", fake_download), \
             mock.patch.object(tm, "_llm", lambda req: mock.MagicMock()), \
             mock.patch("engine.slides.judge.make_judge", lambda *a, **k: None), \
             mock.patch.object(tm, "_accept_samples", lambda *a, **k: 0.9), \
             mock.patch.object(tm, "VIDEO_SEARCH_SEC", 20), \
             mock.patch.object(tm, "pool_targets", lambda rng: (6, 2)):
            res = tm.collect_theme_pool(req)
        tl = json.loads(res.timeline_file.read_text(encoding="utf-8"))
        vids = [v["id"] for v in tl["videos"]]
        imgs = [i["id"] for i in tl["images"]]
        self.assertIn("v_hd", vids)
        self.assertNotIn("v_sd", vids, "360p не берём")
        self.assertNotIn("tiny", imgs, "мелкие картинки не берём")
        self.assertTrue(any(i.startswith("g") for i in imgs) and any(i.startswith("p") for i in imgs), imgs)

    def test_google_share_depends_on_subject(self) -> None:
        from engine.slides.story_context import StoryContext
        from engine.slides.theme_media import _google_need
        from engine.slides.theme_plan import ThemePlan

        def need(subject):
            return _google_need(ThemePlan(summary="", context=StoryContext(subject=subject), video_queries=[], image_queries=[]), 40)

        self.assertGreater(need("person"), need("place"))
        self.assertGreater(need("place"), need("nature") - 1)


class LlmRetryTests(unittest.TestCase):
    def _client(self, handler):
        from engine.slides import llm_client as lc

        class MockClient(httpx.Client):
            def __init__(self, **kw):
                super().__init__(transport=httpx.MockTransport(handler), **kw)

        # внутренний повтор openai SDK (max_retries) спит ~0.5 с на каждый 503 — в тестах без пауз
        no_wait = mock.patch("openai._base_client.BaseClient._calculate_retry_timeout", return_value=0)
        no_wait.start()
        self.addCleanup(no_wait.stop)

        with mock.patch.object(lc.httpx, "Client", MockClient), \
             mock.patch.object(lc, "_retry_delay", lambda exc, attempt: 0):
            return lc.LLMClient(api_key="k", model="m", base_url="https://llm.test/v1")

    @staticmethod
    def _ok(text: str) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "c", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
        })

    def test_retries_transient_then_succeeds(self) -> None:
        from engine.slides import llm_client as lc

        calls = []

        def handler(req):
            calls.append(1)
            return httpx.Response(503, json={"error": "busy"}) if len(calls) < 3 else self._ok("готово")

        c = self._client(handler)
        with mock.patch.object(lc, "_retry_delay", lambda exc, attempt: 0):
            self.assertEqual(c.complete(label="t", system="s", user="u"), "готово")
        self.assertGreaterEqual(len(calls), 3)

    def test_overloaded_service_waits_longer_instead_of_failing(self) -> None:
        """«No upstream accounts… retry in a minute» — ждём минутами, а не роняем ролик через 50 секунд."""
        from engine.slides import llm_client as lc

        busy = {"error": {"message": "No upstream accounts are available right now", "code": "pool_empty"}}
        calls = []

        def handler(req):
            calls.append(1)
            return httpx.Response(503, json=busy) if len(calls) <= 130 else self._ok("готово")

        c = self._client(handler)
        waits: list[float] = []
        with mock.patch.object(lc, "_retry_delay", lambda exc, attempt: 0), \
             mock.patch.object(c, "_sleep", lambda sec: waits.append(sec)):
            self.assertEqual(c.complete(label="t", system="s", user="u"), "готово")
        # перегружены все модели — круг по ним, пауза, снова круг; кругов больше обычных 6 попыток
        self.assertGreater(len(waits), lc._CHAT_ATTEMPTS, "дольше обычных 6 попыток")
        self.assertIn(60, waits, "раз в минуту, как просит сервис")

    def test_overloaded_tries_other_models_and_returns_to_main(self) -> None:
        import json as _json

        from engine.slides import llm_client as lc

        busy = {"error": {"message": "No upstream accounts are available right now", "code": "pool_empty"}}
        models: list[str] = []
        c = self._client(lambda req: handler(req))
        c.model = "gpt-5.6-terra"
        chain = c.chain("t")

        def handler(req):
            model = _json.loads(req.content)["model"]
            models.append(model)
            # запасная ожила только ко второму кругу
            ok = model == "gpt-6-sol" and models.count("gpt-5.6-terra") >= 3  # библиотека повторяет 503 раз

            return self._ok(f"ответ {model}") if ok else httpx.Response(503, json=busy)

        with mock.patch.object(lc, "_retry_delay", lambda exc, attempt: 0), mock.patch.object(c, "_sleep", lambda sec: None):
            self.assertEqual(c.complete(label="t", system="s", user="u"), "ответ gpt-6-sol")
        tried = [m for i, m in enumerate(models) if i == 0 or m != models[i - 1]]  # повтор самой библиотеки — не в счёт
        self.assertEqual(tried[: len(chain)], chain, "первый круг: основная, потом остальные по цене")
        self.assertEqual(tried[len(chain)], "gpt-5.6-terra", "после паузы — снова с основной")
        self.assertEqual(c.model, "gpt-5.6-terra", "основная модель в настройках не меняется")

    def test_overloaded_too_long_gives_clear_error(self) -> None:
        from engine.slides import llm_client as lc

        c = self._client(lambda req: httpx.Response(503, json={"error": {"message": "No upstream accounts", "code": "pool_empty"}}))
        c.busy_wait = 0
        with mock.patch.object(lc, "_retry_delay", lambda exc, attempt: 0), self.assertRaises(RuntimeError) as err:
            c.complete(label="t", system="s", user="u")
        self.assertIn("перегружен", str(err.exception))
        self.assertIn("Продолжить с места падения", str(err.exception))

    def test_auth_error_not_retried(self) -> None:
        from engine.slides import llm_client as lc

        calls = []

        def handler(req):
            calls.append(1)
            return httpx.Response(401, json={"error": {"message": "bad key"}})

        c = self._client(handler)
        with mock.patch.object(lc, "_retry_delay", lambda exc, attempt: 0), self.assertRaises(Exception):
            c.complete(label="t", system="s", user="u")
        self.assertLessEqual(len(calls), 2, "неверный ключ не надо долбить повторами")

    def test_cancel_closes_client(self) -> None:
        from engine.core.cancel import CancelledError, CancelToken

        c = self._client(lambda req: self._ok("x"))
        token = CancelToken()
        token.register(c)
        token.cancel()
        with self.assertRaises(CancelledError):
            c.complete(label="t", system="s", user="u")




class ModelFallbackTests(unittest.TestCase):
    def test_rejected_model_switches_to_default(self) -> None:
        from unittest import mock

        from engine.slides import llm_client as lc

        class Rejected(Exception):
            status_code = 400

        calls = []

        def create(model, messages, **kw):
            calls.append(model)
            if model == "gpt-5.5":
                raise Rejected("The 'gpt-5.5' model is not supported when using Codex with a ChatGPT account.")
            return mock.Mock(choices=[mock.Mock(message=mock.Mock(content="ok"))])

        client = lc.LLMClient(api_key="k", model="gpt-5.5", base_url="http://127.0.0.1:9")
        try:
            with mock.patch.object(client.client.chat.completions, "create", side_effect=create):
                self.assertEqual(client.complete(label="t", system="s", user="u"), "ok")
                client.complete(label="t2", system="s", user="u")
        finally:
            client.close()
        lc._rejected.clear()
        nxt = calls[1]
        self.assertNotEqual(nxt, "gpt-5.5")
        self.assertEqual(calls, ["gpt-5.5", nxt, nxt], "дальше сразу рабочая модель")

    def test_saved_dead_model_reset(self) -> None:
        from engine.core.config import _normalize

        self.assertEqual(_normalize({"llm_model": "gpt-5.4"})["llm_model"], "")
        self.assertEqual(_normalize({"llm_model": "gpt-6.1-sol"})["llm_model"], "gpt-6.1-sol", "живая модель остаётся")


if __name__ == "__main__":
    unittest.main()
