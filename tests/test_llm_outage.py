"""Сервис LLM лёг: текстовые запросы — в Claude Code, кадры — локально (CLIP), сервис не дёргаем 10 минут."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import unittest
from unittest import mock

import httpx

from engine.slides import llm_client as lc


def handshake() -> httpx.ConnectTimeout:
    return httpx.ConnectTimeout("_ssl.c:1064: The handshake operation timed out")


class OutageTests(unittest.TestCase):
    def setUp(self) -> None:
        lc._down_until = 0.0
        self.addCleanup(setattr, lc, "_down_until", 0.0)
        self.client = lc.LLMClient("k", "m", "https://example.invalid/v1")
        self.addCleanup(self.client.close)
        self.client._sleep = lambda sec: None
        self.create = mock.patch.object(self.client.client.chat.completions, "create").start()
        self.addCleanup(mock.patch.stopall)
        self.claude = mock.patch("engine.rewrite.engines.run_claude", return_value='{"ok": 1}').start()
        mock.patch.object(lc, "claude_fallback_ok", lambda: True).start()

    def test_unreachable_service_goes_to_claude_and_is_remembered(self) -> None:
        self.create.side_effect = handshake()
        self.assertEqual(self.client.complete(label="theme_queries", system="s", user="u"), '{"ok": 1}')
        self.assertEqual(self.create.call_count, lc.CONNECT_FAILS_DOWN, "две неудачи подключения — и хватит")
        self.assertTrue(lc.service_down())
        # следующий запрос — сразу в Claude, сервис не дёргаем
        self.client.complete(label="stars_cast", system="s", user="u")
        self.assertEqual(self.create.call_count, lc.CONNECT_FAILS_DOWN)
        self.assertEqual(self.claude.call_count, 2)
        self.assertIn("s", self.claude.call_args.args[0])

    def test_vision_while_down_is_skipped_for_local_scoring(self) -> None:
        self.create.side_effect = handshake()
        with self.assertRaises(lc.LLMUnavailable):
            self.client.complete_vision(label="judge_pool", system="s", user_text="u", image_b64="AAAA")
        self.assertTrue(lc.service_down())
        calls = self.create.call_count
        with self.assertRaises(lc.LLMUnavailable):
            self.client.complete_vision(label="judge_pool", system="s", user_text="u", image_b64="AAAA")
        self.assertEqual(self.create.call_count, calls, "лежит — сразу CLIP, без ожидания")
        self.claude.assert_not_called()

    def test_service_back_after_outage(self) -> None:
        lc._down_until = 1.0  # срок вышел
        resp = mock.MagicMock()
        resp.choices[0].message.content = "ответ"
        self.create.side_effect = None
        self.create.return_value = resp
        self.assertEqual(self.client.complete(label="t", system="s", user="u"), "ответ")
        self.assertEqual(lc._down_until, 0.0)
        self.claude.assert_not_called()

    def test_claude_also_fails_keeps_original_error(self) -> None:
        from engine.rewrite.engines import RewriteError

        self.create.side_effect = handshake()
        self.claude.side_effect = RewriteError("лимит", kind="limit")
        with self.assertRaises(httpx.ConnectTimeout):
            self.client.complete(label="t", system="s", user="u")

    def test_without_claude_error_as_before(self) -> None:
        mock.patch.object(lc, "claude_fallback_ok", lambda: False).start()
        self.create.side_effect = handshake()
        with self.assertRaises(httpx.ConnectTimeout):
            self.client.complete(label="t", system="s", user="u")
        self.claude.assert_not_called()


class JudgeOutageTests(unittest.TestCase):
    def test_judge_stays_on_after_outage(self) -> None:
        from engine.slides.judge import Judge
        from engine.slides.theme_plan import ThemePlan

        client = mock.MagicMock()
        judge = Judge(client, ThemePlan())
        for _ in range(10):
            judge._failed(lc.LLMUnavailable("лежит"))
        self.assertTrue(judge.enabled, "сервис оживёт — проверка продолжится")


class JunkTests(unittest.TestCase):
    def test_penalty_only_for_confident_junk(self) -> None:
        from engine.slides.clip_ranker import JUNK_MAX_PENALTY, junk_penalty

        self.assertEqual(junk_penalty(0.87), 0.0, "панорамы и групповые фото — не мусор")
        self.assertAlmostEqual(junk_penalty(1.0), JUNK_MAX_PENALTY)

    def test_textual_query_not_penalized(self) -> None:
        from engine.slides.clip_ranker import _TEXTUAL

        self.assertTrue(_TEXTUAL.search("old newspaper headline 1937"))
        self.assertFalse(_TEXTUAL.search("mountain village at sunset"))


if __name__ == "__main__":
    unittest.main()
