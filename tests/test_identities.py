"""Ролик про людей: тёзки — вопрос пользователю, уточнения главнее, точные запросы идут первыми."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import unittest
from unittest import mock


class FakeLLM:
    answer: dict = {}
    users: list = []

    def __init__(self, *a, **kw):
        self.busy_wait = 0

    def complete(self, *, label, system, user, model=None):
        FakeLLM.users.append(user)
        return json.dumps(FakeLLM.answer, ensure_ascii=False)

    def close(self):
        pass


class StarsCastTests(unittest.TestCase):
    def test_star_doubt_stops_with_question(self) -> None:
        from engine.slides import stars_media as sm
        from engine.slides.media_pipeline import MediaError

        answer = {"stars": [{"id": 1, "name": "Александр Петров", "search": "Александр Петров", "who": "актёр",
                             "doubt": "советский / российский"}], "parts": [{"i": 0, "star": 1}]}

        class Client(FakeLLM):
            def complete(self, **kw):
                return json.dumps(answer, ensure_ascii=False)

        with mock.patch("engine.slides.llm_client.LLMClient", Client), mock.patch.object(sm, "stars_notes", lambda: ""), \
                self.assertRaises(MediaError) as err:
            sm.plan_cast(("k", "u", ""), [(0.0, "Александр Петров родился…")])
        self.assertIn("Кто эти люди", str(err.exception))

    def test_star_queries_with_who_first(self) -> None:
        from engine.slides import stars_media as sm
        from engine.slides.person_media import person_query_set
        from engine.slides.theme_plan import ThemePlan

        star = sm.Star(1, "Андрей Миронов", "Andrei Mironov", "Андрей Миронов", "photo", "1941–1987", "советский актёр")
        imgs, _vids = person_query_set(sm._star_plan(ThemePlan(summary="s"), star, star.search), star.search, "photo")
        self.assertEqual(imgs[0], '"Андрей Миронов" советский актёр')


if __name__ == "__main__":
    unittest.main()
