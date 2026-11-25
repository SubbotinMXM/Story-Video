"""Ролик не собирается, если в нём меньше 80 РАЗНЫХ кадров; пул добирается по теме, а не по имени героя."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import tempfile
import unittest
from pathlib import Path

from engine.slides import pool_floor, theme_plan
from engine.slides.media_pipeline import MediaError
from engine.slides.story_context import StoryContext


def _write(run: Path, **kw) -> None:
    (run / "timeline.json").write_text(json.dumps(kw), encoding="utf-8")


def _items(n: int, prefix: str = "pool/p") -> list[dict]:
    return [{"path": f"{prefix}{i}.jpg"} for i in range(n)]


class GateTests(unittest.TestCase):
    def test_eight_pictures_repeated_forever_is_rejected(self) -> None:
        # так выглядел ролик про LM317: 28 кадров начала (8 разных) + блок из тех же 8
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            head = [{"path": f"pool/p{i % 8}.jpg"} for i in range(28)]
            _write(run, slots=head, loop_block=_items(8))
            with self.assertRaises(MediaError) as ctx:
                pool_floor.check_timeline(run)
            self.assertIn("не меньше 80", str(ctx.exception))

    def test_four_photos_repeated_80_times_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            _write(run, slots=[{"path": f"pool/p{i % 4}.jpg"} for i in range(322)])
            with self.assertRaises(MediaError):
                pool_floor.check_timeline(run)

    def test_enough_different_frames_pass(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            _write(run, slots=_items(28, "pool/h"), loop_block=_items(60))
            self.assertEqual(pool_floor.check_timeline(run), 88)

    def test_theme_pool_counts_images_and_videos(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            _write(run, images=_items(75), videos=_items(10, "pool/v"))
            self.assertEqual(pool_floor.check_timeline(run), 85)

    def test_short_video_needs_proportionally_less(self) -> None:
        self.assertEqual(pool_floor.min_frames(2400), 80)
        self.assertEqual(pool_floor.min_frames(600), 40)


class _Client:
    def __init__(self, answer: dict) -> None:
        self.answer = answer

    def complete(self, **_kw) -> str:
        return json.dumps(self.answer)


class TopicQueriesTests(unittest.TestCase):
    def _plan(self) -> theme_plan.ThemePlan:
        ctx = StoryContext(era="1970s", place="California", characters=["Bob Dobkin, engineer"], brief="A voltage regulator")
        return theme_plan.ThemePlan(context=ctx, summary="regulator", image_queries=["Bob Dobkin photo"])

    def test_queries_never_contain_the_heros_name_or_repeat(self) -> None:
        client = _Client({"queries": ["Bob Dobkin portrait", "TO-220 voltage regulator macro", "heat sink fins close-up",
                                      "to-220 voltage regulator macro", "Bob Dobkin photo"]})
        got = pool_floor.topic_queries(self._plan(), "script text", client, used=["Bob Dobkin photo"])
        self.assertEqual(got, ["TO-220 voltage regulator macro", "heat sink fins close-up"])

    def test_llm_failure_gives_no_queries(self) -> None:
        class Broken:
            def complete(self, **_kw):
                raise RuntimeError("down")

        self.assertEqual(pool_floor.topic_queries(self._plan(), "x", Broken(), used=[]), [])


class HeroQueriesTests(unittest.TestCase):
    def _plan(self, subject: str) -> theme_plan.ThemePlan:
        ctx = StoryContext(era="1970s", place="California", characters=["Bob Dobkin"], subject=subject, person_era="photo")
        return theme_plan.ThemePlan(context=ctx, image_queries=[f"voltage regulator {i}" for i in range(12)],
                                    video_queries=["circuit board macro"])

    def test_tech_story_keeps_topic_queries_in_front(self) -> None:
        plan = self._plan("history")
        theme_plan._bias_main_character(plan)
        self.assertEqual(sum("Bob Dobkin" in q for q in plan.image_queries), 0)
        self.assertGreaterEqual(sum("regulator" in q for q in plan.image_queries), 12)

    def test_biography_still_leads_with_the_person(self) -> None:
        plan = self._plan("person")
        theme_plan._bias_main_character(plan)
        self.assertGreaterEqual(sum("Bob Dobkin" in q for q in plan.image_queries), 8)


if __name__ == "__main__":
    unittest.main()
