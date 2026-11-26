"""Итоговый отчёт по пачке задач: что сделано, что нет, почему и как исправить."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import time
import unittest

from server import report


def names(keys):
    table = {"p1/UC1": "@Первый", "p1/UC2": "@Второй", "pc/UCco": "Трогательные рассказы"}
    return [table.get(k, k) for k in keys]


def publish_snap(job_id="j1", status="done", channels=None, **extra):
    return {
        "id": job_id, "kind": "publish", "title": "Выкладка: ролик", "status": status, "finished": time.time(),
        "params": {"channels": list((channels or {}).keys())},
        "state": {"materials": {"title": "ЗАГОЛОВОК"}, "channels": channels or {}},
        "result": {"summary": "Выложено на 2 канал(а/ов)", "lines": [], "warnings": extra.get("warnings", [])},
        "error": extra.get("error"), "message": "",
    }


OK_CH = {"status": "done", "url": "https://youtu.be/AAA", "publish_at": "2026-11-26T17:15:00+03:00",
         "coauthors_ok": ["a", "b"], "coauthors_failed": [], "notes": ["комментарии: новых без ответа нет"]}


class DiagnoseTests(unittest.TestCase):
    def test_verify_is_manual_with_steps(self) -> None:
        d = report.diagnose("Google просит подтвердить вход («Verify it's you») — пароль программа не вводит",
                            profile="Трогательные рассказы")
        self.assertEqual((d["tag"], d["mode"]), ("verify", "manual"))
        self.assertTrue(any("Трогательные рассказы" in s for s in d["steps"]))
        self.assertTrue(any("Принять соавторства" in s for s in d["steps"]))

    def test_adspower_down_is_manual(self) -> None:
        self.assertEqual(report.diagnose("AdsPower не отвечает")["mode"], "manual")

    def test_network_is_auto(self) -> None:
        self.assertEqual(report.diagnose("httpx.ConnectTimeout: timed out")["mode"], "auto")

    def test_limit_waits(self) -> None:
        self.assertEqual(report.diagnose("Достигнут лимит загрузок YouTube")["mode"], "wait")

    def test_money_is_manual(self) -> None:
        self.assertEqual(report.diagnose("RoyalTechno: недостаточно средств")["tag"], "money")

    def test_unknown_can_still_be_retried(self) -> None:
        d = report.diagnose("что-то странное")
        self.assertEqual((d["tag"], d["mode"]), ("other", "auto"))


class BuildTests(unittest.TestCase):
    def test_all_good(self) -> None:
        snap = publish_snap(channels={"p1/UC1": OK_CH, "p1/UC2": {**OK_CH, "url": "https://youtu.be/BBB"}})
        r = report.build([snap], names=names, pending_pairs=[])
        self.assertEqual(r["tone"], "ok")
        self.assertEqual(r["counts"]["videos"], 2)
        self.assertEqual(r["problems"], [])
        rows = r["items"][0]["rows"]
        self.assertEqual([x["channel"] for x in rows], ["@Первый", "@Второй"], "видно, на какой канал что выложено")
        self.assertEqual(rows[0]["when"], "26.11 17:15 МСК")
        self.assertEqual(rows[0]["coauthors_ok"], 2)
        self.assertEqual(r["fix"]["count"], 0)

    def test_pending_verify_is_highlighted_as_manual_and_collabs_can_be_retried(self) -> None:
        snap = publish_snap(channels={"p1/UC1": OK_CH})
        pair = {"owner": "p1/UC1", "co": "pc/UCco", "link": "L", "pending": True, "videos": ["a", "b"],
                "error": "Google просит подтвердить вход («Verify it's you»)"}
        r = report.build([snap], names=names, pending_pairs=[pair])
        self.assertEqual(r["tone"], "warn", "ролики выложены, но есть что доделать")
        p = r["problems"][0]
        self.assertEqual((p["scope"], p["mode"], p["tag"]), ("collabs", "manual", "verify"))
        self.assertIn("Трогательные рассказы", p["title"])
        self.assertEqual(p["action"], {"type": "collabs"})
        self.assertEqual(r["manual"], 1)
        self.assertEqual(r["fix"]["collabs"], False, "ручное — не входит в «исправить всё автоматически»")

    def test_failed_job_is_resumable_and_counted(self) -> None:
        good = publish_snap("j1", channels={"p1/UC1": OK_CH})
        bad = {"id": "j2", "kind": "polls", "title": "Голосования", "status": "failed", "finished": time.time(),
               "params": {"channels": ["p1/UC2"]}, "state": {}, "result": None,
               "error": "httpx.ConnectTimeout: timed out", "message": ""}
        r = report.build([good, bad], names=names)
        self.assertEqual((r["counts"]["ok"], r["counts"]["failed"]), (1, 1))
        self.assertEqual(r["title"], "Готово 1 из 2")
        p = r["problems"][0]
        self.assertEqual((p["mode"], p["action"]), ("auto", {"type": "resume", "job_id": "j2"}))
        self.assertIn("@Второй", p["title"])
        self.assertEqual(r["fix"]["jobs"], ["j2"])

    def test_nothing_worked(self) -> None:
        bad = {"id": "j", "kind": "broll", "title": "Ролик", "status": "failed", "finished": time.time(), "params": {},
               "state": {}, "result": None, "error": "AdsPower не отвечает", "message": ""}
        r = report.build([bad], names=names)
        self.assertEqual(r["tone"], "bad")
        self.assertEqual(r["problems"][0]["mode"], "manual")

    def test_failed_channel_of_publish_is_listed_with_reason(self) -> None:
        snap = publish_snap(status="failed", channels={
            "p1/UC1": OK_CH, "p1/UC2": {"status": "failed", "error": "не дождался элемента (таймаут)"}},
            error="часть каналов не выложена")
        r = report.build([snap], names=names)
        self.assertEqual(r["counts"]["videos"], 1)
        titles = [p["title"] for p in r["problems"]]
        self.assertEqual(titles, ["Не выложено: @Второй"])
        self.assertEqual(r["problems"][0]["action"], {"type": "resume", "job_id": "j1"})

    def test_polls_rows_and_counts(self) -> None:
        snap = {"id": "j", "kind": "polls", "title": "Голосования", "status": "done", "finished": time.time(),
                "params": {"channels": ["p1/UC1"]}, "state": {}, "error": None, "message": "",
                "result": {"summary": "3 из 3", "lines": ["• уже выложен раньше: «А»", "✓ 22.11 22:15: «Б»", "✓ 25.11 22:15: «В»"]}}
        r = report.build([snap], names=names)
        self.assertEqual(r["counts"]["polls"], 2)
        self.assertEqual(len(r["items"][0]["rows"]), 3)

    def test_date_not_shown_is_a_soft_note_not_a_problem(self) -> None:
        ch = {**OK_CH, "notes": ["проверь в Studio: не вижу запланированной даты Nov 26, 2026 (в Studio: «Visibility Scheduled») — x"]}
        r = report.build([publish_snap(channels={"p1/UC1": ch})], names=names)
        self.assertEqual(r["problems"], [])
        self.assertIn("Studio ещё обрабатывала", r["items"][0]["rows"][0]["note"])


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        report._FILE.unlink(missing_ok=True)

    def test_unseen_then_seen(self) -> None:
        r = report.build([publish_snap(channels={"p1/UC1": OK_CH})], names=names)
        report.add(r)
        self.assertEqual(report.unseen()["id"], r["id"])
        self.assertEqual(report.get(r["id"])["title"], r["title"])
        self.assertEqual(report.listing()[0]["id"], r["id"])
        report.mark_seen(r["id"])
        self.assertIsNone(report.unseen())

    def test_old_reports_are_dropped(self) -> None:
        old = report.build([publish_snap(channels={"p1/UC1": OK_CH})], names=names)
        old["created"] = time.time() - 6 * 86400
        report.add(old)
        report.add(report.build([publish_snap("j9", channels={"p1/UC1": OK_CH})], names=names))
        self.assertIsNone(report.get(old["id"]))


class BatchTests(unittest.TestCase):
    """Менеджер очереди собирает отчёт, когда очередь опустела (тихие задачи — без отчёта)."""

    def _manager(self):
        from server.jobs import JobManager

        mgr = JobManager.__new__(JobManager)
        return mgr

    def test_quiet_batch_without_errors_makes_no_report(self) -> None:
        mgr = self._manager()
        batch = {"done": 1, "failed": 0, "sec": 99.0, "last": "", "started": 0.0,
                 "snaps": {"a": {"id": "a", "kind": "analytics", "status": "done", "title": "Аналитика"}}}
        self.assertFalse(mgr._report(batch))

    def test_failed_batch_makes_report_even_if_short(self) -> None:
        from unittest import mock

        mgr = self._manager()
        snap = {"id": "a", "kind": "polls", "status": "failed", "title": "Голосования", "finished": time.time(),
                "params": {"channels": []}, "state": {}, "result": None, "error": "boom", "message": ""}
        batch = {"done": 0, "failed": 1, "sec": 3.0, "last": "", "started": 0.0, "snaps": {"a": snap}}
        with mock.patch("server.jobs.hub") as hub:
            self.assertTrue(mgr._report(batch))
        self.assertIn("report", [c[0][0] for c in hub.publish.call_args_list])


class WaitingTests(unittest.TestCase):
    def test_job_waiting_for_tomorrow_is_in_report_as_wait(self) -> None:
        snap = publish_snap(channels={"p1/UC1": OK_CH})
        r = report.build([snap], names=names, waiting=[
            {"title": "Выкладка: ролик 2", "message": "Лимит загрузок YouTube", "retry_at": time.time() + 8 * 3600}])
        p = r["problems"][0]
        self.assertEqual((p["scope"], p["mode"]), ("wait", "wait"))
        self.assertIn("продолжит", p["steps"][0])
        self.assertEqual(r["fix"]["count"], 0)


class EndToEndTests(unittest.TestCase):
    def test_queue_going_idle_produces_one_report(self) -> None:
        from unittest import mock

        from server.jobs import JobManager

        def builder(kind, params, state):
            def run(job, progress, cancel):
                if kind == "polls":
                    raise ValueError("поломка настроек")
                return {"summary": "готово", "lines": []}
            return f"{kind} {params.get('n', '')}", run

        report._FILE.unlink(missing_ok=True)
        with mock.patch("server.jobs.hub") as hub, mock.patch.object(JobManager, "_load"):
            mgr = JobManager(builder)
            mgr.add(mgr.create("cover", {"n": 1}))
            mgr.add(mgr.create("polls", {"n": 2}))
            deadline = time.time() + 20
            while time.time() < deadline and not report.listing():
                time.sleep(0.1)
        listing = report.listing()
        self.assertEqual(len(listing), 1, "одна пачка — один отчёт")
        full = report.get(listing[0]["id"])
        self.assertEqual((full["counts"]["ok"], full["counts"]["failed"]), (1, 1))
        self.assertEqual(full["fix"]["jobs"] and len(full["fix"]["jobs"]), 1, "упавшую можно продолжить кнопкой")
        events = [c[0][0] for c in hub.publish.call_args_list]
        self.assertIn("report", events)


class NightCumulativeTests(unittest.TestCase):
    """Ночью очередь пустеет много раз: утренний итог — один, за всю ночь, пока прошлый не открыли."""

    def _snap(self, jid: str, kind: str, status: str, title: str) -> dict:
        return {"id": jid, "kind": kind, "title": title, "status": status, "finished": time.time(), "params": {},
                "state": {}, "result": {"summary": "ok", "lines": []} if status == "done" else None,
                "error": "" if status == "done" else "нет связи", "message": ""}

    def test_second_batch_continues_unseen_report(self) -> None:
        from unittest import mock

        from server.jobs import JobManager

        report._FILE.unlink(missing_ok=True)
        mgr = JobManager.__new__(JobManager)
        first = {"done": 2, "failed": 1, "sec": 99.0, "last": "", "started": 100.0, "snaps": {
            "r1": self._snap("r1", "rewrite", "done", "Рерайт 1"), "r2": self._snap("r2", "rewrite", "done", "Рерайт 2"),
            "r3": self._snap("r3", "rewrite", "failed", "Рерайт 3")}}
        second = {"done": 0, "failed": 1, "sec": 99.0, "last": "", "started": 200.0, "snaps": {
            "c1": self._snap("c1", "fixvideo", "failed", "Доделать ролик (обложка): Канал — https://youtu.be/x")}}
        with mock.patch("server.jobs.hub"), mock.patch("server.notify.notify"):
            self.assertTrue(mgr._report(first))
            self.assertTrue(mgr._report(second))
        self.assertEqual(len(report.listing()), 1, "прошлый непросмотренный вытеснен новым")
        full = report.get(report.listing()[0]["id"])
        self.assertNotIn("snaps", full)
        self.assertEqual((full["counts"]["jobs"], full["counts"]["ok"], full["counts"]["failed"]), (4, 2, 2))
        self.assertEqual(full["title"], "Готово 2 из 4")
        self.assertEqual(full["started"], 100.0)
        self.assertEqual(sorted(full["fix"]["jobs"]), ["c1", "r3"], "«Исправить» продолжает ВСЁ упавшее, не только последнюю пачку")
        self.assertTrue(any("Канал" in p["title"] for p in full["problems"]), "в проблеме видно, какой это ролик")

    def test_seen_report_is_not_carried(self) -> None:
        from unittest import mock

        from server.jobs import JobManager

        report._FILE.unlink(missing_ok=True)
        mgr = JobManager.__new__(JobManager)
        b1 = {"done": 0, "failed": 1, "sec": 99.0, "last": "", "started": 1.0,
              "snaps": {"a": self._snap("a", "polls", "failed", "Голосования")}}
        b2 = {"done": 1, "failed": 0, "sec": 99.0, "last": "", "started": 2.0,
              "snaps": {"b": self._snap("b", "cover", "done", "Обложка")}}
        with mock.patch("server.jobs.hub"), mock.patch("server.notify.notify"):
            mgr._report(b1)
            report.mark_seen(None)
            mgr._report(b2)
        full = report.get(report.listing()[0]["id"])
        self.assertEqual(full["counts"]["jobs"], 1)
        self.assertEqual(len(report.listing()), 2)


if __name__ == "__main__":
    unittest.main()
