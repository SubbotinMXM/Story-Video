"""Голосования: проверка опросов от нейросети, расписание (сразу + каждые N дней), память вопросов, задача."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import shutil
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from engine.publish import polls
from engine.publish.store import publish_dir


class CleanTests(unittest.TestCase):
    def test_long_option_cut_by_word_and_duplicates_dropped(self) -> None:
        long = "Очень длинный вариант ответа, который никак не помещается в шестьдесят пять символов YouTube"
        p = polls.clean({"question": "  Что  важнее? ", "options": [long, "Да", "да", ""]})
        self.assertEqual(p.question, "Что важнее?")
        self.assertLessEqual(len(p.options[0]), polls.OPTION_MAX)
        self.assertFalse(p.options[0].endswith(","))
        self.assertEqual(p.options[1:], ["Да"])

    def test_rejects_without_question_or_two_options(self) -> None:
        self.assertIsNone(polls.clean({"question": "", "options": ["a", "b"]}))
        self.assertIsNone(polls.clean({"question": "q", "options": ["a"]}))
        self.assertIsNone(polls.clean("мусор"))

    def test_options_capped_at_five(self) -> None:
        p = polls.clean({"question": "q", "options": [str(i) for i in range(8)]})
        self.assertEqual(len(p.options), polls.OPTIONS_MAX)

    def test_parse_tolerates_wrapping_text(self) -> None:
        raw = 'Вот: {"polls": [{"question": "Вопрос?", "options": ["А", "Б", "В"]}, {"question": "", "options": []}]} готово'
        self.assertEqual([p.question for p in polls.parse(raw)], ["Вопрос?"])
        self.assertEqual(polls.parse("не json"), [])


class PlanTests(unittest.TestCase):
    def test_first_now_rest_every_step(self) -> None:
        start = datetime(2026, 11, 15, 10, 7, 30, tzinfo=timezone.utc)
        got = polls.plan(3, 2, start)
        self.assertIsNone(got[0])
        self.assertEqual(got[1], datetime(2026, 11, 17, 10, 15, tzinfo=timezone.utc))
        self.assertEqual(got[2], datetime(2026, 11, 19, 10, 15, tzinfo=timezone.utc))

    def test_round_up_keeps_exact_slot(self) -> None:
        t = datetime(2026, 1, 1, 9, 30, tzinfo=timezone.utc)
        self.assertEqual(polls.round_up(t), t)
        self.assertEqual(polls.round_up(t + timedelta(seconds=1)), t + timedelta(minutes=15))

    def test_fractional_step(self) -> None:
        start = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(polls.plan(2, 0.5, start)[1], datetime(2026, 11, 15, 22, 0, tzinfo=timezone.utc))


class GenerateTests(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(publish_dir(), ignore_errors=True)
        self.addCleanup(shutil.rmtree, publish_dir(), True)

    def _answer(self, *questions: str) -> str:
        return json.dumps({"polls": [{"question": q, "options": ["Да, конечно", "Нет, никогда", "Смотря как"]}
                                     for q in questions]}, ensure_ascii=False)

    def test_asks_again_when_short_and_skips_repeats(self) -> None:
        answers = iter([self._answer("Старый вопрос?", "Первый?"), self._answer("Первый?", "Второй?")])
        prompts = []

        def ask(system, user, llm, cancel):
            prompts.append(user)
            return next(answers)

        with mock.patch.object(polls, "ask", ask):
            got = polls.generate("Канал", ["Ролик 1"], 2, asked=["старый вопрос?"], llm=("k", "u", "m"))
        self.assertEqual([p.question for p in got], ["Первый?", "Второй?"])
        self.assertIn("Первый?", prompts[1])  # второй запрос знает, что уже есть
        self.assertIn("Нужно опросов: 1", prompts[1])

    def test_no_titles_is_error(self) -> None:
        with self.assertRaises(ValueError):
            polls.generate("Канал", [], 3)

    def test_history_remembered_per_channel(self) -> None:
        polls.remember("UC1", polls.Poll("Вопрос?", ["а", "б"]), None, "published")
        self.assertEqual([h["question"] for h in polls.history("UC1")], ["Вопрос?"])
        self.assertEqual(polls.history("UC2"), [])

    def test_prompt_mentions_titles_and_asked(self) -> None:
        text = polls.prompt_for("Право на счастье", ["Свекровь выгнала невестку"], 3, "ru", ["Было?"])
        self.assertIn("Свекровь выгнала невестку", text)
        self.assertIn("Было?", text)


class AskTests(unittest.TestCase):
    def test_codex_then_builtin_never_claude(self) -> None:
        from engine.rewrite import engines
        from engine.rewrite.engines import RewriteError

        used = []

        def run_sub(engine, prompt, **kw):
            used.append(engine)
            raise RewriteError("лимит")

        client = mock.MagicMock()
        client.complete.return_value = "ответ"
        with mock.patch.object(engines, "sub_available", lambda e: True), mock.patch.object(engines, "run_sub", run_sub), \
                mock.patch("engine.slides.llm_client.LLMClient", return_value=client):
            self.assertEqual(polls.ask("s", "u", ("k", "u", "m"), None), "ответ")
        self.assertEqual(used, ["codex"])
        self.assertFalse(client.complete.call_args.kwargs["subs"])  # встроенная без захода в Claude


class JobTests(unittest.TestCase):
    def test_builder_needs_channel(self) -> None:
        from server import runners

        with self.assertRaises(ValueError):
            runners._polls_job({"adspower_api_key": "k"}, {"channel": ""}, {})

    def test_polls_run_in_upload_lane(self) -> None:
        from server.jobs import UPLOAD_KINDS

        self.assertIn("polls", UPLOAD_KINDS)


if __name__ == "__main__":
    unittest.main()


# ---------- задача целиком: повтор не выкладывает второй раз ----------

from contextlib import contextmanager  # noqa: E402

from engine.publish import registry  # noqa: E402
from engine.publish import run as pub  # noqa: E402
from engine.publish.adspower import Profile  # noqa: E402


class _FakeAds:
    def __init__(self, *a) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        pass

    def is_open(self, pid) -> bool:
        return False

    def start(self, pid, **kw):
        return mock.Mock(cdp_url="ws://x")

    def stop(self, pid) -> None:
        pass


@contextmanager
def _page(cdp):
    yield mock.MagicMock()


class RunPollsTests(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(publish_dir(), ignore_errors=True)
        self.addCleanup(shutil.rmtree, publish_dir(), True)
        registry.sync([Profile("p1", "1", "Истории")])
        registry.set_channels("p1", [registry.Channel("UCa", "Канал А", handle="@a")])
        self.posted: list[tuple[str, object]] = []
        self.fail: dict[str, Exception] = {}
        for p in (mock.patch.object(pub, "AdsPower", _FakeAds), mock.patch.object(pub.youtube, "studio_page", _page),
                  mock.patch.object(pub.youtube, "switch_to", lambda *a, **k: None),
                  mock.patch.object(polls, "post_poll", self._post), mock.patch.object(polls, "discard", lambda page: None)):
            p.start()
            self.addCleanup(p.stop)

    def _post(self, page, channel, poll, when, cancel=None):
        if poll.question in self.fail:
            raise self.fail.pop(poll.question)
        self.posted.append((poll.question, when))
        return poll.question

    POLLS = [{"question": f"Вопрос {k}?", "options": ["Да", "Нет"]} for k in range(3)]

    def _run(self, memo, polls_=None):
        return pub.run_polls("p1/UCa", self.POLLS if polls_ is None else polls_, 2, 3, ("k", ""), None, [], memo=memo)

    def test_first_now_rest_scheduled_and_history(self) -> None:
        memo: dict = {}
        res = self._run(memo)
        self.assertEqual([q for q, _ in self.posted], ["Вопрос 0?", "Вопрос 1?", "Вопрос 2?"])
        self.assertIsNone(self.posted[0][1])
        start = datetime.fromisoformat(memo["start"])
        self.assertEqual(self.posted[1][1], polls.round_up(start + timedelta(days=2)))
        self.assertEqual(memo["done"], [0, 1, 2])
        self.assertEqual([h["status"] for h in polls.history("UCa")], ["published", "scheduled", "scheduled"])
        self.assertEqual(res["warnings"], [])

    def test_retry_skips_posted_and_keeps_schedule(self) -> None:
        memo: dict = {}
        self.fail["Вопрос 1?"] = RuntimeError("Studio упала")
        self._run(memo)
        self.assertEqual(memo["done"], [0, 2])
        first = dict(self.posted)
        self.posted.clear()
        self._run(memo)  # «Продолжить»: только недостающий, время — по старому расписанию
        self.assertEqual(self.posted, [("Вопрос 1?", first.get("Вопрос 1?") or polls.round_up(
            datetime.fromisoformat(memo["start"]) + timedelta(days=2)))])

    def test_unverified_post_is_not_repeated(self) -> None:
        memo: dict = {}
        self.fail["Вопрос 0?"] = polls.Unverified("не вижу поста")
        res = self._run(memo)
        self.assertIn(0, memo["done"])
        self.assertTrue(any("не вижу поста" in w for w in res["warnings"]))
        self.assertEqual(polls.history("UCa")[0]["status"], "unverified")
        self.posted.clear()
        self._run(memo)
        self.assertEqual(self.posted, [])

    def test_generated_polls_kept_for_retry(self) -> None:
        memo: dict = {}
        gen = [polls.Poll("Нейро?", ["а", "б"])]
        with mock.patch.object(polls, "channel_titles", return_value=("Канал", ["Ролик"])), \
                mock.patch.object(polls, "generate", return_value=gen) as g:
            self.fail["Нейро?"] = RuntimeError("сбой")
            with self.assertRaises(RuntimeError):
                pub.run_polls("p1/UCa", [], 2, 1, ("k", ""), None, [], memo=memo)
            pub.run_polls("p1/UCa", [], 2, 1, ("k", ""), None, [], memo=memo)
        self.assertEqual(g.call_count, 1)  # второй раз не переписывает опросы
        self.assertEqual([q for q, _ in self.posted], ["Нейро?"])

    def test_late_retry_moves_past_times_forward(self) -> None:
        memo = {"start": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(), "done": [0]}
        self._run(memo)
        times = [w for _q, w in self.posted]
        self.assertTrue(all(w > datetime.now(timezone.utc) for w in times))
        self.assertEqual(times[1] - times[0], timedelta(days=2))  # шаг между опросами сохранён

    def test_all_failed_raises(self) -> None:
        for p in self.POLLS:
            self.fail[p["question"]] = RuntimeError("нет")
        with self.assertRaises(RuntimeError):
            self._run({})

    def test_browser_lost_restarts_and_finishes(self) -> None:
        """Браузер упал/завис посреди опросов: заход начинается заново, выложенное не повторяется."""
        memo: dict = {}
        self.fail["Вопрос 1?"] = RuntimeError("Target closed")
        with mock.patch("engine.publish.scan.release"):
            res = self._run(memo)
        self.assertEqual(memo["done"], [0, 1, 2])
        self.assertEqual([q for q, _ in self.posted], ["Вопрос 0?", "Вопрос 1?", "Вопрос 2?"])
        self.assertEqual(res["warnings"], [])

    def test_guard_closes_profile_when_browser_is_silent(self) -> None:
        import time as _t

        from engine.publish import scan

        calls: list[str] = []
        with mock.patch.object(scan, "release", lambda k, u, pid: calls.append(pid)), \
                mock.patch.object(scan.threading.Event, "wait", lambda self, t=None: _t.sleep(0.01) or self.is_set()):
            with scan.guard("k", "", "pX", stall=0.05) as g:
                _t.sleep(0.4)
            self.assertTrue(g.fired)
        self.assertEqual(calls, ["pX"])

    def test_unknown_channel(self) -> None:
        with self.assertRaises(ValueError):
            pub.run_polls("p1/UCzzz", self.POLLS, 2, 3, ("k", ""), None, [])


class ReadbackTests(unittest.TestCase):
    def test_norm_handles_narrow_spaces(self) -> None:
        self.assertEqual(polls._norm("6:30 PM"), "6:30 pm")

    def test_youtube_clock_reads_poll_times(self) -> None:
        from engine.publish import youtube

        self.assertEqual(youtube.clock_of("6:30 PM"), (18, 30))
        self.assertEqual(youtube.clock_of("12:00 AM"), (0, 0))
        self.assertEqual(youtube.clock_of("18:45"), (18, 45))

    def test_posts_url_keeps_account(self) -> None:
        self.assertTrue(polls.posts_url(registry.Channel("UCa", "x", authuser=2)).endswith("/posts?authuser=2"))
        self.assertTrue(polls.posts_url(registry.Channel("UCa", "x")).endswith("/channel/UCa/posts"))
