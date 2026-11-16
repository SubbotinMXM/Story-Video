"""Папка для готовых роликов: выбрана — туда, не выбрана или пропала — рабочий стол."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.broll import output
from engine.slides import paths


class OutputRootTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.desk = self.root / "desk"
        self.env = mock.patch.dict(os.environ, {"STORY_VIDEO_NO_OUTPUT_DIR": ""})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def _cfg(self, value: str):
        return mock.patch("engine.core.config.load_config", lambda: {"broll_output_dir": value, "slides_output_dir": value})

    def test_empty_means_desktop(self) -> None:
        with self._cfg(""):
            self.assertEqual(paths.output_root("slides", self.desk), self.desk)

    def test_chosen_folder(self) -> None:
        chosen = self.root / "Ролики"
        chosen.mkdir()
        with self._cfg(str(chosen)):
            self.assertEqual(paths.output_root("slides", self.desk), chosen)

    def test_missing_folder_falls_back(self) -> None:
        with self._cfg(str(self.root / "нет такой")):
            self.assertEqual(paths.output_root("slides", self.desk), self.desk)

    def test_tests_never_use_chosen_folder(self) -> None:
        chosen = self.root / "Ролики"
        chosen.mkdir()
        with self._cfg(str(chosen)), mock.patch.dict(os.environ, {"STORY_VIDEO_NO_OUTPUT_DIR": "1"}):
            self.assertEqual(paths.output_root("slides", self.desk), self.desk)

    def test_broll_folder_created_and_renamed_inside_chosen(self) -> None:
        chosen = self.root / "Ролики"
        chosen.mkdir()
        with self._cfg(str(chosen)), mock.patch.object(output, "desktop_dir", lambda: self.desk):
            work = output.run_output_dir("черновик")
            final = output.finalize_run_dir(work, "Готовый ролик")
        self.assertEqual(work.parent, chosen)
        self.assertEqual(final.parent, chosen)
        self.assertTrue(final.is_dir())
        self.assertFalse(self.desk.exists())

    def test_sections_are_separate(self) -> None:
        a, b = self.root / "a", self.root / "b"
        a.mkdir()
        b.mkdir()
        cfg = {"broll_output_dir": str(a), "slides_output_dir": ""}
        with mock.patch("engine.core.config.load_config", lambda: cfg):
            self.assertEqual(paths.output_root("broll", self.desk), a)
            self.assertEqual(paths.output_root("slides", self.desk), self.desk)

    def test_old_single_setting_migrates(self) -> None:
        from engine.core.config import _normalize

        cfg = _normalize({"output_dir": "/x/Ролики"})
        self.assertEqual((cfg["broll_output_dir"], cfg["slides_output_dir"]), ("/x/Ролики", "/x/Ролики"))


if __name__ == "__main__":
    unittest.main()
