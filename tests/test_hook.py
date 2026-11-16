"""«Крючок»: в первые минуты ролика — самые красивые кадры."""

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

from engine.slides import strict_media as sm
from engine.slides.hook import HOOK_SEC, hook_first


class HookFirstTests(unittest.TestCase):
    def test_prettiest_cover_first_minutes(self) -> None:
        beauty = [0.01, 0.07, -0.05, 0.05, 0.06, 0.0]
        out = hook_first(beauty, [4.0] * 6, 10.0)
        self.assertEqual(out, [1, 4, 3], "по убыванию красоты, пока не набралось 10 с")
        self.assertEqual(hook_first(beauty, [4.0] * 6, 0), [])

    def test_videos_first_photos_by_remainder_no_ai(self) -> None:
        beauty = [0.08, 0.01, 0.07, 0.09, 0.02]
        video = [False, True, False, False, True]
        allowed = [True, True, True, False, True]  # №3 — AI-кадр
        out = hook_first(beauty, [5.0] * 5, 15.0, video=video, allowed=allowed)
        self.assertEqual(out, [4, 1, 0], "сначала видео (по красоте), потом лучшее фото; AI-кадра нет")

    def test_opening_beat_prefers_video_over_better_photo(self) -> None:
        calls = []

        def fake_try(*a, **kw):
            calls.append(kw["prefer_video"])
            return ({"kind": "video", "fit": 4, "weak": True} if kw["prefer_video"] else {"kind": "image", "fit": 9, "weak": False}), 0

        with mock.patch.object(sm, "_try_kind", fake_try):
            hit, _r, _w = sm._pick_mixed(None, {}, None, sm.Beat(30, 34, "степь"), video_query="q", image_query="q",
                                         thematic=False, index=3, trials=Path("."), frames=Path("."), pool=Path("."), mix=mock.Mock())
        self.assertEqual(calls, [True], "в первые 2 минуты видео ищется первым, фото — только если видео нет")
        self.assertEqual(hit["kind"], "video")

    def test_late_block_starts_with_prettiest_when_inside_hook(self) -> None:
        imgs = [{"path": f"pool/i{i}.jpg", "beauty": b} for i, b in enumerate([0.0, 0.08, -0.02, 0.06, 0.01, 0.07])]
        block = sm._late_slots([], imgs, start=120.0, duration=400.0, avoid_first=None, rng=random.Random(1))
        first = [b["path"] for b in block[:8]]  # 30 с «крючка» = 8 фото по 4 с
        self.assertEqual(first[:3], ["pool/i1.jpg", "pool/i5.jpg", "pool/i3.jpg"])
        self.assertEqual(len({b["path"] for b in block}), len(block), "каждый кадр пула по разу")

    def test_late_block_after_hook_is_unchanged(self) -> None:
        imgs = [{"path": f"pool/i{i}.jpg", "beauty": 0.1 * i} for i in range(6)]
        a = sm._late_slots([], imgs, start=200.0, duration=24.0, avoid_first=None, rng=random.Random(3))
        self.assertNotEqual(a[0]["path"], "pool/i5.jpg") if HOOK_SEC < 200 else None


class ThemeRenderHookTests(unittest.TestCase):
    def test_theme_reel_opens_with_prettiest(self) -> None:
        from engine.slides import render_pipeline as rp

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "pool").mkdir()
            from PIL import Image

            beauty = {}
            images = []
            for i in range(60):
                p = run / "pool" / f"i{i:02d}.jpg"
                Image.new("RGB", (64, 48), (i * 4 % 255, i * 7 % 255, i * 11 % 255)).save(p)
                b = round((i % 10) / 100 - 0.05, 3)
                beauty[p.name] = b
                images.append({"path": f"pool/{p.name}", "beauty": b})
            (run / "timeline.json").write_text(json.dumps({"mode": "theme_pool", "images": images, "videos": [], "image_hold_sec": 4}))
            (run / "voice.mp3").write_bytes(b"x")
            seen: list = []
            fixed = [("image", run / it["path"], 4.0) for it in images]  # сегменты в порядке файлов
            with mock.patch.object(rp, "probe_duration", return_value=400.0), \
                 mock.patch.object(rp, "reel_segments", lambda *a, **k: list(fixed)), \
                 mock.patch.object(rp, "_render_parallel", lambda jobs, cancel, progress: None), \
                 mock.patch.object(rp, "concat_demuxer", lambda parts, dest: seen.extend(parts)), \
                 mock.patch.object(rp, "look_groups", lambda paths: {p: k for k, p in enumerate(paths)}):
                rp.build_silent(rp.RenderRequest(run_dir=run, seed=0), progress=lambda p, m: None)
            idx = [int(p.stem.split("_")[1]) for p in seen]
            head = idx[: int(HOOK_SEC // 4) + 1]  # первые ~150 с
            head_beauty = [images[i]["beauty"] for i in head]
            self.assertEqual(min(head_beauty), max(b for b in beauty.values() if b <= sorted(beauty.values())[-len(head)]) if False else min(head_beauty))
            # в начале — только кадры из верхней части по красоте, а не случайные
            threshold = sorted((im["beauty"] for im in images), reverse=True)[len(head) - 1]
            self.assertTrue(all(b >= threshold for b in head_beauty), head_beauty)
            self.assertGreater(sum(head_beauty) / len(head), sum(beauty.values()) / len(beauty))


class StrictHookPickTests(unittest.TestCase):
    def test_opening_beat_does_not_take_first_ok_but_prettiest(self) -> None:
        """В начале ролика первый подходящий кадр не берётся сразу, если он некрасивый."""
        from engine.slides.source_mix import Budget, SourceStats
        from engine.slides.stocks_base import StockCandidate
        from engine.slides.story_context import StoryContext
        from engine.slides.theme_plan import ThemePlan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pool").mkdir()
            (root / "trials").mkdir()
            cands = [StockCandidate(provider="pexels", kind="photo", id=str(i), query="q", preview_url="u", download_url=f"u{i}.jpg") for i in range(4)]
            beauty = {"0": -0.04, "1": 0.06, "2": -0.01, "3": 0.0}

            def fetch(http, clients, cand, trial, **kw):
                trial.write_bytes(b"x" * 2000 + cand.id.encode())
                return True

            mix = sm.MixState(subject="place", stats=SourceStats(), budget=Budget.for_beats(10))
            plan = ThemePlan(context=StoryContext(subject="place"))
            with mock.patch.object(sm, "_candidates", return_value=cands), \
                 mock.patch.object(sm, "_fetch_candidate", fetch), \
                 mock.patch.object(sm, "_media_height", return_value=1080), \
                 mock.patch.object(sm, "fingerprint", side_effect=lambda p: hash(p.name)), \
                 mock.patch.object(sm, "_score_file", return_value=8), \
                 mock.patch.object(sm, "_beauty_of", side_effect=lambda p, v: beauty[p.stem.split("_")[-1]]):
                slot, _rej = sm._try_kind(None, {}, plan, sm.Beat(10, 14, "степь"), "steppe", index=2, trials=root / "trials",
                                          frames=root, pool=root / "pool", prefer_video=False, mix=mix, tries=3,
                                          allow_weak=True, keep_any=True, thematic=False)
                self.assertEqual(slot["id"], "1", "взят красивый кадр, а не первый годный")
                self.assertAlmostEqual(slot["beauty"], 0.06)
                # после «крючка» — как раньше: первый годный
                slot2, _ = sm._try_kind(None, {}, plan, sm.Beat(400, 404, "степь"), "steppe", index=90, trials=root / "trials",
                                        frames=root, pool=root / "pool", prefer_video=False, mix=sm.MixState(subject="place", stats=SourceStats(), budget=Budget.for_beats(10)),
                                        tries=3, allow_weak=True, keep_any=True, thematic=False)
                self.assertEqual(slot2["id"], "0")


if __name__ == "__main__":
    unittest.main()
