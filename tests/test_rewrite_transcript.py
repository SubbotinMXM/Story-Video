"""Рерайт по расшифровке из файла: таймкоды прочь, дальше — тот же цикл, что и по ссылке."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.rewrite import run as rr
from engine.rewrite.engines import Result
from engine.rewrite.transcript import clean_transcript

YT = "\n".join(f"{i // 60}:{i % 60:02d}\n{i % 7 + 1} секунды\nФраза номер {i} про жизнь в Камбодже и людей" for i in range(0, 120, 4))


class CleanTests(unittest.TestCase):
    def test_youtube_copy(self) -> None:
        text = clean_transcript("0:00\n2 секунды\n[Музыка]\nДобрый вечер\n0:05\n5 секунд\nв 10:30 утра на рынке\n1:02:03\n1 час 2 минуты 3 секунды\nконец")
        self.assertEqual(text, "Добрый вечер в 10:30 утра на рынке конец")

    def test_srt_and_vtt(self) -> None:
        srt = "1\n00:00:01,000 --> 00:00:04,000\nHello there\n\n2\n00:00:04,500 --> 00:00:07,000\ngeneral Kenobi"
        self.assertEqual(clean_transcript(srt), "Hello there general Kenobi")
        vtt = "WEBVTT\nKind: captions\n\n00:00:01.000 --> 00:00:03.000\n<c>Привет</c>\n\n00:00:03.000 --> 00:00:05.000\nмир"
        self.assertEqual(clean_transcript(vtt), "Привет мир")

    def test_leading_timestamps_inline(self) -> None:
        self.assertEqual(clean_transcript("0:00 Начало\n0:07 – продолжение\n[00:12] финал"), "Начало продолжение финал")

    def test_english_durations(self) -> None:
        self.assertEqual(clean_transcript("0:01\n1 minute, 5 seconds\nWelcome back"), "Welcome back")


class TextSourceTests(unittest.TestCase):
    def test_rewrite_from_text_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seen = {}

            def fake_llm(prompt, source, **kw):
                seen["source"] = source
                return Result("Новый рассказ. " * 300, "Название")

            with mock.patch.object(rr, "REWRITES_DIR", Path(tmp)), mock.patch.object(rr, "rewrite_llm", fake_llm), \
                    mock.patch.object(rr, "fetch_source", side_effect=AssertionError("сеть не нужна")), \
                    mock.patch("engine.rewrite.review.review_story", lambda story, **kw: __import__("engine.rewrite.review", fromlist=["Review"]).Review(story)):
                out = rr.rewrite_video(rr.RewriteRequest(url="", prompt="Перепиши {языке}", llm=("k", "u", ""),
                                                         source_text=YT, source_name="Камбоджа.txt"))
            self.assertNotIn("0:04", seen["source"])
            self.assertNotIn("секунды", seen["source"])
            self.assertIn("Фраза номер 4 про жизнь", seen["source"])
            self.assertEqual(out.source.title, "Камбоджа")
            self.assertTrue(out.story_file.name.startswith("Камбоджа"))
            self.assertEqual(out.source.origin, "text")

    def test_too_little_speech(self) -> None:
        with self.assertRaises(Exception):
            rr.text_source("0:00\n2 секунды\nпривет\n0:02\nпока")


if __name__ == "__main__":
    unittest.main()
