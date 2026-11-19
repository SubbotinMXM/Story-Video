"""Соавторства: каждое отправленное приглашение обязано быть принято (журнал пар + фоновое подтверждение)."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import time
import unittest
from unittest import mock

from engine.publish import collabs, registry, youtube
from engine.publish.store import read_json

from test_publish import fresh_store  # noqa: E402


def target(pid: str, cid: str, name: str) -> registry.Target:
    return registry.Target(profile_id=pid, profile=f"№{pid}", channel=registry.Channel(id=cid, name=name, handle=f"@{cid}"))


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        fresh_store(self)

    def test_invite_is_pending_until_accepted(self) -> None:
        self.assertEqual(collabs.record_invites("p1/UC1", "Канал 1", {"pc/UCa": "L1", "pc/UCb": "L2", "pc/UCc": ""},
                                                title="Ролик", url="u"), 2, "пустая ссылка — приглашения нет")
        self.assertEqual({(r["owner"], r["co"]) for r in collabs.pending()}, {("p1/UC1", "pc/UCa"), ("p1/UC1", "pc/UCb")})
        collabs.mark_accepted("p1/UC1", "pc/UCa")
        self.assertEqual([r["co"] for r in collabs.pending()], ["pc/UCb"])

    def test_new_invites_after_acceptance_are_pending_again(self) -> None:
        collabs.record_invites("p1/UC1", "К", {"pc/UCa": "L"}, title="Первый")
        collabs.mark_accepted("p1/UC1", "pc/UCa")
        self.assertEqual(collabs.pending(), [])
        collabs.record_invites("p1/UC1", "К", {"pc/UCa": "L"}, title="Второй")
        self.assertEqual(len(collabs.pending()), 1, "новый ролик — новое приглашение")
        self.assertEqual(collabs.pending()[0]["videos"], ["Второй"])

    def test_failure_keeps_pair_with_reason_and_backoff(self) -> None:
        collabs.record_invites("p1/UC1", "К", {"pc/UCa": "L"}, title="Ролик")
        now = time.time()
        collabs.mark_failed("p1/UC1", "pc/UCa", "Google просит подтвердить вход")
        rec = collabs.pending()[0]
        self.assertEqual((rec["tries"], rec["error"]), (1, "Google просит подтвердить вход"))
        self.assertEqual(collabs.pending(now, only_due=True), [], "срок следующей попытки ещё не подошёл")
        self.assertEqual(len(collabs.pending(now + 11 * 60, only_due=True)), 1, "через 10 минут — снова")
        collabs.mark_failed("p1/UC1", "pc/UCa", "опять")
        collabs.mark_failed("p1/UC1", "pc/UCa", "и опять")
        self.assertEqual(len(collabs.pending(time.time() + 61 * 60, only_due=True)), 1, "дальше — раз в час, но не бросаем")

    def test_failed_without_record_still_remembered(self) -> None:
        collabs.mark_failed("p1/UC1", "pc/UCa", "профиль не запустился", link="L", owner_label="К")
        self.assertEqual(len(collabs.pending()), 1)

    def test_backfill_once_from_old_jobs(self) -> None:
        states = [{"channels": {"p1/UC1": {"status": "done", "invites": {"pc/UCa": "L1", "pc/UCb": "L2"}},
                                "p1/UC2": {"status": "failed", "invites": {"pc/UCa": "L3"}}}}]
        self.assertEqual(collabs.backfill(states), 2, "только выложенные каналы")
        self.assertEqual(collabs.backfill(states), 0, "второй раз не добавляем")
        collabs.mark_accepted("p1/UC1", "pc/UCa")
        collabs.backfill(states)
        self.assertEqual(len(collabs.pending()), 1, "подтверждённое не возвращается")
        self.assertTrue(read_json(collabs.FILE, {})["backfilled"])


class PublishRecordsTests(unittest.TestCase):
    def setUp(self) -> None:
        fresh_store(self)

    def run_accept(self, fake):
        from engine.publish import run as pub

        co = target("pc", "UCco", "Соавтор")
        t1, t2 = target("p1", "UC1", "А"), target("p1", "UC2", "Б")
        results = {t.key: {"status": pub.DONE, "title": "Одно и то же длинное название", "invites": {co.key: f"L-{t.key}"}} for t in (t1, t2)}
        for t in (t1, t2):
            collabs.record_invites(t.key, t.label(), results[t.key]["invites"], title="Одно и то же длинное название")
        ap = mock.MagicMock()
        ap.__enter__.return_value = ap
        with mock.patch.object(pub, "AdsPower", return_value=ap), mock.patch.object(youtube, "accept_coauthor", side_effect=fake):
            pub._accept_coauthors(mock.Mock(adspower=("k", "u"), visible=False), [t1, t2], [co], results, lambda **_: None, None)
        return t1, t2, co, results

    def test_accepted_pairs_leave_the_ledger(self) -> None:
        self.run_accept(lambda *a, **k: ["Одно и то же длинное название"])
        self.assertEqual(collabs.pending(), [])

    def test_failed_pair_stays_and_is_not_lost(self) -> None:
        def fake(cdp, ch, link, **kw):
            if link.endswith("UC2"):
                raise youtube.StudioError("«Принять» не сработало — окно приглашения не закрылось")
            return ["Одно и то же длинное название"]

        t1, t2, co, results = self.run_accept(fake)
        left = collabs.pending()
        self.assertEqual([(r["owner"], r["co"]) for r in left], [(t2.key, co.key)], "в списке осталась только неудавшаяся пара")
        self.assertIn("не сработало", left[0]["error"])
        self.assertTrue(results[t2.key].get("coauthors_failed"))


class ReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        fresh_store(self)
        self.co = target("pc", "UCco", "Соавтор")
        collabs.record_invites("p1/UC1", "Канал 1", {self.co.key: "L1"}, title="Ролик А")
        collabs.record_invites("p1/UC2", "Канал 2", {self.co.key: "L2"}, title="Ролик А")

    def run_reconcile(self, accept, *, resolve=None):
        ap = mock.MagicMock()
        ap.__enter__.return_value = ap
        ap.alive.return_value = True
        with mock.patch("engine.publish.adspower.AdsPower", return_value=ap), \
             mock.patch.object(registry, "resolve", resolve or (lambda keys, what="": [self.co])), \
             mock.patch.object(youtube, "accept_coauthor_links", accept):
            return collabs.reconcile("k", "u"), ap

    def test_all_links_of_a_profile_in_one_visit(self) -> None:
        seen = []

        def accept(cdp, ch, links, cancel=None):
            seen.append(list(links))
            return {"L1": ["Ролик А"], "L2": []}

        out, ap = self.run_reconcile(accept)
        self.assertEqual(seen, [["L1", "L2"]], "один заход в профиль соавтора — обе ссылки")
        self.assertEqual((len(out["ok"]), out["failed"], out["total"]), (2, [], 2))
        self.assertEqual(collabs.pending(), [])
        ap.stop.assert_called_once()

    def test_failed_link_stays_others_are_confirmed(self) -> None:
        out, _ap = self.run_reconcile(lambda cdp, ch, links, cancel=None: {"L1": ["Ролик А"], "L2": "окно не закрылось"})
        self.assertEqual(len(out["ok"]), 1)
        self.assertEqual(len(out["failed"]), 1)
        left = collabs.pending()
        self.assertEqual([r["link"] for r in left], ["L2"])
        self.assertEqual(left[0]["error"], "окно не закрылось")

    def test_profile_crash_keeps_everything_pending(self) -> None:
        def accept(*a, **k):
            raise youtube.StudioError("Studio не открылась")

        out, _ap = self.run_reconcile(accept)
        self.assertEqual(len(out["failed"]), 2)
        self.assertEqual(len(collabs.pending()), 2, "ничего не потеряно")

    def test_browser_closed_midway_profile_is_reopened(self) -> None:
        calls = []

        def accept(cdp, ch, links, cancel=None):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("Locator.is_visible: Target page, context or browser has been closed")
            return {"L1": ["Ролик А"], "L2": ["Ролик А"]}

        out, ap = self.run_reconcile(accept)
        self.assertEqual(len(calls), 2, "браузер закрылся — открыли профиль заново и дошли до конца")
        self.assertEqual((len(out["ok"]), out["failed"]), (2, []))
        self.assertEqual(ap.start.call_count, 2)
        self.assertEqual(collabs.pending(), [])

    def test_browser_keeps_closing_gives_up_but_keeps_pairs(self) -> None:
        def accept(*a, **k):
            raise RuntimeError("Target page, context or browser has been closed")

        out, ap = self.run_reconcile(accept)
        self.assertEqual(ap.start.call_count, collabs.PROFILE_TRIES)
        self.assertEqual(len(out["failed"]), 2)
        self.assertEqual(len(collabs.pending()), 2)

    def test_unknown_coauthor_channel_is_reported(self) -> None:
        def nope(keys, what=""):
            raise ValueError("канала нет")

        out, _ap = self.run_reconcile(lambda *a, **k: {}, resolve=nope)
        self.assertEqual(len(out["failed"]), 2)
        self.assertIn("нет в учёте", out["failed"][0])

    def test_nothing_pending_is_a_no_op(self) -> None:
        collabs.mark_accepted("p1/UC1", self.co.key)
        collabs.mark_accepted("p1/UC2", self.co.key)
        self.assertEqual(collabs.reconcile("k", "u"), {"ok": [], "failed": [], "total": 0})


class TickTests(unittest.TestCase):
    def setUp(self) -> None:
        fresh_store(self)

    def manager(self, jobs):
        m = mock.MagicMock()
        m.list.return_value = jobs
        m.add.return_value = mock.Mock(id="j1")
        return m

    def test_nothing_pending_no_job(self) -> None:
        m = self.manager([])
        self.assertEqual(collabs.tick(m), "")
        m.add.assert_not_called()

    def test_pending_queues_one_job(self) -> None:
        collabs.record_invites("p1/UC1", "К", {"pc/UCa": "L"})
        m = self.manager([])
        self.assertEqual(collabs.tick(m), "j1")
        m.create.assert_called_once_with("collabs", {"auto": True})

    def test_not_while_publish_or_collabs_is_running(self) -> None:
        collabs.record_invites("p1/UC1", "К", {"pc/UCa": "L"})
        for kind, st in (("publish", "running"), ("publish", "queued"), ("collabs", "queued")):
            m = self.manager([{"kind": kind, "status": st}])
            self.assertEqual(collabs.tick(m), "", f"{kind}/{st}")
        done = self.manager([{"kind": "publish", "status": "done"}])
        self.assertEqual(collabs.tick(done), "j1")

    def test_waits_for_retry_time_after_a_failure(self) -> None:
        collabs.record_invites("p1/UC1", "К", {"pc/UCa": "L"})
        collabs.mark_failed("p1/UC1", "pc/UCa", "Google просит вход")
        m = self.manager([])
        self.assertEqual(collabs.tick(m), "", "через 10 минут, а не каждую минуту")
        self.assertEqual(collabs.tick(m, now=time.time() + 11 * 60), "j1")

    def test_cannot_create_job_is_not_fatal(self) -> None:
        collabs.record_invites("p1/UC1", "К", {"pc/UCa": "L"})
        m = self.manager([])
        m.create.side_effect = ValueError("Нужен ключ AdsPower")
        self.assertEqual(collabs.tick(m), "")


class WiringTests(unittest.TestCase):
    def test_job_kind_and_lane(self) -> None:
        from server import jobs, runners

        self.assertIn("collabs", jobs.UPLOAD_KINDS, "подтверждение — в очереди выкладки, профили AdsPower по одному")
        self.assertIn("collabs", runners._BUILDERS)

    def test_accept_links_returns_per_link_result(self) -> None:
        import inspect

        self.assertIn("allow_none", inspect.signature(youtube.accept_coauthor).parameters)
        self.assertTrue(callable(youtube.accept_coauthor_links))


if __name__ == "__main__":
    unittest.main()
