"""Ответы зрителям: нейросеть пачками, пропуск спама, вариант YouTube — в приоритете, память ответов."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)
from _human import setUpModule, tearDownModule  # noqa: F401  (простые клики вместо «человеческих»)

import json
import shutil
import unittest
from unittest import mock

from engine.publish import comments
from engine.publish.store import publish_dir


class CommentTests(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(publish_dir(), ignore_errors=True)
        self.addCleanup(shutil.rmtree, publish_dir(), True)

    def test_replies_parsed_and_spam_skipped(self) -> None:
        cs = [comments.Comment("c1", "v", "@a", "Спасибо, Мурманск"), comments.Comment("c2", "v", "@b", "заработок тут bit.ly/x")]
        answer = json.dumps({"items": [{"id": "c1", "reply": "Большой привет Мурманску!", "skip": False},
                                       {"id": "c2", "reply": "", "skip": True}]}, ensure_ascii=False)
        with mock.patch.object(comments, "_ask", lambda user, llm: answer):
            got = comments.generate_replies(cs, ("k", "u", "m"))
        self.assertEqual(got, {"c1": "Большой привет Мурманску!", "c2": None})

    def test_batches_of_25(self) -> None:
        calls = []

        def ask(user, llm):
            calls.append(user)
            return '{"items": []}'

        with mock.patch.object(comments, "_ask", ask):
            comments.generate_replies([comments.Comment(f"c{i}", "v", "@a", "ок") for i in range(60)], ("k", "u", "m"))
        self.assertEqual(len(calls), 3)

    def test_prompt_asks_question_on_negative_and_no_signature(self) -> None:
        self.assertIn("встречный вопрос", comments._SYSTEM)
        self.assertIn("без подписи", comments._SYSTEM)

    def test_youtube_suggestion_first(self) -> None:
        page, thread = mock.MagicMock(), mock.MagicMock()
        thread.locator.return_value.all_inner_texts.return_value = ["Спасибо, что слушаете из Нарвы!", "Привет Эстонии!"]
        with mock.patch("engine.publish.youtube._act", lambda c: None):
            text, suggested = comments._reply_text(page, thread, "ответ нейросети", None)
        self.assertTrue(suggested)
        self.assertIn(text, ["Спасибо, что слушаете из Нарвы!", "Привет Эстонии!"])
        thread.locator.return_value.all_inner_texts.return_value = []
        with mock.patch("engine.publish.youtube._act", lambda c: None):
            self.assertEqual(comments._reply_text(page, thread, "ответ нейросети", None), ("ответ нейросети", False))

    def test_answered_remembered(self) -> None:
        comments.remember("UC1", "c1")
        self.assertEqual(comments.answered("UC1"), {"c1"})
        self.assertEqual(comments.answered("UC2"), set())


if __name__ == "__main__":
    unittest.main()
