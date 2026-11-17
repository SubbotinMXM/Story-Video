"""Ролик про человека: только кадры с ним (лица, имя, дубли, режимы подбора)."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.slides import faces
from engine.slides.person_media import _hamming, image_hash, name_in, person_mode, person_query_set
from engine.slides.story_context import StoryContext
from engine.slides.theme_plan import ThemePlan


def _vec(seed: int, base: np.ndarray | None = None, noise: float = 0.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.normal(size=128).astype(np.float32) if base is None else base + rng.normal(size=128).astype(np.float32) * noise
    return v / np.linalg.norm(v)


class HeroFaceTests(unittest.TestCase):
    def test_hero_is_the_face_seen_on_most_images(self) -> None:
        hero = _vec(1)
        per_image = [[_vec(10 + i, hero, 0.05)] for i in range(5)]  # герой на 5 фото
        per_image += [[_vec(100 + i)] for i in range(6)]  # чужие люди по одному разу
        per_image += [[_vec(200 + i), _vec(300 + i, hero, 0.05)] for i in range(2)]  # групповые
        ref = faces.find_hero(per_image)
        self.assertIsNotNone(ref)
        self.assertGreater(float(ref @ hero), 0.9)
        self.assertTrue(faces.is_hero_shot(per_image[0], ref))
        self.assertFalse(faces.is_hero_shot(per_image[6], ref), "чужое лицо — мимо")

    def test_no_consistent_face(self) -> None:
        self.assertIsNone(faces.find_hero([[_vec(i)] for i in range(8)]))
        self.assertIsNone(faces.find_hero([[], []]))

    def test_side_face_needs_more_confidence(self) -> None:
        hero = _vec(1)
        other = _vec(2)
        weak = _vec(3, hero, 1.3)  # похож, но не очень
        if float(weak @ hero) >= faces.HERO_MATCH_SIDE:
            self.skipTest("случайный вектор вышел слишком похожим")
        self.assertFalse(faces.is_hero_shot([other, weak], hero))
        self.assertTrue(faces.is_hero_shot([other, _vec(4, hero, 0.05)], hero))


class NameAndQueryTests(unittest.TestCase):
    def test_name_in_title_ignores_diacritics(self) -> None:
        self.assertTrue(name_in("ANDA CALUGAREANU - Noi nu (1972)", "Anda Călugăreanu"))
        self.assertTrue(name_in("Interviu cu Călugăreanu", "Anda Călugăreanu"))
        self.assertFalse(name_in("Muzică românească anii 70", "Anda Călugăreanu"))

    def test_all_queries_have_quoted_name_and_no_statues_for_photo_era(self) -> None:
        ctx = StoryContext(era="1946-1992 Romania", place="Bucharest", characters=["Anda Călugăreanu"], subject="person", person_era="photo")
        plan = ThemePlan(context=ctx, image_queries=["Anda Călugăreanu bronze bust", "Bucharest 1970s"], video_queries=["Anda Călugăreanu concert"])
        images, videos = person_query_set(plan, "Anda Călugăreanu", "photo")
        self.assertTrue(all('"Anda Călugăreanu"' in q for q in images + videos))
        self.assertFalse(any("bust" in q for q in images))
        self.assertIn('"Anda Călugăreanu" concert', videos)

    def test_person_mode_only_for_person_with_name(self) -> None:
        self.assertEqual(person_mode(ThemePlan(context=StoryContext(subject="person", characters=["Julius Caesar"], era="1st century BC", person_era="pre_photo"))), ("Julius Caesar", "ancient"))
        self.assertIsNone(person_mode(ThemePlan(context=StoryContext(subject="place", characters=["Mongols"]))))
        self.assertIsNone(person_mode(ThemePlan(context=StoryContext(subject="person", characters=[]))))


class DuplicateTests(unittest.TestCase):
    def test_same_photo_resized_is_duplicate(self) -> None:
        from PIL import Image, ImageDraw

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            img = Image.new("RGB", (800, 600), "white")
            d = ImageDraw.Draw(img)
            d.ellipse((200, 100, 600, 500), fill="black")
            d.rectangle((50, 50, 150, 550), fill="gray")
            img.save(root / "a.jpg", quality=95)
            img.resize((400, 300)).save(root / "b.jpg", quality=60)
            other = Image.new("RGB", (800, 600), "black")
            ImageDraw.Draw(other).rectangle((300, 0, 800, 600), fill="white")
            other.save(root / "c.jpg")
            ha, hb, hc = (image_hash(root / n) for n in ("a.jpg", "b.jpg", "c.jpg"))
            self.assertLessEqual(_hamming(ha, hb), 10)
            self.assertGreater(_hamming(ha, hc), 10)


class RoutingTests(unittest.TestCase):
    def test_strict_mode_uses_hero_pool_and_resume_accepts_it(self) -> None:
        from engine.slides import strict_media as sm
        from engine.slides.resume import timeline_matches

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "script.txt").write_text("Текст", encoding="utf-8")
            (run / "timings.json").write_text(json.dumps({"words": [{"word": "Текст", "start": 0, "end": 1}]}), encoding="utf-8")
            plan = ThemePlan(context=StoryContext(subject="person", characters=["Anda Călugăreanu"], era="1946-1992", person_era="photo"))
            req = mock.Mock(run_dir=run, llm_api_key="k", strict_full=False)
            req.keys.youtube_api_key = ""
            with mock.patch.object(sm, "ensure_theme_plan", return_value=plan), \
                 mock.patch.object(sm, "_llm", return_value=mock.Mock()), \
                 mock.patch("engine.slides.theme_media.collect_theme_pool", return_value="POOL") as pool:
                self.assertEqual(sm.collect_strict_timeline(req), "POOL")
            pool.assert_called_once()
            (run / "pool").mkdir(exist_ok=True)
            (run / "timeline.json").write_text(json.dumps({"mode": "theme_pool", "person": "Anda Călugăreanu", "images": [{"path": "x"}]}))
            self.assertTrue(timeline_matches(run, strict=True, full=False), "«Продолжить» не пересобирает пул героя")


if __name__ == "__main__":
    unittest.main()
