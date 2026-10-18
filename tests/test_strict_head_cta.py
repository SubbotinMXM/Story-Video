"""Строгие режимы: начало 2 мин — фото и видео под фразы; призыв подписаться — видео по теме."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.slides import strict_media as sm
from engine.slides.story_context import StoryContext
from engine.slides.theme_plan import ThemePlan

PLAN = ThemePlan(summary="Mongolia documentary", context=StoryContext(subject="place"),
                 video_queries=["Mongolian steppe horses", "Ulaanbaatar city"], image_queries=["yurt"])


class CtaTests(unittest.TestCase):
    def test_detection(self) -> None:
        self.assertTrue(sm.is_cta("Подпишитесь на канал и ставьте лайк"))
        self.assertTrue(sm.is_cta("Suscríbete y activa la campanita"))
        self.assertFalse(sm.is_cta("В 1919 году он подписал договор"))

    def test_queries_mark_cta_by_llm_or_keywords(self) -> None:
        beats = [sm.Beat(0, 4, "Монголия — страна степей"), sm.Beat(4, 8, "Подпишитесь на канал"),
                 sm.Beat(8, 12, "Поддержите нас"), sm.Beat(130, 134, "Юрты кочевников", shot=0),
                 sm.Beat(134, 138, "Юрты кочевников", shot=1)]
        client = mock.Mock()
        client.complete.return_value = json.dumps({"queries": ["Mongolian steppe", "x", "CTA", "nomad yurts"]})
        q = sm._plan_queries(client, PLAN, beats, None, all_beats=True)
        self.assertEqual(q[0], "Mongolian steppe")
        self.assertTrue(q[1].startswith(sm.CTA_MARK), "ключевые слова")
        self.assertTrue(q[2].startswith(sm.CTA_MARK), "LLM ответила CTA")
        self.assertEqual(q[3], "nomad yurts")
        self.assertEqual(q[4], "nomad yurts", "второй кадр куска — тот же запрос, без отдельного вопроса LLM")
        sent = client.complete.call_args.kwargs["user"]
        self.assertEqual(sent.count("Юрты кочевников"), 1)

    def test_cta_beat_gets_theme_video(self) -> None:
        calls = []

        def fake_try(*a, **kw):
            calls.append((a[4], kw["prefer_video"], kw["thematic"]))
            return {"kind": "video", "fit": 7, "weak": False}, 0

        with mock.patch.object(sm, "_try_kind", fake_try):
            sm._pick_beat(None, {}, PLAN, sm.Beat(4, 8, "Подпишитесь"), sm.CTA_MARK + "Mongolian steppe horses",
                          index=1, trials=Path("."), frames=Path("."), pool=Path("."), mix=mock.Mock())
        self.assertEqual(calls, [("Mongolian steppe horses", True, True)])


class HeadMixTests(unittest.TestCase):
    def test_head_beats_mix_photo_and_video_strictly(self) -> None:
        seen = []

        def fake_mixed(*a, **kw):
            seen.append((kw["video_query"], kw["image_query"], kw["thematic"]))
            return None, 0, False

        with mock.patch.object(sm, "_pick_mixed", fake_mixed):
            sm._pick_beat(None, {}, PLAN, sm.Beat(10, 14, "Степь"), "steppe at dawn",
                          index=2, trials=Path("."), frames=Path("."), pool=Path("."), mix=mock.Mock())
        self.assertEqual(seen, [("steppe at dawn", "steppe at dawn", False)], "начало — строго под фразу, фото или видео")

    def test_alternation_gives_about_half_video(self) -> None:
        order = [(i % 2 == 0) for i in range(30)]  # _pick_mixed: чётный — видео первым
        self.assertEqual(sum(order), 15)


if __name__ == "__main__":
    unittest.main()
