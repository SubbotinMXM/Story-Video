"""Подписка: 3 показа (≈25 с, середина, за 30 с до конца), внизу кадра, без блюра."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import random
import unittest

from engine.compositor.subscribe import subscribe_graph, subscribe_starts


class SubscribeTimesTests(unittest.TestCase):
    def test_three_shows_in_long_video(self) -> None:
        starts = subscribe_starts(600.0, 10.0, random.Random(1))
        self.assertEqual(len(starts), 3)
        self.assertTrue(20 <= starts[0] <= 30)
        self.assertAlmostEqual(starts[1], 295.0)
        self.assertAlmostEqual(starts[2], 570.0)

    def test_short_video_fewer_shows_without_overlap(self) -> None:
        starts = subscribe_starts(60.0, 10.0, random.Random(1))
        self.assertGreaterEqual(len(starts), 1)
        for a, b in zip(starts, starts[1:]):
            self.assertGreaterEqual(b - a, 10.0 + 10.0)
        self.assertTrue(all(t + 10.0 <= 60.0 for t in starts))

    def test_graph_has_no_blur_and_sits_at_bottom(self) -> None:
        graph = ";".join(subscribe_graph(
            3, True, starts=[25.0, 300.0, 570.0], sub_dur=10.0, crop=(10, 20, 300, 80), width=700, margin=48,
            fps=30, chroma="0x60FB01", similarity=0.12, blend=0.08, volume=0.8,
        ))
        self.assertNotIn("blur", graph)
        self.assertEqual(graph.count("overlay=(W-w)/2:H-h-48"), 3)
        self.assertIn("crop=300:80:10:20", graph)
        self.assertIn("amix=inputs=4", graph)
        self.assertTrue(graph.endswith("[aout]"))
        self.assertIn("[vout]", graph)


if __name__ == "__main__":
    unittest.main()
