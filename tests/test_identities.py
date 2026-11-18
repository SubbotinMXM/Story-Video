"""Кто есть кто: тёзки отсеиваются по подборке, сомнения — вопрос пользователю, уточнения в скобках главнее."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import unittest
from unittest import mock

from engine.slides import identities as idm
from engine.slides.thumbnail import parse_names


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


class IdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeLLM.users = []
        p = mock.patch("engine.slides.llm_client.LLMClient", FakeLLM)
        p.start()
        self.addCleanup(p.stop)

    def test_dash_part_is_hint_not_name(self) -> None:
        from engine.slides.identities import split_hint

        self.assertEqual(split_hint("Marga Barbu — Марга Барбу"), ("Marga Barbu", "Марга Барбу"))
        self.assertEqual(split_hint("Андрей Мартынов - старшина Васков"), ("Андрей Мартынов", "старшина Васков"))
        self.assertEqual(split_hint("Draga Olteanu-Matei"), ("Draga Olteanu-Matei", ""))

    def test_comment_after_comma_or_slash_stays_with_name(self) -> None:
        from engine.slides.identities import split_hint

        got = parse_names("Андрей Миронов, советский актёр, 1941–1987\nMarga Barbu / Марга Барбу, актриса\nОлег Даль, Олег Ефремов")
        self.assertEqual(len(got), 4, got)
        self.assertEqual(split_hint(got[0]), ("Андрей Миронов", "советский актёр; 1941–1987"))
        self.assertEqual(split_hint(got[1]), ("Marga Barbu", "Марга Барбу; актриса"))
        self.assertEqual(got[2:], ["Олег Даль", "Олег Ефремов"])

    def test_parse_names_keeps_commas_in_hint(self) -> None:
        self.assertEqual(parse_names("Андрей Миронов (советский актёр, 1941–1987), Юрий Никулин"),
                         ["Андрей Миронов (советский актёр, 1941–1987)", "Юрий Никулин"])
        self.assertEqual(idm.split_hint("Андрей Миронов (советский актёр)"), ("Андрей Миронов", "советский актёр"))

    def test_doubt_without_hint_asks_user(self) -> None:
        FakeLLM.answer = {"people": [{"name": "Александр Петров", "who": "актёр", "doubt": "советский / российский"}]}
        cast = idm.identify(["Александр Петров"], llm=("k", "u", ""))
        self.assertEqual(cast.doubts, ["Александр Петров: советский / российский"])
        self.assertIn("Уточни", idm.doubt_message(cast.doubts, "в скобках"))

    def test_user_hint_removes_doubt_and_goes_to_llm(self) -> None:
        FakeLLM.answer = {"people": [{"name": "Александр Петров", "who": "мультипликатор", "doubt": "кто-то"}]}
        cast = idm.identify(["Александр Петров (советский мультипликатор)"], llm=("k", "u", ""))
        self.assertEqual(cast.doubts, [])
        self.assertIn("уточнение пользователя: советский мультипликатор", FakeLLM.users[0])
        self.assertEqual(cast.people[0].who, "мультипликатор")

    def test_no_llm_plain_names(self) -> None:
        cast = idm.identify(["Юрий Никулин"], llm=None)
        self.assertEqual((cast.people[0].name, cast.doubts), ("Юрий Никулин", []))

    def test_thumbnail_search_uses_who_first(self) -> None:
        from engine.slides import thumbnail as th

        seen = []
        person = idm.Person("Андрей Миронов", who="советский актёр", search="Андрей Миронов советский актёр")
        with mock.patch("engine.slides.person_media._search", lambda clients, src, qs, **kw: seen.append(qs) or []):
            th.find_portrait("Андрей Миронов", {}, None, __import__("pathlib").Path("/tmp"), person=person)
        self.assertEqual(seen[0][0], '"Андрей Миронов" советский актёр')
        self.assertEqual(seen[0][-1], '"Андрей Миронов"', "голое имя — последним")


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
