"""Конкуренция подниши по языкам: где выгоднее — залетают маленькие каналы и мало серьёзных конкурентов."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import os
import tempfile
import unittest
from unittest import mock

from engine.research import competition as comp
from engine.research import niches

NOW = 1_800_000_000.0
DAY = 86400


def _vid(i, ch, views, days=60, title=None, audio=""):
    return {"id": f"v{i}", "title": title or f"ролик {i}", "channel": ch, "views": views, "minutes": 15.0,
            "ts": NOW - days * DAY, "queries": [], "audio": audio}


def _ch(cid, subs, hidden=False):
    return niches.Found(id=cid, title=cid.upper(), subs=subs, hidden=hidden)


def _measure(videos, channels, language="pl"):
    r = comp.LangResult(language=language, found=len(videos))
    comp.measure(r, videos, channels, now=NOW)
    return r


class CriteriaTests(unittest.TestCase):
    def test_small_channel_win(self) -> None:
        self.assertTrue(comp.is_win(50_000, 2_000), "2 тыс. подписчиков, ролик 50 тыс. — залёт")
        self.assertTrue(comp.is_win(25_000, 0))
        self.assertFalse(comp.is_win(15_000, 1_000), "меньше 20 тыс. просмотров — не залёт")
        self.assertFalse(comp.is_win(60_000, 8_000), "меньше чем ×10 к подписчикам")
        self.assertFalse(comp.is_win(900_000, 50_000), "большой канал — не «маленький»")
        self.assertFalse(comp.is_win(900_000, 100, hidden=True), "подписчики скрыты — не судим")

    def test_rival_is_established_channel_that_makes_the_subniche(self) -> None:
        self.assertTrue(comp.is_rival(2, 15_000, 50_000), "снимает тему: 2 ролика с заметными просмотрами")
        self.assertTrue(comp.is_rival(1, 150_000, 300_000), "один ролик-хит")
        self.assertFalse(comp.is_rival(1, 40_000, 300_000), "разовый ролик — не конкурент")
        self.assertFalse(comp.is_rival(1, 600_000, 15_000), "разовый хит среднего канала — не конкурент")
        self.assertFalse(comp.is_rival(2, 5_000, 50_000), "два ролика без просмотров — не конкурент")
        self.assertFalse(comp.is_rival(5, 300_000, 3_000), "маленький канал — не конкурент, а признак открытой ниши")
        self.assertTrue(comp.is_rival(3, 30_000, 0, hidden=True), "подписчики скрыты — считаем состоявшимся")


class MeasureTests(unittest.TestCase):
    def test_free_niche_with_small_winners_and_few_rivals(self) -> None:
        chans = {"s1": _ch("s1", 2_000), "s2": _ch("s2", 800), "r1": _ch("r1", 60_000), "r2": _ch("r2", 40_000),
                 "once": _ch("once", 500_000)}
        vids = [_vid(1, "s1", 120_000), _vid(2, "s1", 9_000), _vid(3, "s2", 30_000),
                _vid(4, "r1", 40_000), _vid(5, "r1", 20_000), _vid(6, "r2", 25_000), _vid(7, "r2", 11_000),
                _vid(8, "once", 50_000)]  # разовый ролик крупного канала — не конкурент
        r = _measure(vids, chans)
        self.assertEqual((r.winners, r.rivals, r.small_channels), (2, 2, 2))
        self.assertEqual(r.verdict, "best")
        self.assertIn("маленькие каналы залетают (2)", r.reason)
        self.assertEqual(r.winner_list[0]["id"], "s1", "сильнее залёт (просмотры к подписчикам) — выше")
        self.assertEqual(r.winner_list[0]["ratio"], 60.0)
        self.assertEqual({c["id"] for c in r.rival_list}, {"r1", "r2"})

    def test_old_wins_only_mean_topic_may_have_cooled(self) -> None:
        chans = {"s": _ch("s", 2_000), "t": _ch("t", 900), "r": _ch("r", 60_000)}
        old = [_vid(1, "s", 120_000, days=150), _vid(2, "r", 40_000, days=150), _vid(3, "r", 20_000, days=170),
               _vid(4, "t", 30_000, days=160), _vid(5, "r", 9_000, days=140)]
        r = _measure(old, chans)
        self.assertEqual((r.winners, r.recent_wins, r.fresh_hits), (2, 0, 0))
        self.assertEqual(r.verdict, "ok", "залёт был полгода назад — не «выгодно»")
        self.assertIn("интерес мог остыть", r.reason)
        hot = _measure([*old, _vid(6, "r", 150_000, days=12)], chans)
        self.assertEqual((hot.fresh_hits, hot.verdict), (1, "best"), "ролик на 100 тыс.+ за месяц — тема жива")
        self.assertEqual(hot.fresh_examples[0]["days"], 12)
        self.assertEqual(hot.minutes, 15.0)

    def test_crowded_niche_is_hard(self) -> None:
        chans = {f"b{k}": _ch(f"b{k}", 500_000) for k in range(12)}
        vids = [_vid(k, f"b{k % 12}", 60_000) for k in range(36)]
        r = _measure(vids, chans, "en")
        self.assertEqual((r.winners, r.rivals, r.big_rivals), (0, 12, 12))
        self.assertEqual(r.verdict, "hard")
        self.assertIn("тему плотно снимают", r.reason)
        self.assertEqual(len(r.rival_list), comp.LIST_MAX)

    def test_ok_when_winners_but_several_rivals_or_few_rivals_without_winners(self) -> None:
        chans = {"s": _ch("s", 1_000), **{f"r{k}": _ch(f"r{k}", 50_000) for k in range(5)}}
        vids = [_vid(0, "s", 40_000), *[_vid(10 + k, f"r{k % 5}", 30_000) for k in range(10)]]
        self.assertEqual(_measure(vids, chans).verdict, "ok", "залёт есть, но 5 конкурентов")
        chans = {"r": _ch("r", 50_000), "x": _ch("x", 30_000)}
        vids = [_vid(k, "r", 40_000) for k in range(4)] + [_vid(9, "x", 3_000)]
        self.assertEqual(_measure(vids, chans).verdict, "ok", "конкурент один, но маленькие не залетают")

    def test_one_win_is_not_enough_for_best(self) -> None:
        chans = {"s": _ch("s", 1_000), "r": _ch("r", 50_000)}
        vids = [_vid(1, "s", 40_000, days=10)] + [_vid(k, "r", 30_000, days=20) for k in range(2, 8)]
        r = _measure(vids, chans)
        self.assertEqual((r.winners, r.verdict), (1, "ok"))
        self.assertIn("может быть случайностью", r.reason)
        self.assertTrue(any("выборка маленькая" in n for n in r.notes))

    def test_too_little_data(self) -> None:
        self.assertEqual(_measure([], {}).verdict, "empty")
        few = _measure([_vid(1, "x", 90_000), _vid(2, "x", 80_000)], {"x": _ch("x", 900)})
        self.assertEqual(few.verdict, "empty")
        self.assertIn("всего 2", few.reason)
        quiet = _measure([_vid(k, "x", 900) for k in range(10)], {"x": _ch("x", 50_000)})
        self.assertEqual(quiet.verdict, "empty")
        self.assertIn("зрителей почти нет", quiet.reason)

    def test_rank(self) -> None:
        rs = [comp.LangResult(language="a", verdict="empty"),
              comp.LangResult(language="b", verdict="hard", winners=3, rivals=20),
              comp.LangResult(language="c", verdict="best", winners=1, rivals=3),
              comp.LangResult(language="d", verdict="best", winners=4, rivals=2),
              comp.LangResult(language="e", error="квота"),
              comp.LangResult(language="f", verdict="ok", winners=2, rivals=6)]
        self.assertEqual([r.language for r in comp.rank(rs)], ["d", "c", "f", "b", "a", "e"])


SUB = {"subniche": "философы и их учения", "broader": "исторические личности",
       "signs": ["в центре — философ и его идеи"], "not_this": ["полководцы", "правители"], "language": "ru",
       "queries": {"en": ["philosophers explained"], "de": ["Philosophen erklärt"]}, "given": False}


class ClassifyTests(unittest.TestCase):
    def test_only_subniche_titles_in_batches(self) -> None:
        videos = [_vid(k, "c", 1000, title=t) for k, t in enumerate(
            ["Nietzsche explained", "Napoleon's battles", "Stoicism of Seneca"] * 50)]
        prompts = []

        def ask(system, user, llm, cancel, label="x", **kw):
            prompts.append(system)
            return json.dumps({"yes": [n for n, line in enumerate(user.splitlines(), 1) if "Napoleon" not in line]})

        with mock.patch("engine.publish.polls.ask", ask):
            keep, failed = comp.classify(videos, SUB, "en", None, None)
        self.assertEqual((len(prompts), failed), (2, 0), "150 названий — двумя пачками по 100")
        self.assertIn("философы и их учения", prompts[0])
        self.assertIn("исторические личности", prompts[0], "общую нишу называем, чтобы её не путать с поднишей")
        self.assertIn("на языке «английский»", prompts[0])
        self.assertEqual(len(keep), 100)

    def test_failed_batch_is_retried_and_reported(self) -> None:
        videos = [_vid(k, "c", 1000) for k in range(120)]
        calls = {"n": 0}

        def ask(system, user, llm, cancel, label="x", **kw):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("занято")  # первая пачка не разобрана и со второй попытки
            return json.dumps({"yes": [1]})

        with mock.patch("engine.publish.polls.ask", ask):
            keep, failed = comp.classify(videos, SUB, "en", None, None)
        self.assertEqual((len(keep), failed, calls["n"]), (1, 1, 3))
        with mock.patch("engine.publish.polls.ask", side_effect=RuntimeError("нет")):
            with self.assertRaises(comp.CompetitionError):
                comp.classify(videos[:10], SUB, "en", None, None)

    def test_language_filter(self) -> None:
        self.assertTrue(comp.same_language("", "de"), "язык не указан — решит нейросеть по названию")
        self.assertTrue(comp.same_language("en-us", "en"))
        self.assertFalse(comp.same_language("en", "de"))


class SubnicheMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"STORY_VIDEO_PUBLISH_DIR": self.tmp})
        p.start()
        self.addCleanup(p.stop)
        self.channel = {"id": "UCx", "snippet": {"title": "Философия"}}
        self.sample = [{"title": "Ницше", "views": 10, "minutes": 20, "ts": NOW}]

    def test_reused_and_only_new_languages_asked(self) -> None:
        asked = []

        def ask(system, user, llm, cancel, label="x", **kw):
            asked.append(system)
            if "Для КАЖДОГО языка из списка напиши" in system:
                return json.dumps({"queries": {"pl": ["filozofowie"]}})
            return json.dumps(SUB)

        with mock.patch("engine.publish.polls.ask", ask):
            first = comp.subniche_for(self.channel, self.sample, ["en", "de"], None, None)
            again = comp.subniche_for(self.channel, self.sample, ["en", "de", "pl"], None, None)
        self.assertEqual(first["subniche"], "философы и их учения")
        self.assertFalse(first.get("reused_at"))
        self.assertEqual(len(asked), 2, "второй раз — только запросы на польском")
        self.assertEqual(again["queries"]["en"], ["philosophers explained"], "старые запросы те же — цифры сравнимы")
        self.assertEqual(again["queries"]["pl"], ["filozofowie"])
        self.assertTrue(again["reused_at"])

    def test_given_subniche_and_fresh_override_memory(self) -> None:
        seen = []

        def ask(system, user, llm, cancel, label="x", **kw):
            seen.append(system)
            return json.dumps({**SUB, "subniche": "нейросеть написала своё"})

        with mock.patch("engine.publish.polls.ask", ask):
            comp.subniche_for(self.channel, self.sample, ["en"], None, None)
            given = comp.subniche_for(self.channel, self.sample, ["en"], None, None, given="стоики")
            comp.subniche_for(self.channel, self.sample, ["en"], None, None, fresh=True)
        self.assertEqual(given["subniche"], "стоики", "подниша человека — дословно")
        self.assertIn("ЗАДАЛ человек: «стоики»", seen[1])
        self.assertEqual(len(seen), 3, "заданная подниша и «определить заново» не берутся из памяти")


class AnalyzeTests(unittest.TestCase):
    def test_ranks_languages_by_subniche_only(self) -> None:
        channel = {"id": "UCsrc", "snippet": {"title": "Философия"}, "statistics": {"subscriberCount": "5000"}}
        sample = [{"title": "Ницше", "views": 10_000, "minutes": 20, "ts": NOW}]
        en_ch = {f"g{k}": _ch(f"g{k}", 2_000_000) for k in range(6)}
        en_v = {f"e{k}": {**_vid(k, f"g{k % 6}", 300_000, title=f"Kant {k}"), "id": f"e{k}"} for k in range(30)}
        de_ch = {"s": _ch("s", 1_500), "t": _ch("t", 700), "r": _ch("r", 40_000)}
        de_v = {f"d{k}": {**_vid(100 + k, "s" if k < 2 else "t" if k < 3 else "r", 90_000 if k < 3 else 30_000,
                                 days=20, title=f"Kant {k}"), "id": f"d{k}"} for k in range(8)}
        found = {"en": (en_ch, en_v), "de": (de_ch, de_v)}
        for chans, vids in found.values():
            vids["own"] = {**_vid(999, "UCsrc", 1_000_000, title="Kant own"), "id": "own"}  # образец — не конкурент
            vids["war"] = {**_vid(998, next(iter(chans)), 5_000_000, title="Napoleon"), "id": "war"}  # не подниша
            vids["fr"] = {**_vid(997, next(iter(chans)), 9_000_000, title="Kant fr", audio="fr"), "id": "fr"}

        def search(api, queries, f, **kw):
            return found[f.language]

        def ask(system, user, llm, cancel, label="x", **kw):
            if "Определи его УЗКУЮ поднишу" in system:
                return json.dumps(SUB)
            return json.dumps({"yes": [n for n, line in enumerate(user.splitlines(), 1) if "Napoleon" not in line]})

        tmp = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"STORY_VIDEO_PUBLISH_DIR": tmp}), \
             mock.patch("engine.rewrite.channels._Api", return_value=mock.Mock()), \
             mock.patch.object(niches, "resolve_channel", return_value=channel), \
             mock.patch.object(niches, "sample_videos", return_value=sample), \
             mock.patch.object(niches, "search", side_effect=search), \
             mock.patch("engine.publish.polls.ask", ask), \
             mock.patch.object(comp.time, "time", return_value=NOW):
            res = comp.analyze("https://youtube.com/@x", ["en", "de", "xx"], ["k"], None)
            self.assertEqual(comp.runs()[0]["id"], res["id"], "проверка в истории")
        self.assertEqual(res["subniche"]["subniche"], "философы и их учения")
        self.assertEqual([r["language"] for r in res["results"]], ["de", "en"], "неизвестный язык отброшен")
        de, en = res["results"]
        self.assertEqual(de["on_topic"], 8, "Наполеон, французский ролик и сам образец — не в счёт")
        self.assertEqual(de["found"], 9, "образец и ролик на французском отсеяны ещё до нейросети")
        self.assertEqual((de["verdict"], de["winners"], de["rivals"]), ("best", 2, 1))
        self.assertEqual((en["verdict"], en["winners"], en["rivals"]), ("hard", 0, 6))
        head, lines = comp.summary(res)
        self.assertIn("выгоднее всего — немецкий", head)
        self.assertTrue(lines[0].startswith("1. Немецкий: выгодно"))
        # повтор: подниша из памяти, ролики уже разобраны — нейросеть не спрашиваем ни разу
        calls = []
        with mock.patch.dict(os.environ, {"STORY_VIDEO_PUBLISH_DIR": tmp}), \
             mock.patch("engine.rewrite.channels._Api", return_value=mock.Mock()), \
             mock.patch.object(niches, "resolve_channel", return_value=channel), \
             mock.patch.object(niches, "sample_videos", return_value=sample), \
             mock.patch.object(niches, "search", side_effect=search), \
             mock.patch("engine.publish.polls.ask", lambda *a, **k: calls.append(a) or ask(*a, **k)), \
             mock.patch.object(comp.time, "time", return_value=NOW):
            again = comp.analyze("https://youtube.com/@x", ["en", "de"], ["k"], None)
        self.assertEqual(calls, [], "повторная проверка — без единого вопроса нейросети")
        self.assertEqual([(r["language"], r["on_topic"], r["verdict"]) for r in again["results"]],
                         [(r["language"], r["on_topic"], r["verdict"]) for r in res["results"]])

    def test_needs_languages_and_keys(self) -> None:
        with self.assertRaises(comp.CompetitionError):
            comp.analyze("u", [], ["k"], None)
        with self.assertRaises(comp.CompetitionError):
            comp.analyze("u", ["en"], [], None)

    def test_llm_failure_is_explained(self) -> None:
        with mock.patch("engine.publish.polls.ask", side_effect=RuntimeError("нет входа")):
            with self.assertRaises(comp.CompetitionError) as err:
                comp.describe_subniche({"snippet": {}}, [{"title": "a", "views": 1, "ts": 1}], ["en"], None, None)
        self.assertIn("поднишу", str(err.exception))


if __name__ == "__main__":
    unittest.main()
