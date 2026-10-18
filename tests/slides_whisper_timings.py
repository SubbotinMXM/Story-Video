"""Тесты Whisper timings (без загрузки модели)."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import tempfile
import unittest
from pathlib import Path

from engine.slides.whisper_timings import (
    TimingsResult,
    WordTiming,
    save_timings_json,
    save_timings_preview,
    timings_to_dict,
)


class TimingsIOTests(unittest.TestCase):
    def _sample(self) -> TimingsResult:
        words = [
            WordTiming("Привет", 0.0, 0.4, 0.9),
            WordTiming("мир", 0.45, 0.8, 0.85),
            WordTiming("это", 1.0, 1.2, 0.7),
            WordTiming("тест", 1.25, 1.6, 0.95),
        ]
        return TimingsResult(
            words=words,
            language="ru",
            duration=1.6,
            model_size="base",
            text="Привет мир это тест",
        )

    def test_json_roundtrip_shape(self) -> None:
        result = self._sample()
        data = timings_to_dict(result)
        self.assertEqual(data["word_count"], 4)
        self.assertEqual(data["words"][0]["word"], "Привет")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "timings.json"
            save_timings_json(path, result)
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["language"], "ru")
            self.assertEqual(len(loaded["words"]), 4)

    def test_preview_contains_words(self) -> None:
        result = self._sample()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "preview.txt"
            save_timings_preview(path, result, sample=2)
            text = path.read_text(encoding="utf-8")
            self.assertIn("Привет", text)
            self.assertIn("тест", text)
            self.assertIn("words: 4", text)


if __name__ == "__main__":
    unittest.main()
