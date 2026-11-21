"""Рерайт «по каналам»: рейтинг залетевших роликов, журнал без повторов, принудительный повтор по ссылке."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import unittest
from unittest import mock

from engine.publish.store import write_json
from engine.rewrite import channels as ch

DAY = 86400.0
NOW = 1_800_000_000.0


def vid(i: str, views: int, days: float, minutes: float = 20, live: bool = False) -> ch.Video:
    return ch.Video(id=i.ljust(11, "x"), title=f"ролик {i}", views=views, published=NOW - days * DAY,
                    duration=minutes * 60, live=live)


class RankTest(unittest.TestCase):
    def test_recent_hits_first_then_older_by_ratio(self) -> None:
        videos = [vid("old_hit", 900_000, 400), vid("fresh_hit", 200_000, 4), vid("fresh_meh", 10_000, 5),
                  vid("short", 5_000_000, 2, minutes=1), vid("stream", 5_000_000, 3, live=True),
                  vid("old_meh", 8_000, 300), vid("mid", 50_000, 20)]
        order = [v.title for v in ch.rank(videos, min_sec=600, now=NOW)]
        self.assertEqual(order[:3], ["ролик fresh_hit", "ролик mid", "ролик fresh_meh"], "свежие — по залётности")
        self.assertEqual(order[3:], ["ролик old_hit", "ролик old_meh"], "потом старые — по отрыву от канала")
        self.assertNotIn("ролик short", order)
        self.assertNotIn("ролик stream", order)

    def test_channel_link_forms(self) -> None:
        self.assertEqual(ch._channel_query("https://www.youtube.com/@Точкинакарте"), {"forHandle": "@Точкинакарте"})
        self.assertEqual(ch._channel_query("https://www.youtube.com/channel/UC52q_NYd-XE0BQtUHRRggeA/videos"),
                         {"id": "UC52q_NYd-XE0BQtUHRRggeA"})
        self.assertEqual(ch._channel_query("https://youtube.com/c/SomeName"), {"forUsername": "SomeName"})
        with self.assertRaises(ch.ChannelError):
            ch._channel_query("https://example.com/x")


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        write_json(ch.FILE, {})
        self.videos = [vid(str(i), 100_000 - i * 1000, 3 + i) for i in range(8)]

    def _pick(self, n: int, jobs=None) -> list[str]:
        with mock.patch.object(ch, "list_channel", lambda url, keys: ("UCx", "Конкурент", [ch.Video(**v.__dict__)
                                                                                          for v in self.videos])):
            got = ch.pick("https://youtube.com/@k", n, section="broll", keys=[], jobs=jobs)
        for i, v in enumerate(got["videos"]):
            ch.reserve(v.id, title=v.title, channel="Конкурент", section="broll", job=f"j{v.id}")
        return [v.id for v in got["videos"]]

    def test_next_batch_without_repeats(self) -> None:
        first = self._pick(3)
        second = self._pick(3)
        self.assertFalse(set(first) & set(second))
        self.assertEqual(ch.channel_stats()["UCx"]["left"], 2)
        third = self._pick(5)
        self.assertEqual(len(third), 2, "канал закончился — остаток")
        self.assertEqual(self._pick(5), [])

    def test_deleted_or_cancelled_job_returns_video(self) -> None:
        first = self._pick(2)
        jobs = {f"j{first[0]}": "cancelled"}  # вторую задачу удалили — её нет в очереди
        again = self._pick(2, jobs=jobs)
        self.assertEqual(again, first, "отменённые/удалённые до рерайта — снова в пуле")
        ch.mark_done(first[0])
        self.assertNotIn(first[0], self._pick(8, jobs={}), "переписанный — навсегда занят")

    def test_duplicates_for_manual_links(self) -> None:
        ch.reserve("abcdefghijk", title="Хит", channel="К", section="slides", job="j1")
        ch.mark_done("abcdefghijk")
        dups = ch.duplicates(["https://www.youtube.com/watch?v=abcdefghijk", "https://youtu.be/zzzzzzzzzzz"], {})
        self.assertEqual([d["title"] for d in dups], ["Хит"])
        self.assertEqual(dups[0]["status"], "переписан")


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        write_json(ch.FILE, {})

    def test_repeat_link_asks_then_force_goes(self) -> None:
        from fastapi.testclient import TestClient

        from server import app as appmod

        ch.reserve("abcdefghijk", title="Хит", channel="К", section="broll", job="j1")
        ch.mark_done("abcdefghijk")
        client = TestClient(appmod.app)
        item = {"kind": "rewrite", "params": {"url": "https://youtu.be/abcdefghijk"}}
        r = client.post("/api/jobs", json=[item])
        self.assertEqual(r.status_code, 409)
        self.assertIn("Хит", r.json()["detail"])
        with mock.patch.object(appmod, "_enqueue", lambda items: [{"id": "J2", "kind": "rewrite",
                                                                  "params": dict(items[0].params)}]):
            r = client.post("/api/jobs", json=[{**item, "params": {**item["params"], "force": True}}])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(ch.ledger()["abcdefghijk"]["times"], 1, "повтор учтён")


    def test_same_link_twice_in_one_request(self) -> None:
        from fastapi.testclient import TestClient

        from server import app as appmod

        client = TestClient(appmod.app)
        items = [{"kind": "rewrite", "params": {"url": "https://youtu.be/qqqqqqqqqqq"}},
                 {"kind": "rewrite", "params": {"url": "https://www.youtube.com/watch?v=qqqqqqqqqqq"}}]
        r = client.post("/api/jobs", json=items)
        self.assertEqual(r.status_code, 409)
        self.assertIn("дважды", r.json()["detail"])


class ExcludeTest(LedgerTest):
    def test_same_channel_two_links_no_repeats(self) -> None:
        with mock.patch.object(ch, "list_channel", lambda url, keys: ("UCx", "К", [ch.Video(**v.__dict__)
                                                                                    for v in self.videos])):
            a = ch.pick("https://youtube.com/@k", 3, section="broll", keys=[], jobs={})
            b = ch.pick("https://youtube.com/channel/UCx", 3, section="broll", keys=[], jobs={},
                        exclude={v.id for v in a["videos"]})
        self.assertFalse({v.id for v in a["videos"]} & {v.id for v in b["videos"]})


class SpreadTests(unittest.TestCase):
    """Сколько роликов человек написал, столько и берётся — сколько бы каналов-доноров ни было."""

    def entry(self, name, ids, before=0):
        from types import SimpleNamespace

        return {"channel": name, "videos": [SimpleNamespace(id=i) for i in ids], "left": 0, "taken_before": before}

    def test_exact_total_not_per_channel(self) -> None:
        a, b = self.entry("A", ["a1", "a2", "a3", "a4", "a5"]), self.entry("B", ["b1", "b2", "b3", "b4", "b5"])
        out = ch.spread([a, b], 5)
        self.assertEqual([v.id for _e, v in out], ["a1", "b1", "a2", "b2", "a3"])
        self.assertEqual((a["taken"], b["taken"]), (3, 2))
        self.assertEqual((a["left"], b["left"]), (2, 3), "неиспользованные кандидаты остаются в «осталось»")

    def test_ten_donors_one_video(self) -> None:
        entries = [self.entry(f"C{i}", [f"c{i}"]) for i in range(10)]
        self.assertEqual(len(ch.spread(entries, 1)), 1)
        self.assertEqual(len(ch.spread([self.entry(f"C{i}", [f"c{i}"]) for i in range(10)], 4)), 4)

    def test_short_channel_is_topped_up_from_others_and_nothing_to_give_is_fine(self) -> None:
        a, b = self.entry("A", ["a1"]), self.entry("B", ["b1", "b2", "b3", "b4"])
        self.assertEqual([v.id for _e, v in ch.spread([a, b], 4)], ["a1", "b1", "b2", "b3"])
        self.assertEqual(len(ch.spread([self.entry("A", [])], 5)), 0)

    def test_donor_used_least_so_far_goes_first(self) -> None:
        a, b = self.entry("A", ["a1", "a2"], before=7), self.entry("B", ["b1", "b2"], before=1)
        self.assertEqual([v.id for _e, v in ch.spread([a, b], 1)], ["b1"], "малый счёт — первым: доноры чередуются")


class ShortfallEndpointTests(unittest.TestCase):
    """Доноры кончились: ставится сколько есть и сказано, сколько не нашлось; совсем пусто — честная ошибка."""

    def call(self, per_channel, count):
        from types import SimpleNamespace
        from unittest import mock

        from fastapi.testclient import TestClient

        import server.app as app_mod

        def pick(url, n, *, section, keys, jobs=None, exclude=None):
            ids = per_channel[url][:n]
            return {"channel": url[-1], "id": url, "videos": [SimpleNamespace(id=i, url=f"https://y/{i}", title=i) for i in ids],
                    "left": len(per_channel[url]) - len(ids), "total": len(per_channel[url]), "all": 9}

        with mock.patch("engine.rewrite.channels.pick", pick), \
                mock.patch.object(app_mod, "_enqueue", lambda items, *a, **k: list(items)), \
                mock.patch.object(app_mod, "_reserve_links", lambda jobs: None):
            return TestClient(app_mod.app).post("/api/rewrite/channels", json={"channels": list(per_channel), "count": count, "params": {}})

    def test_fewer_found_than_asked_says_so(self) -> None:
        r = self.call({"https://y/@A": ["a1", "a2"]}, 5)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(r.json()["jobs"]), 2)
        self.assertIn("2 из 5", r.json()["notes"][0])

    def test_enough_means_no_note_and_nothing_left_is_an_error(self) -> None:
        ok = self.call({"https://y/@A": ["a1", "a2", "a3"], "https://y/@B": ["b1", "b2"]}, 4)
        self.assertEqual((len(ok.json()["jobs"]), ok.json()["notes"]), (4, []))
        empty = self.call({"https://y/@A": []}, 3)
        self.assertEqual(empty.status_code, 400)
        self.assertIn("брать больше нечего", empty.json()["detail"])


if __name__ == "__main__":
    unittest.main()
