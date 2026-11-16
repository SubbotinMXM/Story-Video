"""«Строго первые 3 минуты»: по фразам только первые 3 мин, дальше — пул без повторов подряд."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.core.spread import max_run
from engine.slides import strict_media as sm


def _pool(run: Path, n_img: int, n_vid: int) -> tuple[list[dict], list[dict]]:
    (run / "pool").mkdir(exist_ok=True)
    images, videos = [], []
    for i in range(n_img):
        (run / "pool" / f"photo_{i:02d}_google.jpg").write_bytes(b"x")
        images.append({"path": f"pool/photo_{i:02d}_google.jpg", "provider": "google", "id": f"g{i}", "score": 0.7})
    for i in range(n_vid):
        (run / "pool" / f"video_{i:02d}_pexels.mp4").write_bytes(b"x")
        videos.append({"path": f"pool/video_{i:02d}_pexels.mp4", "provider": "pexels", "id": f"v{i}", "duration": 9.0, "score": 0.6})
    return videos, images


class LateBlockTests(unittest.TestCase):
    def test_block_has_each_item_once_without_adjacent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            videos, images = _pool(Path(tmp), 8, 3)
        block = sm._late_slots(videos, images, 180.0, 3333.0, avoid_first="pool/x.mp4", rng=random.Random(1))
        self.assertEqual(len(block), 11, "каждый кадр пула ровно один раз")
        self.assertEqual(len({s["path"] for s in block}), 11)
        self.assertAlmostEqual(block[0]["start"], 180.0)
        for a, b in zip(block, block[1:]):
            self.assertAlmostEqual(a["end"], b["start"], delta=0.01)
        self.assertNotEqual(block[0]["path"], block[-1]["path"], "стык блока с самим собой")

    def test_short_tail_cuts_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            videos, images = _pool(Path(tmp), 20, 2)
        block = sm._late_slots(videos, images, 180.0, 30.0, avoid_first=None, rng=random.Random(1))
        self.assertAlmostEqual(block[-1]["end"], 210.0, delta=0.01)

    def test_does_not_repeat_last_early_clip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            videos, images = _pool(Path(tmp), 1, 1)
        for seed in range(20):
            block = sm._late_slots(videos, images, 180.0, 60.0, avoid_first=videos[0]["path"], rng=random.Random(seed))
            self.assertNotEqual(block[0]["path"], videos[0]["path"])


class Strict3MinFlowTests(unittest.TestCase):
    def test_only_first_minutes_searched_per_phrase(self) -> None:
        from engine.slides.media_pipeline import MediaRequest, MediaSourcesConfig
        from engine.slides.story_context import StoryContext
        from engine.slides.theme_plan import ThemePlan

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "script.txt").write_text("текст", encoding="utf-8")
            words = [{"word": f"слово{i}" + ("." if i % 8 == 7 else ""), "start": i * 0.75, "end": i * 0.75 + 0.6} for i in range(800)]
            (run / "timings.json").write_text(json.dumps({"words": words, "language": "ru", "duration": 600}), encoding="utf-8")
            (run / "voice.mp3").write_bytes(b"x")
            pool_calls: list[int] = []
            picked: list[float] = []

            def fake_pick(http, clients, plan, beat, query, *, index, trials, frames, pool, mix, full=False):
                picked.append(beat.start)
                (pool / f"beat_{index:04d}_youtube.mp4").write_bytes(b"x")
                return {"kind": "video", "path": f"pool/beat_{index:04d}_youtube.mp4", "start": beat.start, "end": beat.end,
                        "duration": beat.duration, "provider": "youtube", "id": f"y{index}", "fit": 7, "weak": False}, 0, False

            def fake_late_pool(run_dir, req, plan, **kw):
                pool_calls.append(1)
                return _pool(run_dir, 55, 7)

            plan = ThemePlan(summary="s", context=StoryContext(subject="place"), video_queries=["v"] * 4, image_queries=["i"] * 6)
            req = MediaRequest(run_dir=run, keys=MediaSourcesConfig(), copy_preview_to_desktop=False, llm_api_key="k", strict_sync=True)
            with mock.patch.object(sm, "_llm", lambda r: mock.MagicMock()), \
                 mock.patch.object(sm, "ensure_theme_plan", lambda *a: plan), \
                 mock.patch.object(sm, "_plan_queries", lambda c, p, beats, cancel, all_beats=False, cta=None: ["q"] * len(beats)), \
                 mock.patch.object(sm, "_build_clients", lambda keys: {}), \
                 mock.patch.object(sm, "get_clip", lambda: None), \
                 mock.patch.object(sm, "probe_duration", lambda p: 600.0), \
                 mock.patch.object(sm, "_pick_beat", fake_pick), \
                 mock.patch.object(sm, "_late_pool", fake_late_pool):
                sm.collect_strict_timeline(req)
            tl = json.loads((run / "timeline.json").read_text(encoding="utf-8"))
        self.assertTrue(all(s < sm.HEAD_SEC for s in picked), "по фразам — только первые 2 минуты")
        self.assertLess(len(picked), 45)
        self.assertEqual(len(pool_calls), 1)
        slots = tl["slots"]
        self.assertAlmostEqual(slots[-1]["end"], tl["loop_start"], delta=0.01)
        block = tl["loop_block"]
        self.assertEqual(len({b["path"] for b in block}), len(block), "в блоке каждый кадр по разу")
        # фото по 4 с, видео до 6 с: блок из 55 фото + 7 видео ≈ 4,3 мин, хвост 8 мин —
        # рендер повторит блок склейкой
        self.assertTrue(all(b["duration"] <= sm.LATE_VIDEO_MAX + 0.01 for b in block))
        self.assertTrue(all(abs(b["duration"] - sm.LATE_IMAGE_HOLD) < 0.01 for b in block if b["kind"] == "image"))
        self.assertEqual(max_run(slots + block, key=lambda s: s["path"]), 1)
        self.assertEqual(tl["variant"], "3min")

    def test_late_pool_reused_after_crash(self) -> None:
        from engine.slides.media_pipeline import MediaRequest, MediaSourcesConfig

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            videos, images = _pool(run, 5, 2)
            (run / sm.LATE_POOL_NAME).write_text(json.dumps({"videos": videos, "images": images}), encoding="utf-8")
            req = MediaRequest(run_dir=run, keys=MediaSourcesConfig(), llm_api_key="k")
            with mock.patch("engine.slides.theme_media.gather_pool", side_effect=AssertionError("пул собирается заново")):
                v, i = sm._late_pool(run, req, None, http=None, clients={}, pool=run / "pool", cancel=None, progress=lambda *a: None)
        self.assertEqual((len(v), len(i)), (2, 5))


if __name__ == "__main__":
    unittest.main()
