"""Тексты в ролике из полей раздела: перед началом, ~30-я секунда, в конце. Пусто — ничего, других призывов нет."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.rewrite import cta
from engine.rewrite.prompts import DEFAULT_PROMPTS

BODY = "\n\n".join(" ".join(f"Анна вспомнила день номер {i * 5 + k}, когда всё было иначе." for k in range(5))
                   for i in range(8))
START = "Здравствуйте! Напишите в комментариях, откуда вы нас слушаете."
MID = "Подпишитесь на канал, а теперь продолжим."
END = "Спасибо, что дослушали до конца!"


def no_ask(**kw):
    raise AssertionError("язык тот же — перевод не нужен")


class PlaceTests(unittest.TestCase):
    def test_empty_fields_mean_text_untouched(self) -> None:
        text, notes = cta.place_texts(BODY, cta.VoiceTexts())
        self.assertEqual(text, BODY)
        self.assertEqual(notes, [])

    def test_all_three_in_their_places(self) -> None:
        with mock.patch.object(cta, "_ask", no_ask):
            text, notes = cta.place_texts(BODY, cta.VoiceTexts(START, MID, END), language="ru")
        self.assertTrue(text.startswith(START + "\n\n"))
        self.assertTrue(text.endswith("\n\n" + END))
        before = text.split(MID)[0][len(START):]
        self.assertTrue(60 <= len(before.split()) <= 120, len(before.split()))
        self.assertTrue(before.rstrip().endswith("."), "вставка — на границе предложения")
        self.assertEqual(len(notes), 3)

    def test_only_given_fields(self) -> None:
        with mock.patch.object(cta, "_ask", no_ask):
            text, _ = cta.place_texts(BODY, cta.VoiceTexts(mid=MID, end=END), language="ru")
        self.assertTrue(text.startswith("Анна"), "начало пустое — рассказ с первой фразы")
        self.assertEqual(text.count(MID), 1)
        self.assertTrue(text.endswith(END))

    def test_short_story_skips_mid(self) -> None:
        short = "Анна открыла дверь. Муж молчал у окна."
        with mock.patch.object(cta, "_ask", no_ask):
            text, notes = cta.place_texts(short, cta.VoiceTexts(mid=MID), language="ru")
        self.assertEqual(text, short)
        self.assertIn("слишком короткий", notes[0])

    def test_other_language_translated_once_and_cached(self) -> None:
        story = " ".join("Ana a deschis ușa și a văzut că nu era nimeni în casă." for _ in range(30))
        asked = []

        def ask(**kw):
            asked.append(kw["user"])
            return json.dumps({"start": "Bună ziua!", "end": "Mulțumim!"})

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cta, "_ask", ask):
            texts = cta.VoiceTexts(start="Здравствуйте!", end="Спасибо!")
            first, _ = cta.place_texts(story, texts, llm=("k", "u", "m"), cache_dir=Path(tmp))
            second, _ = cta.place_texts(story, texts, llm=("k", "u", "m"), cache_dir=Path(tmp))
        self.assertTrue(first.startswith("Bună ziua!"))
        self.assertTrue(first.endswith("Mulțumim!"))
        self.assertNotIn("Здравствуйте", first)
        self.assertEqual(first, second)
        self.assertEqual(len(asked), 1, "перевод запомнен")
        self.assertIn("«ro»", asked[0])

    def test_translation_failure_stops_instead_of_wrong_language(self) -> None:
        story = " ".join("Ana a deschis ușa și a văzut că nu era nimeni în casă." for _ in range(30))
        with mock.patch.object(cta, "_ask", lambda **kw: "не JSON"):
            with self.assertRaises(RuntimeError):
                cta.place_texts(story, cta.VoiceTexts(end="Спасибо, что дослушали!"))

    def test_section_fields_are_separate(self) -> None:
        cfg = {"broll_text_start": "  Привет  ", "slides_text_end": "Пока"}
        self.assertEqual(cta.section_texts(cfg, "broll"), cta.VoiceTexts(start="Привет"))
        self.assertEqual(cta.section_texts(cfg, "slides"), cta.VoiceTexts(end="Пока"))


class PromptTests(unittest.TestCase):
    def test_default_prompts_leave_ctas_to_program(self) -> None:
        for p in DEFAULT_PROMPTS:
            self.assertNotIn("Дамы и господа", p["text"])
            self.assertNotIn("около тридцатой секунды", p["text"])

    def test_strip_removes_model_ctas(self) -> None:
        story = "Анна открыла дверь. Дорогие зрители, поставьте лайк этому видео и подпишитесь на наш канал. Он уехал."
        text, removed = cta.strip_cta(story)
        self.assertEqual(text, "Анна открыла дверь. Он уехал.")
        self.assertEqual(removed, 1)


class JobTests(unittest.TestCase):
    def setUp(self) -> None:
        from server import runners

        self.runners = runners
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.broll = root / "футажи"
        self.broll.mkdir()
        (self.broll / "f.mp4").write_bytes(b"")
        self.intro = root / "интро"
        self.intro.mkdir()
        for p in (mock.patch.object(runners, "_need_tts", lambda cfg, audio: None),
                  mock.patch.object(runners, "_llm_opt", lambda cfg: ("k", "u", "m")),
                  mock.patch.object(cta, "_ask", no_ask)):
            p.start()
            self.addCleanup(p.stop)

    def _rewrite(self, section: str, cfg: dict):
        from engine.rewrite.run import RewriteOutput
        from engine.rewrite.source import Source

        story = Path(self.tmp.name) / "История.txt"
        story.write_text(BODY + "\n\nДорогие зрители, подпишитесь на наш канал и поставьте лайк этому видео.", encoding="utf-8")
        src = Source("https://youtu.be/x", "x", "История", "Канал", "ru", "текст " * 100, "subtitles")
        out = RewriteOutput(src, story, None, 20, "ru", Path(self.tmp.name), "встроенная нейросеть")
        seen = {}

        def fake_rewrite(req, **kw):
            seen["prompt"] = req.prompt
            return out

        desk = Path(self.tmp.name) / "desk"
        with mock.patch("engine.rewrite.run.rewrite_video", fake_rewrite), mock.patch("engine.slides.paths.DESKTOP", desk):
            params = {"url": "https://youtu.be/x", "section": section, "review": True}
            _t, run = self.runners._rewrite_job({"broll_dir": str(self.broll), "llm_api_key": "k", **cfg}, params, {})
            res = run(None, lambda p, m: None, None)
        return story.read_text(encoding="utf-8"), seen["prompt"], res

    def test_rewrite_only_cleans_texts_go_at_build(self) -> None:
        """Рерайт: призывы исходника прочь, своих не добавляет — тексты из полей ставит сборка ролика."""
        text, prompt, res = self._rewrite("broll", {"intro_dir": str(self.intro), "broll_text_end": END})
        self.assertNotIn("подпишитесь", text)
        self.assertNotIn(END, text)
        self.assertNotIn("Здравствуйте", text, "нет интро-видео — больше не повод для вступления")
        self.assertIn("не добавляй в рассказ никаких призывов", prompt)
        self.assertEqual(res["follow"]["params"]["language"], "ru")
        self.assertTrue(any("при сборке" in line for line in res["lines"]))

    def test_rewrite_with_empty_fields_has_no_ctas(self) -> None:
        text, _p, res = self._rewrite("slides", {})
        self.assertNotIn("подпишитесь", text.lower())
        self.assertTrue(text.startswith("Анна"))
        self.assertTrue(any("не будет призывов" in line for line in res["lines"]))

    def test_broll_build_inserts_field_texts(self) -> None:
        got = {}

        def fake_broll(req, **kw):
            got["text"] = req.story_text
            raise RuntimeError("стоп после озвучки")

        cfg = {"broll_dir": str(self.broll), "broll_text_start": START, "broll_text_end": END}
        _t, run = self.runners._broll(cfg, {"text": BODY}, {})
        job = mock.MagicMock(state={})
        with mock.patch.object(self.runners, "run_broll", fake_broll), \
             mock.patch.object(self.runners, "run_output_dir", lambda hint: Path(self.tmp.name) / "out"), \
             self.assertRaises(RuntimeError):
            run(job, lambda p, m: None, None)
        self.assertTrue(got["text"].startswith(START))
        self.assertTrue(got["text"].endswith(END))
        self.assertNotIn(MID, got["text"])

    def test_slides_build_inserts_field_texts_and_own_text_kept(self) -> None:
        got = {}

        def fake_pipeline(req, **kw):
            got["text"] = req.tts.text
            raise RuntimeError("стоп")

        own = BODY + "\n\nПодпишитесь на канал!"  # свой текст пользователя — не чистится
        cfg = {"llm_api_key": "k", "slides_text_mid": MID}
        _t, run = self.runners._slides_full(cfg, {"text": own}, {})
        job = mock.MagicMock(state={})
        with mock.patch.object(self.runners, "run_full_pipeline", fake_pipeline), \
             mock.patch.object(self.runners, "new_run_dir", lambda title_hint: Path(self.tmp.name) / "run"), \
             self.assertRaises(RuntimeError):
            run(job, lambda p, m: None, None)
        self.assertEqual(got["text"].count(MID), 1)
        self.assertIn("Подпишитесь на канал!", got["text"])
        self.assertFalse(got["text"].startswith(MID))

    def test_ready_audio_cannot_take_texts(self) -> None:
        text, notes = self.runners._with_texts({"broll_text_end": END}, {}, "broll", "", audio=True)
        self.assertEqual(text, "")
        self.assertIn("готовой озвучке", notes[0])


if __name__ == "__main__":
    unittest.main()
