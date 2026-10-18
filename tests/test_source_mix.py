"""Смешивание источников и выбор качества кадра."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.slides import strict_media as sm
from engine.slides.source_mix import Budget, SourceStats, allot, base_weights, subject_of
from engine.slides.stocks_base import StockCandidate
from engine.slides.story_context import StoryContext
from engine.slides.theme_plan import ThemePlan


class AllotTests(unittest.TestCase):
    def test_every_source_gets_a_slot(self) -> None:
        q = allot({"youtube": 4, "stocks": 1, "archive": 1}, ["youtube", "stocks", "archive"], 6)
        self.assertEqual(sum(q.values()), 6)
        self.assertEqual(q, {"youtube": 4, "stocks": 1, "archive": 1})

    def test_missing_source_shares_go_to_others(self) -> None:
        q = allot({"google": 3, "wikimedia": 1, "stocks": 1, "archive": 1}, ["google", "stocks"], 6)
        self.assertEqual(sum(q.values()), 6)
        self.assertGreater(q["google"], q["stocks"])

    def test_subject_changes_mix(self) -> None:
        person = base_weights("person", video=False)
        place = base_weights("place", video=False)
        self.assertGreater(person["google"], person.get("stocks", 0))
        self.assertGreater(place["stocks"], place["google"])

    def test_subject_fallback_for_old_runs(self) -> None:
        self.assertEqual(subject_of("", visual_style="nature"), "nature")
        self.assertEqual(subject_of("", historical=True), "history")
        self.assertEqual(subject_of("Person"), "person")


class StatsTests(unittest.TestCase):
    def test_good_source_gets_more_weight(self) -> None:
        st = SourceStats()
        for _ in range(5):
            st.observe("youtube", 2)
            st.observe("pexels", 8)
        w = st.adjusted({"youtube": 3, "stocks": 3})
        self.assertGreater(w["stocks"], w["youtube"])

    def test_overuse(self) -> None:
        st = SourceStats()
        for _ in range(5):
            st.picked("google")
        st.picked("pexels")
        self.assertTrue(st.overused("google"))
        self.assertFalse(st.overused("unsplash"))


def _plan(subject: str) -> ThemePlan:
    return ThemePlan(summary="s", context=StoryContext(subject=subject), video_queries=[], image_queries=[])


def _cands(provider: str, n: int, kind: str = "video") -> list[StockCandidate]:
    return [
        StockCandidate(provider=provider, kind=kind, id=f"{provider}{i}", query="", preview_url="", download_url=f"u{provider}{i}")
        for i in range(n)
    ]


class CandidatesMixTests(unittest.TestCase):
    def test_slots_split_between_sources(self) -> None:
        clients = {"youtube": object(), "pexels": object(), "archive": object()}
        by_group = {"youtube": _cands("youtube", 10), "stocks": _cands("pexels", 10), "archive": _cands("archive", 10)}

        def fake_search(clients, source, query, *, prefer_video, rng):
            return list(by_group[source])

        mix = sm.MixState(subject="place", stats=SourceStats(), budget=Budget())
        with mock.patch.object(sm, "_search_source", fake_search):
            got = sm._candidates(clients, "q", _plan("place"), prefer_video=True, index=0, mix=mix)
        providers = [c.provider for c in got]
        self.assertEqual(len(got), 6)
        self.assertGreaterEqual(providers.count("pexels"), 2, providers)  # место — стоки весомые
        self.assertGreaterEqual(len(set(providers)), 2, "все кандидаты из одного источника")


class QualityPreferenceTests(unittest.TestCase):
    def _run(self, phase: str, cands: list[tuple[str, int, int]]):
        """cands: (provider, height, fit) в порядке проверки."""
        objs = [StockCandidate(provider=p, kind="video", id=f"{p}{i}", query="", preview_url="", download_url="x") for i, (p, _h, _f) in enumerate(cands)]
        heights = {c.id: h for c, (_p, h, _f) in zip(objs, cands)}
        fits = {c.id: f for c, (_p, _h, f) in zip(objs, cands)}
        budget = Budget()
        budget.soft, budget.hard = {"normal": (1e9, 1e9), "soft": (0, 1e9), "hard": (0, 0)}[phase]
        mix = sm.MixState(subject="other", stats=SourceStats(), budget=budget)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "pool").mkdir()

            def fetch(http, clients, cand, trial, *, prefer_video, index):
                trial.write_bytes(b"x" * 2000)
                return True

            with mock.patch.object(sm, "_candidates", lambda *a, **k: list(objs)), \
                 mock.patch.object(sm, "_fetch_candidate", fetch), \
                 mock.patch.object(sm, "is_landscape", lambda p: True), \
                 mock.patch.object(sm, "_media_height", lambda p, video: heights[p.stem.split("_", 2)[2]]), \
                 mock.patch.object(sm, "_score_file", lambda trial, *a, **k: fits[trial.stem.split("_", 2)[2]]):
                hit, _rej = sm._try_kind(
                    None, {}, _plan("other"), sm.Beat(0, 6, "t"), "q", index=0,
                    trials=tmp_path, frames=tmp_path, pool=tmp_path / "pool",
                    prefer_video=True, mix=mix, tries=3, allow_weak=True, keep_any=True, thematic=False,
                )
        return hit

    def test_prefers_1080_over_720_with_same_fit(self) -> None:
        hit = self._run("normal", [("youtube", 720, 7), ("pexels", 1080, 7)])
        self.assertEqual(hit["provider"], "pexels")

    def test_soft_phase_accepts_good_720(self) -> None:
        hit = self._run("soft", [("youtube", 720, 7), ("pexels", 1080, 7)])
        self.assertEqual(hit["provider"], "youtube")

    def test_hard_phase_takes_best_so_far(self) -> None:
        hit = self._run("hard", [("youtube", 720, 3), ("pexels", 1080, 9)])
        self.assertEqual(hit["provider"], "youtube")

    def test_below_720_never_taken(self) -> None:
        hit = self._run("normal", [("youtube", 480, 9)])
        self.assertIsNone(hit)


if __name__ == "__main__":
    unittest.main()
