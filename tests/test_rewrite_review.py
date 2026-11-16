"""Проверка рерайта: точечные правки имён/смысла и замена слабого хука одним запросом."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import unittest
from unittest import mock

from engine.rewrite import review as rv

BODY = " ".join(f"Предложение номер {i} о том, как Анна искала сына в чужом городе." for i in range(60))
STORY = "Анна проснулась рано. Было холодно. " + BODY + " Мария обняла сына и заплакала."
LLM = ("k", "https://llm.example/v1", "m")


class FakeClient:
    answer: dict = {}
    calls: list = []

    def __init__(self, *a, **kw) -> None:
        self.busy_wait = 0

    def complete(self, *, label, system, user, model=None):
        FakeClient.calls.append((label, system, user))
        if isinstance(FakeClient.answer, Exception):
            raise FakeClient.answer
        return "```json\n" + json.dumps(FakeClient.answer, ensure_ascii=False) + "\n```"

    def close(self) -> None:
        pass


class ReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeClient.calls = []
        p = mock.patch("engine.slides.llm_client.LLMClient", FakeClient)
        p.start()
        self.addCleanup(p.stop)

    def run_review(self, answer, **kw):
        FakeClient.answer = answer
        return rv.review_story(STORY, section=kw.pop("section", "broll"), llm=LLM, **kw)

    def test_name_fix_applied_pointwise(self) -> None:
        res = self.run_review({"fixes": [{"find": "Мария обняла сына", "replace": "Анна обняла сына", "why": "имя"}],
                               "hook_score": 9, "hook": ""})
        self.assertIn("Анна обняла сына и заплакала.", res.text)
        self.assertNotIn("Мария", res.text)
        self.assertEqual((res.fixed, res.hook), (1, False))
        self.assertEqual(len(FakeClient.calls), 1, "одна проверка, без повторов")

    def test_misquoted_fix_skipped(self) -> None:
        res = self.run_review({"fixes": [{"find": "Этого нет в тексте вообще", "replace": "x"}], "hook_score": 9})
        self.assertEqual(res.text, STORY.strip())
        self.assertEqual(res.fixed, 0)

    def test_fix_with_different_spacing(self) -> None:
        res = self.run_review({"fixes": [{"find": "Мария   обняла\nсына", "replace": "Анна обняла сына"}], "hook_score": 9})
        self.assertEqual(res.fixed, 1)

    def test_weak_hook_replaced_as_first_paragraph(self) -> None:
        hook = ("Анна не знала, что её сын жив. Семнадцать лет она хранила его фотографию, а потом в чужом городе "
                "незнакомец назвал её мамой. Что он скрывал все эти годы и почему вернулся именно сейчас? "
                "Ответ перевернул всю её жизнь.")
        res = self.run_review({"fixes": [], "hook_score": 3, "hook": hook})
        self.assertTrue(res.hook)
        self.assertTrue(res.text.startswith(hook + "\n\n"))
        self.assertNotIn("Анна проснулась рано", res.text, "слабое начало заменено")
        self.assertIn("Мария обняла сына", res.text, "остальной текст не тронут")

    def test_strong_hook_kept(self) -> None:
        res = self.run_review({"fixes": [], "hook_score": 9, "hook": "Новое начало " * 10})
        self.assertFalse(res.hook)
        self.assertTrue(res.text.startswith("Анна проснулась рано"))

    def test_bad_hook_rejected(self) -> None:
        res = self.run_review({"fixes": [], "hook_score": 2, "hook": "Подпишитесь на канал и поставьте лайк! " * 5})
        self.assertFalse(res.hook, "призыв вместо хука не ставим")
        res = self.run_review({"fixes": [], "hook_score": 2, "hook": "Коротко."})
        self.assertFalse(res.hook, "слишком короткий хук не ставим")

    def test_llm_failure_keeps_text(self) -> None:
        res = self.run_review(RuntimeError("503 overloaded"))
        self.assertEqual(res.text, STORY)
        self.assertIn("пропущена", res.notes[0])

    def test_no_key_no_request(self) -> None:
        res = rv.review_story(STORY, section="broll", llm=None)
        self.assertEqual(res.text, STORY)
        self.assertEqual(FakeClient.calls, [])

    def test_slides_get_source_and_fact_rules(self) -> None:
        self.run_review({"fixes": [], "hook_score": 9}, section="slides", source="Грузия, Тбилиси, исходник.")
        _label, system, user = FakeClient.calls[0]
        self.assertIn("фактические ошибки", system)
        self.assertIn("ИСХОДНИК", user)

    def test_broll_gets_cast_not_source(self) -> None:
        self.run_review({"fixes": [], "hook_score": 9}, source="исходник", cast=["Анна, мать, 52"])
        _label, system, user = FakeClient.calls[0]
        self.assertIn("путаница имён", system)
        self.assertIn("Анна, мать, 52", user)
        self.assertNotIn("ИСХОДНИК", user)

    def test_opening_is_whole_sentences(self) -> None:
        head, rest = rv.opening(STORY)
        self.assertTrue(head.endswith("."))
        self.assertGreaterEqual(len(head.split()), rv.OPENING_WORDS)
        self.assertEqual(f"{head} {rest}", STORY.strip())


if __name__ == "__main__":
    unittest.main()


class YoTests(unittest.TestCase):
    def test_eshche_and_ee_get_yo(self) -> None:
        text, n = rv.fix_yo("Еще раз она увидела ее письмо и еще одно. Её же. Неее. Мое, свое, еще-то")
        self.assertEqual(text, "Ещё раз она увидела её письмо и ещё одно. Её же. Неее. Мое, свое, еще-то")
        self.assertEqual(n, 3)

    def test_review_prompt_mentions_tts_stress(self) -> None:
        self.assertIn("ИИ-диктор", rv._COMMON)


class PromptTtsTests(unittest.TestCase):
    def test_both_default_prompts_have_tts_rules(self) -> None:
        from engine.rewrite.prompts import DEFAULT_PROMPTS

        for p in DEFAULT_PROMPTS:
            self.assertIn("ИИ-диктор", p["text"], p["id"])
            self.assertIn("«дома» → «у себя дома»", p["text"], p["id"])


from engine.rewrite import engines as en  # noqa: E402


class ReviewOrderTests(unittest.TestCase):
    """Проверка рерайта: Claude → Codex → встроенная; весь текст одним запросом к подписке."""

    STORY = "Слово " * 300

    def test_subscription_first_then_next(self) -> None:
        from engine.core import switches
        from engine.rewrite import review as rv_mod

        asked = []

        def fake_sub(engine, prompt, *, model="", cancel=None, timeout=None):
            asked.append((engine, model, len(prompt)))
            if engine == "claude_code":
                raise en.RewriteError("лимит", kind="limit")
            return '{"fixes": [], "hook_score": 9}'

        got = []
        switches.set_callback(lambda job, item: got.append(item))
        self.addCleanup(switches.set_callback, None)
        with mock.patch.object(en, "run_sub", fake_sub):
            out = rv_mod.review_story(self.STORY, section="broll", llm=None,
                                      subs=[("claude_code", "opus"), ("codex", "gpt-6.1-sol")])
        self.assertEqual([a[:2] for a in asked], [("claude_code", "opus"), ("codex", "gpt-6.1-sol")])
        self.assertGreater(asked[0][2], len(self.STORY), "весь текст одним запросом")
        self.assertEqual(out.hook_score, 9)
        self.assertEqual([(g["from"], g["to"]) for g in got], [("Claude", "Codex (ChatGPT)")])

    def test_builtin_last(self) -> None:
        from engine.rewrite import review as rv_mod

        def fake_sub(engine, prompt, **kw):
            raise en.RewriteError("нет входа", kind="auth")

        class FakeClient:
            busy_wait = 0

            def __init__(self, *a):
                pass

            def complete(self, **kw):
                return '{"fixes": [], "hook_score": 7}'

            def close(self):
                pass

        with mock.patch.object(en, "run_sub", fake_sub), \
                mock.patch("engine.slides.llm_client.LLMClient", FakeClient):
            out = rv_mod.review_story(self.STORY, section="broll", llm=("k", "u", "m"), subs=[("codex", "")])
        self.assertEqual(out.hook_score, 7)
