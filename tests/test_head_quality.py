"""Начало ролика: без повторов. Дыры — лучшими кадрами пула, пул без кадров начала."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from engine.slides import strict_media as sm


def picture(path: Path, seed: int) -> Path:
    import random

    rng = random.Random(seed)
    img = Image.new("L", (16, 9))
    img.putdata([rng.randrange(256) for _ in range(16 * 9)])  # у каждого seed — своя «картинка»
    img.resize((640, 360)).convert("RGB").save(path, "JPEG")
    return path


def slot(path: str, start: float, kind: str = "image", **kw) -> dict:
    return {"kind": kind, "path": path, "start": start, "end": start + 4, "provider": "p", "id": path, **kw}


class HeadQualityTests(unittest.TestCase):
    def test_holes_filled_from_pool_videos_first_and_removed_from_pool(self) -> None:
        beats = [sm.Beat(i * 4.0, i * 4.0 + 4, f"фраза {i}") for i in range(4)]
        slots = [slot("pool/a.jpg", 0), None, slot("pool/b.jpg", 8), None]
        videos = [{"path": "pool/v1.mp4", "score": 0.7, "beauty": 0.01, "provider": "x", "id": "v1"},
                  {"path": "pool/v2.mp4", "score": 0.8, "beauty": 0.05, "provider": "x", "id": "v2"}]
        images = [{"path": "pool/i1.jpg", "score": 0.9, "beauty": 0.09, "provider": "x", "id": "i1"}]
        with mock.patch.object(sm, "add_beauty", lambda items, d: None):
            sm._fill_head_from_pool(beats, slots, videos, images, Path("/r"))
        self.assertEqual(slots[1]["path"], "pool/v2.mp4", "сначала видео, красивейшее")
        self.assertEqual(slots[3]["path"], "pool/v1.mp4")
        self.assertEqual(slots[1]["start"], 4.0)
        self.assertFalse(slots[1]["fallback"])
        self.assertEqual([v["path"] for v in videos], [], "взятые из пула не повторятся после 2 минут")
        self.assertEqual(len(images), 1)

    def test_pool_drops_frames_already_in_head_but_keeps_shared_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "pool").mkdir()
            picture(run / "pool/head.jpg", 1)
            picture(run / "pool/same_look.jpg", 1)  # та же картинка с другого сайта
            picture(run / "pool/other.jpg", 5)
            picture(run / "pool/taken.jpg", 3)
            head = [slot("pool/head.jpg", 0), slot("pool/taken.jpg", 4)]
            images = [{"path": "pool/same_look.jpg", "provider": "q", "id": "s"},
                      {"path": "pool/other.jpg", "provider": "q", "id": "o"},
                      {"path": "pool/taken.jpg", "provider": "p", "id": "pool/taken.jpg"}]
            _v, kept = sm._drop_like_head([], images, head, run)
            self.assertEqual([x["path"] for x in kept], ["pool/other.jpg"])
            self.assertFalse((run / "pool/same_look.jpg").exists())
            self.assertTrue((run / "pool/taken.jpg").exists(), "файл, стоящий в начале, не удаляется")

    def test_hole_in_head_borrows_from_later_part_not_from_head(self) -> None:
        beats = [sm.Beat(i * 5.0, i * 5.0 + 5, f"t{i}") for i in range(3)] + [sm.Beat(200.0, 205.0, "late")]
        slots = [slot("pool/h0.mp4", 0, "video"), None, slot("pool/h2.mp4", 10, "video"), slot("pool/late.mp4", 200, "video")]
        out = sm._fill_holes(beats, slots)
        self.assertEqual(out[1]["path"], "pool/late.mp4")


    def test_weak_head_frames_replaced_by_strong_pool_videos(self) -> None:
        beats = [sm.Beat(i * 5.0, i * 5.0 + 5, f"t{i}") for i in range(4)]
        slots = [
            slot("pool/flag.jpg", 0, "image", quality=4),            # флаг во весь экран
            slot("pool/fly.mp4", 5, "video", quality=9),             # пролёт — остаётся
            slot("pool/dark.mp4", 10, "video", quality=5),           # тёмный, скучный
            slot("pool/ok.mp4", 15, "video", quality=7),
        ]
        videos = [{"path": "pool/v_best.mp4", "quality": 10, "score": 0.9, "provider": "x", "id": "a"},
                  {"path": "pool/v_good.mp4", "quality": 8, "score": 0.8, "provider": "x", "id": "b"},
                  {"path": "pool/v_meh.mp4", "quality": 6, "score": 0.9, "provider": "x", "id": "c"}]
        sm._upgrade_head(beats, slots, videos, Path("/r"))
        self.assertEqual(slots[0]["path"], "pool/v_best.mp4", "фото — первым на замену")
        self.assertEqual(slots[0]["kind"], "video")
        self.assertEqual(slots[0]["start"], 0.0)
        self.assertEqual(slots[2]["path"], "pool/v_good.mp4")
        self.assertEqual(slots[1]["path"], "pool/fly.mp4")
        self.assertEqual(slots[3]["path"], "pool/ok.mp4")
        self.assertEqual([v["path"] for v in videos], ["pool/v_meh.mp4"], "средние видео пула в начало не идут")

    def test_no_google_in_opening(self) -> None:
        from engine.slides.source_mix import Budget, SourceStats
        from engine.slides.theme_plan import ThemePlan

        seen_groups: list[str] = []

        def search(clients, group, query, **kw):
            seen_groups.append(group)
            return []

        mix = sm.MixState(subject="place", stats=SourceStats(), budget=Budget())
        clients = {"google": object(), "youtube": object(), "pexels": object()}
        with mock.patch.object(sm, "_search_source", search):
            sm._candidates(clients, "q", ThemePlan(), prefer_video=False, index=0, mix=mix, hook=True)
            self.assertNotIn("google", seen_groups, "в начале ролика — без флагов, афиш и скриншотов из Google")
            seen_groups.clear()
            sm._candidates(clients, "q2", ThemePlan(), prefer_video=False, index=0, mix=mix, hook=False)
            self.assertIn("google", seen_groups)


if __name__ == "__main__":
    unittest.main()
