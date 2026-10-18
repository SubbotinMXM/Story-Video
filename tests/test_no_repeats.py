"""Никаких одинаковых кадров подряд: строгий ряд, пул «по смыслу», футажи."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import random
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.core.spread import max_run, spread


class SpreadTests(unittest.TestCase):
    def test_fuzz_no_adjacent_when_possible(self) -> None:
        r = random.Random(3)
        for t in range(1500):
            items = [r.randrange(r.randint(1, 9)) for _ in range(r.randint(1, 40))]
            out = spread(items, rng=random.Random(t))
            self.assertEqual(sorted(out), sorted(items))
            if max(Counter(items).values()) <= (len(items) + 1) // 2:
                self.assertEqual(max_run(out), 1, (items, out))

    def test_round_order_changes(self) -> None:
        out = spread(list("abcdefgh") * 4, rng=random.Random(1))
        rounds = {"".join(out[i : i + 8]) for i in range(0, 32, 8)}
        self.assertGreater(len(rounds), 1, "каждый круг — в своём порядке")

    def test_dominant_split_evenly(self) -> None:
        self.assertEqual(max_run(spread(list("aaaab"), rng=random.Random(0))), 2)

    def test_avoid_first(self) -> None:
        for s in range(20):
            self.assertEqual(spread(list("ab"), rng=random.Random(s), avoid_first="a")[0], "b")


class FootagePickTests(unittest.TestCase):
    def test_few_files_no_adjacent_and_balanced(self) -> None:
        from engine.compositor.picker import pick_broll

        with tempfile.TemporaryDirectory() as tmp:
            for i in range(3):
                (Path(tmp) / f"c{i}.mp4").touch()
            for s in range(100):
                got = pick_broll(Path(tmp), random.Random(s))
                self.assertEqual(len(got), 8)
                self.assertEqual(max_run(got), 1)
                c = Counter(got).values()
                self.assertLessEqual(max(c) - min(c), 1)


class StrictHolesTests(unittest.TestCase):
    def test_holes_never_copy_neighbour(self) -> None:
        from engine.slides import strict_media as sm

        B = sm.Beat
        beats = [B(i * 6.0, i * 6.0 + 6, str(i)) for i in range(30)] + [B(180 + i * 12.0, 192 + i * 12.0, "l") for i in range(8)]
        v = lambda p: {"kind": "video", "path": p, "provider": "x", "id": p}
        im = lambda p: {"kind": "image", "path": p, "provider": "x", "id": p}
        r = random.Random(5)
        for _ in range(200):
            vids = [v(f"v{k}") for k in range(r.randint(3, 6))]  # с двумя видео «v0 _ v1» без повтора не закрыть
            imgs = [im(f"i{k}") for k in range(r.randint(1, 4))]
            slots = [r.choice(vids) if r.random() > 0.4 else None for _ in range(30)]
            slots += [r.choice(imgs + vids) if r.random() > 0.5 else None for _ in range(8)]
            # реальный подбор сам не ставит один клип в два куска — убираем такие повторы
            seen: set = set()
            for i, s in enumerate(slots):
                if s and s["path"] in seen:
                    slots[i] = None
                elif s:
                    seen.add(s["path"])
            if not any(s and s["kind"] == "video" for s in slots[:30]):
                slots[0] = vids[0]
            out = sm._fill_holes(beats, slots)
            self.assertEqual(len(out), len(beats))
            self.assertEqual(max_run(out, key=lambda s: s["path"]), 1, [s["path"] for s in out])

    def test_unavoidable_case_still_keeps_every_beat(self) -> None:
        from engine.slides import strict_media as sm

        beats = [sm.Beat(0, 6, "a"), sm.Beat(6, 12, "b"), sm.Beat(12, 18, "c")]
        v = lambda p: {"kind": "video", "path": p}
        out = sm._fill_holes(beats, [v("v0"), None, v("v1")])  # видео всего два, картинку сюда нельзя
        self.assertEqual(len(out), 3)
        self.assertEqual(out[1]["kind"], "video")

    def test_one_clip_one_beat(self) -> None:
        from engine.slides import strict_media as sm
        from engine.slides.source_mix import Budget, SourceStats
        from engine.slides.stocks_base import StockCandidate

        mix = sm.MixState(subject="other", stats=SourceStats(), budget=Budget())
        c = StockCandidate(provider="youtube", kind="video", id="same", query="", preview_url="", download_url="")
        self.assertTrue(mix.claim(c))
        self.assertFalse(mix.claim(c), "второй кусок не должен взять тот же клип")
        mix.release(c)
        self.assertTrue(mix.claim(c))

    def test_rejected_candidate_released_kept_stays_taken(self) -> None:
        from engine.slides import strict_media as sm
        from engine.slides.source_mix import Budget, SourceStats
        from engine.slides.stocks_base import StockCandidate
        from engine.slides.story_context import StoryContext
        from engine.slides.theme_plan import ThemePlan

        cands = [StockCandidate(provider="p", kind="video", id=f"c{i}", query="", preview_url="", download_url="x") for i in range(2)]
        fits = {"c0": 2, "c1": 8}
        mix = sm.MixState(subject="other", stats=SourceStats(), budget=Budget())
        plan = ThemePlan(summary="", context=StoryContext(), video_queries=[], image_queries=[])
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "pool").mkdir()

            def fetch(http, clients, cand, trial, *, prefer_video, index):
                trial.write_bytes(b"x" * 2000)
                return True

            with mock.patch.object(sm, "_candidates", lambda *a, **k: list(cands)), \
                 mock.patch.object(sm, "_fetch_candidate", fetch), \
                 mock.patch.object(sm, "is_landscape", lambda p: True), \
                 mock.patch.object(sm, "_media_height", lambda p, video: 1080), \
                 mock.patch.object(sm, "_score_file", lambda trial, *a, **k: fits[trial.stem.split("_", 2)[2]]):
                hit, _ = sm._try_kind(None, {}, plan, sm.Beat(0, 6, "t"), "q", index=0, trials=t, frames=t, pool=t / "pool",
                                      prefer_video=True, mix=mix, tries=3, allow_weak=True, keep_any=True, thematic=False)
        self.assertEqual(hit["id"], "c1")
        self.assertEqual(mix.taken, {("p", "c1")}, "отбракованный c0 освобождён, выбранный c1 занят")


class ThemeReelOrderTests(unittest.TestCase):
    def test_repeated_pool_is_reshuffled_without_adjacent(self) -> None:
        from engine.slides import render_pipeline as rp

        captured: list = []
        segs = [("image", Path(f"/m/{k}.jpg"), 3.0) for k in "abcde"]
        with mock.patch.object(rp, "concat_demuxer", lambda parts, dest: captured.extend(parts)):
            order = rp.spread([i for _ in range(4) for i in range(len(segs))], key=lambda i: str(segs[i][1]), rng=random.Random(1))
            rp.concat_demuxer([Path(f"seg_{i}") for i in order], Path("x"))
        names = [p.name for p in captured]
        self.assertEqual(max_run(names), 1)
        rounds = {tuple(names[i : i + 5]) for i in range(0, 20, 5)}
        self.assertGreater(len(rounds), 1)


if __name__ == "__main__":
    unittest.main()
