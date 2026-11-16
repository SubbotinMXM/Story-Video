"""Пошаговый промпт рерайта: «===ШАГ===» — сначала отдельный разбор исходника, потом рерайт с ним."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.rewrite import analysis as an
from engine.rewrite.prompts import DEFAULT_PROMPTS


class SplitTest(unittest.TestCase):
    def test_split(self) -> None:
        self.assertEqual(an.split_steps("Разбери.\n===ШАГ===\nПерепиши."), ("Разбери.", "Перепиши."))
        self.assertEqual(an.split_steps("Перепиши."), ("", "Перепиши."))
        self.assertEqual(an.split_steps("  == ШАГ ==\nПерепиши."), ("", "Перепиши."), "пустой шаг — одно задание")

    def test_default_slides_prompt_is_two_steps(self) -> None:
        slides = next(p for p in DEFAULT_PROMPTS if p["section"] == "slides")
        task, rest = an.split_steps(slides["text"])
        self.assertIn("проанализируй", task)
        self.assertTrue(rest.startswith("Тогда исправим"))

    def test_carry(self) -> None:
        out = an.with_analysis("Перепиши.", "1. Затянуто начало")
        self.assertIn("РАЗБОР ИСХОДНИКА", out)
        self.assertIn("Затянуто начало", out)
        self.assertEqual(an.with_analysis("Перепиши.", ""), "Перепиши.")


class AnalyzeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())

    def test_claude_then_cached(self) -> None:
        calls = []

        def claude(prompt, **kw):
            calls.append(prompt)
            return "1. Слабый хук"

        with mock.patch("engine.rewrite.engines.run_claude", claude):
            a = an.analyze("Разбери.", "исходник", self.folder, engine="claude_code", llm=None)
            b = an.analyze("Разбери.", "исходник", self.folder, engine="claude_code", llm=None)
        self.assertEqual((a, b), ("1. Слабый хук", "1. Слабый хук"))
        self.assertEqual(len(calls), 1, "повтор задачи — разбор из файла")
        self.assertIn("ТОЛЬКО РАЗБОР", calls[0])
        self.assertTrue((self.folder / an.FILE).is_file())

    def test_claude_limit_falls_back_to_llm(self) -> None:
        from engine.rewrite.engines import RewriteError

        class Client:
            def __init__(self, *a):
                pass

            def complete(self, **kw):
                self.label = kw["label"]
                return "1. Ошибка в дате"

            def close(self):
                pass

        with mock.patch("engine.rewrite.engines.run_claude", side_effect=RewriteError("лимит", kind="limit")), \
                mock.patch("engine.slides.llm_client.LLMClient", Client):
            got = an.analyze("Разбери.", "исх", self.folder, engine="claude_code", llm=("k", "u", "m"))
        self.assertEqual(got, "1. Ошибка в дате")

    def test_failure_gives_empty(self) -> None:
        class Client:
            def __init__(self, *a):
                pass

            def complete(self, **kw):
                raise RuntimeError("down")

            def close(self):
                pass

        with mock.patch("engine.slides.llm_client.LLMClient", Client):
            self.assertEqual(an.analyze("Разбери.", "исх", self.folder, engine="llm", llm=("k", "u", "m")), "")


if __name__ == "__main__":
    unittest.main()
