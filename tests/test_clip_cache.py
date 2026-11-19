"""CLIP: признаки кадра считаются один раз на файл — оценки те же, кодирований меньше."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import torch
from PIL import Image

from engine.slides import clip_ranker as cr


class FakeModel:
    def __init__(self) -> None:
        self.image_batches: list[int] = []

    def encode_image(self, batch):
        self.image_batches.append(len(batch))
        # признак — среднее по каналам: у разных картинок разный, у одной и той же — одинаковый
        mean = batch.mean(dim=(2, 3))
        return torch.cat([mean, mean.flip(1) + 1.0], dim=1)

    def encode_text(self, tokens):
        return torch.ones(len(tokens), 6)


def make_bundle(model: FakeModel) -> dict:
    return {
        "torch": torch, "model": model, "device": torch.device("cpu"),
        "preprocess": lambda img: torch.tensor([[[p / 255.0]] for p in img.getpixel((0, 0))]).repeat(1, 4, 4),
        "tokenizer": lambda texts: torch.zeros(len(texts), 3),
    }


def paint(path: Path, color: tuple[int, int, int]) -> Path:
    Image.new("RGB", (8, 8), color).save(path)
    return path


class FeatureCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        # подставная модель не должна оставить свои текстовые признаки настоящей (другие тесты идут после)
        saved = (cr._beauty_text, cr._junk_text)
        cr._features.clear()
        cr._beauty_text = cr._junk_text = None

        def restore() -> None:
            cr._features.clear()
            cr._beauty_text, cr._junk_text = saved

        self.addCleanup(restore)

    def test_same_file_encoded_once_across_functions(self) -> None:
        model = FakeModel()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cr, "get_clip", lambda **kw: make_bundle(model)):
            a, b = paint(Path(tmp) / "a.png", (200, 10, 10)), paint(Path(tmp) / "b.png", (10, 200, 10))
            first = cr.score_image_paths("a dog", [a, b])
            self.assertEqual(model.image_batches, [2])
            cr.beauty_scores([a, b])
            cr.junk_scores([a])
            second = cr.score_image_paths("a cat", [b, a])
            self.assertEqual(model.image_batches, [2], "те же файлы — заново не кодируются")
            self.assertEqual({p for p, _s in first}, {p for p, _s in second})

    def test_cached_scores_equal_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = [paint(Path(tmp) / f"{k}.png", (k * 40, 255 - k * 40, 90)) for k in range(4)]
            with mock.patch.object(cr, "get_clip", lambda **kw: make_bundle(FakeModel())):
                fresh = cr.score_image_paths("q", paths)
                cached = cr.score_image_paths("q", paths)
            self.assertEqual(fresh, cached)

    def test_new_and_old_files_mixed(self) -> None:
        model = FakeModel()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cr, "get_clip", lambda **kw: make_bundle(model)):
            a = paint(Path(tmp) / "a.png", (1, 2, 3))
            cr.beauty_scores([a])
            b = paint(Path(tmp) / "b.png", (9, 8, 7))
            got = cr.beauty_scores([a, b])
            self.assertEqual(model.image_batches, [1, 1], "кодируется только новый файл")
            self.assertEqual(set(got), {a, b})

    def test_moved_file_keeps_cache_and_broken_file_skipped(self) -> None:
        model = FakeModel()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cr, "get_clip", lambda **kw: make_bundle(model)):
            a = paint(Path(tmp) / "a.png", (5, 5, 5))
            cr.junk_scores([a])
            moved = Path(tmp) / "pool_a.png"
            a.replace(moved)  # из временной папки в пул
            bad = Path(tmp) / "bad.png"
            bad.write_bytes(b"not an image")
            got = cr.junk_scores([moved, bad])
            self.assertEqual(model.image_batches, [1], "переехавший файл — из кеша")
            self.assertEqual(set(got), {moved}, "битый файл пропущен, не падаем")


if __name__ == "__main__":
    unittest.main()
