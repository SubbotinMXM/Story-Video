"""Переписывание сценария по ссылке: промпты и переменные, части со связностью, Claude Code, субтитры, очередь."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.rewrite import engines as en
from engine.rewrite import prompts as pr
from engine.rewrite import source as so


def setUpModule() -> None:
    # проверка рерайта ходит в нейросеть — здесь её подменяем (свои тесты в test_rewrite_review)
    from engine.rewrite import review

    patch = mock.patch.object(review, "review_story", lambda story, **kw: review.Review(story))
    patch.start()
    unittest.addModuleCleanup(patch.stop)


class PromptTests(unittest.TestCase):
    def test_fill_variables_and_legacy_placeholders(self) -> None:
        text = pr.fill("ЯЗЫК: {язык}. Пиши на {языке} языке, {слов} слов. Старое: {указать язык}, {указать количество слов, по умолчанию 7000-8000}",
                       language="ro", words="5000–6000")
        self.assertEqual(text, "ЯЗЫК: румынский. Пиши на румынском языке, 5000–6000 слов. Старое: румынском, 5000–6000")

    def test_default_prompts_use_variables(self) -> None:
        for p in pr.DEFAULT_PROMPTS:
            self.assertEqual(pr.missing_variables(p["text"]), [], p["name"])
            self.assertNotIn("{указать", p["text"])
        self.assertEqual(pr.missing_variables("Перепиши текст"), ["{язык}", "{слов}"])

    def test_word_range(self) -> None:
        self.assertEqual(pr.word_range("7000–8000"), (7000, 8000))
        self.assertEqual(pr.word_range("8 000 - 7 000"), (7000, 8000))
        self.assertEqual(pr.word_range("5000"), (4500, 5500))

    def test_pick_prompt_by_section(self) -> None:
        self.assertEqual(pr.pick_prompt([], {}, "slides")["id"], "default-slides")
        mine = [{"id": "a", "name": "A", "section": "broll", "text": "x"}, {"id": "b", "name": "B", "section": "broll", "text": "y"}]
        self.assertEqual(pr.pick_prompt(mine, {"broll": "b"}, "broll")["id"], "b")
        self.assertEqual(pr.pick_prompt(mine, {"broll": "b"}, "broll", "a")["id"], "a")
        with self.assertRaises(ValueError):
            pr.pick_prompt(mine, {}, "slides")


class OutputTests(unittest.TestCase):
    def test_parse_story_and_package(self) -> None:
        raw = f"Вот результат.\n{en.STORY_MARK}\n## Часть 1\nОна вошла в дом. **Тихо.**\n{en.PACKAGE_MARK}\nНазвание: Дом\n"
        _plan, story, package = en._split_marks(raw)
        self.assertEqual(story, "Она вошла в дом. Тихо.")
        self.assertEqual(package, "Название: Дом")

    def test_without_marks_all_is_story(self) -> None:
        self.assertEqual(en._split_marks("Просто текст.")[1], "Просто текст.")


class FakeLLM:
    """Встроенная LLM: план, части, пакет. Пишет factor × просимого объёма; запоминает, что ей прислали."""

    def __init__(self, short_first: bool = False, fail_at: int | None = None, factor: float = 1.0,
                 last_factor: float | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.short_first = short_first
        self.fail_at = fail_at
        self.factor = factor
        self.last_factor = last_factor

    def complete(self, *, label: str, system: str, user: str) -> str:
        import re as _re

        self.calls.append((label, system, user))
        if label == "rewrite_plan":
            n = int(_re.search(r"Частей ровно (\d+)", system).group(1))
            parts = [{"n": i + 1, "beats": f"События части {i + 1}", "words": 300} for i in range(n)]
            return json.dumps({"names": {"Мария": "Елена"}, "characters": [{"name": "Елена"}], "setting": "Тула",
                               "style": "тёплый", "parts": parts}, ensure_ascii=False)
        if label.startswith("rewrite_part_"):
            n = int(label.split("_")[2])
            if self.fail_at == n:
                raise RuntimeError("сервис лёг")
            if self.short_first and label == "rewrite_part_1":
                return "Коротко."
            asked = int(_re.search(r"около (\d+) слов", system).group(1))
            last = "последняя часть" in system or "весь текст целиком" in system
            f = self.last_factor if last and self.last_factor is not None else self.factor
            if last and "Прошлый вариант" in user:
                f = 1.0  # повтор финала с точным объёмом — попадает
            return f"Часть {n}: Мария шла домой. " + "слово " * max(1, int(asked * f) - 5)
        if label == "rewrite_package":
            return "Название: Чужой дом"
        raise AssertionError(label)

    def close(self) -> None:
        pass


class ChunkedTests(unittest.TestCase):
    def _run(self, fake: FakeLLM, state: Path, words: str = "7000–8000") -> en.Result:
        with mock.patch("engine.slides.llm_client.LLMClient", return_value=fake):
            return en.rewrite_llm("ЗАДАНИЕ", "Мария вернулась. " * 200, words=words, llm=("k", "u", "m"), state_file=state)

    def test_parts_in_order_with_continuity_and_name_fix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeLLM()
            res = self._run(fake, Path(tmp) / "state.json")
        labels = [c[0] for c in fake.calls]
        self.assertEqual(labels, ["rewrite_plan", *[f"rewrite_part_{i}" for i in range(1, 5)], "rewrite_package"],
                         "7000–8000 — 4 части по ~1900 слов, а не 7: промпт и план уходят реже")
        second = fake.calls[2][2]
        self.assertIn("ПОСЛЕДНИЕ СТРОКИ ПРЕДЫДУЩЕЙ ЧАСТИ", second, "стык с предыдущей частью")
        self.assertIn("Елена", second, "паспорт с новыми именами в каждой части")
        self.assertIn("ЧАСТЬ 1 из 4", fake.calls[1][1])
        self.assertIn("начало", fake.calls[1][1])
        self.assertIn("последняя", fake.calls[4][1])
        self.assertNotIn("Мария", res.story, "старое имя проскочило — заменено на новое")
        self.assertEqual(res.package, "Название: Чужой дом")
        self.assertTrue(6300 <= res.words <= 8800, res.words)

    def test_package_gets_outline_not_whole_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeLLM()
            res = self._run(fake, Path(tmp) / "state.json")
        pkg_user = fake.calls[-1][2]
        self.assertIn("ЧТО ПРОИСХОДИТ ПО ЧАСТЯМ", pkg_user)
        self.assertLess(len(pkg_user), len(res.story) / 3, "весь текст в запрос пакета не уходит")

    def test_underdelivery_compensated_without_rewrites(self) -> None:
        """Пишет 80% просимого — следующие части просят больше; переписываний нет, итог в ±10%."""
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeLLM(factor=0.8)
            res = self._run(fake, Path(tmp) / "state.json")
        labels = [c[0] for c in fake.calls]
        self.assertEqual(labels.count("rewrite_part_1"), 1)
        self.assertTrue(6300 <= res.words <= 8800, res.words)

    def test_short_first_part_compensated_by_next(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeLLM(short_first=True)
            res = self._run(fake, Path(tmp) / "state.json")
        labels = [c[0] for c in fake.calls]
        self.assertEqual(labels.count("rewrite_part_1"), 2, "почти пустая часть — один повтор")
        self.assertTrue(6300 <= res.words <= 8800, res.words)

    def test_last_part_out_of_range_rewritten_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeLLM(last_factor=2.5)
            res = self._run(fake, Path(tmp) / "state.json")
        labels = [c[0] for c in fake.calls]
        self.assertEqual(labels.count("rewrite_part_4"), 2)
        self.assertTrue(6300 <= res.words <= 8800, res.words)

    def test_short_story_single_part(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeLLM()
            res = self._run(fake, Path(tmp) / "state.json", words="1800–2200")
        self.assertEqual([c[0] for c in fake.calls], ["rewrite_plan", "rewrite_part_1", "rewrite_package"])
        self.assertIn("весь текст целиком", fake.calls[1][1])
        self.assertTrue(1620 <= res.words <= 2420, res.words)

    def test_resume_after_failure_keeps_done_parts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            with self.assertRaises(RuntimeError):
                self._run(FakeLLM(fail_at=3), state)
            fake = FakeLLM()
            self._run(fake, state)
        labels = [c[0] for c in fake.calls]
        self.assertNotIn("rewrite_plan", labels, "план уже есть")
        self.assertEqual([x for x in labels if x.startswith("rewrite_part_")][0], "rewrite_part_3", "продолжаем с упавшей части")


class ClaudeTests(unittest.TestCase):
    def _fake_claude(self, folder: Path, payload: dict, code: int = 0) -> Path:
        script = folder / "claude"
        (folder / "out.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        script.write_text(f"#!/bin/sh\ncat > /dev/null\ncat '{folder / 'out.json'}'\nexit {code}\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return script

    def test_short_story_in_one_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            story = "Слово " * 3000
            binary = self._fake_claude(Path(tmp), {"type": "result", "is_error": False,
                                                   "result": f"{en.STORY_MARK}\n{story}\n{en.PACKAGE_MARK}\nНазвание"})
            with mock.patch.object(en, "claude_binary", lambda: str(binary)):
                res = en.rewrite_claude("ЗАДАНИЕ", "исходник", words="3000–3500")
        self.assertEqual(res.words, 3000)
        self.assertEqual(res.package, "Название")

    def _parts(self, sizes, *, package_on_last=True):
        """Подменённый Claude: на каждый вызов — часть заданного размера; запоминает запросы."""
        calls = []

        def fake(ask, **kw):
            calls.append(ask)
            n = sizes[min(len(calls) - 1, len(sizes) - 1)]
            if isinstance(n, Exception):
                raise n
            plan = f"{en.PLAN_MARK}\n1. Завязка\n" if len(calls) == 1 else ""
            pkg = f"\n{en.PACKAGE_MARK}\nНазвания" if package_on_last and "последняя часть" in ask else ""
            return f"{plan}{en.STORY_MARK}\n" + "слово " * n + pkg

        return fake, calls

    def test_long_story_written_in_parts_hits_target(self) -> None:
        """7000–8000: Claude пишет частями, следующая часть — по остатку; итог в заданном объёме."""
        fake, calls = self._parts([2500, 2500, 2400])
        with mock.patch.object(en, "run_claude", fake):
            res = en.rewrite_claude("ЗАДАНИЕ", "исходник", words="7000–8000")
        self.assertTrue(res.complete)
        self.assertEqual(res.package, "Названия")
        self.assertGreaterEqual(res.words, 7000)
        self.assertEqual(len(calls), 3)
        self.assertIn("ИСХОДНИК", calls[0])
        self.assertIn("ПЛАН ИСТОРИИ:\n1. Завязка", calls[1])
        self.assertNotIn("ИСХОДНИК:", calls[1], "продолжения — по плану и написанному, исходник второй раз не шлём")

    def test_underdelivering_parts_are_compensated(self) -> None:
        """Claude пишет ~80% просимого — следующие части просят с поправкой, итог всё равно в объёме."""
        import re as _re

        calls = []

        def fake(ask, **kw):
            calls.append(ask)
            asked = int(_re.findall(r"около (\d+) слов", ask)[-1])
            plan = f"{en.PLAN_MARK}\nплан\n" if len(calls) == 1 else ""
            pkg = f"\n{en.PACKAGE_MARK}\nНазвания" if "последняя часть" in ask else ""
            return f"{plan}{en.STORY_MARK}\n" + "слово " * int(asked * 0.8) + pkg

        with mock.patch.object(en, "run_claude", fake):
            res = en.rewrite_claude("ЗАДАНИЕ", "исходник", words="7000–8000")
        self.assertGreaterEqual(res.words, 6800)
        self.assertLessEqual(res.words, 8400)
        self.assertTrue(res.complete)

    def test_outline_instead_of_story_is_rejected(self) -> None:
        outline = "\n".join(f"{i}. Героиня делает шаг {i} и узнаёт правду" for i in range(1, 13))

        def fake(ask, **kw):
            return f"{en.STORY_MARK}\nКороткий пересказ.\n{outline}\n{en.PACKAGE_MARK}\nНазвания"

        with mock.patch.object(en, "run_claude", fake), self.assertRaises(en.RewriteError):
            en.rewrite_claude("ЗАДАНИЕ", "исходник", words="3000–3500")

    def test_short_middle_part_not_rewritten(self) -> None:
        """2000 из 2500 — не переписываем (лишние токены): следующая часть доберёт."""
        fake, calls = self._parts([2000, 2900, 2600])
        with mock.patch.object(en, "run_claude", fake):
            res = en.rewrite_claude("ЗАДАНИЕ", "исходник", words="7000–8000")
        self.assertFalse(any("ПРОШЛЫЙ" in c for c in calls))
        self.assertEqual(len(calls), 3)
        self.assertTrue(6300 <= res.words <= 8800, res.words)

    def test_final_out_of_range_rewritten_once(self) -> None:
        fake, calls = self._parts([2500, 2500, 6000, 2500])
        with mock.patch.object(en, "run_claude", fake):
            res = en.rewrite_claude("ЗАДАНИЕ", "исходник", words="7000–8000")
        self.assertEqual(sum("ПРОШЛЫЙ ВАРИАНТ ЭТОЙ ЧАСТИ" in c for c in calls), 1)
        self.assertTrue(6300 <= res.words <= 8800, res.words)

    def test_static_context_first_for_cache(self) -> None:
        """Промпт, план и написанное — в начале запроса, указания части — в конце (кеш у Claude)."""
        fake, calls = self._parts([2500, 2500, 2500])
        with mock.patch.object(en, "run_claude", fake):
            en.rewrite_claude("ЗАДАНИЕ", "исходник", words="7000–8000")
        self.assertTrue(calls[1].startswith("ЗАДАНИЕ\n\nПЛАН ИСТОРИИ"))
        self.assertLess(calls[1].index("УЖЕ НАПИСАНО"), calls[1].index("ФОРМАТ ОТВЕТА"))
        written_2 = calls[1].split("УЖЕ НАПИСАНО:\n", 1)[1].split("\n\n━", 1)[0]
        self.assertTrue(calls[2].split("УЖЕ НАПИСАНО:\n", 1)[1].startswith(written_2), "начало запроса 3 = запрос 2")

    def test_dialogue_is_not_outline(self) -> None:
        dialogue = "\n".join(f"— Реплика номер {i}, — сказала она и посмотрела в окно на тёмный двор." for i in range(40))
        self.assertFalse(en.looks_like_outline(dialogue))
        self.assertTrue(en.looks_like_outline("\n".join(f"{i}. Поворот сюжета номер {i}" for i in range(1, 12))))

    def test_short_part_is_asked_again(self) -> None:
        fake, calls = self._parts([300, 2600, 2600, 2600])
        with mock.patch.object(en, "run_claude", fake):
            en.rewrite_claude("ЗАДАНИЕ", "исходник", words="7000–8000")
        self.assertIn("ПРОШЛЫЙ ОТВЕТ НЕ ПОДОШЁЛ", calls[1])

    def test_failure_midway_returns_written_for_continuation(self) -> None:
        limit = en.RewriteError("лимит", kind="limit")
        fake, _calls = self._parts([2600, limit])
        with mock.patch.object(en, "run_claude", fake):
            res = en.rewrite_claude("ЗАДАНИЕ", "исходник", words="7000–8000")
        self.assertFalse(res.complete)
        self.assertEqual(res.words, 2600)
        self.assertIs(res.error, limit)

    def test_not_logged_in_is_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binary = self._fake_claude(Path(tmp), {"is_error": True, "result": "Invalid API key · Please run /login"}, code=1)
            with mock.patch.object(en, "claude_binary", lambda: str(binary)), self.assertRaises(en.RewriteError) as err:
                en.run_claude("x")
        self.assertIn("не выполнен вход", str(err.exception))

    def test_not_installed(self) -> None:
        with mock.patch.object(en, "claude_binary", lambda: None), self.assertRaises(en.RewriteError) as err:
            en.run_claude("x")
        self.assertIn("не установлен", str(err.exception))


class SubtitleTests(unittest.TestCase):
    def test_json3(self) -> None:
        raw = json.dumps({"events": [{"segs": [{"utf8": "Привет "}, {"utf8": "мир"}]}, {"segs": [{"utf8": " [музыка] дальше"}]}]})
        self.assertEqual(so._json3_text(raw), "Привет мир дальше")

    def test_vtt_dedupes_auto_caption_repeats(self) -> None:
        raw = "WEBVTT\n\n00:00.000 --> 00:01.000\nпервая строка\n\n00:01.000 --> 00:02.000\nпервая строка\nвторая <c>строка</c>\n"
        self.assertEqual(so._vtt_text(raw), "первая строка вторая строка")

    def test_pick_original_language(self) -> None:
        info = {"language": "ro", "subtitles": {}, "automatic_captions": {"en": [], "ro-orig": [{"ext": "json3"}], "ro": []}}
        self.assertEqual(so._pick_track(info)[:2], ("ro-orig", "auto_subtitles"))
        info["subtitles"] = {"ro": [{"ext": "vtt"}]}
        self.assertEqual(so._pick_track(info)[:2], ("ro", "subtitles"), "ручные субтитры лучше автоматических")


class FallbackTests(unittest.TestCase):
    """Claude: лимит до ответа, обрыв на середине, лимит запомнен, повтор без второго обращения."""

    def setUp(self) -> None:
        from engine.rewrite import run as rr
        from engine.rewrite.source import Source

        self.rr = rr
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.src = Source("https://youtu.be/x", "vid1", "История", "Канал", "ru", "исходник " * 100, "subtitles")
        self.llm_calls: list[str] = []
        self.claude_calls = 0

        def fake_llm(prompt, source, **kw):
            self.llm_calls.append(kw.get("prefix", ""))
            return en.Result(((kw.get("prefix") or "") + " Продолжение. " + "слово " * 900).strip(), "Пакет")

        for p in (mock.patch.object(rr, "REWRITES_DIR", root), mock.patch.object(rr, "LIMIT_FILE", root / "limit.json"),
                  mock.patch.object(rr, "fetch_source", lambda url, **kw: self.src),
                  mock.patch.object(rr, "rewrite_llm", fake_llm),
                  mock.patch.object(rr, "_sub_ready", lambda engine: engine == self.backup)):
            p.start()
            self.addCleanup(p.stop)
        self.backup = ""  # запасная подписка (Claude ↔ Codex): по умолчанию входа нет

    def req(self, **kw):
        return self.rr.RewriteRequest(url=self.src.url, prompt="на {языке}", words="1000–1200", engine="claude_code",
                                      llm=("k", "u", "m"), **kw)

    def claude(self, outcome, by_engine=None):
        def fake(prompt, source, **kw):
            self.claude_calls += 1
            self.engines_used = [*getattr(self, "engines_used", []), kw.get("engine")]
            got = (by_engine or {}).get(kw.get("engine"), outcome)
            if isinstance(got, Exception):
                raise got
            return got
        return mock.patch.object(self.rr, "rewrite_claude", fake)

    def test_claude_limit_goes_to_codex_when_logged_in(self) -> None:
        import time

        self.backup = "codex"
        limit = en.RewriteError("Claude: кончился лимит", kind="limit", until=time.time() + 3600)
        whole = en.Result("Слово " * 1100, "Пакет", complete=True)
        with self.claude(None, by_engine={"claude_code": limit, "codex": whole}):
            out = self.rr.rewrite_video(self.req())
        self.assertEqual(self.engines_used, ["claude_code", "codex"])
        self.assertIn("Codex", out.engine_note)
        self.assertIn("лимит", out.engine_note)
        self.assertEqual(self.llm_calls, [], "встроенная не понадобилась")

    def test_switches_are_reported_with_reason(self) -> None:
        """Каждая автосмена — уведомление: Claude → Codex (лимит), Codex → встроенная (нет входа)."""
        import time

        from engine.core import switches

        got = []
        switches.set_callback(lambda job, item: got.append(item))
        self.addCleanup(switches.set_callback, None)
        self.backup = "codex"
        limit = en.RewriteError("лимит", kind="limit", until=time.time() + 1800)
        auth = en.RewriteError("нет входа", kind="auth")
        with self.claude(None, by_engine={"claude_code": limit, "codex": auth}):
            self.rr.rewrite_video(self.req())
        self.assertEqual([(g["from"], g["to"]) for g in got],
                         [("Claude", "Codex (ChatGPT)"), ("Codex (ChatGPT)", "встроенная нейросеть")])
        self.assertIn("лимит подписки до", got[0]["reason"])
        self.assertIn("не выполнен вход", got[1]["reason"])
        self.assertEqual(got[0]["task"], "рерайт")

    def test_codex_selected_order_is_codex_then_claude(self) -> None:
        from engine.core import switches

        self.assertEqual(switches.order_from("claude_code"), ["claude_code", "codex"])
        self.assertEqual(switches.order_from("codex"), ["codex", "claude_code"])

    def test_codex_engine_with_its_own_model(self) -> None:
        whole = en.Result("Слово " * 1100, "Пакет", complete=True)
        seen = {}

        def fake(prompt, source, **kw):
            seen.update(kw)
            return whole
        with mock.patch.object(self.rr, "rewrite_claude", fake):
            out = self.rr.rewrite_video(self.rr.RewriteRequest(
                url=self.src.url, prompt="на {языке}", words="1000–1200", engine="codex", llm=("k", "u", "m"),
                claude_model="opus", codex_model="gpt-6.1-sol"))
        self.assertEqual(seen["engine"], "codex")
        self.assertEqual(seen["model"], "gpt-6.1-sol")
        self.assertEqual(out.engine_note, "Codex (ChatGPT)")

    def test_both_subscriptions_fail_then_builtin(self) -> None:
        self.backup = "codex"
        auth = en.RewriteError("нет входа", kind="auth")
        with self.claude(auth):
            out = self.rr.rewrite_video(self.req())
        self.assertEqual(self.engines_used, ["claude_code", "codex"])
        self.assertEqual(self.llm_calls, [""])
        self.assertIn("встроенная", out.engine_note)

    def test_limit_before_answer_writes_with_builtin_and_remembers(self) -> None:
        import time

        limit = en.RewriteError("Claude: кончился лимит", kind="limit", until=time.time() + 3600)
        with self.claude(limit):
            out = self.rr.rewrite_video(self.req())
            self.assertIn("лимит", out.engine_note)
            self.assertEqual(self.llm_calls, [""], "текст целиком — встроенной нейросетью")
            # следующий ролик очереди: Claude не дёргаем, пока лимит не снимется
            self.src.text = "другой исходник " * 100
            self.src.video_id = "vid2"
            out2 = self.rr.rewrite_video(self.req())
        self.assertEqual(self.claude_calls, 1)
        self.assertRegex(out2.engine_note, "лимит.* до ")

    def test_long_but_unfinished_claude_is_still_continued(self) -> None:
        """Срыв на последней части: слов почти хватает, но финала нет — всё равно дописываем."""
        cut = en.Result("Начало. " + "слово " * 1000, "", complete=False)
        with self.claude(cut):
            out = self.rr.rewrite_video(self.req())
        self.assertEqual(out.engine_note, "Claude + продолжение встроенной нейросетью")

    def test_truncated_claude_is_continued_from_cut(self) -> None:
        cut = en.Result("Начало истории. Елена вошла в дом. Она посмотрела на " , "", complete=False)
        with self.claude(cut):
            out = self.rr.rewrite_video(self.req())
        self.assertEqual(out.engine_note, "Claude + продолжение встроенной нейросетью")
        self.assertTrue(self.llm_calls[0].startswith("Начало истории"), "встроенная получает начало Claude")
        # повтор задачи (например, встроенная упала): Claude второй раз не зовём
        with self.claude(cut):
            self.rr.rewrite_video(self.req())
        self.assertEqual(self.claude_calls, 1)

    def test_complete_claude_is_used_as_is(self) -> None:
        whole = en.Result("Слово " * 1100, "Пакет", complete=True)
        with self.claude(whole):
            out = self.rr.rewrite_video(self.req())
        self.assertEqual(out.engine_note, "Claude")
        self.assertEqual(self.llm_calls, [])

    def test_no_fallback_keeps_error(self) -> None:
        with self.claude(en.RewriteError("Claude: кончился лимит", kind="limit")), self.assertRaises(en.RewriteError):
            self.rr.rewrite_video(self.req(fallback=False))


class ContinuationTests(unittest.TestCase):
    def test_continue_from_prefix_plans_only_the_rest(self) -> None:
        fake = FakeLLM()
        prefix = "Мария стояла у окна. " * 100 + "И тут она поняла, что"
        with tempfile.TemporaryDirectory() as tmp, mock.patch("engine.slides.llm_client.LLMClient", return_value=fake):
            res = en.rewrite_llm("ЗАДАНИЕ", "исходник " * 500, words="1800–2400", llm=("k", "u", "m"),
                                 state_file=Path(tmp) / "s.json", prefix=prefix)
        plan_call = fake.calls[0]
        self.assertIn("УЖЕ НАПИСАНО", plan_call[2])
        self.assertIn("продолжить ровно с места обрыва", plan_call[1])
        first_part = fake.calls[1]
        self.assertNotIn("сильное вступление", first_part[1], "продолжение — без нового хука")
        self.assertIn("стояла у окна", first_part[2], "образец стиля — конец написанного")
        self.assertTrue(res.story.startswith("Мария стояла у окна."))
        self.assertNotIn("поняла, что\n", res.story, "оборванное предложение отрезано")

    def test_trim_to_sentence(self) -> None:
        self.assertEqual(en.trim_to_sentence("Первое. Второе! Недописанное пред"), "Первое. Второе!")


class CacheTests(unittest.TestCase):
    def test_ready_rewrite_is_not_redone(self) -> None:
        """Повтор цепочки после сбоя: текст уже переписан — не тратим 10 минут и лимит подписки заново."""
        from engine.rewrite import run as rr
        from engine.rewrite.source import Source

        src = Source("https://youtu.be/x", "vid1", "История", "Канал", "ru", "исходник " * 100, "subtitles")
        calls = []

        def fake_llm(prompt, source, **kw):
            calls.append(1)
            return en.Result("Новый текст " * 50, "Название")

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(rr, "REWRITES_DIR", Path(tmp)), \
             mock.patch.object(rr, "fetch_source", lambda url, **kw: src), \
             mock.patch.object(rr, "rewrite_llm", fake_llm):
            req = rr.RewriteRequest(url=src.url, prompt="на {языке}", llm=("k", "u", "m"))
            first = rr.rewrite_video(req)
            second = rr.rewrite_video(req)
            self.assertEqual(len(calls), 1)
            self.assertEqual(second.story_file, first.story_file)
            self.assertEqual(second.words, 100)
            rr.rewrite_video(rr.RewriteRequest(url=src.url, prompt="другой промпт {языке}", llm=("k", "u", "m")))
            self.assertEqual(len(calls), 2, "другой промпт — переписываем заново")


class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        from server import runners

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.broll = Path(self.tmp.name) / "футажи"
        self.broll.mkdir()
        (self.broll / "f.mp4").write_bytes(b"")
        for p in (mock.patch.object(runners, "_need_tts", lambda cfg, audio: None),
                  mock.patch.object(runners, "_llm_opt", lambda cfg: ("k", "u", "m"))):
            p.start()
            self.addCleanup(p.stop)

    def cfg(self, **kw) -> dict:
        return {"broll_dir": str(self.broll), "llm_api_key": "k", **kw}

    def _out(self, tmp: Path):
        from engine.rewrite.run import RewriteOutput
        from engine.rewrite.source import Source

        story = tmp / "История.txt"
        story.write_text("Новый рассказ.", encoding="utf-8")
        pkg = tmp / "описание.txt"
        pkg.write_text("Название", encoding="utf-8")
        src = Source("https://youtu.be/x", "x", "История", "Канал", "ru", "текст " * 100, "subtitles")
        return RewriteOutput(src, story, pkg, 2, "ru", tmp)

    def test_review_mode_waits_for_user(self) -> None:
        from server import runners

        with tempfile.TemporaryDirectory() as tmp:
            out = self._out(Path(tmp))
            desk = Path(tmp) / "desk"
            with mock.patch("engine.rewrite.run.rewrite_video", lambda req, **kw: out), \
                 mock.patch("engine.slides.paths.DESKTOP", desk):
                _title, run = runners._rewrite_job(self.cfg(rewrite_review=True), {"url": "https://youtu.be/x", "section": "broll"}, {})
                res = run(None, lambda p, m: None, None)
            self.assertTrue((desk / "Рерайты" / "История.txt").is_file(), "текст на проверку — на рабочем столе")
            self.assertEqual(res["follow"]["kind"], "broll")
            self.assertEqual(res["follow"]["params"]["text_file"], str(desk / "Рерайты" / "История.txt"))

    def test_auto_mode_queues_build(self) -> None:
        from server import runners

        added = []
        mgr = mock.MagicMock()
        mgr.create.side_effect = lambda kind, params: (kind, params)
        mgr.add.side_effect = lambda job, after=None: added.append(job)
        with tempfile.TemporaryDirectory() as tmp:
            out = self._out(Path(tmp))
            with mock.patch("engine.rewrite.run.rewrite_video", lambda req, **kw: out), \
                 mock.patch("server.jobs.get_manager", lambda: mgr):
                params = {"url": "https://youtu.be/x", "section": "slides", "review": False}
                _title, run = runners._rewrite_job(self.cfg(media_mode="stars"), params, {})
                run(None, lambda p, m: None, None)
        self.assertEqual(added[0][0], "slides_full")
        self.assertEqual(added[0][1]["media_mode"], "stars", "режим кадров — на момент постановки")
        self.assertTrue(added[0][1]["text_file"].endswith("История.txt"))

    def test_missing_settings_fail_before_rewrite(self) -> None:
        from server import runners

        with self.assertRaisesRegex(ValueError, "папку футажей"), mock.patch.object(runners, "load_config", lambda: {}):
            runners._rewrite_job({"llm_api_key": "k"}, {"url": "https://youtu.be/x", "section": "broll"}, {})

    def test_queue_refusal_keeps_rewritten_text(self) -> None:
        from server import runners

        mgr = mock.MagicMock()
        mgr.create.side_effect = ValueError("Укажи папку футажей")
        with tempfile.TemporaryDirectory() as tmp:
            out = self._out(Path(tmp))
            with mock.patch("engine.rewrite.run.rewrite_video", lambda req, **kw: out), \
                 mock.patch("server.jobs.get_manager", lambda: mgr):
                params = {"url": "https://youtu.be/x", "section": "broll", "review": False}
                _t, run = runners._rewrite_job(self.cfg(), params, {})
                res = run(None, lambda p, m: None, None)
        self.assertIn("не поставлен в очередь", " ".join(res["lines"]))
        self.assertTrue(params["review"], "кнопка «Собрать ролик» остаётся")
        self.assertEqual(res["follow"]["params"]["text_file"], str(out.story_file))

    def test_follow_up_not_queued_twice_on_retry(self) -> None:
        from server import runners

        mgr = mock.MagicMock()
        job = mock.MagicMock()
        job.state = {"follow_job": "abc"}
        with tempfile.TemporaryDirectory() as tmp:
            out = self._out(Path(tmp))
            with mock.patch("engine.rewrite.run.rewrite_video", lambda req, **kw: out), \
                 mock.patch("server.jobs.get_manager", lambda: mgr):
                _t, run = runners._rewrite_job(self.cfg(), {"url": "https://youtu.be/x", "section": "broll", "review": False}, {})
                run(job, lambda p, m: None, None)
        mgr.add.assert_not_called()

    def test_build_reads_text_file_and_attaches_package(self) -> None:
        from server import runners

        with tempfile.TemporaryDirectory() as tmp:
            out = self._out(Path(tmp))
            tj = runners._text_job({"text_file": str(out.story_file)})
            self.assertEqual(tj.text, "Новый рассказ.")
            self.assertEqual(tj.title_hint, "История")
            folder = Path(tmp) / "ролик"
            folder.mkdir()
            runners._attach(folder, {"package_file": str(out.package_file)})
            self.assertEqual((folder / "описание.txt").read_text(encoding="utf-8"), "Название")


if __name__ == "__main__":
    unittest.main()


class BotCheckRetryTests(unittest.TestCase):
    def test_youtube_bot_check_is_retried_later(self) -> None:
        from server.jobs import is_transient

        err = so.SourceError("YouTube просит подтвердить, что это не робот (временное ограничение с этого адреса) — повторю сам позже")
        self.assertTrue(is_transient(err))


class CodexRunnerTests(unittest.TestCase):
    """Codex CLI: команда без доступа к файлам, ответ из файла, понятные ошибки входа и лимита."""

    def _fake_bin(self, tmp: Path, script: str) -> str:
        b = tmp / "codex"
        b.write_text("#!/bin/sh\n" + script)
        b.chmod(0o755)
        return str(b)

    def test_exec_reads_answer_and_uses_readonly_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            log_file = tmp / "args.txt"
            # login status → 0; exec → пишет ответ в файл после -o, аргументы — в журнал
            binary = self._fake_bin(tmp, f"""
if [ "$1" = "login" ]; then echo "Logged in using ChatGPT"; exit 0; fi
echo "$@" > {log_file}
cat > /dev/null
while [ $# -gt 0 ]; do if [ "$1" = "-o" ]; then echo "готовый текст" > "$2"; fi; shift; done
""")
            with mock.patch.object(en, "codex_binary", lambda: binary):
                text = en.run_codex("задание", model="gpt-6.1-sol")
            args = log_file.read_text()
        self.assertEqual(text, "готовый текст")
        for part in ("exec", "--sandbox read-only", "--ephemeral", "--skip-git-repo-check", "-m gpt-6.1-sol"):
            self.assertIn(part, args)

    def test_not_logged_in_fails_fast(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            binary = self._fake_bin(Path(t), 'echo "Not logged in"; exit 1\n')
            with mock.patch.object(en, "codex_binary", lambda: binary), self.assertRaises(en.RewriteError) as cm:
                en.run_codex("задание")
        self.assertEqual(cm.exception.kind, "auth")

    def test_usage_limit(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            binary = self._fake_bin(Path(t), """
if [ "$1" = "login" ]; then echo "Logged in using ChatGPT"; exit 0; fi
cat > /dev/null
echo "ERROR: You've hit your usage limit. Try again in 2 hours 5 minutes." >&2; exit 1
""")
            with mock.patch.object(en, "codex_binary", lambda: binary), self.assertRaises(en.RewriteError) as cm:
                en.run_codex("задание")
        self.assertEqual(cm.exception.kind, "limit")
        import time
        self.assertAlmostEqual(cm.exception.until - time.time(), 2 * 3600 + 5 * 60, delta=60)

    def test_not_installed(self) -> None:
        with mock.patch.object(en, "codex_binary", lambda: None), self.assertRaises(en.RewriteError) as cm:
            en.run_codex("x")
        self.assertEqual(cm.exception.kind, "install")

    def test_account_from_auth_file(self) -> None:
        import base64
        import json as _json

        claims = {"email": "user@example.com", "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"}}
        token = "h." + base64.urlsafe_b64encode(_json.dumps(claims).encode()).decode().rstrip("=") + ".s"
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "auth.json").write_text(_json.dumps({"tokens": {"id_token": token}}))
            with mock.patch.dict(os.environ, {"CODEX_HOME": t}):
                acc = en.codex_account()
        self.assertEqual(acc, {"email": "user@example.com", "plan": "plus"})



class ClaudeSystemPromptTest(unittest.TestCase):
    """Claude Code вызывается со своим коротким системным промптом (служебный — ~6,5 тыс. токенов на вызов)."""

    def test_short_system_prompt_passed(self) -> None:
        seen = {}

        class FakeProc:
            returncode = 0

            def __init__(self, cmd, **kw):
                seen["cmd"] = cmd

            def communicate(self, prompt, timeout=None):
                return __import__("json").dumps({"result": "готово"}), ""

        with mock.patch.object(en, "claude_binary", lambda: "/bin/claude"), \
                mock.patch.object(en.subprocess, "Popen", FakeProc):
            self.assertEqual(en.run_claude("задание"), "готово")
        cmd = seen["cmd"]
        self.assertIn("--system-prompt", cmd)
        self.assertEqual(cmd[cmd.index("--system-prompt") + 1], en.CLAUDE_SYSTEM)
        self.assertLess(len(en.CLAUDE_SYSTEM), 600)
