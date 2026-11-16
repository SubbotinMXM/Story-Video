"""Ролики про людей: фото для героев XX–XXI века, статуи — только для древних; стоки — не по имени."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import random
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.slides.stocks_base import StockCandidate
from engine.slides.story_context import StoryContext
from engine.slides.theme_plan import ThemePlan, _bias_main_character, person_era, person_queries

STATUE = ("statue", "bust", "sculpture", "marble", "bronze", "engraving", "ancient")


class EraTests(unittest.TestCase):
    def test_eras(self) -> None:
        cases = {
            "1930s-1998 communist and post-communist Romania": "photo",
            "1940s–1992 communist Romania": "photo",
            "modern day": "photo",
            "3rd century BC Ancient Greece": "ancient",
            "18th century Russia": "ancient",
            "1812 Napoleonic Russia": "painting",
        }
        for era, kind in cases.items():
            self.assertEqual(person_era(era), kind, era)


class ModernFirstTests(unittest.TestCase):
    def test_story_reaching_today_is_not_historical(self) -> None:
        for era in ("1947-present contemporary India", "ancient China to modern day", "1960s-2020s Romania"):
            self.assertFalse(StoryContext.from_dict({"era": era, "historical": True}).historical, era)
        for era in ("1940s–1992 communist Romania", "3rd century BC Ancient Greece", "1910-1945 Japanese colonial period"):
            self.assertTrue(StoryContext.from_dict({"era": era, "historical": True}).historical, era)


class QueryTests(unittest.TestCase):
    def _plan(self, era: str, llm_images: list[str]) -> ThemePlan:
        ctx = StoryContext(era=era, place="Bucharest, Romania", characters=["Anda Călugăreanu"], subject="person", historical=True)
        return ThemePlan(context=ctx, summary="x", video_queries=["Bucharest 1970s street"], image_queries=llm_images)

    def test_modern_person_gets_photos_not_statues(self) -> None:
        plan = self._plan("1940s–1992 communist Romania", ["Anda Călugăreanu bronze bust museum", "Bucharest 1970s theatre"])
        _bias_main_character(plan)
        hero = [q for q in plan.image_queries if "Anda Călugăreanu" in q]
        self.assertTrue(hero and all(not any(w in q.lower() for w in STATUE) for q in hero), hero)
        self.assertIn("Anda Călugăreanu photo", plan.image_queries)
        self.assertIn("Anda Călugăreanu 1990s", plan.image_queries)
        self.assertIn("Bucharest 1970s theatre", plan.image_queries, "фон эпохи от LLM остаётся")

    def test_llm_answer_wins_over_era_text(self) -> None:
        from engine.slides.theme_plan import hero_era

        # эпоха без годов, но LLM знает: это живая звезда
        self.assertEqual(hero_era(StoryContext.from_dict({"era": "Soviet and Russian pop stage", "person_era": "photo"})), "photo")
        self.assertEqual(hero_era(StoryContext.from_dict({"era": "Roman Republic", "person_era": "pre_photo"})), "ancient")
        self.assertEqual(hero_era(StoryContext.from_dict({"era": "1812 Napoleonic Russia", "person_era": "pre_photo"})), "painting")
        self.assertEqual(StoryContext.from_dict({"person_era": "мусор"}).person_era, "")

    def test_ancient_person_keeps_statues(self) -> None:
        images, _videos = person_queries("Archimedes", "3rd century BC Ancient Greece", "Syracuse")
        self.assertIn("Archimedes marble statue", images)


class StocksSkipNameTests(unittest.TestCase):
    def test_theme_gather_skips_stocks_for_name_queries(self) -> None:
        from engine.slides import theme_media as tm

        calls: list[tuple[str, str]] = []

        def fake_search(clients, source, query, *, prefer_video, rng):
            calls.append((source, query))
            return [StockCandidate(provider=source, kind="photo", id=f"{source}{len(calls)}", query=query, preview_url="u", download_url="u")]

        clients = {"google": object(), "pexels": object(), "wikimedia": object()}
        with mock.patch.object(tm, "_search_source", fake_search):
            tm._gather(clients, ["Anda Călugăreanu photo", "Bucharest 1970s street"], ["google", "wikimedia", "stocks"],
                       prefer_video=False, rng=random.Random(1), cancel=None, hero_name="Anda Călugăreanu")
        self.assertNotIn(("stocks", "Anda Călugăreanu photo"), calls)
        self.assertIn(("google", "Anda Călugăreanu photo"), calls)
        self.assertIn(("stocks", "Bucharest 1970s street"), calls, "фон эпохи/места на стоках ищем")

    def test_strict_candidates_skip_stocks_for_name_queries(self) -> None:
        from engine.slides import strict_media as sm

        groups: list[str] = []

        def fake_search(clients, group, query, *, prefer_video, rng):
            groups.append(group)
            return []

        ctx = StoryContext(era="1940s–1992 communist Romania", characters=["Anda Călugăreanu"], subject="person")
        plan = ThemePlan(context=ctx)
        import threading

        mix = mock.Mock()
        mix._lock = threading.Lock()
        mix.searches = {}
        mix.subject = "person"
        mix.weights.side_effect = lambda video: sm.base_weights("person", video=video)
        mix.stats.adjusted.side_effect = lambda w: w
        clients = {"google": object(), "pexels": object(), "wikimedia": object()}
        with mock.patch.object(sm, "_search_source", fake_search):
            sm._candidates(clients, "Anda Călugăreanu on stage", plan, prefer_video=False, index=0, mix=mix)
        self.assertNotIn("stocks", groups)
        self.assertIn("google", groups)


if __name__ == "__main__":
    unittest.main()
