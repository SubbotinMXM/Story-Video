"""Тесты путей и языков."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from engine.slides.paths import desktop_copy_name, new_run_dir
from engine.slides.publish import export_ready_folder, keyword_from_script, video_keyword


class PathsTests(unittest.TestCase):
    def test_new_run_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("engine.slides.paths.RUNS_DIR", Path(tmp)):
                d = new_run_dir(title_hint="Hello World!")
                self.assertTrue(d.is_dir())
                self.assertTrue(d.name.endswith("hello-world"))

    def test_desktop_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            desk = Path(tmp)
            with mock.patch("engine.slides.paths.DESKTOP", desk):
                a = desktop_copy_name("voice_test", ".mp3")
                a.write_text("x", encoding="utf-8")
                b = desktop_copy_name("voice_test", ".mp3")
                self.assertNotEqual(a, b)
                self.assertTrue(b.name.endswith("_2.mp3"))


class PublishTests(unittest.TestCase):
    def test_keyword_is_the_person(self) -> None:
        text = (
            "Представьте человека. Архимед жил в Сиракузах. "
            "Архимед писал труды. Архимед учил."
        )
        self.assertEqual(keyword_from_script(text), "Архимед")

    def test_theme_title_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "script.txt").write_text("Архимед " * 5, encoding="utf-8")
            (run / "theme.json").write_text(
                '{"story_context": {"title": "Архимед"}}',
                encoding="utf-8",
            )
            self.assertEqual(video_keyword(run), "Архимед")

    def test_folder_name_and_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            desk = Path(tmp) / "desk"
            run = Path(tmp) / "run"
            run.mkdir()
            (run / "final.mp4").write_bytes(b"mp4")
            (run / "voice.mp3").write_bytes(b"mp3")
            (run / "script.txt").write_text("Архимед писал.\n", encoding="utf-8")
            (run / "subs.srt").write_text("1\n", encoding="utf-8")
            loose = desk / "voice_old.mp3"
            desk.mkdir()
            loose.write_bytes(b"old")
            when = datetime(2026, 9, 30, 12, 0, 0)
            with mock.patch("engine.slides.publish.DESKTOP", desk):
                folder = export_ready_folder(run, loose=[str(loose)], when=when)
            assert folder is not None
            self.assertEqual(folder.name, "Архимед 30.09")
            self.assertTrue((folder / "ролик.mp4").is_file())
            self.assertTrue((folder / "озвучка.mp3").is_file())
            self.assertTrue((folder / "сценарий.txt").is_file())
            self.assertTrue((folder / "субтитры.srt").is_file())
            self.assertFalse(loose.exists())


if __name__ == "__main__":
    unittest.main()
