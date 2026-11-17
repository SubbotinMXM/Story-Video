"""Автообновление «Аналитики» по расписанию: раз в день в заданное время МСК, только каналы группы."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import shutil
import unittest
from datetime import datetime
from unittest import mock

from engine.publish import analytics_auto as aa
from engine.publish.schedule import MSK
from engine.publish.store import publish_dir


def at(day: int, h: int, m: int = 0) -> datetime:
    return datetime(2026, 11, day, h, m, tzinfo=MSK)


ON = {"enabled": True, "group": "g1", "time": "07:00"}


class FakeManager:
    def __init__(self, jobs=None) -> None:
        self.jobs = list(jobs or [])
        self.added: list[dict] = []

    def list(self):
        return self.jobs

    def create(self, kind, params):
        return mock.Mock(id=f"job{len(self.added) + 1}", kind=kind, params=params)

    def add(self, job):
        self.added.append(job.params)
        return job


class DueTests(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(publish_dir(), ignore_errors=True)
        self.addCleanup(shutil.rmtree, publish_dir(), True)

    def test_enabled_after_time_waits_for_tomorrow(self) -> None:
        self.assertFalse(aa.due(ON, at(16, 12)))  # включили в 12:00 — сегодняшние 7:00 уже прошли
        self.assertFalse(aa.due(ON, at(17, 6, 59)))
        self.assertTrue(aa.due(ON, at(17, 7)))
        aa.mark("j", at(17, 7))
        self.assertFalse(aa.due(ON, at(17, 9)))  # раз в день
        self.assertTrue(aa.due(ON, at(18, 9, 30)))  # Mac спал в 7:00 — догоняем, как проснулся

    def test_enabled_before_time_runs_today(self) -> None:
        self.assertFalse(aa.due(ON, at(16, 5)))
        self.assertTrue(aa.due(ON, at(16, 7, 1)))

    def test_changed_time_counts_from_change(self) -> None:
        aa.due(ON, at(16, 5))
        later = {**ON, "time": "06:00"}
        self.assertFalse(aa.due(later, at(16, 6, 30)))  # поменяли в 6:30 на 6:00 — сегодня уже не надо
        self.assertTrue(aa.due(later, at(17, 6)))

    def test_off_or_no_group_never(self) -> None:
        aa.due({**ON, "enabled": False}, at(16, 1))
        self.assertFalse(aa.due({**ON, "enabled": False}, at(16, 8)))
        aa.due({**ON, "group": ""}, at(16, 1))
        self.assertFalse(aa.due({**ON, "group": ""}, at(16, 8)))
        self.assertEqual(aa.next_run({**ON, "enabled": False}), 0.0)

    def test_next_run_right_after_enabling_is_tomorrow(self) -> None:
        self.assertEqual(aa.next_run(ON, at(16, 12)), at(17, 7).timestamp())  # проверка ещё не успела пройти

    def test_next_run(self) -> None:
        aa.due(ON, at(16, 5))
        self.assertEqual(aa.next_run(ON, at(16, 5)), at(16, 7).timestamp())
        aa.mark("j", at(16, 7))
        self.assertEqual(aa.next_run(ON, at(16, 8)), at(17, 7).timestamp())

    def test_normalize(self) -> None:
        self.assertEqual(aa.normalize({"enabled": 1, "group": " g ", "time": "7:05"}),
                         {"enabled": True, "group": "g", "time": "07:05"})
        self.assertEqual(aa.normalize({"time": "25:00"})["time"], "07:00")
        self.assertEqual(aa.normalize(None), aa.DEFAULT)


class TickTests(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(publish_dir(), ignore_errors=True)
        self.addCleanup(shutil.rmtree, publish_dir(), True)
        self.cfg = {"analytics_auto": ON,
                    "analytics_groups": [{"id": "g1", "name": "Максим", "channels": ["UC1", "UC2"]}]}

    def _tick(self, mgr, now):
        with mock.patch.object(aa, "datetime", wraps=datetime) as dt:
            dt.now.return_value = now
            return aa.tick(mgr, self.cfg)

    def test_queues_group_job_once_a_day(self) -> None:
        mgr = FakeManager()
        self.assertIsNone(self._tick(mgr, at(16, 5)))
        self.assertEqual(self._tick(mgr, at(16, 7)), "job1")
        self.assertEqual(mgr.added, [{"group": "g1", "auto": True}])
        self.assertIsNone(self._tick(mgr, at(16, 7, 30)))
        self.assertEqual(len(mgr.added), 1)

    def test_running_sync_not_doubled(self) -> None:
        mgr = FakeManager([{"id": "x", "kind": "analytics", "status": "running"}])
        self._tick(mgr, at(16, 5))
        self.assertEqual(self._tick(mgr, at(16, 7)), "x")
        self.assertEqual(mgr.added, [])

    def test_empty_group_skipped(self) -> None:
        self.cfg["analytics_groups"][0]["channels"] = []
        mgr = FakeManager()
        self._tick(mgr, at(16, 5))
        self.assertIsNone(self._tick(mgr, at(16, 7)))
        self.assertEqual(mgr.added, [])


class SyncOnlyGroupTest(unittest.TestCase):
    def test_only_group_channels_and_profiles(self) -> None:
        from engine.publish import analytics, registry

        ch = lambda i: registry.Channel(id=i, name=i)  # noqa: E731
        profs = {"p1": mock.Mock(id="p1", missing=False, channels=[ch("UC1"), ch("UCx")], label=lambda: "№1"),
                 "p2": mock.Mock(id="p2", missing=False, channels=[ch("UCy")], label=lambda: "№2")}
        opened, read = [], []

        def in_profile(key, url, pid, work, **kw):
            opened.append(pid)
            work("cdp", mock.Mock())

        def collect(cdp, chans, **kw):
            read.extend(c.id for c in chans)

        saved = {}

        def set_channels(pid, found, studio=None):
            saved[pid] = [c.id for c in found]
            return mock.Mock(channels=found)

        scanned = [ch("UC1"), ch("UCx"), ch("UCnew")]  # в профиле группы появился новый канал
        with mock.patch.object(registry, "load", return_value=profs), \
                mock.patch.object(registry, "set_channels", set_channels), \
                mock.patch("engine.publish.youtube.scan_channels", lambda *a, **k: scanned), \
                mock.patch("engine.publish.scan.in_profile", in_profile), \
                mock.patch.object(analytics, "collect", collect), \
                mock.patch.object(analytics, "_refresh_profiles", return_value="") as refresh:
            res = analytics.sync("k", "u", channel_ids=["UC1"])
        refresh.assert_called_once()  # список профилей из AdsPower — свежий (это дёшево)
        self.assertEqual(opened, ["p1"])  # профили без каналов группы не открываем
        self.assertEqual(saved, {"p1": ["UC1", "UCx", "UCnew"]})  # новый канал — в общий учёт, во все разделы
        self.assertEqual(read, ["UC1"])  # цифры — только по каналам группы
        self.assertIn("UCnew", str(res))


if __name__ == "__main__":
    unittest.main()
