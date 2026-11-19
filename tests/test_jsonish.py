"""Разбор JSON из ответа нейросети — один на все места."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import unittest

from engine.core.jsonish import json_dict, json_object


class JsonObjectTests(unittest.TestCase):
    def test_plain(self) -> None:
        self.assertEqual(json_object('{"a": 1}'), {"a": 1})

    def test_fence_and_chatter(self) -> None:
        raw = 'Вот ответ:\n```json\n{"a": [1, 2], "b": "x"}\n```\nНадеюсь, помог {не очень}.'
        self.assertEqual(json_object(raw), {"a": [1, 2], "b": "x"})

    def test_text_around_without_fence(self) -> None:
        self.assertEqual(json_object('Конечно! {"stars": []} Готово.'), {"stars": []})

    def test_broken_or_missing(self) -> None:
        self.assertIsNone(json_object('{"a": '))
        self.assertIsNone(json_object("нет json"))
        self.assertIsNone(json_object(None))
        self.assertIsNone(json_object("[1, 2]"), "массив — не объект")
        self.assertEqual(json_dict("мусор"), {})


if __name__ == "__main__":
    unittest.main()
