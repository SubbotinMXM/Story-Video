"""Известные люди на обложке: другое настоящее фото того же человека (не тот же кадр, что на образце)."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from engine.publish import cover_people as cpl
from engine.slides import faces


def unit(*v: float) -> np.ndarray:
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


HERO = unit(1, 0, 0)
SAME_SHOT = unit(1, 0.05, 0)  # почти то же лицо — тот же снимок, что на образце
OTHER_SHOT = unit(1, 0.9, 0)  # тот же человек, другой снимок
STRANGER = unit(0, 0, 1)


class PickPhotoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def _img(self, name: str, size=(800, 800)) -> Path:
        p = self.tmp / name
        Image.new("RGB", size, "gray").save(p)
        return p

    def _run(self, emb: dict[str, list[np.ndarray]], reference_faces: list[np.ndarray]):
        paths = [self._img(n) for n in emb]
        with mock.patch.object(faces, "face_embeddings", lambda p: emb[p.name]), \
                mock.patch.object(faces, "find_hero", lambda per: HERO), \
                mock.patch.object(faces, "is_hero_shot", lambda vecs, hero: float(vecs[0] @ hero) >= 0.45):
            return cpl.pick_photo(paths, reference_faces)

    def test_skips_same_shot_as_reference(self) -> None:
        got = self._run({"a.jpg": [SAME_SHOT], "b.jpg": [OTHER_SHOT]}, [SAME_SHOT])
        self.assertEqual(got.name, "b.jpg")

    def test_prefers_single_person(self) -> None:
        got = self._run({"group.jpg": [OTHER_SHOT, STRANGER], "solo.jpg": [unit(1, 0.7, 0)]}, [])
        self.assertEqual(got.name, "solo.jpg")

    def test_stranger_only_is_none(self) -> None:
        self.assertIsNone(self._run({"x.jpg": [STRANGER]}, []))

    def test_no_hero_is_none(self) -> None:
        p = self._img("a.jpg")
        with mock.patch.object(faces, "face_embeddings", lambda p: [HERO]), \
                mock.patch.object(faces, "find_hero", lambda per: None):
            self.assertIsNone(cpl.pick_photo([p], []))


class FindPhotosTests(unittest.TestCase):
    def test_found_photo_cached_and_missing_is_none(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        calls: list[str] = []

        def search(q, n):
            calls.append(q)
            return [f"https://img/{q}/{k}.jpg" for k in range(3)] if "Пугачёва" in q else []

        def download(url, dest):
            Image.new("RGB", (900, 1200), "white").save(dest)
            return True

        with mock.patch.object(cpl, "_download", download), \
                mock.patch.object(cpl, "pick_photo", lambda paths, ref: paths[1] if paths else None), \
                mock.patch.object(faces, "face_embeddings", lambda p: []):
            got = cpl.find_photos(["Алла Пугачёва", "Никто"], b"", tmp, search=search)
            self.assertIsNone(got["Никто"])
            photo = got["Алла Пугачёва"]
            self.assertTrue(photo.is_file())
            self.assertLessEqual(max(Image.open(photo).size), cpl.PHOTO_SIDE)
            n = len(calls)
            again = cpl.find_photos(["Алла Пугачёва"], b"", tmp, search=search)
        self.assertEqual(again["Алла Пугачёва"], photo)
        self.assertEqual(len(calls), n, "повтор берёт уже найденное фото, не ищет заново")

    def test_people_capped(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        seen: list[str] = []
        with mock.patch.object(faces, "face_embeddings", lambda p: []):
            cpl.find_photos([f"Человек {k}" for k in range(15)], None, tmp,
                            search=lambda q, n: seen.append(q) or [])
        self.assertEqual(len({q.split(" фото")[0] for q in seen}), cpl.MAX_PEOPLE)


if __name__ == "__main__":
    unittest.main()
