"""Нарезка строгого ряда и склейка без повтора. Сеть не нужна."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import tempfile
import unittest
from pathlib import Path

from engine.slides.render_pipeline import ordered_segments
from engine.slides.resume import detect_stage, timeline_matches
from engine.slides.strict_media import (
    VIDEO_PHASE_SEC,
    Beat,
    _fill_holes,
    _parse_queries,
    split_beats,
)
from engine.slides.whisper_timings import WordTiming


def _words(n: int, step: float = 1.0, dot_every: int = 0) -> list[WordTiming]:
    out: list[WordTiming] = []
    for i in range(n):
        text = f"w{i}." if dot_every and (i + 1) % dot_every == 0 else f"w{i}"
        out.append(WordTiming(text, i * step, (i + 1) * step))
    return out


class SplitBeatsTests(unittest.TestCase):
    def test_sentence_groups_cover_voice(self) -> None:
        beats = split_beats(_words(30, dot_every=6), 30.0)
        self.assertGreaterEqual(len(beats), 4)
        self.assertEqual(beats[0].start, 0.0)
        self.assertAlmostEqual(beats[-1].end, 30.0)
        for prev, cur in zip(beats, beats[1:]):
            self.assertAlmostEqual(prev.end, cur.start)
            self.assertGreaterEqual(cur.start - prev.start, 3.0)
            self.assertLessEqual(cur.start - prev.start, 5.01)

    def test_no_punctuation_cuts_every_five(self) -> None:
        beats = split_beats(_words(18), 18.0)
        self.assertEqual([round(b.end - b.start, 2) for b in beats], [5.0, 5.0, 5.0, 3.0])

    def test_short_tail_merges(self) -> None:
        # 11с → 5 + 5 + 1; хвост 1с < TAIL_MERGE → сливается с предыдущим
        beats = split_beats(_words(11), 11.0)
        self.assertEqual(len(beats), 2)
        self.assertAlmostEqual(beats[-1].end, 11.0)

    def test_whole_short_voice_is_one_beat(self) -> None:
        beats = split_beats(_words(4), 4.0)
        self.assertEqual(len(beats), 1)
        self.assertAlmostEqual(beats[0].end, 4.0)


class PhaseTests(unittest.TestCase):
    def test_video_phase_boundary(self) -> None:
        self.assertTrue(Beat(0, 8, "a").video_phase)
        self.assertTrue(Beat(VIDEO_PHASE_SEC - 0.1, VIDEO_PHASE_SEC + 5, "b").video_phase)
        self.assertFalse(Beat(VIDEO_PHASE_SEC, VIDEO_PHASE_SEC + 8, "c").video_phase)

    def test_fill_holes_keeps_kind(self) -> None:
        beats = [
            Beat(0, 8, "a"),
            Beat(8, 16, "b"),
            Beat(VIDEO_PHASE_SEC, VIDEO_PHASE_SEC + 8, "c"),
            Beat(VIDEO_PHASE_SEC + 8, VIDEO_PHASE_SEC + 16, "d"),
        ]
        v = {"path": "pool/v.mp4", "kind": "video", "fit": 7}
        img = {"path": "pool/i.jpg", "kind": "image", "fit": 8}
        filled = _fill_holes(beats, [v, None, None, img])
        self.assertEqual(len(filled), 4)
        self.assertEqual(filled[0]["kind"], "video")
        # соседний повтор хуже, чем фото: в начале теперь годятся и фото, и видео
        self.assertNotEqual(filled[1]["path"], filled[0]["path"])
        self.assertTrue(filled[1]["fallback"])
        # дыра после начала — не копия соседа (рядом фото — значит, видео)
        self.assertNotIn(filled[2]["path"], {filled[1]["path"], filled[3]["path"]})
        self.assertEqual(filled[3]["kind"], "image")


class OrderedTests(unittest.TestCase):
    def test_keeps_order_and_does_not_loop(self) -> None:
        items = [
            ("image", Path("a.jpg"), 6.0),
            ("video", Path("b.mp4"), 6.0),
            ("image", Path("c.jpg"), 6.0),
        ]
        out = ordered_segments(items, 15.0)
        self.assertEqual([kind for kind, _, _ in out], ["image", "video", "image"])
        self.assertEqual([path.name for _, path, _ in out], ["a.jpg", "b.mp4", "c.jpg"])
        self.assertAlmostEqual(sum(d for _, _, d in out), 15.0)

    def test_extends_last_to_voice(self) -> None:
        items = [("image", Path("a.jpg"), 6.0), ("image", Path("b.jpg"), 6.0)]
        out = ordered_segments(items, 14.0)
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(out[-1][2], 8.0)
        self.assertAlmostEqual(sum(d for _, _, d in out), 14.0)


class HoleTests(unittest.TestCase):
    def test_hole_copies_neighbor(self) -> None:
        beats = [Beat(0, 6, "a"), Beat(6, 12, "b"), Beat(12, 18, "c")]
        src = {"path": "pool/beat_0001.mp4", "kind": "video", "fit": 8}
        filled = _fill_holes(beats, [None, src, None])
        self.assertEqual(len(filled), 3)
        self.assertEqual(filled[0]["text"], "a")
        self.assertEqual(filled[0]["kind"], "video")
        self.assertEqual(filled[2]["path"], "pool/beat_0001.mp4")

    def test_image_can_fill_head_hole(self) -> None:
        beats = [Beat(0, 6, "a"), Beat(6, 12, "b")]
        img = {"path": "pool/i.jpg", "kind": "image", "fit": 8}
        filled = _fill_holes(beats, [None, img])
        # в начале теперь и фото: дыра закрыта, ни один кусок не выпал
        self.assertEqual(len(filled), 2)

    def test_parse_queries(self) -> None:
        raw = '```json\n{"queries": ["marble statue", "harbor"]}\n```'
        self.assertEqual(_parse_queries(raw), ["marble statue", "harbor"])


class TimelineModeTests(unittest.TestCase):
    def test_strict_counts_as_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "pool").mkdir()
            (run / "timeline.json").write_text(
                json.dumps({"mode": "strict_sync", "slots": [{"path": "a.jpg"}]}),
                encoding="utf-8",
            )
            self.assertEqual(detect_stage(run), "media_done")
            self.assertTrue(timeline_matches(run, strict=True))
            self.assertFalse(timeline_matches(run, strict=False))


class YoutubeIsoTests(unittest.TestCase):
    def test_parse_duration(self) -> None:
        from engine.slides.source_youtube import _parse_iso8601

        self.assertEqual(_parse_iso8601("PT1H2M3S"), 3723.0)
        self.assertEqual(_parse_iso8601("PT15S"), 15.0)
        self.assertIsNone(_parse_iso8601(""))
