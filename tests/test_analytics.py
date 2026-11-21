"""Аналитика каналов (engine.publish.analytics): чтение цифр Studio, хранение, синхронизация по профилям."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import unittest
from unittest import mock

from engine.publish import analytics as an
from engine.publish import registry, youtube


class NumbersTest(unittest.TestCase):
    def test_formats(self) -> None:
        cases = {"9,981": 9981, "1,832,282": 1832282, "777,373.7": 777373.7, "113.0K": 113000, "9 981": 9981,
                 "777 373,7": 777373.7, "1,2 тыс.": 1200, "3,4 млн": 3400000, "395.7": 395.7, "7": 7,
                 "2.5M": 2500000, "1.832.282": 1832282, "—": None, "": None}
        for text, want in cases.items():
            got = an.parse_number(text)
            if want is None:
                self.assertIsNone(got, text)
            else:
                self.assertAlmostEqual(got, want, places=3, msg=text)

    def test_hub_cards(self) -> None:
        cards = [("Key metrics\nViews\n113.0K\nWatch time (hours)\n38.1K\nSubscribers\n+533", False),
                 ("48 hour views\n2,180\n140\n70\n0", True), ("Total subscribers\n9,981", False),
                 ("Watched right now\nВидео\n355", False)]
        self.assertEqual(an.read_hub(cards), (9981, 2180))
        ru = [("Просмотры за 48 часов\n12\n4", True), ("Всего подписчиков\n7", False)]
        self.assertEqual(an.read_hub(ru), (7, 12))
        unknown_lang = [("Vues (48 h)\n12", True), ("Total X\n70", False)]
        self.assertEqual(an.read_hub(unknown_lang), (70, 12), "подписчики — соседняя карточка")

    def test_totals_in_url_order(self) -> None:
        self.assertEqual(an.read_totals(["777,373.7", "1,832,282", "9,960"]), (777373.7, 1832282, 9960))
        self.assertEqual(an.read_totals(["402.0"]), (402.0, None, None))


class StoreTest(unittest.TestCase):
    def test_day_before(self) -> None:
        from datetime import datetime

        from engine.publish.schedule import MSK

        t = lambda d, h: datetime(2026, 11, d, h, tzinfo=MSK).timestamp()  # noqa: E731
        hist = [{"t": t(13, 7), "subscribers": 1}, {"t": t(15, 7), "subscribers": 2}, {"t": t(15, 23), "subscribers": 3},
                {"t": t(16, 7), "subscribers": 4}]
        self.assertEqual(an.day_before(hist, t(16, 7))["subscribers"], 3)  # последний вчерашний
        self.assertEqual(an.day_before(hist[:1] + hist[3:], t(16, 7))["subscribers"], 1)  # вчера не было — раньше
        self.assertIsNone(an.day_before(hist[3:], t(16, 7)))

    def test_history_and_error_keeps_numbers(self) -> None:
        an.save_one(an.Stats("UCx1", name="A", subscribers=10, watch_hours=5.0, views_48h=3, synced_at=100))
        an.save_one(an.Stats("UCx1", name="A", subscribers=12, watch_hours=6.5, views_48h=4, synced_at=200))
        an.save_one(an.Stats("UCx1", name="A", error="профиль занят"))
        st = an.load()["UCx1"]
        self.assertEqual((st.subscribers, st.watch_hours, st.synced_at), (12, 6.5, 200), "ошибка не стирает цифры")
        self.assertEqual(st.error, "профиль занят")
        self.assertEqual([h["subscribers"] for h in st.history], [10, 12])
        an.save_one(an.Stats("UCx1", name="A", subscribers=13, synced_at=300))
        self.assertEqual(an.load()["UCx1"].error, "", "удачная синхронизация убирает ошибку")


class ScheduledCountTest(unittest.TestCase):
    """Столбец «Запланировано»: сколько роликов канала ждут выхода (страница «Контент» в Studio)."""

    EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sept", "Oct", "Nov", "Dec"]
    TODAY = __import__("datetime").date(2026, 11, 19)

    def rows(self, *items: str) -> list[str]:
        return list(items)

    def test_scheduled_by_word_and_by_future_date(self) -> None:
        rows = ["… — Scheduled Nov 21, 2026 Scheduled 0 0", "… — Scheduled Nov 20, 2026 Scheduled 0 0",
                "… — Public Nov 10, 2026 Published 32 1", "… — Private Oct 15, 2026 Uploaded 16 0"]
        self.assertEqual(youtube.scheduled_in_rows(rows, self.EN, self.TODAY), (2, False))

    def test_other_languages_by_word(self) -> None:
        self.assertEqual(youtube.scheduled_in_rows(["… Запланировано 21 нояб. 2026 г. 0 0"], [], self.TODAY)[0], 1)
        self.assertEqual(youtube.scheduled_in_rows(["… Programado 21 nov 2026"], [], self.TODAY)[0], 1)
        self.assertEqual(youtube.scheduled_in_rows(["… Geplant 21. Nov. 2026"], [], self.TODAY)[0], 1)

    def test_unknown_language_falls_back_to_future_date(self) -> None:
        self.assertEqual(youtube.scheduled_in_rows(["XYZ 25 Nov 2026 abc 0 0"], self.EN, self.TODAY)[0], 1)
        self.assertEqual(youtube.scheduled_in_rows(["XYZ 10 Nov 2026 abc 3 1"], self.EN, self.TODAY)[0], 0)

    def test_drafts_and_private_with_future_date_are_not_scheduled(self) -> None:
        self.assertEqual(youtube.scheduled_in_rows(["… Private Dec 30, 2026 Uploaded 0 0"], self.EN, self.TODAY)[0], 0)
        self.assertEqual(youtube.scheduled_in_rows(["… Draft Dec 30, 2026"], self.EN, self.TODAY)[0], 0)

    def test_every_row_scheduled_means_look_at_next_page(self) -> None:
        n, every = youtube.scheduled_in_rows(["Scheduled Nov 21, 2026", "Scheduled Nov 22, 2026"], self.EN, self.TODAY)
        self.assertEqual((n, every), (2, True))
        self.assertEqual(youtube.scheduled_in_rows([], self.EN, self.TODAY), (0, False))

    def test_pages_are_turned_while_everything_is_scheduled(self) -> None:
        class Loc:
            def __init__(self, page):
                self.page, self.first = page, self

            def count(self):
                return 1

            def get_attribute(self, _n):
                return None

            def click(self):
                self.page.i += 1

        class Page:
            def __init__(self):
                self.i = 0
                self.pages = [["Scheduled Nov 21, 2026"] * 30, ["Scheduled Nov 25, 2026"] * 5 + ["Public Nov 1, 2026 Published"]]

            def goto(self, *a, **k):
                pass

            def wait_for_selector(self, *a, **k):
                pass

            def wait_for_timeout(self, _ms):
                pass

            def evaluate(self, _js):
                return ScheduledCountTest.EN

            def eval_on_selector_all(self, *a):
                return self.pages[self.i]

            def locator(self, _sel):
                return Loc(self)

        self.assertEqual(youtube.count_scheduled(Page(), "UC1"), 35)

    def test_unreadable_page_is_none(self) -> None:
        page = mock.MagicMock()
        page.wait_for_selector.side_effect = RuntimeError("timeout")
        self.assertIsNone(youtube.count_scheduled(page, "UC1"))

    def test_saved_and_kept_when_next_sync_cannot_read(self) -> None:
        an.save_one(an.Stats("UCs1", name="A", subscribers=1, scheduled=7, synced_at=100))
        an.save_one(an.Stats("UCs1", name="A", subscribers=2, scheduled=None, synced_at=200))
        self.assertEqual(an.load()["UCs1"].scheduled, 7, "не прочиталось — прежнее число, а не пусто")
        an.save_one(an.Stats("UCs1", name="A", subscribers=3, scheduled=0, synced_at=300))
        self.assertEqual(an.load()["UCs1"].scheduled, 0, "ноль — настоящее значение, не «нет данных»")
        an.save_one(an.Stats("UCs1", name="A", error="профиль занят"))
        self.assertEqual(an.load()["UCs1"].scheduled, 0, "ошибка не стирает")

    def test_read_channel_survives_failing_scheduled_read(self) -> None:
        import types

        page = mock.MagicMock()
        page.locator.return_value.count.return_value = 0  # карточек нет — read_hub подменён
        ch = types.SimpleNamespace(id="UC1", name="A", handle="@a")
        with mock.patch.object(youtube, "studio_channel", return_value="UC1"), mock.patch.object(an, "_wait", return_value=True), \
             mock.patch.object(an, "read_hub", return_value=(5, 7)), mock.patch.object(an, "read_totals", return_value=(1.0, 2, 3)), \
             mock.patch.object(an, "pause"), mock.patch.object(youtube, "goto"), \
             mock.patch.object(youtube, "count_videos", side_effect=RuntimeError("Studio поменяла вёрстку")), \
             mock.patch.object(youtube, "count_scheduled", side_effect=RuntimeError("Studio поменяла вёрстку")):
            st = an.read_channel(page, ch)
        self.assertEqual((st.subscribers, st.views_48h, st.scheduled), (5, 7, None), "основные цифры не страдают")

    def test_read_channel_takes_videos_by_visibility(self) -> None:
        import types

        page = mock.MagicMock()
        page.locator.return_value.count.return_value = 0
        ch = types.SimpleNamespace(id="UC1", name="A", handle="@a")
        counts = {"scheduled": 3, "public": 12, "unlisted": 2, "private": 5, "draft": 1}
        with mock.patch.object(youtube, "studio_channel", return_value="UC1"), mock.patch.object(an, "_wait", return_value=True), \
             mock.patch.object(an, "read_hub", return_value=(5, 7)), mock.patch.object(an, "read_totals", return_value=(1.0, 2, 3)), \
             mock.patch.object(an, "pause"), mock.patch.object(youtube, "goto"), \
             mock.patch.object(youtube, "count_videos", return_value=counts):
            st = an.read_channel(page, ch)
        self.assertEqual((st.scheduled, st.public, st.unlisted, st.private), (3, 12, 2, 5))

    def test_visibility_counts_kept_when_not_read(self) -> None:
        an.save_one(an.Stats("UCv1", name="A", subscribers=1, public=10, unlisted=1, private=2, synced_at=100))
        an.save_one(an.Stats("UCv1", name="A", subscribers=2, synced_at=200))
        got = an.load()["UCv1"]
        self.assertEqual((got.public, got.unlisted, got.private), (10, 1, 2))


class SyncScopeTest(unittest.TestCase):
    """Кнопка «Синхронизировать»: каналы выбранных групп (канал может быть в нескольких группах)."""

    def setUp(self) -> None:
        self.CFG = {"adspower_api_key": "k", "adspower_url": "http://x", "analytics_groups": [
            {"id": "g1", "name": "Максим", "channels": ["UC1", "UC2", "UC3"]},
            {"id": "g2", "name": "Рассказы", "channels": ["UC3", "UC4"]},  # UC3 — в обеих группах
            {"id": "g3", "name": "Пустая", "channels": []}]}

    def run_job(self, params):
        from server import runners

        title, run = runners._analytics_job(dict(self.CFG), params, {})
        seen = {}

        def fake_sync(key, url, **kw):
            seen.update(kw)
            return {"ok": ["a"], "failed": [], "total": 1, "added": [], "notes": []}

        with mock.patch.object(an, "sync", fake_sync), mock.patch("server.runners.load_config", lambda: dict(self.CFG)):
            out = run(mock.MagicMock(), lambda *a: None, mock.MagicMock())
        return title, seen, out

    def test_one_group_takes_all_its_channels_even_those_also_in_other_groups(self) -> None:
        title, seen, _ = self.run_job({"groups": ["g1"]})
        self.assertEqual(seen["channel_ids"], ["UC1", "UC2", "UC3"], "«Максим» целиком, включая UC3, который есть и в «Рассказах»")
        self.assertEqual(title, "Аналитика каналов: группа «Максим»")

    def test_several_groups_union_without_duplicates(self) -> None:
        title, seen, _ = self.run_job({"groups": ["g1", "g2"]})
        self.assertEqual(seen["channel_ids"], ["UC1", "UC2", "UC3", "UC4"], "UC3 — один раз")
        self.assertIn("группы «Максим», «Рассказы»", title)

    def test_many_groups_short_title(self) -> None:
        title, _seen, _ = self.run_job({"groups": ["g1", "g2", "g3", "g1x"]})
        self.assertEqual(title, "Аналитика каналов: 4 групп")

    def test_channels_without_group(self) -> None:
        from engine.publish import registry

        entry = lambda *ids: mock.Mock(missing=False, channels=[mock.Mock(id=i) for i in ids])  # noqa: E731
        with mock.patch.object(registry, "load", lambda: {"p1": entry("UC1", "UC5"), "p2": entry("UC6", "UC4")}):
            title, seen, _ = self.run_job({"groups": ["none"]})
        self.assertEqual(seen["channel_ids"], ["UC5", "UC6"], "каналы, которых нет ни в одной группе")
        self.assertIn("без группы", title)

    def test_empty_selection_does_not_sync_everything(self) -> None:
        _title, seen, out = self.run_job({"groups": ["g3"]})
        self.assertEqual(seen, {}, "пустые группы — ничего не синхронизируем (не «всё»)")
        self.assertIn("нет каналов", out["summary"])

    def test_membership_is_read_when_the_job_runs(self) -> None:
        from server import runners

        _title, run = runners._analytics_job(dict(self.CFG), {"groups": ["g2"]}, {})
        self.CFG["analytics_groups"][1]["channels"] = ["UC9"]  # группу поменяли, пока задача ждала в очереди
        seen = {}
        with mock.patch.object(an, "sync", lambda key, url, **kw: seen.update(kw) or {"ok": [], "failed": [], "total": 0, "added": [], "notes": []}), \
             mock.patch("server.runners.load_config", lambda: dict(self.CFG)):
            run(mock.MagicMock(), lambda *a: None, mock.MagicMock())
        self.assertEqual(seen["channel_ids"], ["UC9"])

    def test_old_group_param_for_schedule_still_works(self) -> None:
        title, seen, _ = self.run_job({"group": "g2", "auto": True})
        self.assertEqual(seen["channel_ids"], ["UC3", "UC4"])
        self.assertIn("(по расписанию)", title)

    def test_explicit_channels_win(self) -> None:
        _title, seen, _ = self.run_job({"groups": ["g1"], "channels": ["UC9"]})
        self.assertEqual(seen["channel_ids"], ["UC9"])

    def test_adspower_profile_list_is_refreshed_for_everyone(self) -> None:
        """Группа сужает только чтение цифр: перечитывание списка профилей из AdsPower в sync() идёт по всем."""
        import inspect

        src = inspect.getsource(an.sync)
        self.assertIn("_refresh_profiles(api_key, url)", src)
        self.assertIn("rescan = discover or bool(only)", src, "с группой профили AdsPower тоже обновляются")

    def test_page_has_group_based_sync_without_sync_everything(self) -> None:
        from pathlib import Path

        page = (Path(__file__).resolve().parents[1] / "web" / "src" / "pages" / "AnalyticsPage.tsx").read_text(encoding="utf-8")
        self.assertIn("analytics-sync-groups", page, "выбор групп запоминается")
        self.assertIn("params: { groups: syncEffective }", page)
        self.assertNotIn("Синхронизировать всё", page, "кнопки «синхронизировать всё» больше нет")
        # профили, которые ни разу не открывали, в группах не бывают: каналы в них ищутся кнопкой у профиля
        self.assertIn("params: { profiles: [", page)
        self.assertIn("Синхронизация с AdsPower", page)
        self.assertIn("Найти все каналы в профилях", page)
        self.assertNotIn("Только AdsPower", page)


class SyncTest(unittest.TestCase):
    def setUp(self) -> None:
        registry.save({
            "p1": registry.ProfileEntry("p1", "1", "Один", channels=[registry.Channel("UCa", "A", "@a"),
                                                                    registry.Channel("UCb", "B", "@b")]),
            "p2": registry.ProfileEntry("p2", "2", "Два", channels=[registry.Channel("UCc", "C", "@c")]),
            "p3": registry.ProfileEntry("p3", "3", "Пустой"),
        })

    def test_all_profiles_headless_and_profile_error_marks_its_channels(self) -> None:
        calls = []

        def in_profile(key, url, pid, work, *, headless, timeout, what, again, cancel=None):
            calls.append((pid, headless))
            if pid == "p2":
                from engine.publish.adspower import AdsPowerError
                raise AdsPowerError("занят")
            return work("ws://x", mock.Mock())

        def collect(cdp, channels, *, cancel=None, on_channel=None, pulse=None):
            for ch in channels:
                on_channel(an.Stats(ch.id, ch.name, ch.handle, subscribers=1, watch_hours=2.0, views_48h=3))

        seen = []
        with mock.patch("engine.publish.scan.in_profile", in_profile), mock.patch.object(an, "collect", collect):
            out = an.sync("k", "u", progress=lambda p, m: seen.append(p), discover=False)
        self.assertEqual(calls, [("p1", True), ("p2", True)], "скрыто; профиль без каналов не открывается")
        self.assertEqual(out["total"], 3)
        self.assertEqual(out["ok"], ["A", "B"])
        self.assertIn("занят", out["failed"][0])
        self.assertEqual(seen[-1], 1.0)
        self.assertEqual(an.load()["UCa"].profile, "№1 Один")

    def test_no_channels(self) -> None:
        registry.save({})
        with self.assertRaises(an.AnalyticsError):
            an.sync("k", "u", discover=False)

    def test_discover_finds_new_channels_and_profiles_in_one_visit(self) -> None:
        """По умолчанию: список профилей из AdsPower, в каждом профиле — перечитать каналы (новые сразу с цифрами),
        профиль открывается один раз; пустой результат поиска не стирает известные каналы."""
        opened = []

        def in_profile(key, url, pid, work, *, headless, timeout, what, again, cancel=None):
            opened.append(pid)
            return work("ws://x", mock.Mock())

        def scan(cdp, *, cancel=None, pulse=None, env=None):
            pid = opened[-1]
            if pid == "p1":
                return [registry.Channel("UCa", "A", "@a"), registry.Channel("UCb", "B", "@b"),
                        registry.Channel("UCnew", "Новый", "@new", playlists=["x"])]
            if pid == "p2":
                return []  # сбой чтения — прежний список остаётся
            return [registry.Channel("UCp3", "Из пустого", "@p3")]

        def collect(cdp, channels, *, cancel=None, on_channel=None, pulse=None):
            for ch in channels:
                on_channel(an.Stats(ch.id, ch.name, ch.handle, subscribers=1, views_48h=2))

        with mock.patch("engine.publish.scan.in_profile", in_profile), mock.patch.object(an, "collect", collect), \
                mock.patch("engine.publish.youtube.scan_channels", scan), \
                mock.patch.object(an, "_refresh_profiles", lambda k, u: ""):
            out = an.sync("k", "u")
        self.assertEqual(opened, ["p1", "p2", "p3"], "профиль без известных каналов тоже открывается")
        self.assertEqual(out["total"], 5)
        self.assertEqual(len(out["added"]), 2)
        self.assertIn("Новый", out["added"][0])
        self.assertEqual([c.id for c in registry.load()["p2"].channels], ["UCc"], "пустой поиск не стёр каналы")
        self.assertTrue(any("оставил прежний список" in n for n in out["notes"]))
        self.assertIn("UCnew", an.load())

    def test_group_sync_does_not_open_unknown_profiles(self) -> None:
        """Профили, которых программа не открывала, сами не сканируются: чужие и ненужные профили не трогаем.
        Каналы в них ищутся только по кнопке у конкретного профиля или «Найти все каналы в профилях»."""
        opened = []

        def in_profile(key, url, pid, work, *, headless, timeout, what, again, cancel=None):
            opened.append(pid)
            return work("ws://x", mock.Mock())

        with mock.patch("engine.publish.scan.in_profile", in_profile), mock.patch.object(an, "collect", lambda *a, **k: None), \
                mock.patch("engine.publish.youtube.scan_channels", lambda *a, **k: None), \
                mock.patch.object(an, "_refresh_profiles", lambda k, u: ""):
            an.sync("k", "u", channel_ids=["UCa"])
        self.assertEqual(opened, ["p1"], "p3 (пустой, не сканировался) и p2 (не из группы) не открываются")

    def test_one_profile_scan_finds_channels_with_numbers(self) -> None:
        """Кнопка «Найти каналы» у профиля: открывается только он, каналы попадают в учёт (для всего приложения) и в аналитику."""
        opened = []

        def in_profile(key, url, pid, work, *, headless, timeout, what, again, cancel=None):
            opened.append(pid)
            return work("ws://x", mock.Mock())

        def collect(cdp, channels, *, cancel=None, on_channel=None, pulse=None):
            for ch in channels:
                on_channel(an.Stats(ch.id, ch.name, ch.handle, subscribers=7))

        with mock.patch("engine.publish.scan.in_profile", in_profile), mock.patch.object(an, "collect", collect), \
                mock.patch("engine.publish.youtube.scan_channels", lambda *a, **k: [registry.Channel("UCp3", "Из третьего", "@p3")]):
            out = an.sync("k", "u", profile_ids=["p3"])
        self.assertEqual(opened, ["p3"])
        self.assertEqual([c.id for c in registry.load()["p3"].channels], ["UCp3"])
        self.assertIn("UCp3", an.load())
        self.assertEqual(len(out["added"]), 1)

    def test_empty_profile_is_marked_scanned_not_reopened_every_time(self) -> None:
        def in_profile(key, url, pid, work, *, headless, timeout, what, again, cancel=None):
            return work("ws://x", mock.Mock())

        with mock.patch("engine.publish.scan.in_profile", in_profile), mock.patch.object(an, "collect", lambda *a, **k: None), \
                mock.patch("engine.publish.youtube.scan_channels", lambda *a, **k: []), \
                mock.patch.object(an, "_refresh_profiles", lambda k, u: ""):
            out = an.sync("k", "u", profile_ids=["p3"])
        self.assertTrue(registry.load()["p3"].scanned_at)
        self.assertTrue(any("каналов в профиле нет" in n for n in out["notes"]))


class ApiTest(unittest.TestCase):
    def test_page_endpoint_lists_channels_with_numbers(self) -> None:
        from fastapi.testclient import TestClient

        from server.app import app

        registry.save({"p1": registry.ProfileEntry("p1", "1", "Один", channels=[registry.Channel("UCa", "A", "@a"),
                                                                                registry.Channel("UCz", "Z", "@z")])})
        day = 86400
        an.save_one(an.Stats("UCa", name="A", subscribers=4, watch_hours=1.0, views_48h=1, synced_at=10 * day))
        an.save_one(an.Stats("UCa", name="A", subscribers=5, watch_hours=1.5, views_48h=2, synced_at=11 * day))
        an.save_one(an.Stats("UCa", name="A", subscribers=8, watch_hours=2.5, views_48h=3, synced_at=12 * day))
        an.save_one(an.Stats("UCa", name="A", subscribers=9, watch_hours=2.6, views_48h=4, synced_at=12 * day + 60))
        r = TestClient(app).get("/api/analytics")
        self.assertEqual(r.status_code, 200)
        rows = {c["channel_id"]: c for c in r.json()["channels"]}
        self.assertEqual(rows["UCa"]["subscribers"], 9)
        # сравнение — с последним снимком вчерашнего дня (5), а не с прошлой синхронизацией сегодня (8)
        self.assertEqual(rows["UCa"]["prev"]["subscribers"], 5)
        self.assertIsNone(rows["UCz"]["subscribers"], "канал без синхронизации — в списке, без цифр")


class EndpointsTest(unittest.TestCase):
    def test_every_api_path_used_by_web_exists(self) -> None:
        """Каждый адрес /api/…, к которому обращается интерфейс, есть на сервере (ловит случайно удалённые)."""
        import re
        from pathlib import Path

        from server.app import app

        api_ts = (Path(__file__).resolve().parents[1] / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
        used = {re.sub(r"\$\{[^}]+\}", "{x}", m) for m in re.findall(r'["`](/api/[^"`?]+)', api_ts)}
        routes = [re.sub(r"\{[^}]+\}", "{x}", getattr(r, "path", "")) for r in app.routes]
        missing = sorted(u for u in used if u not in routes)
        self.assertEqual(missing, [], "интерфейс обращается к адресам, которых нет на сервере")


class GotoRetryTest(unittest.TestCase):
    class Page:
        def __init__(self, fails: int) -> None:
            self.fails, self.calls, self.url = fails, 0, "about:blank"

        def goto(self, url, **kw):
            self.calls += 1
            if self.calls <= self.fails:
                raise RuntimeError("Page.goto: net::ERR_CONNECTION_CLOSED at " + url)
            self.url = url

        def wait_for_timeout(self, ms):
            pass

    def test_retries_dropped_connection(self) -> None:
        page = self.Page(fails=2)
        youtube.goto(page, "https://studio.youtube.com/")
        self.assertEqual(page.calls, 3)

    def test_gives_up_with_clear_error(self) -> None:
        with self.assertRaises(youtube.StudioError) as ctx:
            youtube.goto(self.Page(fails=9), "https://studio.youtube.com/")
        self.assertIn("связь обрывается", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
