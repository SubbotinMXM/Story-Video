"""Порядок для всех задач: Claude → Codex → встроенная нейросеть; массовые задачи — сразу встроенная."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import base64
import unittest
from unittest import mock

from engine.core import switches
from engine.rewrite import engines as en
from engine.slides import llm_client as lc


class SubPriorityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = lc.LLMClient("k", "m", "http://localhost:1")
        self.addCleanup(self.client.close)
        self.calls: list[tuple] = []
        self.builtin = mock.patch.object(lc.LLMClient, "_chat", lambda s, label, system, user, **kw: "встроенная").start()
        self.addCleanup(mock.patch.stopall)
        self.events: list[dict] = []
        switches.set_callback(lambda job, item: self.events.append(item))
        self.addCleanup(switches.set_callback, None)

    def _subs(self, outcomes: dict, available=("claude_code", "codex")):
        def fake_run(engine, prompt, *, model="", cancel=None, timeout=None, images=None):
            self.calls.append((engine, bool(images)))
            out = outcomes[engine]
            if isinstance(out, Exception):
                raise out
            return out
        mock.patch.object(en, "run_sub", fake_run).start()
        mock.patch.object(en, "sub_available", lambda e: e in available).start()
        mock.patch.object(en, "note_limit", lambda e, exc: None).start()

    def test_claude_first(self) -> None:
        self._subs({"claude_code": '{"ok": 1}', "codex": "не звать"})
        self.assertEqual(self.client.complete(label="theme_context", system="s", user="u"), '{"ok": 1}')
        self.assertEqual(self.calls, [("claude_code", False)])

    def test_claude_limit_then_codex_then_builtin(self) -> None:
        self._subs({"claude_code": en.RewriteError("лимит", kind="limit"), "codex": "ответ Codex"})
        self.assertEqual(self.client.complete(label="theme_context", system="s", user="u"), "ответ Codex")
        self.assertEqual([(e["from"], e["to"]) for e in self.events], [("Claude", "Codex (ChatGPT)")])
        self.calls.clear()
        self.events.clear()
        self._subs({"claude_code": en.RewriteError("x", kind="other"), "codex": en.RewriteError("нет входа", kind="auth")})
        self.assertEqual(self.client.complete(label="theme_context", system="s", user="u"), "встроенная")
        self.assertEqual(self.events[-1]["to"], "встроенная нейросеть")

    def test_no_logged_in_subscription_goes_builtin_silently(self) -> None:
        self._subs({}, available=())
        self.assertEqual(self.client.complete(label="theme_context", system="s", user="u"), "встроенная")
        self.assertEqual(self.events, [], "нет входа ни в одну подписку — без уведомлений на каждый запрос")

    def test_bulk_and_builtin_rewrite_parts_skip_subscriptions(self) -> None:
        self._subs({"claude_code": "нет", "codex": "нет"})
        img = base64.b64encode(b"jpg").decode()
        self.assertEqual(self.client.complete(label="chapters", system="s", user="u"), "встроенная", "главы — встроенной")
        for label in ("judge_pool", "judge_7", "judge_identity", "beat_queries_3"):
            self.assertEqual(self.client.complete_vision(label=label, system="s", user_text="u", image_b64=img)
                             if label.startswith("judge") else self.client.complete(label=label, system="s", user="u"),
                             "встроенная")
        self.assertEqual(self.client.complete(label="rewrite_part_2", system="s", user="u"), "встроенная")
        self.assertEqual(self.calls, [])

    def test_cover_vision_goes_to_subscription_with_image(self) -> None:
        self._subs({"claude_code": '{"best": 1}', "codex": "нет"})
        img = base64.b64encode(b"\xff\xd8jpeg").decode()
        out = self.client.complete_vision(label="cover_pick", system="s", user_text="u", image_b64=img)
        self.assertEqual(out, '{"best": 1}')
        self.assertEqual(self.calls, [("claude_code", True)])
