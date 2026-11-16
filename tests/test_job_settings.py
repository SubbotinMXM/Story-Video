"""Задача идёт по настройкам на момент постановки: смена голоса/копий/переводов не задевает уже запущенные."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import runners

OLD = {"tts_provider": "voicegen", "speed_copies": False, "speed_copies_count": 1, "broll_output_dir": "/old",
       "tts": {"voicegen": {"api_key": "KEY-OLD", "template": "voice-A", "translate": ["ro", "pl"]}},
       "llm_api_key": "LLM-OLD"}
NEW = {"tts_provider": "voicegen", "speed_copies": True, "speed_copies_count": 5, "broll_output_dir": "/new",
       "tts": {"voicegen": {"api_key": "KEY-NEW", "template": "voice-B", "translate": []}},
       "llm_api_key": "LLM-NEW"}


class SnapshotTests(unittest.TestCase):
    def test_snapshot_has_no_keys(self) -> None:
        snap = runners.snapshot_settings(OLD)
        self.assertNotIn("llm_api_key", snap)
        self.assertNotIn("api_key", snap["tts"]["voicegen"])
        self.assertEqual(snap["tts"]["voicegen"]["translate"], ["ro", "pl"])

    def test_job_keeps_its_settings_but_takes_fresh_keys(self) -> None:
        params = {runners.SETTINGS_KEY: runners.snapshot_settings(OLD)}
        with mock.patch.object(runners, "load_config", lambda: dict(NEW)):
            cfg = runners.effective_settings(params)
        self.assertEqual(cfg["tts"]["voicegen"]["template"], "voice-A", "голос — как при постановке")
        self.assertEqual(cfg["tts"]["voicegen"]["translate"], ["ro", "pl"], "переводы — как при постановке")
        self.assertFalse(cfg["speed_copies"], "копии — как при постановке")
        self.assertEqual(cfg["broll_output_dir"], "/old")
        self.assertEqual(cfg["tts"]["voicegen"]["api_key"], "KEY-NEW", "ключ — текущий")
        self.assertEqual(cfg["llm_api_key"], "LLM-NEW")

    def test_without_snapshot_current_settings(self) -> None:
        with mock.patch.object(runners, "load_config", lambda: dict(NEW)):
            self.assertEqual(runners.effective_settings({})["speed_copies_count"], 5)

    def test_output_dir_from_job_snapshot(self) -> None:
        from engine.slides import paths

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"STORY_VIDEO_NO_OUTPUT_DIR": ""}):
            mine, other = Path(tmp) / "mine", Path(tmp) / "other"
            mine.mkdir()
            other.mkdir()
            with mock.patch("engine.core.config.load_config", lambda: {"broll_output_dir": str(other)}):
                token = paths.job_output_dirs.set({"broll": str(mine), "slides": ""})
                try:
                    self.assertEqual(paths.output_root("broll", Path(tmp) / "desk"), mine)
                    self.assertEqual(paths.output_root("slides", Path(tmp) / "desk"), Path(tmp) / "desk")
                finally:
                    paths.job_output_dirs.reset(token)
                self.assertEqual(paths.output_root("broll", Path(tmp) / "desk"), other)

    def test_build_runs_with_snapshot_dirs(self) -> None:
        from engine.slides import paths

        seen = {}

        def builder(cfg, params, state):
            def run(job, progress, cancel):
                seen["dirs"] = paths.job_output_dirs.get()
                seen["copies"] = cfg["speed_copies_count"]
                return {}
            return "t", run

        params = {runners.SETTINGS_KEY: runners.snapshot_settings(OLD)}
        with mock.patch.dict(runners._BUILDERS, {"x": builder}), mock.patch.object(runners, "load_config", lambda: dict(NEW)):
            _t, run = runners.build("x", params, {})
            run(None, None, None)
        self.assertEqual(seen, {"dirs": {"broll": "/old", "slides": ""}, "copies": 1})
        self.assertIsNone(paths.job_output_dirs.get(), "после задачи — снова из настроек")


if __name__ == "__main__":
    unittest.main()
