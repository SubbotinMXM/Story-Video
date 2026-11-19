"""Память проверки фото героев: один и тот же файл нейросеть не проверяет второй раз."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import os
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from engine.slides import judge_cache as jc
from engine.slides import stars_media as st


def photo(path: Path, seed: int) -> Path:
    rng = random.Random(seed)
    img = Image.new("RGB", (16, 9))
    img.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(16 * 9)])
    img.resize((300, 400)).save(path, "JPEG")
    return path


class CacheFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"STORY_VIDEO_JUDGE_CACHE_DIR": self.tmp.name})
        patch.start()
        self.addCleanup(patch.stop)
        jc.reset_for_tests()
        self.addCleanup(jc.reset_for_tests)

    def test_remembers_between_runs(self) -> None:
        jc.put("k1", "watermark")
        jc.put("k2", "")
        jc.flush()
        jc.reset_for_tests()  # «новый запуск программы»
        self.assertEqual(jc.get("k1"), "watermark")
        self.assertEqual(jc.get("k2"), "", "чистый кадр — тоже запоминаем")
        self.assertIsNone(jc.get("нет такого"))

    def test_same_bytes_same_fingerprint_different_files_differ(self) -> None:
        a, b, c = (photo(Path(self.tmp.name) / f"{n}.jpg", s) for n, s in (("a", 1), ("b", 1), ("c", 2)))
        self.assertEqual(jc.fingerprint(a), jc.fingerprint(b), "та же картинка под другим именем — один отпечаток")
        self.assertNotEqual(jc.fingerprint(a), jc.fingerprint(c))

    def test_broken_file_is_ignored(self) -> None:
        (Path(self.tmp.name) / "judge_photos.json").write_text("{не json", encoding="utf-8")
        jc.reset_for_tests()
        self.assertIsNone(jc.get("k"))
        jc.put("k", "text")
        jc.flush()
        self.assertEqual(json.loads((Path(self.tmp.name) / "judge_photos.json").read_text())["k"][0], "text")

    def test_size_is_capped_oldest_dropped(self) -> None:
        with mock.patch.object(jc, "MAX_ENTRIES", 3):
            for n in range(5):
                jc.put(f"k{n}", "")
            jc.flush()
            jc.reset_for_tests()
            self.assertEqual([jc.get(f"k{n}") is not None for n in range(5)], [False, False, True, True, True])

    def test_nothing_written_when_nothing_changed(self) -> None:
        jc.flush()
        self.assertFalse((Path(self.tmp.name) / "judge_photos.json").exists())


class StarsUseCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"STORY_VIDEO_JUDGE_CACHE_DIR": self.tmp.name})
        patch.start()
        self.addCleanup(patch.stop)
        jc.reset_for_tests()
        self.addCleanup(jc.reset_for_tests)
        self.run_dir = Path(self.tmp.name) / "run"
        (self.run_dir / "pool").mkdir(parents=True)
        self.star = st.Star(1, "Никулин", "Nikulin", "Юрий Никулин")

    def items(self, seeds):
        out = []
        for s in seeds:
            photo(self.run_dir / "pool" / f"p{s}.jpg", s)
            out.append({"path": f"pool/p{s}.jpg"})
        return out

    def judge(self, bad=()):
        asked = []

        def rate(path, query=""):
            asked.append(path.name)
            return SimpleNamespace(reject="Burned-in text and watermark" if path.name in bad else "")

        j = mock.MagicMock()
        j.enabled = True
        j.pool.return_value.rate.side_effect = rate
        return j, asked

    def test_second_video_does_not_ask_again(self) -> None:
        j, asked = self.judge(bad={"p2.jpg"})
        kept = st._clean_by_judge(j, self.star, self.items([1, 2, 3]), self.run_dir)
        self.assertEqual([it["path"] for it in kept], ["pool/p1.jpg", "pool/p3.jpg"], "плохой убран")
        self.assertEqual(sorted(asked), ["p1.jpg", "p2.jpg", "p3.jpg"])
        # другой ролик: те же файлы (под другими именами) + один новый
        j2, asked2 = self.judge(bad=set())
        again = [{"path": "pool/q1.jpg"}, {"path": "pool/q2.jpg"}, {"path": "pool/q3.jpg"}, {"path": "pool/q4.jpg"}]
        for new, seed in zip(("q1", "q2", "q3", "q4"), (1, 2, 3, 4)):
            photo(self.run_dir / "pool" / f"{new}.jpg", seed)
        kept2 = st._clean_by_judge(j2, st.Star(2, "Вицин", "Vitsin", "Георгий Вицин"), again, self.run_dir)
        self.assertEqual(asked2, ["q4.jpg"], "нейросети отправлен только новый файл")
        self.assertEqual([it["path"] for it in kept2], ["pool/q1.jpg", "pool/q3.jpg", "pool/q4.jpg"],
                         "плохой из прошлого ролика убран без повторного вопроса")
        self.assertFalse((self.run_dir / "pool" / "q2.jpg").exists())

    def test_only_look_problems_are_remembered_not_person_specific_rejects(self) -> None:
        j = mock.MagicMock()
        j.enabled = True
        j.pool.return_value.rate.return_value = SimpleNamespace(reject="wrong era, looks like a modern actor")
        items = self.items([5])
        st._clean_by_judge(j, self.star, items, self.run_dir)
        self.assertEqual(jc.get(jc.fingerprint(self.run_dir / items[0]["path"])), "",
                         "отказ из-за «не тот человек» зависит от героя — для другого ролика он не отказ")

    def test_failed_answer_is_not_remembered(self) -> None:
        j = mock.MagicMock()
        j.enabled = True
        j.pool.return_value.rate.return_value = None  # нейросеть не ответила
        items = self.items([6])
        kept = st._clean_by_judge(j, self.star, items, self.run_dir)
        self.assertEqual(len(kept), 1)
        self.assertIsNone(jc.get(jc.fingerprint(self.run_dir / items[0]["path"])), "нет ответа — повторим в следующий раз")


if __name__ == "__main__":
    unittest.main()
