"""Проверка кадров зрячей LLM: паспорт ролика, лист кандидатов, выбор лучшего, AI-кадры с повтором."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from engine.slides.ffmpeg_util import find_ffmpeg
from engine.slides.story_context import StoryContext
from engine.slides.theme_plan import ThemePlan


def japan_plan() -> ThemePlan:
    ctx = StoryContext.from_dict({
        "era": "modern day", "place": "Japan, Hokkaido", "brief": "A film about life in modern Japan.",
        "visual_markers": ["Japanese signage", "tiled roofs"], "wrong_look": ["European alpine village"],
    })
    return ThemePlan(context=ctx, summary="Life in Japan", video_queries=[], image_queries=[])


def image(path: Path, color: tuple[int, int, int]) -> Path:
    img = Image.new("RGB", (1280, 720), color)
    for x in range(0, 1280, 80):  # не однотонная: у разных картинок разный «отпечаток»
        for y in range(0, 720, 80):
            if (x // 80 + y // 80) % (2 + sum(color) % 3) == 0:
                img.paste((255 - color[0], color[1], 255 - color[2]), (x, y, x + 80, y + 80))
    img.save(path, "JPEG")
    return path


class FakeClient:
    def __init__(self, answers: list) -> None:
        self.answers = list(answers)
        self.users: list[str] = []
        self.images: list[str] = []
        self.busy_wait = 0
        self.prompts: list[str] = []

    def complete_vision(self, *, label, system, user_text, image_b64, **kw):
        self.users.append(user_text)
        self.images.append(image_b64)
        ans = self.answers.pop(0)
        if isinstance(ans, Exception):
            raise ans
        return ans

    def complete(self, *, label, system, user):
        self.prompts.append(user)
        ans = self.answers.pop(0)
        return ans

    def close(self) -> None:
        pass


def verdicts(*items) -> str:
    return json.dumps({"items": [dict(n=i + 1, context=c, beat=b, quality=q, reject=r) for i, (c, b, q, r) in enumerate(items)]})


class JudgeTests(unittest.TestCase):
    def test_brief_has_place_markers_and_wrong_look(self) -> None:
        from engine.slides.judge import brief_of

        text = brief_of(japan_plan())
        self.assertIn("A film about life in modern Japan.", text)
        self.assertIn("Japanese signage", text)
        self.assertIn("European alpine village", text)

    def test_rate_sends_one_sheet_and_parses(self) -> None:
        from engine.slides.judge import Judge

        client = FakeClient([verdicts((2, 6, 8, "wrong country: Alps"), (8, 7, 9, ""))])
        judge = Judge(client, japan_plan())
        with tempfile.TemporaryDirectory() as tmp:
            a = image(Path(tmp) / "a.jpg", (200, 30, 30))
            b = image(Path(tmp) / "b.jpg", (30, 30, 200))
            got = judge.rate([a, b], moment="Деревня на Хоккайдо зимой", query="Hokkaido village")
        self.assertEqual(len(client.images), 1, "оба кандидата — одним листом")
        self.assertIn("Деревня на Хоккайдо зимой", client.users[0])
        self.assertIn("Japan, Hokkaido", client.users[0])
        self.assertFalse(got[0].ok)
        self.assertTrue(got[1].ok)
        self.assertGreater(got[1].score, got[0].score)

    def test_video_row_has_two_frames(self) -> None:
        from engine.slides import judge as jm

        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "c.mp4"
            subprocess.run([find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:d=3", str(clip)], check=True)
            frames = jm._frames_of(clip, Path(tmp), video=True)
            self.assertEqual(len(frames), 2)
            sheet = jm.contact_sheet([frames, [image(Path(tmp) / "i.jpg", (10, 120, 10))]])
        self.assertGreater(len(sheet), 1000)

    def test_photo_only_sheet_is_narrower_than_sheet_with_video(self) -> None:
        """Фото — один кадр в строке: пустую половину листа не отправляем (токены картинки); у видео — два кадра."""
        import io

        from PIL import Image

        from engine.slides import judge as jm

        with tempfile.TemporaryDirectory() as tmp:
            photos = [[image(Path(tmp) / f"p{k}.jpg", (10 * k, 120, 10))] for k in range(1, 4)]
            narrow = Image.open(io.BytesIO(jm.contact_sheet(photos)))
            wide = Image.open(io.BytesIO(jm.contact_sheet([photos[0] * 2, photos[1]])))
        self.assertLess(narrow.width, wide.width * 0.7, "лист из одних фото заметно уже")
        self.assertEqual(narrow.height, 3 * (jm._TILE_H + 10))
        self.assertGreaterEqual(narrow.width, 70 + jm._TILE_W, "кадр помещается целиком")

    def test_low_context_is_not_ok(self) -> None:
        from engine.slides.judge import Verdict

        self.assertFalse(Verdict(3, 9, 9).ok, "чужая страна/эпоха — брак, даже если красиво")
        self.assertTrue(Verdict(6, 4, 5).ok)

    def test_failures_turn_judge_off_and_unsupported_at_once(self) -> None:
        from engine.slides import judge as jm

        with tempfile.TemporaryDirectory() as tmp:
            a = image(Path(tmp) / "a.jpg", (200, 30, 30))
            client = FakeClient([RuntimeError("timeout")] * jm._MAX_FAILS)
            judge = jm.Judge(client, japan_plan())
            for _ in range(jm._MAX_FAILS):
                self.assertIsNone(judge.rate([a]))
            self.assertFalse(judge.enabled, "сервис не отвечает — дальше по CLIP")
            client = FakeClient([RuntimeError("Error code: 400 - image input is not supported")])
            judge = jm.Judge(client, japan_plan())
            self.assertIsNone(judge.rate([a]))
            self.assertFalse(judge.enabled, "модель не видит картинки — сразу по CLIP")


class StrictJudgedPickTests(unittest.TestCase):
    def _run(self, answers: list, start: float = 40):
        from engine.slides import strict_media as sm
        from engine.slides.judge import Judge
        from engine.slides.source_mix import Budget, SourceStats
        from engine.slides.stocks_base import StockCandidate

        cands = [StockCandidate(provider="p", kind="image", id=f"c{i}", query="", preview_url="", download_url="x.jpg")
                 for i in range(5)]
        colors = [(200, 30, 30), (30, 200, 30), (30, 30, 200), (200, 200, 30), (30, 200, 200)]
        client = FakeClient(answers)
        mix = sm.MixState(subject="place", stats=SourceStats(), budget=Budget(), judge=Judge(client, japan_plan()))
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "pool").mkdir()

            def fetch(http, clients, cand, trial, *, prefer_video, index):
                image(trial, colors[int(cand.id[1:])])
                return True

            with mock.patch.object(sm, "_candidates", lambda *a, **k: list(cands)), \
                 mock.patch.object(sm, "_fetch_candidate", fetch), \
                 mock.patch.object(sm, "_beauty_of", lambda *a, **k: 0.0), \
                 mock.patch.object(sm, "JUDGE_BATCH_HOOK", 2), \
                 mock.patch.object(sm, "_score_file", lambda *a, **k: 5):
                # начало ролика: здесь кадры смотрит LLM (лист из 2 кандидатов — под ответы теста)
                hit, rej = sm._try_kind(None, {}, japan_plan(), sm.Beat(start, start + 5, "Деревня"), "Hokkaido village",
                                        index=50, trials=t, frames=t, pool=t / "pool", prefer_video=False, mix=mix,
                                        tries=3, allow_weak=True, keep_any=True, thematic=False)
                left = sorted(p.name for p in t.glob("b0050_*"))
        return hit, rej, mix, client, left

    def test_llm_picks_the_right_country_not_the_prettier_one(self) -> None:
        hit, rej, mix, client, left = self._run([verdicts((2, 8, 10, "European alpine village"), (8, 7, 7, ""))])
        self.assertEqual(hit["id"], "c1")
        self.assertEqual(hit["fit"], 7)  # 0.45·8 + 0.35·7 + 0.2·7
        self.assertIn("контекст 8", hit["judge"])
        self.assertEqual(rej, 1)
        self.assertEqual(mix.taken, {("p", "c1")})
        self.assertEqual(left, [], "проигравшие файлы удалены")

    def test_first_30_seconds_wait_for_wow_shot(self) -> None:
        """Первые 30 секунд: «нормальный» кадр (качество 7) не берём — ищем дальше, до «вау» (8+)."""
        hit, _rej, _mix, client, _ = self._run([
            verdicts((9, 8, 7, ""), (8, 8, 7, "")),
            verdicts((8, 6, 9, ""), (7, 7, 6, "")),
        ], start=10)
        self.assertEqual(len(client.images), 2, "первый лист без «вау» — смотрим второй")
        self.assertEqual(hit["id"], "c2")
        self.assertIn("FIRST 30 SECONDS", client.users[0], "судье сказано, что это первые 30 секунд")

    def test_all_wrong_looks_at_next_sheet(self) -> None:
        hit, _rej, _mix, client, _ = self._run([
            verdicts((1, 5, 9, "Swiss Alps"), (2, 5, 9, "China")),
            verdicts((7, 8, 8, ""), (3, 3, 3, "")),
        ])
        self.assertEqual(len(client.images), 2, "первый лист мимо — смотрим второй")
        self.assertEqual(hit["id"], "c2")

    def test_nothing_fits_returns_none(self) -> None:
        bad = verdicts((1, 1, 9, "Alps"), (1, 1, 9, "Alps"))
        hit, _rej, _mix, _client, _ = self._run([bad, bad, bad])
        self.assertIsNone(hit, "лучше дыра (её закроет AI-кадр или повтор), чем чужая страна")

    def test_opening_rejects_dull_shot_even_if_on_topic(self) -> None:
        from engine.slides import strict_media as sm
        from engine.slides.judge import Judge
        from engine.slides.source_mix import Budget, SourceStats
        from engine.slides.stocks_base import StockCandidate

        cands = [StockCandidate(provider="p", kind="image", id=f"c{i}", query="", preview_url="", download_url="x.jpg") for i in range(3)]
        client = FakeClient([verdicts((10, 10, 4, ""), (8, 5, 9, ""))])
        mix = sm.MixState(subject="place", stats=SourceStats(), budget=Budget(), judge=Judge(client, japan_plan()))
        colors = [(200, 30, 30), (30, 200, 30), (30, 30, 200)]
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "pool").mkdir()

            def fetch(http, clients, cand, trial, *, prefer_video, index):
                image(trial, colors[int(cand.id[1:])])
                return True

            with mock.patch.object(sm, "_candidates", lambda *a, **k: list(cands)), \
                 mock.patch.object(sm, "_fetch_candidate", fetch), \
                 mock.patch.object(sm, "_score_file", lambda *a, **k: 5), \
                 mock.patch.object(sm, "_beauty_of", lambda *a, **k: 0.0):
                hit, _ = sm._try_kind(None, {}, japan_plan(), sm.Beat(3, 8, "Флаг страны"), "flag", index=0, trials=t,
                                      frames=t, pool=t / "pool", prefer_video=False, mix=mix, tries=3,
                                      allow_weak=True, keep_any=True, thematic=False)
        self.assertEqual(hit["id"], "c1", "начало ролика: красивый общий план важнее буквального флага")
        self.assertEqual(hit["quality"], 9)
        self.assertIn("OPENS THE FILM", client.users[0])

    def test_after_opening_no_llm_wait(self) -> None:
        """После начала ролика кадр берётся быстрым подбором — LLM не спрашиваем."""
        from engine.slides import strict_media as sm
        from engine.slides.judge import Judge
        from engine.slides.source_mix import Budget, SourceStats
        from engine.slides.stocks_base import StockCandidate

        client = FakeClient([])
        mix = sm.MixState(subject="place", stats=SourceStats(), budget=Budget(), judge=Judge(client, japan_plan()))
        cands = [StockCandidate(provider="p", kind="image", id="c0", query="", preview_url="", download_url="x.jpg")]
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "pool").mkdir()

            def fetch(http, clients, cand, trial, *, prefer_video, index):
                image(trial, (200, 30, 30))
                return True

            with mock.patch.object(sm, "_candidates", lambda *a, **k: list(cands)), \
                 mock.patch.object(sm, "_fetch_candidate", fetch), \
                 mock.patch.object(sm, "_score_file", lambda *a, **k: 8):
                hit, _ = sm._try_kind(None, {}, japan_plan(), sm.Beat(600, 605, "Деревня"), "Hokkaido village",
                                      index=200, trials=t, frames=t, pool=t / "pool", prefer_video=False, mix=mix,
                                      tries=3, allow_weak=True, keep_any=True, thematic=False)
        self.assertEqual(hit["id"], "c0")
        self.assertEqual(client.images, [], "после начала ролика LLM не ждём")

    def test_judge_down_falls_back_to_clip(self) -> None:
        hit, _rej, _mix, _client, _ = self._run([RuntimeError("503 busy")])
        self.assertIsNotNone(hit, "проверка недоступна — кадр по CLIP, как раньше")
        self.assertEqual(hit["fit"], 5)


class AiCheckedTests(unittest.TestCase):
    def test_prompt_written_rejected_fixed_and_cached(self) -> None:
        from engine.slides import ai_fill
        from engine.slides.ai_gen import AiConfig
        from engine.slides.judge import Judge

        plan = japan_plan()
        slots = [{"kind": "image", "start": 200 + i * 4, "end": 204 + i * 4, "text": f"Фраза {i}", "query": "village",
                  "fit": 2, "path": f"pool/b{i}.jpg"} for i in range(10)]
        cfg = AiConfig(api_key="k", image_model="img", video_model="")
        gens: list[str] = []

        def fake_generate(cfg, tasks, **kw):
            for t in tasks:
                gens.append(t.prompt)
                t.result = image(t.dest.with_suffix(".jpg"), (40, 90, 160))

        client = FakeClient([
            json.dumps({"prompts": ["Snowy village in Hokkaido, Japan, tiled roofs, Japanese signage blurred"]}),
            verdicts((2, 6, 8, "looks like Swiss Alps")),
            verdicts((8, 7, 8, "")),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            pool.mkdir()
            with mock.patch.object(ai_fill, "generate", fake_generate):
                out = ai_fill.fill_slots(slots, plan, cfg, pool=pool, share=0.1, judge=Judge(client, plan))
            self.assertEqual(len(gens), 2, "первая попытка + одна исправленная")
            self.assertIn("Hokkaido, Japan", gens[0], "описание написал LLM по паспорту ролика")
            self.assertIn("looks like Swiss Alps", gens[1])
            self.assertIn("Japan, Hokkaido", gens[1])
            self.assertEqual(out[0]["provider"], "ai")
            self.assertTrue((Path(tmp) / out[0]["path"]).is_file())
            cache = json.loads((Path(tmp) / "ai_prompts.json").read_text(encoding="utf-8"))
            self.assertIn("beat", json.dumps(cache) + "beat")
            self.assertTrue(any(v.get("prompt", "").startswith("Snowy village") for v in cache.values()),
                            "описание сохранено — «Продолжить» не заплатит второй раз")

    def test_rejected_twice_keeps_live_frame(self) -> None:
        from engine.slides import ai_fill
        from engine.slides.ai_gen import AiConfig
        from engine.slides.judge import Judge

        plan = japan_plan()
        slots = [{"kind": "image", "start": 200 + i * 4, "end": 204 + i * 4, "text": f"Фраза {i}", "query": "village",
                  "fit": 2, "path": f"pool/b{i}.jpg"} for i in range(10)]
        cfg = AiConfig(api_key="k", image_model="img", video_model="")

        def fake_generate(cfg, tasks, **kw):
            for t in tasks:
                t.result = image(t.dest.with_suffix(".jpg"), (40, 90, 160))

        client = FakeClient([json.dumps({"prompts": ["p"]}), verdicts((2, 6, 8, "Alps")), verdicts((3, 6, 8, "still Alps"))])
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            pool.mkdir()
            with mock.patch.object(ai_fill, "generate", fake_generate):
                out = ai_fill.fill_slots(slots, plan, cfg, pool=pool, share=0.1, judge=Judge(client, plan))
            self.assertEqual(out[0]["path"], "pool/b0.jpg", "живой кадр остался")
            self.assertEqual([p.name for p in pool.glob("ai_*")], [], "забракованные AI-файлы удалены")
            _lines, warnings = ai_fill.report_lines(Path(tmp))
            self.assertIn("не прошёл проверку", warnings[0])


class ShakeTests(unittest.TestCase):
    def test_tripod_vs_handheld(self) -> None:
        from engine.slides.shake import SHAKY, shakiness

        with tempfile.TemporaryDirectory() as tmp:
            still = Path(tmp) / "still.mp4"
            shaky = Path(tmp) / "shaky.mp4"
            src = "testsrc2=s=960x540:d=4:r=25"
            subprocess.run([find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", src, "-vf", "crop=640:360:100:60", str(still)], check=True)
            # кадр прыгает туда-сюда каждые 2 кадра — как съёмка с рук
            jitter = "crop=640:360:'100+40*sin(n*2.3)':'60+30*cos(n*1.7)'"
            subprocess.run([find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", src, "-vf", jitter, str(shaky)], check=True)
            calm, rough = shakiness(still), shakiness(shaky)
        self.assertLess(calm, SHAKY)
        self.assertGreater(rough, SHAKY)


if __name__ == "__main__":
    unittest.main()
