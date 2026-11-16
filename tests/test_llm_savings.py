"""Экономия ключа LLM: главы по началам фраз, запросы кадров пачками по 32, без лишних повторов."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import unittest
from unittest import mock

from engine.core import chapters as ch
from engine.slides import llm_client


class ChapterPromptTests(unittest.TestCase):
    def test_prompt_keeps_only_sentence_starts(self) -> None:
        long = " ".join(f"слово{i}" for i in range(30)) + "."
        sents = [(i * 10.0, long) for i in range(50)]
        prompt = ch._prompt(sents, 5, 8, "ru")
        self.assertIn("слово9 …", prompt)
        self.assertNotIn("слово10", prompt)
        self.assertIn("[49] 8:10", prompt, "номер и время каждой фразы на месте")

    def test_quota_stops_model_rotation(self) -> None:
        calls = []

        class Quota:
            def __init__(self, *a, **kw):
                self.busy_wait = 0

            def complete(self, **kw):
                calls.append(1)
                raise llm_client.LLMQuotaError("квота")

            def close(self):
                pass

        sents = [(i * 30.0, f"Фраза номер {i} про страну.") for i in range(40)]
        with mock.patch.object(llm_client, "LLMClient", Quota):
            self.assertEqual(ch.plan_chapters(sents, 1200, language="ru", llm=("k", "u", "m")), [])
        self.assertEqual(len(calls), 1, "другие модели того же ключа не перебираем")


class QueryBatchTests(unittest.TestCase):
    def _beats(self, n):
        from engine.slides.strict_media import Beat

        return [Beat(start=i * 3, end=i * 3 + 3, text=f"кусок {i}") for i in range(n)]  # всё в первых 2 мин

    def _plan(self):
        from engine.slides.story_context import StoryContext
        from engine.slides.theme_plan import ThemePlan

        return ThemePlan(summary="s", context=StoryContext(era="modern", place="Laos", subject="place"),
                         video_queries=["q"], image_queries=["q"])

    def test_32_beats_one_request(self) -> None:
        from engine.slides import strict_media as sm

        client = mock.Mock()
        client.complete.side_effect = lambda **kw: json.dumps({"queries": [f"q{i}" for i in range(32)]})
        out = sm._plan_queries(client, self._plan(), self._beats(32), None)
        self.assertEqual(client.complete.call_count, 1)
        self.assertEqual(out[5], "q5")

    def test_wrong_answer_count_reasked_in_halves(self) -> None:
        """Ответов меньше, чем кусков, — иначе запросы съехали бы на чужие куски."""
        from engine.slides import strict_media as sm

        def answer(**kw):
            n = kw["user"].count("\n", kw["user"].index("BEATS:")) - 2
            if n == 32:
                return json.dumps({"queries": ["x"] * 30})
            return json.dumps({"queries": [f"half{n}_{i}" for i in range(n)]})

        client = mock.Mock()
        client.complete.side_effect = answer
        out = sm._plan_queries(client, self._plan(), self._beats(32), None)
        self.assertEqual(client.complete.call_count, 3)
        self.assertEqual(out[0], "half16_0")
        self.assertEqual(out[16], "half16_0")
        self.assertEqual(out[31], "half16_15")


if __name__ == "__main__":
    unittest.main()
