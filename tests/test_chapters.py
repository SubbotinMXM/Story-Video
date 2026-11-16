"""Главы YouTube: «ГЛАВЫ:\n0:00 — …», 7–15 штук, таймкоды с учётом интро и скорости копии."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.core import chapters as ch


def good_llm(n: int = 8) -> mock.MagicMock:
    fake = mock.MagicMock()
    picks = [{"i": k * 15, "title": f"Дорога номер {k}"} for k in range(n)]
    fake.complete.return_value = json.dumps({"chapters": picks}, ensure_ascii=False)
    return fake


LLM = ("k", "u", "m")


def story(n: int = 120) -> str:
    return " ".join(f"Фраза номер {i} о долгой дороге через горы и степи." for i in range(n))


class ChaptersTests(unittest.TestCase):
    def test_sentence_times_follow_word_timings(self) -> None:
        words = [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4} for i, w in enumerate("Один два три. Четыре пять шесть.".split())]
        sents = ch.timed_sentences("Один два три. Четыре пять шесть.", words, 3.0)
        self.assertEqual([t for t, _ in sents], [0.0, 1.5])

    def test_proportional_without_timings(self) -> None:
        sents = ch.timed_sentences("Аааа. Бббб.", None, 10.0)
        self.assertEqual(sents[0][0], 0.0)
        self.assertAlmostEqual(sents[1][0], 5.0, delta=0.6)

    def test_format_like_youtube_with_intro_and_copy_speed(self) -> None:
        chapters = [(0.0, "Страна, из которой бежали"), (100.0, "Страна на краю карты"), (700.0, "Дорога домой")]
        text = ch.format_chapters(chapters, language="ru", offset=11.0, total=900)
        self.assertEqual(text, "ГЛАВЫ:\n0:00 — Страна, из которой бежали\n1:51 — Страна на краю карты\n11:51 — Дорога домой\n")
        copy = ch.format_chapters(chapters, language="ru", offset=11.0, rate=1.01, total=900)
        self.assertIn("1:49 — Страна на краю карты", copy)
        en = ch.format_chapters(chapters, language="en", total=4000)
        self.assertTrue(en.startswith("CHAPTERS:\n0:00:00 — "))

    def test_llm_chapters_cleaned_and_spaced(self) -> None:
        text = story()
        sents = ch.timed_sentences(text, None, 1200.0)
        picks = [{"i": 0, "title": "1. Начало пути."}, {"i": 1, "title": "Слишком рано"}]
        picks += [{"i": k * 10, "title": f"Глава {k}"} for k in range(1, 12)]
        fake = mock.MagicMock()
        fake.complete.return_value = "```json\n" + json.dumps({"chapters": picks}, ensure_ascii=False) + "\n```"
        with mock.patch("engine.slides.llm_client.LLMClient", return_value=fake):
            out = ch.plan_chapters(sents, 1200.0, language="ru", llm=("k", "u", "m"))
        self.assertEqual(out[0], (0.0, "Начало пути"))
        self.assertNotIn("Слишком рано", [t for _, t in out], "глава через 10 с после начала — выброшена")
        self.assertTrue(7 <= len(out) <= 15, len(out))
        gaps = [b[0] - a[0] for a, b in zip(out, out[1:])]
        self.assertTrue(all(g >= ch.MIN_GAP for g in gaps))

    def test_without_llm_no_chapters(self) -> None:
        sents = ch.timed_sentences(story(), None, 1800.0)
        self.assertEqual(ch.plan_chapters(sents, 1800.0, llm=None), [], "обрывки фраз вместо названий — хуже, чем ничего")

    def test_llm_failure_tries_other_models_then_gives_up(self) -> None:
        fake = mock.MagicMock()
        fake.complete.side_effect = RuntimeError("нет сети")
        sents = ch.timed_sentences(story(), None, 1800.0)
        with mock.patch("engine.slides.llm_client.LLMClient", return_value=fake) as made:
            out = ch.plan_chapters(sents, 1800.0, llm=LLM)
        self.assertEqual(out, [])
        self.assertEqual(made.call_count, ch._MODEL_TRIES)

    def test_content_filter_on_one_model_other_model_writes(self) -> None:
        refused = mock.MagicMock()
        refused.complete.side_effect = RuntimeError("Error code: 403 - rejected by our content policy")
        sents = ch.timed_sentences(story(), None, 1800.0)
        with mock.patch("engine.slides.llm_client.LLMClient", side_effect=[refused, good_llm()]):
            out = ch.plan_chapters(sents, 1800.0, llm=LLM)
        self.assertEqual(len(out), 8)

    def test_content_filter_on_whole_text_splits_into_parts(self) -> None:
        """Фильтр сервиса ложно срабатывает на часть текста — остальное всё равно размечается."""
        def answer(*, label, system, user):
            if "ЗАПРЕТ" in user:
                raise RuntimeError("Error code: 403 - content_policy_violation")
            idx = [int(x) for x in __import__("re").findall(r"^\[(\d+)\]", user, __import__("re").M)]
            return json.dumps({"chapters": [{"i": k, "title": f"Новая часть пути {k}"} for k in idx[::12]]}, ensure_ascii=False)

        fake = mock.MagicMock()
        fake.complete.side_effect = answer
        text = " ".join(f"Фраза номер {i} о дороге." if i not in (5,) else "ЗАПРЕТ здесь." for i in range(160))
        sents = ch.timed_sentences(text, None, 2400.0)
        with mock.patch("engine.slides.llm_client.LLMClient", return_value=fake):
            out = ch.plan_chapters(sents, 2400.0, language="ru", llm=LLM)
        self.assertGreaterEqual(len(out), 5)
        self.assertEqual(out[0][0], 0.0)
        self.assertGreater(out[-1][0], 1200, "вторая половина размечена")

    def test_fragment_titles_are_rewritten(self) -> None:
        fake = mock.MagicMock()
        bad = [{"i": k * 15, "title": f"Часть {k}: в этой стране можно иметь смартфон и…"} for k in range(8)]
        good = [{"i": k * 15, "title": f"Как государство следит за людьми {k}"} for k in range(8)]
        fake.complete.side_effect = [json.dumps({"chapters": bad}, ensure_ascii=False), json.dumps({"chapters": good}, ensure_ascii=False)]
        sents = ch.timed_sentences(story(), None, 1800.0)
        with mock.patch("engine.slides.llm_client.LLMClient", return_value=fake):
            out = ch.plan_chapters(sents, 1800.0, language="ru", llm=LLM)
        self.assertEqual(fake.complete.call_count, 2)
        self.assertIn("без многоточий", fake.complete.call_args.kwargs["user"])
        self.assertTrue(all(not t.endswith("…") for _, t in out))
        self.assertEqual(len(out), 8)

    def test_title_rules(self) -> None:
        self.assertTrue(ch._title_ok("Граница на замке"))
        self.assertFalse(ch._title_ok("В этой стране можно иметь смартфон и…"))
        self.assertFalse(ch._title_ok("Такой вид не отменяет репрессий..."))
        self.assertFalse(ch._title_ok("Слово"))
        self.assertFalse(ch._title_ok("очень длинное название главы из десяти слов которое никто не дочитает"))

    def test_write_files_for_video_and_copies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch("engine.slides.llm_client.LLMClient", return_value=good_llm()):
            folder = Path(tmp)
            dest = ch.write_chapters(folder, text=story(), words=None, voice_duration=1800.0, language="ru", llm=LLM,
                                     offset=5.0, total=1815.0, copies=[("99 процентов главы.txt", 0.99)])
            self.assertEqual(dest, folder / "главы.txt")
            main = dest.read_text(encoding="utf-8").splitlines()
            copy = (folder / "99 процентов главы.txt").read_text(encoding="utf-8").splitlines()
            self.assertEqual(main[0], "ГЛАВЫ:")
            self.assertEqual(len(main), len(copy))
            self.assertTrue(main[1].startswith("0:00 — "))

    def test_short_video_no_chapters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ch.write_chapters(Path(tmp), text="Короткий рассказ. Совсем.", words=None, voice_duration=12.0))
            self.assertEqual(list(Path(tmp).iterdir()), [])


    def test_youtube_authors_under_chapters(self) -> None:
        from engine.slides.publish import attribution_block

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            run.mkdir()
            slots = [
                {"provider": "youtube", "id": "abc", "source_title": "Steppe from above", "source_author": "Nomad Drone"},
                {"provider": "youtube", "id": "abc", "source_title": "Steppe from above", "source_author": "Nomad Drone"},
                {"provider": "pexels", "id": "1"},
            ]
            (run / "timeline.json").write_text(json.dumps({"slots": slots}), encoding="utf-8")
            footer = attribution_block(run, "ru")
            self.assertEqual(footer, "Видеофрагменты (лицензия CC BY):\n• Nomad Drone — «Steppe from above» — https://youtu.be/abc\n")
            self.assertTrue(attribution_block(run, "de").startswith("Videoausschnitte"))
            folder = Path(tmp) / "out"
            with mock.patch("engine.slides.llm_client.LLMClient", return_value=good_llm()):
                ch.write_chapters(folder, text=story(), words=None, voice_duration=1800.0, language="ru", footer=footer,
                                  copies=[("99 процентов главы.txt", 0.99)], llm=LLM)
            text = (folder / "главы.txt").read_text(encoding="utf-8")
            self.assertTrue(text.startswith("ГЛАВЫ:\n0:00 — "))
            self.assertTrue(text.endswith("\n\n" + footer), "авторы — после пустой строки под главами")
            self.assertIn("youtu.be/abc", (folder / "99 процентов главы.txt").read_text(encoding="utf-8"))
            # короткий ролик: глав нет, но авторов указать всё равно нужно
            short = Path(tmp) / "short"
            ch.write_chapters(short, text="Коротко. Совсем.", words=None, voice_duration=12.0, footer=footer)
            self.assertEqual((short / "главы.txt").read_text(encoding="utf-8"), footer)

    def test_no_youtube_no_footer(self) -> None:
        from engine.slides.publish import attribution_block

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "timeline.json").write_text(json.dumps({"slots": [{"provider": "pexels", "id": "1"}]}), encoding="utf-8")
            self.assertEqual(attribution_block(Path(tmp)), "")


if __name__ == "__main__":
    unittest.main()


class DubChaptersTests(unittest.TestCase):
    """Перевод ролика: главы оригинала на тех же местах, названия — одним коротким запросом."""

    ORIG = " ".join(f"Глава {k} начинается здесь. " + "Обычная фраза про страну и людей. " * 12 for k in range(6))
    DUB = " ".join(f"Chapter {k} starts here. " + "An ordinary sentence about the country and people. " * 12 for k in range(6))

    def _llm(self, answers):
        calls = []

        class Fake:
            def __init__(self, *a, **kw):
                self.busy_wait = 0

            def complete(self, *, label, system, user, model=None):
                calls.append(label)
                return answers(label, user)

            def close(self):
                pass

        return Fake, calls

    def _orig_answer(self, label, user):
        return json.dumps({"chapters": [{"i": i * 13, "title": f"Часть номер {i} про страну"} for i in range(6)]})

    def test_dub_translates_titles_and_keeps_places(self) -> None:
        from engine.slides import llm_client

        def answers(label, user):
            if label == "chapters":
                return self._orig_answer(label, user)
            titles = json.loads(user)
            return json.dumps({"titles": [t.replace("Часть номер", "Part number").replace("про страну", "about country") for t in titles]})

        Fake, calls = self._llm(answers)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(llm_client, "LLMClient", Fake):
            ch.write_chapters(Path(tmp) / "ru", text=self.ORIG, words=None, voice_duration=1200, language="ru", llm=("k", "u", ""))
            ch.write_chapters(Path(tmp) / "en", text=self.DUB, words=None, voice_duration=1300, language="en",
                              llm=("k", "u", ""), base_text=self.ORIG)
            en = (Path(tmp) / "en" / ch.CHAPTERS_NAME).read_text(encoding="utf-8")
        self.assertEqual(calls, ["chapters", "chapters_translate"], "перевод — без новой разметки текста")
        self.assertIn("Part number 3 about country", en)
        self.assertEqual(en.count(" — "), 6)

    def test_bad_translation_falls_back_to_full_planning(self) -> None:
        from engine.slides import llm_client

        def answers(label, user):
            if label == "chapters_translate":
                return json.dumps({"titles": ["only one"]})
            return self._orig_answer(label, user)

        Fake, calls = self._llm(answers)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(llm_client, "LLMClient", Fake):
            ch.write_chapters(Path(tmp) / "ru", text=self.ORIG, words=None, voice_duration=1200, language="ru", llm=("k", "u", ""))
            ch.write_chapters(Path(tmp) / "en", text=self.DUB, words=None, voice_duration=1300, language="en",
                              llm=("k", "u", ""), base_text=self.ORIG)
        self.assertEqual(calls, ["chapters", "chapters_translate", "chapters"])
