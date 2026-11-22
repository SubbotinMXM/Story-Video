"""Сторож связи: отключился VPN — очередь на паузе без траты попыток, VPN переподключается, связь вернулась — работа идёт."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import threading
import time
import unittest
from unittest import mock

from engine.core import netguard
from server import jobs as jobs_mod
from test_core_runtime import _ManagerCase, _wait


class _NetCase(unittest.TestCase):
    def setUp(self) -> None:
        netguard._set(True, "")
        self.addCleanup(netguard._set, True, "")
        p = mock.patch.object(netguard, "_notify", lambda *a: None)
        p.start()
        self.addCleanup(p.stop)


class StateTests(_NetCase):
    def test_down_and_back_notifies_listeners_once(self) -> None:
        seen: list[bool] = []
        netguard.on_change(lambda s: seen.append(s["up"]))
        netguard._set(False, "ConnectTimeout")
        netguard._set(False, "ConnectTimeout")  # тот же обрыв — без повторного события
        self.assertTrue(netguard.is_down())
        self.assertEqual(netguard.snapshot()["reason"], "ConnectTimeout")
        self.assertGreater(netguard.snapshot()["down_since"], 0)
        netguard._set(True, "")
        self.assertFalse(netguard.is_down())
        self.assertEqual(seen, [False, True])
        self.assertEqual(netguard.snapshot()["down_since"], 0)

    def test_check_now_is_off_in_tests(self) -> None:
        with mock.patch.object(netguard, "probe", side_effect=AssertionError("в тестах — без сети")):
            self.assertTrue(netguard.check_now())

    def test_check_now_confirms_outage_twice(self) -> None:
        with mock.patch.dict("os.environ", {"STORY_VIDEO_NO_NETGUARD": ""}), \
             mock.patch.object(netguard, "CONFIRM_DELAY", 0), \
             mock.patch.object(netguard, "probe", side_effect=[(False, "x"), (True, "")]) as probe:
            self.assertTrue(netguard.check_now(), "одна неудача — ещё не обрыв")
            self.assertEqual(probe.call_count, 2)
        with mock.patch.dict("os.environ", {"STORY_VIDEO_NO_NETGUARD": ""}), \
             mock.patch.object(netguard, "CONFIRM_DELAY", 0), \
             mock.patch.object(netguard, "probe", return_value=(False, "ERR_FAILED")):
            self.assertFalse(netguard.check_now())
        self.assertTrue(netguard.is_down())


class RepairTests(_NetCase):
    def test_connect_first_then_restart_vpn_app(self) -> None:
        calls: list[str] = []
        app = ("Happ", "/Applications/Happ.app", "happ://connect")
        with mock.patch.object(netguard, "vpn_app", return_value=app), \
             mock.patch.object(netguard, "_connect", lambda *a: calls.append("connect")), \
             mock.patch.object(netguard, "_restart", lambda *a: calls.append("restart")):
            netguard._set(False, "x")
            netguard._repair({})
            netguard._repair({})  # минута ещё не прошла — не дёргаем VPN снова
            self.assertEqual(calls, ["connect"])
            netguard._state.down_since -= netguard.RESTART_AFTER + 1
            netguard._state.last_connect = 0
            netguard._repair({})
            self.assertEqual(calls, ["connect", "restart"], "«подключиться» не помогло — перезапуск приложения VPN")
            netguard._state.last_connect = 0
            netguard._repair({})
            self.assertEqual(calls[-1], "connect", "перезапуск — не чаще раза в 15 мин, между ними — «подключиться»")
        self.assertEqual(netguard.snapshot()["restarts"], 1)

    def test_reconnect_can_be_turned_off(self) -> None:
        self.assertIsNone(netguard.vpn_app({"vpn_reconnect": False}))
        self.assertIsNone(netguard.vpn_app({"vpn_app": "НетТакого"}))


class QueueTests(_NetCase, _ManagerCase):
    def setUp(self) -> None:
        _NetCase.setUp(self)
        _ManagerCase.setUp(self)

    def test_outage_failure_waits_for_network_without_spending_retries(self) -> None:
        runs: list[int] = []

        def builder(kind, params, state):
            def run(job, progress, cancel):
                runs.append(1)
                if len(runs) == 1:
                    raise RuntimeError("Page.goto: net::ERR_FAILED")
                return {"summary": "ок"}

            return "Выкладка", run

        m = jobs_mod.JobManager(builder)
        with mock.patch.object(netguard, "check_now", lambda: (netguard._set(False, "ERR_FAILED"), False)[1]):
            job = m.add(m.create("publish", {}))
            _wait(lambda: job.state.get("wait_net") and job.status == jobs_mod.QUEUED)
        self.assertNotIn("auto_retries", job.state, "обрыв связи — попытка не засчитана")
        self.assertIn("VPN", job.message)
        time.sleep(0.3)
        self.assertEqual(len(runs), 1, "пока связи нет — задача не начинается")
        netguard._set(True, "")
        _wait(lambda: job.status == jobs_mod.DONE)
        self.assertEqual(len(runs), 2)
        self.assertNotIn("wait_net", job.state)

    def test_retry_at_dropped_when_network_returns(self) -> None:
        started = threading.Event()

        def builder(kind, params, state):
            def run(job, progress, cancel):
                started.set()
                return {}

            return "Х", run

        m = jobs_mod.JobManager(builder)
        netguard._set(False, "x")
        job = m.create("publish", {})
        job.state.update(wait_net=True, retry_at=time.time() + 3600)
        m.add(job)
        time.sleep(0.2)
        self.assertFalse(started.is_set())
        netguard._set(True, "")
        self.assertTrue(started.wait(3), "связь вернулась — не ждём часовую паузу")


class MemoryTests(_ManagerCase):
    def test_second_heavy_video_waits_when_memory_is_tight(self) -> None:
        gate = threading.Event()
        order: list[str] = []

        def builder(kind, params, state):
            def run(job, progress, cancel):
                order.append(params["n"])
                if params["n"] == "1":
                    gate.wait(3)
                return {}

            return params["n"], run

        box: dict = {}

        def tight(need=0):  # память занимает только наш первый ролик: доделался — освободилась
            m = box.get("m")
            return bool(m) and any(j.status == jobs_mod.RUNNING and j.kind in jobs_mod.HEAVY_KINDS for j in m._jobs)

        with mock.patch.object(jobs_mod.memguard, "tight", tight), \
             mock.patch.object(jobs_mod.memguard, "describe", return_value="свободно 2 из 16 ГБ"):
            m = box["m"] = jobs_mod.JobManager(builder)
            a = m.add(m.create("slides_full", {"n": "1"}))
            b = m.add(m.create("slides_full", {"n": "2"}))
            r = m.add(m.create("rewrite", {"n": "r"}))
            _wait(lambda: r.status == jobs_mod.DONE)
            self.assertEqual(b.status, jobs_mod.QUEUED, "второй ролик ждёт, пока освободится память")
            gate.set()
            _wait(lambda: b.status == jobs_mod.DONE, timeout=20)
        self.assertEqual(a.status, jobs_mod.DONE)


class StudioOfflineTests(unittest.TestCase):
    def test_browser_error_page_means_no_connection(self) -> None:
        from engine.publish.studio_kit import no_connection

        page = mock.Mock(url="chrome-error://chromewebdata/")
        self.assertEqual(no_connection(page, TimeoutError("Page.goto: Timeout 30000ms exceeded.")),
                         "страница ошибки браузера")
        self.assertEqual(no_connection(mock.Mock(url="https://studio.youtube.com"),
                                       RuntimeError("Page.goto: net::ERR_TIMED_OUT at https://x")), "ERR_TIMED_OUT")
        self.assertEqual(no_connection(mock.Mock(url="https://studio.youtube.com"), TimeoutError("locator")), "")

    def test_no_connection_is_transient(self) -> None:
        self.assertTrue(jobs_mod.is_transient(RuntimeError("шаг «x»: нет связи с YouTube — браузер не открыл страницу")))


if __name__ == "__main__":
    unittest.main()
