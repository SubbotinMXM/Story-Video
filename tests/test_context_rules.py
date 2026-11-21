"""Кадры подбираются по теме всего ролика: слово с несколькими значениями («гусеница») читается по контексту."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import unittest

from engine.slides import ai_quality, judge, strict_media, theme_plan


class AmbiguityRuleTests(unittest.TestCase):
    def test_every_prompt_that_writes_queries_or_rates_frames_has_the_rule(self) -> None:
        for name, text in (("запросы по смыслу", theme_plan.QUERY_SYSTEM), ("запросы к фразам", strict_media._QUERY_SYSTEM),
                           ("AI-описания", ai_quality._SYSTEM)):
            self.assertIn("THIS FILM'S TOPIC", text, name)
            self.assertIn("tank", text, name)
        self.assertIn("wrong meaning of the word", judge._SYSTEM, "оценка кадров: чужое значение слова — брак")
        # у зрячей оценки по-прежнему есть паспорт фильма, к которому всё привязано
        self.assertIn("THIS film's world", judge._SYSTEM)

    def test_rule_is_the_same_text_in_all_query_prompts(self) -> None:
        from engine.slides.story_context import AMBIGUITY_RULE

        for text in (theme_plan.QUERY_SYSTEM, strict_media._QUERY_SYSTEM, ai_quality._SYSTEM):
            self.assertIn(AMBIGUITY_RULE, text)


if __name__ == "__main__":
    unittest.main()
