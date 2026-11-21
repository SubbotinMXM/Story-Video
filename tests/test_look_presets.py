"""Разное оформление от ролика к ролику: субтитры, цветовая подача, ритм и движение кадров."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import random
import unittest
from pathlib import Path

from engine.slides import look_presets as lp
from engine.slides.render_pipeline import reel_segments
from engine.slides.render_segment import image_motion_filter


class PresetTests(unittest.TestCase):
    def test_same_video_same_look_different_videos_differ(self) -> None:
        self.assertEqual(lp.subs_force_style("Ролик 1"), lp.subs_force_style("Ролик 1"))
        styles = {lp.subs_preset(f"Ролик {i}")["name"] for i in range(40)}
        grades = {lp.grade_of(f"Ролик {i}")[0] for i in range(40)}
        self.assertGreaterEqual(len(styles), 5, "стили не должны сводиться к одному")
        self.assertGreaterEqual(len(grades), 4)

    def test_empty_key_is_the_old_look(self) -> None:
        self.assertEqual(lp.subs_preset("")["name"], "Классика")
        self.assertEqual(lp.grade_filter(""), "")

    def test_style_string_is_escaped_for_ffmpeg_filter(self) -> None:
        s = lp.subs_force_style("x")
        self.assertNotIn(",", s.replace("\\,", ""), "запятые между полями экранированы")
        self.assertIn("FontName=", s)


class RhythmTests(unittest.TestCase):
    def test_lengths_vary_but_average_stays(self) -> None:
        d = lp.rhythm_durations(80, 4.0, random.Random(3))
        self.assertEqual(len(d), 80)
        self.assertAlmostEqual(sum(d) / 80, 4.0, delta=0.25)
        self.assertGreater(max(d) - min(d), 2.0, "короткие и длинные планы")
        self.assertTrue(all(2.0 <= x <= 6.8 for x in d))

    def test_reel_uses_rhythm_only_when_asked(self) -> None:
        images = [Path(f"i{n}.jpg") for n in range(20)]
        fixed = reel_segments(images, [], [], 3600, 4, random.Random(1))
        self.assertEqual({d for _, _, d in fixed}, {4})
        varied = reel_segments(images, [], [], 3600, 4, random.Random(1), rhythm=random.Random(2))
        self.assertGreater(len({d for _, _, d in varied}), 5)


class MotionTests(unittest.TestCase):
    def test_all_modes_build_and_neighbours_differ(self) -> None:
        modes = [lp.motion_mode(s) for s in range(200)]
        self.assertEqual(set(modes), set(range(6)))
        self.assertTrue(all(a != b for a, b in zip(modes, modes[1:])))
        for s in range(12):
            vf, frames = image_motion_filter(duration=3, seed=s, fade_sec=0.5, w=1920, h=1080, fps=25, grade="eq=contrast=1.05")
            self.assertIn("zoompan", vf)
            self.assertIn("eq=contrast=1.05", vf)


class CopyLookTests(unittest.TestCase):
    def test_every_copy_of_a_video_looks_different(self) -> None:
        looks = [lp.copy_look("ролик", i, (1920, 1080)) for i in range(5)]
        self.assertEqual(len(set(looks)), 5)
        self.assertTrue(all("crop=" in x and "scale=1920:1080" in x for x in looks))

    def test_no_size_means_color_only_and_other_video_other_order(self) -> None:
        self.assertNotIn("crop=", lp.copy_look("a", 0, None))
        firsts = {lp.copy_look(f"ролик {i}", 0, None) for i in range(30)}
        self.assertGreater(len(firsts), 2)


if __name__ == "__main__":
    unittest.main()
