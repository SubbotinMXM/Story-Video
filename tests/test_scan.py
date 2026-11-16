"""«Обновить каналы»: предел по времени, освобождение профиля, повтор после закрытого браузера."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import threading
import unittest
from unittest import mock

from engine.publish import scan, youtube
from engine.publish.adspower import AdsPowerError


class FakeAP:
    opened = False
    calls: list = []

    def __init__(self, *a, **k) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def is_open(self, pid):
        return FakeAP.opened

    def start(self, pid, headless=False):
        FakeAP.calls.append("start")
        return mock.Mock(cdp_url="ws://x")

    def stop(self, pid):
        FakeAP.calls.append("stop")


class ScanTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeAP.opened, FakeAP.calls = False, []
        for p in (mock.patch.object(scan, "AdsPower", FakeAP), mock.patch.object(scan, "RELEASE_PAUSE", 0)):
            p.start()
            self.addCleanup(p.stop)

    def test_ok_closes_profile_it_opened(self) -> None:
        with mock.patch.object(youtube, "scan_channels", lambda cdp, **k: ["A"]):
            self.assertEqual(scan.scan_profile("k", "u", "p1"), ["A"])
        self.assertEqual(FakeAP.calls, ["start", "stop"])

    def test_user_opened_profile_stays_open(self) -> None:
        FakeAP.opened = True
        with mock.patch.object(youtube, "scan_channels", lambda cdp, **k: ["A"]):
            scan.scan_profile("k", "u", "p1")
        self.assertEqual(FakeAP.calls, ["start"])

    def test_hang_restarts_once_then_errors(self) -> None:
        import time

        runs = []

        def hang(cdp, **k):
            runs.append(1)
            time.sleep(1.5)  # «вкладка не отвечает»
            raise RuntimeError("Target closed")

        with mock.patch.object(youtube, "scan_channels", hang):
            with self.assertRaisesRegex(AdsPowerError, "завис"):
                scan.scan_profile("k", "u", "p1", timeout=0.2)
        self.assertEqual(len(runs), 2, "завис — профиль перезапущен и поиск начат заново один раз")
        self.assertIn("stop", FakeAP.calls)

    def test_no_progress_is_a_hang_but_steps_keep_it_alive(self) -> None:
        import time

        def slow_but_alive(cdp, pulse=None, **k):
            for _ in range(6):
                time.sleep(0.25)
                pulse.touch(0.5, "шаг")  # шаги идут — не зависание, хоть и дольше предела «без движения»
            return ["A"]

        seen = []
        with mock.patch.object(youtube, "scan_channels", slow_but_alive):
            got = scan.in_profile("k", "u", "p1", lambda cdp, pulse: youtube.scan_channels(cdp, pulse=pulse),
                                  stall=0.6, progress=lambda f, m: seen.append(m))
        self.assertEqual(got, ["A"])
        self.assertIn("шаг", seen)

    def test_stop_closes_profile_at_once(self) -> None:
        import time

        from engine.core.cancel import CancelledError, CancelToken

        token = CancelToken()
        threading.Timer(0.3, token.cancel).start()
        t0 = time.monotonic()
        with mock.patch.object(youtube, "scan_channels", lambda cdp, **k: time.sleep(10)):
            with self.assertRaises(CancelledError):
                scan.scan_profile("k", "u", "p1", cancel=token)
        self.assertLess(time.monotonic() - t0, 6, "«Стоп» не ждёт конца поиска")
        self.assertIn("stop", FakeAP.calls)

    def test_browser_closed_by_hand_retried_once(self) -> None:
        runs = []

        def flaky(cdp, **k):
            runs.append(1)
            if len(runs) == 1:
                raise RuntimeError("Target page, context or browser has been closed")
            return ["A", "B"]

        with mock.patch.object(youtube, "scan_channels", flaky):
            self.assertEqual(scan.scan_profile("k", "u", "p1"), ["A", "B"])
        self.assertEqual(len(runs), 2)

    def test_other_errors_not_retried(self) -> None:
        runs = []

        def bad(cdp, **k):
            runs.append(1)
            raise youtube.StudioError("нет каналов")

        with mock.patch.object(youtube, "scan_channels", bad):
            with self.assertRaises(youtube.StudioError):
                scan.scan_profile("k", "u", "p1")
        self.assertEqual(len(runs), 1)


if __name__ == "__main__":
    unittest.main()
