"""Тесты рендера (без полного часа)."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import random
import tempfile
import unittest
from pathlib import Path

from engine.slides.render_pipeline import _image_durations, reel_segments, repeat_count
from engine.slides.theme_media import pool_targets
from engine.slides.render_segment import image_motion_filter
from engine.slides.subtitles import align_script_words, timings_to_srt, _ts


class SubtitlesTests(unittest.TestCase):
    def test_ts(self) -> None:
        self.assertEqual(_ts(0), "00:00:00,000")
        self.assertEqual(_ts(65.5), "00:01:05,500")

    def test_srt_from_timings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tpath = Path(tmp) / "timings.json"
            tpath.write_text(
                json.dumps(
                    {
                        "words": [
                            {"word": "Hello", "start": 0.0, "end": 0.4},
                            {"word": "world.", "start": 0.45, "end": 0.9},
                            {"word": "Test", "start": 1.0, "end": 1.3},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            srt = timings_to_srt(tpath, Path(tmp) / "a.srt", max_words=4)
            self.assertIsNotNone(srt)
            text = srt.read_text(encoding="utf-8")
            self.assertIn("Hello", text)
            self.assertIn("-->", text)

    def test_script_words_replace_whisper(self) -> None:
        aligned = align_script_words(
            "Архимед писал труды.",
            [
                {"word": "Архемед", "start": 0.0, "end": 0.4},
                {"word": "песал", "start": 0.4, "end": 0.8},
                {"word": "труды", "start": 0.8, "end": 1.2},
            ],
        )
        self.assertEqual(
            [w["word"] for w in aligned],
            ["Архимед", "писал", "труды."],
        )
        self.assertAlmostEqual(aligned[0]["start"], 0.0)
        self.assertGreater(aligned[-1]["end"], aligned[0]["start"])

    def test_motion_is_supersampled(self) -> None:
        vf, frames = image_motion_filter(
            duration=10, seed=1, fade_sec=0.5, w=1920, h=1080, fps=30
        )
        self.assertEqual(frames, 300)
        self.assertIn("scale=3840:2160:flags=lanczos", vf)  # движение на 2× кадре (полпикселя на выходе)
        self.assertIn("zoompan=", vf)
        self.assertIn("s=1920x1080", vf, "зум отдаёт сразу 1080p — без 4K-кадра и обратного уменьшения")
        self.assertNotIn("s=3840x2160", vf)
        self.assertNotIn("-loop", vf)


class ReelTests(unittest.TestCase):
    def test_one_pass_not_the_whole_hour(self) -> None:
        images = [Path(f"i{n}.jpg") for n in range(4)]
        videos = [Path(f"v{n}.mp4") for n in range(3)]
        plan = reel_segments(
            images, videos, [10, 10, 10], 3600, 10, random.Random(1)
        )
        self.assertEqual(plan[0][0], "image")
        self.assertEqual(sum(1 for k, _, _ in plan if k == "image"), 4)
        self.assertEqual(sum(1 for k, _, _ in plan if k == "video"), 3)
        self.assertAlmostEqual(sum(d for _, _, d in plan), 70, places=2)
        self.assertEqual(repeat_count(70, 3600), 52)
        self.assertEqual(repeat_count(600, 3600), 6)
        kinds = [k for k, _, _ in plan]
        first, second = [i for i, k in enumerate(kinds) if k == "video"][:2]
        self.assertGreater(second, first + 1)

    def test_images_only_not_stretched(self) -> None:
        plan = reel_segments([Path("a.jpg")], [], [], 3600, 10, random.Random(0))
        self.assertEqual(plan, [("image", Path("a.jpg"), 10)])
        self.assertEqual(repeat_count(10, 3600), 360)
        self.assertEqual(_image_durations(25, 10), [10.0, 10.0, 5.0])

    def test_short_voice_does_not_render_extra(self) -> None:
        images = [Path(f"i{n}.jpg") for n in range(6)]
        plan = reel_segments(images, [], [], 25, 10, random.Random(0))
        self.assertAlmostEqual(sum(d for _, _, d in plan), 25, places=2)
        self.assertEqual(repeat_count(25, 25), 1)


class PoolTargetTests(unittest.TestCase):
    def test_range(self) -> None:
        images, videos = pool_targets(random.Random(1))
        self.assertGreaterEqual(images, 80)
        self.assertLessEqual(images, 100)
        self.assertGreaterEqual(videos, 10)
        self.assertLessEqual(videos, 15)
        again = {pool_targets(random.Random(i))[0] for i in range(20)}
        self.assertGreater(len(again), 1)


if __name__ == "__main__":
    unittest.main()
