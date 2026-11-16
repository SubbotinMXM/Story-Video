"""Вкладка «Превью»: имена → портреты из интернета на чёрном фоне, поровну сверху и снизу."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from engine.slides import thumbnail as th


def photo(path: Path, seed: int) -> Path:
    rng = random.Random(seed)
    img = Image.new("RGB", (16, 9))
    img.putdata([(rng.randrange(256), rng.randrange(100), rng.randrange(256)) for _ in range(16 * 9)])
    img.resize((900, 1200)).save(path, "JPEG")
    return path


class NamesTests(unittest.TestCase):
    def test_parse_lines_commas_numbering_duplicates(self) -> None:
        text = "1. Юрий Никулин\n2) Андрей Миронов, Анатолий Папанов;\n- Евгений Леонов\n\nюрий никулин\n"
        self.assertEqual(th.parse_names(text), ["Юрий Никулин", "Андрей Миронов", "Анатолий Папанов", "Евгений Леонов"])

    def test_even_count(self) -> None:
        self.assertEqual(th.even_names(["a", "b", "c"]), (["a", "b"], "c"))
        self.assertEqual(th.even_names(["a", "b"]), (["a", "b"], None))
        self.assertEqual(th.even_names(["a"]), (["a"], None))


class LayoutTests(unittest.TestCase):
    def _make(self, n: int, color: bool) -> Image.Image:
        with tempfile.TemporaryDirectory() as tmp:
            picks = [(photo(Path(tmp) / f"{k}.jpg", k), (300.0, 200.0, 300.0, 380.0)) for k in range(n)]
            dest = th.make_thumbnail(picks, Path(tmp) / "t.jpg", color=color)
            return Image.open(dest).convert("RGB").copy()

    def test_two_rows_black_band_no_text(self) -> None:
        img = self._make(6, color=True)
        self.assertEqual(img.size, (1280, 720))
        band = [img.getpixel((x, y)) for x in range(0, 1280, 40) for y in range(300, 420, 10)]
        self.assertTrue(all(max(p) < 20 for p in band), "полоса посередине — пустая чёрная")
        self.assertGreater(max(img.getpixel((200, 140))), 40, "верхний ряд")
        self.assertGreater(max(img.getpixel((200, 600))), 40, "нижний ряд")
        cw = (1280 - th.GAP * 2) // 3
        self.assertLess(max(img.getpixel((cw + th.GAP // 2, 140))), 25, "3 сверху: зазор между фото")

    def test_few_people_narrow_centered_cells(self) -> None:
        img = self._make(2, color=True)
        self.assertLess(max(img.getpixel((40, 140))), 20, "по краям чёрное — ячейка не на всю ширину")
        self.assertGreater(max(img.getpixel((640, 140))), 40, "фото по центру")

    def test_color_or_grayscale(self) -> None:
        colored = self._make(2, color=True).getpixel((640, 140))
        gray = self._make(2, color=False).getpixel((640, 140))
        self.assertGreater(max(colored) - min(colored), 10, "цветные остаются цветными")
        self.assertEqual(len(set(gray)), 1, "чёрно-белые")


class BuildTests(unittest.TestCase):
    def test_build_reports_missing_and_keeps_even(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pics = {n: (photo(Path(tmp) / f"{k}.jpg", k), (300.0, 200.0, 300.0, 380.0)) for k, n in enumerate("abcd")}

            def portrait(name, clients, http, workdir, *, cancel=None, person=None):
                return [] if name == "d" else [pics[name]]

            with mock.patch.object(th, "find_portrait", portrait), \
                 mock.patch("engine.slides.media_pipeline._build_clients", lambda keys: {}):
                dest, used, missing = th.build_thumbnail(["a", "b", "c", "d"], keys=None, dest=Path(tmp) / "out" / "p.jpg")
            self.assertTrue(dest.is_file())
            self.assertEqual(used, ["a", "b"], "3 найдено → 2 на превью (поровну)")
            self.assertEqual(missing[0], "d")
            self.assertIn("c", missing[1])

    def test_llm_picks_portrait_without_watermark(self) -> None:
        import json

        from engine.slides.judge import Judge
        from engine.slides.theme_plan import ThemePlan

        class Fake:
            busy_wait = 0
            users: list = []

            def complete_vision(self, **kw):
                return json.dumps({"items": [{"n": 1, "context": 9, "beat": 9, "quality": 9, "reject": "watermark 7дней.ру"},
                                             {"n": 2, "context": 9, "beat": 9, "quality": 7, "reject": ""}]})

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            opts = [(photo(Path(tmp) / f"{k}.jpg", k), (300.0, 200.0, 300.0, 380.0)) for k in range(2)]
            got = th._pick_clean(Judge(Fake(), ThemePlan()), "Евгений Леонов", opts, 420, 280, Path(tmp), True)
        self.assertEqual(got, opts[1], "лучший по лицу, но с водяным знаком — не берём")

    def test_unique_file_name(self) -> None:
        from datetime import datetime

        with tempfile.TemporaryDirectory() as tmp:
            when = datetime(2026, 10, 21, 18, 5)
            first = th.thumbnail_path(Path(tmp), when)
            first.write_bytes(b"x")
            self.assertEqual(first.name, "Превью 21.10 18-05.jpg")
            self.assertEqual(th.thumbnail_path(Path(tmp), when).name, "Превью 21.10 18-05 (2).jpg")


if __name__ == "__main__":
    unittest.main()
