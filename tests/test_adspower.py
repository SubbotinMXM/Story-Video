"""AdsPower Local API: ключ в заголовке, понятные ошибки, профили постранично, запуск → адрес для Playwright."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import unittest
from unittest import mock

import httpx

from engine.publish import adspower as ap


def client(handler) -> ap.AdsPower:
    return ap.AdsPower("KEY", "http://ads.test", transport=httpx.MockTransport(handler))


class AdsPowerTests(unittest.TestCase):
    def setUp(self) -> None:
        for name, value in (("MIN_INTERVAL", 0), ("TRANSPORT_PAUSES", (0, 0, 0))):
            p = mock.patch.object(ap, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_timeout_then_answer_is_not_adspower_down(self) -> None:
        calls = []

        def flaky(req):
            calls.append(1)
            if len(calls) < 3:
                raise httpx.ReadTimeout("timed out")
            return httpx.Response(200, json={"code": 0, "data": {}})

        with client(flaky) as c:
            c.status()
        self.assertEqual(len(calls), 3, "две заминки — не повод считать AdsPower закрытым")

    def test_key_in_bearer_header(self) -> None:
        seen = []

        def handler(req):
            seen.append(req.headers.get("authorization"))
            return httpx.Response(200, json={"code": 0, "msg": "success"})

        with client(handler) as c:
            c.status()
        self.assertEqual(seen, ["Bearer KEY"])

    def test_profiles_all_pages_sorted(self) -> None:
        def handler(req):
            page = int(req.url.params["page"])
            n = 100 if page == 1 else 3
            items = [{"user_id": f"u{page}_{i}", "serial_number": str(1000 - (page * 100 + i)), "name": f"P{i}"}
                     for i in range(n)]
            return httpx.Response(200, json={"code": 0, "data": {"list": items}})

        with client(handler) as c:
            profiles = c.profiles()
        self.assertEqual(len(profiles), 103)
        self.assertEqual([int(p.number) for p in profiles], sorted(int(p.number) for p in profiles))

    def test_start_gives_cdp_address(self) -> None:
        body = {}

        def handler(req):
            body.update(json.loads(req.content))
            return httpx.Response(200, json={"code": 0, "data": {
                "ws": {"puppeteer": "ws://127.0.0.1:9222/devtools/browser/x", "selenium": "127.0.0.1:9222"},
                "debug_port": "9222", "webdriver": "/path/chromedriver"}})

        with client(handler) as c:
            s = c.start("k1ixgwcn")
        self.assertEqual(s.cdp_url, "ws://127.0.0.1:9222/devtools/browser/x")
        self.assertEqual(body["profile_id"], "k1ixgwcn")
        self.assertEqual(body["headless"], "0")

    def test_rate_limit_retried(self) -> None:
        calls = []

        def handler(req):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(200, json={"code": -1, "msg": "Too many request per second, please check"})
            return httpx.Response(200, json={"code": 0, "data": {"status": "Active"}})

        with client(handler) as c:
            self.assertTrue(c.is_open("x"))
        self.assertEqual(len(calls), 2)

    def test_readable_errors(self) -> None:
        with client(lambda req: httpx.Response(200, json={"code": -1, "msg": "Require api-key"})) as c, \
                self.assertRaisesRegex(ap.AdsPowerError, "ключ"):
            c.status()

        def down(req):
            raise httpx.ConnectError("refused")

        with client(down) as c, self.assertRaisesRegex(ap.AdsPowerError, "открой программу AdsPower"):
            c.status()
        with self.assertRaisesRegex(ap.AdsPowerError, "Нет ключа"):
            ap.AdsPower("", "http://ads.test").status()



class PauseTests(unittest.TestCase):
    def test_pause_between_actions_is_1_5_to_5_5_seconds(self) -> None:
        import engine.publish as pub

        clock = [0.0]
        with mock.patch.object(pub.time, "monotonic", lambda: clock[0]), \
             mock.patch.object(pub.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s)):
            waits = [pub.pause() for _ in range(200)]
        self.assertTrue(all(1.5 <= w <= 5.5 for w in waits))
        self.assertGreater(max(waits) - min(waits), 2, "каждый раз разная")




class BusyProfileTest(unittest.TestCase):
    def test_busy_profile_explained(self) -> None:
        import httpx

        from engine.publish import adspower

        def h(req):
            return httpx.Response(200, json={"code": -1, "msg": "[k1] is being used by [x@example.com] and is not allowed to open"})

        ap = adspower.AdsPower("k", transport=httpx.MockTransport(h))
        with mock.patch.object(adspower, "BUSY_WAITS", (0, 0)), mock.patch.object(adspower, "MIN_INTERVAL", 0):
            with self.assertRaises(adspower.ProfileBusy) as err:
                ap.start("k1")
        self.assertIn("x@example.com", str(err.exception))
        self.assertIn("Освободить", str(err.exception))

    def test_stale_busy_released_and_retried(self) -> None:
        import httpx

        from engine.publish import adspower

        calls = []

        def h(req):
            calls.append(req.url.path)
            if req.url.path.endswith("/start") and calls.count(req.url.path) == 1:
                return httpx.Response(200, json={"code": -1, "msg": "[k1] is being used by [x@example.com] and is not allowed to open"})
            if req.url.path.endswith("/start"):
                return httpx.Response(200, json={"code": 0, "data": {"ws": {"puppeteer": "ws://x"}}})
            return httpx.Response(200, json={"code": 0, "data": {}})

        ap = adspower.AdsPower("k", transport=httpx.MockTransport(h))
        with mock.patch.object(adspower, "BUSY_WAITS", (0, 0)), mock.patch.object(adspower, "MIN_INTERVAL", 0):
            s = ap.start("k1")
        self.assertEqual(s.cdp_url, "ws://x")
        self.assertEqual([c.rsplit("/", 1)[-1] for c in calls], ["start", "stop", "start"], "освободил и открыл заново")

if __name__ == "__main__":
    unittest.main()
