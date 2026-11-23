"""Выкладка «Футажей» на YouTube (каркас): материалы по кругу, даты по Москве, каналы, задача по каналам."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)
from _human import setUpModule, tearDownModule  # noqa: F401  (простые клики вместо «человеческих»)

import os
import random
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from engine.publish import materials, registry, schedule, youtube
from engine.publish import run as pub
from engine.publish.adspower import Profile, Session
from engine.publish.store import publish_dir


def fresh_store(test: unittest.TestCase) -> None:
    """Каждый тест — с пустым учётом выкладки."""
    shutil.rmtree(publish_dir(), ignore_errors=True)
    test.addCleanup(shutil.rmtree, publish_dir(), True)


class MaterialsTests(unittest.TestCase):
    def setUp(self) -> None:
        fresh_store(self)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.thumbs = self.tmp / "обложки"
        self.thumbs.mkdir()
        for i in range(3):
            (self.thumbs / f"{i}.jpg").write_bytes(b"x")
        self.titles = self.tmp / "названия.txt"
        self.titles.write_text("1. Первое\n2. Второе\n3) Третье\n", encoding="utf-8")
        self.descs = self.tmp / "описания.txt"
        self.descs.write_text("1. Описание раз\nвторая строка #теги\n\n2. Описание два\n", encoding="utf-8")

    def test_numbered_multiline(self) -> None:
        items = materials.read_numbered(self.descs, "описаний")
        self.assertEqual(items[1], "Описание раз\nвторая строка #теги")
        self.assertEqual(items[2], "Описание два")
        self.assertEqual(set(materials.read_numbered(self.titles, "названий")), {1, 2, 3})

    def test_thumbnails_cycle_without_repeats_then_reset(self) -> None:
        rng = random.Random(1)
        picked = [Path(materials.pick(str(self.thumbs), str(self.titles), str(self.descs), rng=rng).thumbnail).name
                  for _ in range(3)]
        self.assertEqual(len(set(picked)), 3, "за круг — без повторов")
        self.assertTrue(all(n.startswith(materials.USED_MARK) for n in picked))
        # круг пройден: пометки сняты, новая обложка снова помечена, остальные — чистые
        materials.pick(str(self.thumbs), str(self.titles), str(self.descs), rng=rng)
        names = sorted(p.name for p in self.thumbs.iterdir())
        self.assertEqual(sum(n.startswith(materials.USED_MARK) for n in names), 1, names)

    def test_titles_and_descriptions_no_repeat_within_cycle(self) -> None:
        rng = random.Random(2)
        titles = [materials.pick(str(self.thumbs), str(self.titles), str(self.descs), rng=rng).title for _ in range(3)]
        self.assertEqual(sorted(titles), ["Второе", "Первое", "Третье"])

    def test_check_reports_missing_inputs(self) -> None:
        with self.assertRaisesRegex(ValueError, "Нет папки обложек"):
            materials.check(str(self.tmp / "нет такой"), str(self.titles), str(self.descs))
        with self.assertRaisesRegex(ValueError, "Файл названий"):
            materials.check(str(self.thumbs), "", str(self.descs))
        empty = self.tmp / "пусто.txt"
        empty.write_text("просто текст без номеров", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "нет пунктов"):
            materials.check(str(self.thumbs), str(empty), str(self.descs))


class NoCoverFolderTest(unittest.TestCase):
    """24.11: рассказы без обложки — папку обложек не указали, ролики выходят без неё, без предупреждений и доделок."""

    setUp = MaterialsTests.setUp

    def test_empty_folder_means_no_cover_by_design(self) -> None:
        materials.check("", str(self.titles), str(self.descs))  # запуск не падает
        m = materials.pick("", str(self.titles), str(self.descs), rng=random.Random(3))
        self.assertEqual(m.thumbnail, "")
        self.assertEqual(m.warnings, (), "без папки — не «⚠ Обложки нет»")
        self.assertTrue(m.title)

    def test_folder_given_but_empty_still_warns(self) -> None:
        empty = self.tmp / "пустая"
        empty.mkdir()
        m = materials.pick(str(empty), str(self.titles), str(self.descs), rng=random.Random(4))
        self.assertEqual(m.thumbnail, "")
        self.assertTrue(m.warnings and m.warnings[0].startswith(materials.COVER_MISSING))


class ScheduleTests(unittest.TestCase):
    NOW = datetime(2026, 11, 11, 12, 0, tzinfo=schedule.MSK)

    def setUp(self) -> None:
        fresh_store(self)

    def test_default_next_day_after_last_scheduled_on_studio(self) -> None:
        when = schedule.slot("UC1", studio_last=date(2026, 11, 14), now=self.NOW, rng=random.Random(0))
        self.assertEqual(when.date(), date(2026, 11, 15))
        nxt = schedule.slot("UC1", studio_last=date(2026, 11, 14), now=self.NOW, rng=random.Random(0))
        self.assertEqual(nxt.date(), date(2026, 11, 16), "дальше — каждый день подряд")

    def test_random_time_16_to_20_moscow_step_15(self) -> None:
        for k in range(50):
            t = schedule.slot(f"UC{k}", now=self.NOW, rng=random.Random(k))
            self.assertEqual(t.utcoffset(), timedelta(hours=3))
            self.assertTrue(16 <= t.hour < 20 and t.minute % 15 == 0, t)

    def test_own_time_and_one_per_day(self) -> None:
        a = schedule.slot("UC1", wanted=date(2026, 11, 20), at="18:30", now=self.NOW)
        b = schedule.slot("UC1", wanted=date(2026, 11, 20), at="18:30", now=self.NOW)
        self.assertEqual((a.date(), a.hour, a.minute), (date(2026, 11, 20), 18, 30))
        self.assertEqual(b.date(), date(2026, 11, 21), "на канал — не больше одного ролика в день")
        self.assertEqual(schedule.slot("UC2", wanted=date(2026, 11, 20), at="18:30", now=self.NOW).date(),
                         date(2026, 11, 20), "у другого канала свой день")

    def test_not_in_the_past(self) -> None:
        late = datetime(2026, 11, 11, 19, 30, tzinfo=schedule.MSK)
        self.assertEqual(schedule.slot("UC9", at="18:00", now=late).date(), date(2026, 11, 12))

    def test_plan_dates_in_order_then_daily(self) -> None:
        plan = schedule.create_plan(["2026-11-17", "2026-11-15"], "")
        got = [schedule.take_plan_date(plan)[0] for _ in range(4)]
        self.assertEqual(got, [date(2026, 11, 15), date(2026, 11, 17), date(2026, 11, 18), date(2026, 11, 19)])


class ScheduleStepTests(unittest.TestCase):
    """Шаг между роликами: 1 — каждый день, 2 — через день, 3 — через два дня."""

    NOW = datetime(2026, 11, 11, 12, 0, tzinfo=schedule.MSK)

    def setUp(self) -> None:
        fresh_store(self)

    def test_step_two_means_every_other_day(self) -> None:
        days = [schedule.slot("UC1", studio_last=date(2026, 11, 14), now=self.NOW, rng=random.Random(0), step=2).date()
                for _ in range(3)]
        self.assertEqual(days, [date(2026, 11, 16), date(2026, 11, 18), date(2026, 11, 20)])

    def test_step_three(self) -> None:
        a = schedule.slot("UC1", studio_last=date(2026, 11, 14), now=self.NOW, step=3).date()
        b = schedule.slot("UC1", studio_last=date(2026, 11, 14), now=self.NOW, step=3).date()
        self.assertEqual((a, b), (date(2026, 11, 17), date(2026, 11, 20)))

    def test_default_step_is_every_day(self) -> None:
        a = schedule.slot("UC1", studio_last=date(2026, 11, 14), now=self.NOW).date()
        b = schedule.slot("UC1", studio_last=date(2026, 11, 14), now=self.NOW).date()
        self.assertEqual((a, b), (date(2026, 11, 15), date(2026, 11, 16)))

    def test_first_video_on_empty_channel_goes_to_the_nearest_day(self) -> None:
        self.assertEqual(schedule.slot("UC9", at="18:00", now=self.NOW, step=3).date(), date(2026, 11, 11),
                         "шаг считается от последнего ролика; своего нет — ближайший день")

    def test_bad_step_values(self) -> None:
        self.assertEqual([schedule.clamp_step(x) for x in (0, -3, "x", None, 2, "4", 99)], [1, 1, 1, 1, 2, 4, 30])

    def test_plan_dates_continue_with_the_step(self) -> None:
        plan = schedule.create_plan(["2026-11-15", "2026-11-17"], "", step=2)
        got = [schedule.take_plan_date(plan)[0] for _ in range(5)]
        self.assertEqual(got, [date(2026, 11, 15), date(2026, 11, 17), date(2026, 11, 19), date(2026, 11, 21), date(2026, 11, 23)])
        self.assertEqual(schedule.plan_step(plan), 2)
        self.assertEqual(schedule.plan_step(""), 1)
        self.assertEqual(schedule.plan_step("нет такого"), 1)

    def test_old_plan_without_step_is_daily(self) -> None:
        plan = schedule.create_plan(["2026-11-15"], "")
        self.assertEqual([schedule.take_plan_date(plan)[0] for _ in range(3)],
                         [date(2026, 11, 15), date(2026, 11, 16), date(2026, 11, 17)])

    def test_config_keeps_step_in_range(self) -> None:
        from engine.core.config import _normalize

        self.assertEqual(_normalize({})["broll_publish_step"], 1)
        self.assertEqual(_normalize({"slides_publish_step": "3"})["slides_publish_step"], 3)
        self.assertEqual(_normalize({"broll_publish_step": 0})["broll_publish_step"], 1)
        self.assertEqual(_normalize({"broll_publish_step": 500})["broll_publish_step"], 30)

    def test_run_passes_step_to_slot(self) -> None:
        import inspect

        from engine.publish import run

        src = inspect.getsource(run)
        self.assertIn("plan_step", src)
        self.assertIn("step=int(state.get(\"plan_step\") or 1)", src)


class StudioParseTests(unittest.TestCase):
    def test_private_and_unlisted_do_not_set_schedule(self) -> None:
        self.assertTrue(youtube.counts_for_schedule("… — Public 10 Nov 2026 Published 32 1"))
        self.assertTrue(youtube.counts_for_schedule("… — Scheduled 20 Nov 2026"))
        self.assertTrue(youtube.counts_for_schedule("… — Для всех 13 сент. 2026 г. Опубликовано"))
        self.assertFalse(youtube.counts_for_schedule("ТЕСТ 3 — удалить — Private 11 Nov 2026 Uploaded 0 0"))
        self.assertFalse(youtube.counts_for_schedule("… — Unlisted 5 Nov 2026"))
        self.assertFalse(youtube.counts_for_schedule("… — Ограниченный доступ 11 нояб. 2026 г."))

    def test_dates_from_studio_rows_any_language(self) -> None:
        en = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sept", "Oct", "Nov", "Dec"]
        ru = ["янв.", "февр.", "март", "апр.", "май", "июнь", "июль", "авг.", "сент.", "окт.", "нояб.", "дек."]
        self.assertEqual(youtube.find_dates("… — Public 10 Nov 2026 Published 32 1", en), [date(2026, 11, 10)])
        self.assertEqual(youtube.find_dates("Scheduled Nov 9, 2026", en), [date(2026, 11, 9)])
        self.assertEqual(youtube.find_dates("— Для всех 13 сент. 2026 г. Опубликовано 17 261", ru), [date(2026, 9, 13)])
        self.assertEqual(youtube.find_dates("Черновик Редактировать черновик", ru), [])


class CommentTests(unittest.TestCase):
    TEXT = "Подписывайтесь на канал, чтобы не пропустить новые выпуски, а также включайте колокольчик уведомлений 🔔"

    def setUp(self) -> None:
        fresh_store(self)
        self.ch = registry.Channel("UCx", "Канал", handle="@канал")
        self.calls: list[str] = []
        from contextlib import contextmanager

        @contextmanager
        def page(cdp):
            yield mock.MagicMock()

        for p in (mock.patch.object(youtube, "studio_page", page), mock.patch.object(youtube, "switch_to", lambda *a, **k: None),
                  mock.patch.object(youtube, "COMMENT_GAP", (0.0, 0.0))):
            p.start()
            self.addCleanup(p.stop)

    def run_backfill(self, videos, states):
        def comment_on(page, vid, text, cancel, handle=""):
            self.calls.append(vid)
            return states.get(vid, "posted")

        with mock.patch.object(youtube, "_public_videos", lambda page, cid: videos), \
             mock.patch.object(youtube, "_comment_on", comment_on):
            return youtube.backfill_comments("ws://x", self.ch, self.TEXT)

    def test_goes_back_until_comment_already_there(self) -> None:
        notes = self.run_backfill(["v5", "v4", "v3", "v2"], {"v3": "exists"})
        self.assertEqual(self.calls, ["v5", "v4", "v3"], "пропущенные — да, дальше уже прокомментированного — нет")
        self.assertIn("2", notes[0])

    def test_next_time_only_new_video(self) -> None:
        self.run_backfill(["v2", "v1"], {"v1": "exists"})
        self.calls.clear()
        self.run_backfill(["v3", "v2", "v1"], {})
        self.assertEqual(self.calls, ["v3"], "обычный режим — один, только что вышедший ролик")

    def test_unpinnable_channel_still_counts_comment(self) -> None:
        notes = self.run_backfill(["v2", "v1"], {"v2": "posted_unpinned", "v1": "exists"})
        self.assertIn("закрепление недоступно: 1", notes[0])

    def test_hidden_comment_not_marked_done_and_retried(self) -> None:
        notes = self.run_backfill(["v3", "v2", "v1"], {"v2": "hidden"})
        self.assertEqual(self.calls, ["v3", "v2"], "YouTube убрал комментарий — дальше на канале не идём")
        self.assertTrue(any("убрал" in n for n in notes))
        self.calls.clear()
        self.run_backfill(["v3", "v2", "v1"], {"v3": "exists"})
        self.assertEqual(self.calls, [], "v3 сделан — дальше не смотрим")
        self.calls.clear()
        self.run_backfill(["v4", "v2", "v1"], {})
        self.assertEqual(self.calls, ["v4", "v2", "v1"], "в следующий раз v2 пробуем снова")

    def test_comment_on_reports_hidden_after_reload(self) -> None:
        page = mock.MagicMock()
        with mock.patch.object(youtube, "_act", lambda c: None), \
             mock.patch("engine.publish.studio_kit.el", lambda *a, **k: mock.MagicMock()), \
             mock.patch.object(youtube, "_pin", lambda *a: False), \
             mock.patch.object(youtube, "_has_ours", lambda *a: False), \
             mock.patch.object(youtube, "_still_there", lambda *a: False):
            self.assertEqual(youtube._comment_on(page, "v1", self.TEXT, None, "@канал"), "hidden")
        with mock.patch.object(youtube, "_act", lambda c: None), \
             mock.patch("engine.publish.studio_kit.el", lambda *a, **k: mock.MagicMock()), \
             mock.patch.object(youtube, "_pin", lambda *a: False), \
             mock.patch.object(youtube, "_has_ours", lambda *a: False), \
             mock.patch.object(youtube, "_still_there", lambda *a: True):
            self.assertEqual(youtube._comment_on(page, "v1", self.TEXT, None, "@канал"), "posted_unpinned")

    def test_pin_skipped_when_menu_has_no_pin(self) -> None:
        page, thread = mock.MagicMock(), mock.MagicMock()
        page.locator.return_value.all_inner_texts.return_value = ["Edit", "Delete"]
        with mock.patch.object(youtube, "_act", lambda c: None):
            self.assertFalse(youtube._pin(page, thread, "v1", None))
        page.locator.return_value.nth.assert_not_called()

    def test_emoji_ignored_when_matching(self) -> None:
        shown = "Подписывайтесь на канал, чтобы не пропустить новые выпуски, а также включайте колокольчик уведомлений"
        self.assertEqual(youtube._comment_key(shown), youtube._comment_key(self.TEXT))


class UploadWaitTests(unittest.TestCase):
    def test_finished_only_on_full_upload(self) -> None:
        self.assertTrue(youtube._upload_finished("Upload complete ... Processing will begin shortly"))
        self.assertTrue(youtube._upload_finished("Checks complete. No issues found."))
        self.assertTrue(youtube._upload_finished("Uploading 100%"))
        self.assertFalse(youtube._upload_finished("Uploading 99% ... 2 minutes left"))
        self.assertFalse(youtube._upload_finished(""))

    def test_slow_upload_is_flagged_but_never_aborted(self) -> None:
        texts = ["Uploading 40%"] * 6 + ["Upload complete"]
        clock = [0.0]

        class Status:
            def inner_text(self, timeout=0):
                return texts.pop(0)

        class Page:
            def locator(self, sel):
                return mock.Mock(first=Status(), inner_text=lambda timeout=0: "")

            def wait_for_timeout(self, ms):
                clock[0] += 10 * 60  # каждая проверка — «прошло 10 минут»

        seen = []
        with mock.patch.object(youtube.time, "monotonic", lambda: clock[0]):
            youtube._wait_uploaded(Page(), lambda p, m: seen.append(m), None)
        self.assertTrue(any("долго грузится" in m for m in seen), seen)


class Fixture(unittest.TestCase):
    """Два профиля с каналами, материалы и папка ролика с копиями."""

    def setUp(self) -> None:
        fresh_store(self)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        registry.sync([Profile("p1", "1", "Истории"), Profile("p2", "2", "Драмы")])
        registry.set_channels("p1", [registry.Channel("UCa", "Канал А", playlists=["Для вас"]),
                                     registry.Channel("UCb", "Канал Б", playlists=["Для вас"])])
        registry.set_channels("p2", [registry.Channel("UCc", "Канал В", playlists=["Для вас"]),
                                     registry.Channel("UCa", "Канал А", playlists=["Для вас"])])
        thumbs = self.tmp / "обложки"
        thumbs.mkdir()
        (thumbs / "1.jpg").write_bytes(b"x")
        (self.tmp / "t.txt").write_text("1. Название", encoding="utf-8")
        (self.tmp / "d.txt").write_text("1. Описание", encoding="utf-8")
        self.mat = (str(thumbs), str(self.tmp / "t.txt"), str(self.tmp / "d.txt"))
        self.folder = self.tmp / "ролик"
        self.folder.mkdir()
        self.files = []
        for pct in (100, 99, 101):
            f = self.folder / f"{pct}.mp4"
            f.write_bytes(b"v")
            self.files.append((str(f), pct))

    def request(self, channels: list[str], **kw) -> pub.PublishRequest:
        return pub.PublishRequest(folder=self.folder, files=self.files[: len(channels)], channels=channels,
                                  thumbs_dir=self.mat[0], titles_file=self.mat[1], descs_file=self.mat[2],
                                  adspower=("KEY", "http://ads.test"), **kw)


class RegistryTests(Fixture):
    def test_same_channel_in_two_profiles_counts_once(self) -> None:
        targets = registry.resolve(["p1/UCa", "p2/UCa", "p2/UCc"])
        self.assertEqual([t.channel.id for t in targets], ["UCa", "UCc"])
        self.assertEqual(pub.copies_for(["p1/UCa", "p1/UCb", "p2/UCc"]), 2, "3 канала → основной + 2 копии")

    def test_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "ни одного канала"):
            pub.validate([], [], *self.mat)
        with self.assertRaisesRegex(ValueError, "и соавтор"):
            pub.validate(["p1/UCa"], ["p2/UCa"], *self.mat)
        with self.assertRaisesRegex(ValueError, "нет канала"):
            pub.validate(["p1/UCz"], [], *self.mat)
        pub.validate(["p1/UCa", "p1/UCb"], ["p2/UCc"], *self.mat)

    def test_sync_keeps_channels_and_marks_missing(self) -> None:
        profiles = registry.sync([Profile("p1", "1", "Истории (новое имя)")])
        self.assertEqual(profiles["p1"].name, "Истории (новое имя)")
        self.assertEqual(len(profiles["p1"].channels), 2, "найденные каналы не теряются")
        self.assertTrue(profiles["p2"].missing)


class FakeAds:
    # управление из тестов: отвечает ли AdsPower и открыт ли браузер профиля
    up = True
    browser_open = True

    def __init__(self, *a, **kw) -> None:
        self.started: list[str] = []

    def alive(self) -> bool:
        return FakeAds.up

    def is_open(self, pid) -> bool:
        return FakeAds.browser_open

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def start(self, pid, headless=False):
        self.started.append(pid)
        return Session(pid, f"ws://{pid}")

    def stop(self, pid) -> None:
        pass


class RunTests(Fixture):
    def run_job(self, req, state, upload):
        def save(**values):
            state.update(values)

        with mock.patch.object(pub, "AdsPower", FakeAds), mock.patch.object(youtube, "READY", True), \
             mock.patch.object(youtube, "upload", upload):
            return pub.run_publish(req, state, save)

    def test_each_channel_gets_different_copy_and_same_materials(self) -> None:
        seen = []

        def upload(cdp, channel, job, cancel=None, progress=None, **kw):
            seen.append((channel.id, job.file.name, job.title, job.publish_at.utcoffset()))
            return youtube.Uploaded(f"https://youtu.be/{channel.id}", [], None, True)

        state: dict = {}
        res = self.run_job(self.request(["p1/UCa", "p1/UCb", "p2/UCc"]), state, upload)
        self.assertEqual([r["status"] for r in state["live"]], ["done"] * 3, "живой статус по каналам")
        self.assertTrue(all(r["when"].endswith("МСК") for r in state["live"]))
        self.assertEqual(len({f for _c, f, _t, _o in seen}), 3, "одинаковый файл дважды не уходит")
        self.assertEqual({t for _c, _f, t, _o in seen}, {"Название"})
        self.assertEqual({o for *_x, o in seen}, {timedelta(hours=3)})
        self.assertEqual(sum(line.startswith("✓") for line in res["lines"]), 3)

    def test_files_not_deleted_when_studio_did_not_prove_the_file(self) -> None:
        """14.11: ролик ушёл в расписание пустым, а файл с компьютера удалили — единственную копию."""
        def upload(cdp, channel, job, cancel=None, progress=None, **kw):
            return youtube.Uploaded("u", [], None, channel.id != "UCb")  # на UCb файл не доказан

        state: dict = {}
        req = self.request(["p1/UCa", "p1/UCb"], keep_files=False)
        res = self.run_job(req, state, upload)
        files = [Path(f) for f, _pct in self.files[:2]]
        self.assertTrue(files and all(f.exists() for f in files), "ничего не удалено")
        self.assertTrue(any("НЕ удалён" in w for w in res["warnings"]), res["warnings"])

    def test_failure_marks_exact_channel_and_retry_only_it(self) -> None:
        calls = []

        def upload(cdp, channel, job, cancel=None, progress=None, **kw):
            calls.append(channel.id)
            if channel.id == "UCb" and calls.count("UCb") == 1:
                raise youtube.StudioError("не нажалась кнопка «Далее»")
            return youtube.Uploaded("u", [], None, True)

        state: dict = {}
        req = self.request(["p1/UCa", "p1/UCb", "p2/UCc"], keep_files=False)
        with self.assertRaises(RuntimeError) as err:
            self.run_job(req, state, upload)
        self.assertIn("✗ Канал Б (№1 Истории): не нажалась", str(err.exception))
        self.assertTrue(self.folder.is_dir(), "не все каналы — файлы не удаляем")
        thumb, when_b = state["materials"]["thumbnail"], state["channels"]["p1/UCb"]["publish_at"]
        self.run_job(req, state, upload)  # «Продолжить»
        self.assertEqual(calls, ["UCa", "UCb", "UCc", "UCb"], "повтор — только упавший канал")
        self.assertEqual(state["materials"]["thumbnail"], thumb, "обложка та же")
        self.assertEqual(state["channels"]["p1/UCb"]["publish_at"], when_b, "дата та же")
        self.assertTrue(self.folder.is_dir(), "папка остаётся")
        self.assertFalse(list(self.folder.glob("*.mp4")), "все каналы готовы, «не оставлять» — видео удалено")

    def test_remove_keeps_text_voice_chapters_and_names_them(self) -> None:
        folder = self.tmp / "Конец терпению_20261107-111324"
        folder.mkdir()
        for name in ("Конец терпению.mp4", "99 процентов Конец терпению.mp4", "Конец терпению.mp3",
                     "Конец терпению.txt", "главы.txt", "99 процентов главы.txt", "описание.txt", ".voicegen_source.json",
                     "чужое.mp4"):
            (folder / name).write_bytes(b"x")
        line = pub.remove_videos(folder, [folder / "Конец терпению.mp4", folder / "99 процентов Конец терпению.mp4"])
        left = sorted(p.name for p in folder.iterdir())
        self.assertEqual(left, sorted([
            ".voicegen_source.json", "Конец терпению 07.11.2026 — 99 процентов главы.txt",
            "Конец терпению 07.11.2026 — главы.txt", "Конец терпению 07.11.2026 — описание.txt",
            "Конец терпению.mp3", "Конец терпению.txt", "чужое.mp4"]), "удалено только выложенное видео")
        self.assertIn("Видео удалено", line)

    def test_slides_folder_names(self) -> None:
        folder = self.tmp / "Архимед 30.09"
        folder.mkdir()
        for name in ("ролик.mp4", "озвучка.mp3", "сценарий.txt"):
            (folder / name).write_bytes(b"x")
        (folder / "обложки").mkdir()
        pub.remove_videos(folder, [folder / "ролик.mp4"])
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ["Архимед 30.09 — озвучка.mp3", "Архимед 30.09 — сценарий.txt", "обложки"])

    def test_run_copy_removed_after_publish(self) -> None:
        runs = self.tmp / "runs"
        run = runs / "20261117_091529_куба"
        (run / "pool").mkdir(parents=True)
        (run / "pool" / "beat_0001_pexels.mp4").write_bytes(b"x" * 10)
        (run / ".dub" / "en").mkdir(parents=True)
        for name in ("final.mp4", ".dub/en/final.mp4", "voice.mp3", "script.txt", "timeline.json"):
            (run / name).write_bytes(b"y")
        outside = self.tmp / "чужая" / "final.mp4"
        outside.parent.mkdir()
        outside.write_bytes(b"z")
        with mock.patch("engine.slides.paths.RUNS_DIR", runs):
            line = pub.remove_run_copy(run)
            self.assertEqual(pub.remove_run_copy(outside.parent), "", "не папка запуска — не трогаем")
            self.assertEqual(pub.remove_run_copy(runs), "", "саму папку запусков не трогаем")
        self.assertIn("Рабочая копия", line)
        self.assertFalse((run / "final.mp4").exists())
        self.assertFalse((run / ".dub" / "en" / "final.mp4").exists())
        self.assertFalse((run / "pool").exists(), "старые стоки — тоже")
        for name in ("voice.mp3", "script.txt", "timeline.json"):
            self.assertTrue((run / name).exists(), f"{name} остаётся")
        self.assertTrue(outside.exists())

    def test_failures_retry_by_themselves_with_growing_pause(self) -> None:
        def upload(cdp, channel, job, cancel=None, progress=None, **kw):
            exc = youtube.StudioError("Studio не открылась: обрыв сети")
            exc.draft_url = "https://youtu.be/half"
            raise exc

        state: dict = {}
        req = self.request(["p1/UCa"])
        waits = []
        for _ in range(4):
            with self.assertRaises(pub.PublishLimit) as err:
                self.run_job(req, state, upload)
            waits.append(round((err.exception.retry_at - datetime.now().timestamp()) / 60))
        self.assertEqual(waits, [10, 30, 60, 60], "без предела попыток: дальше раз в час")
        self.assertIn("youtu.be/half", str(err.exception), "недогруженный черновик — в итогах")

    def test_profile_crash_restarts_and_redoes_channel(self) -> None:
        calls = []

        class TargetClosedError(Exception):
            pass

        def upload(cdp, channel, job, cancel=None, progress=None, **kw):
            calls.append(cdp)
            if len(calls) == 1:
                raise TargetClosedError("Target page, context or browser has been closed")
            return youtube.Uploaded("https://youtu.be/ok", [], None, True)

        with mock.patch.object(pub.time, "sleep", lambda s: None):
            res = self.run_job(self.request(["p1/UCa"]), {}, upload)
        self.assertEqual(len(calls), 2, "после падения профиля — тот же канал ещё раз")
        self.assertTrue(any("лишний черновик" in line for line in res["lines"]))

    def _reset_ads(self) -> None:
        FakeAds.up, FakeAds.browser_open = True, True
        self.addCleanup(lambda: (setattr(FakeAds, "up", True), setattr(FakeAds, "browser_open", True)))

    def test_adspower_down_at_start_waits_without_spending_attempts(self) -> None:
        self._reset_ads()
        FakeAds.up = False
        calls = []
        state: dict = {}
        with self.assertRaises(pub.PublishLimit) as ctx:
            self.run_job(self.request(["p1/UCa"]), state, lambda *a, **kw: calls.append(1))
        self.assertEqual(calls, [], "AdsPower нет — в Studio не идём")
        self.assertNotIn("retries", state, "попытка не потрачена")
        self.assertLess(ctx.exception.retry_at - pub.time.time(), pub.ADSPOWER_POLL_SEC + 5)
        self.assertIn("AdsPower не отвечает", str(ctx.exception))

    def test_adspower_dies_mid_upload_then_continues_same_draft(self) -> None:
        """17.11: AdsPower завис посреди загрузки и его перезапустили — шаг «не для детей» упал. Канал не провален:
        ждём AdsPower и продолжаем тот же черновик."""
        self._reset_ads()
        seen = []

        def upload(cdp, channel, job, cancel=None, progress=None, draft_url="", on_draft=None):
            seen.append(draft_url)
            if not draft_url:
                on_draft("https://youtu.be/draft1")
                FakeAds.up = False  # в этот момент AdsPower закрыли
                raise youtube.StudioError("шаг «не для детей»: нет элемента «not_for_kids»")
            return youtube.Uploaded(draft_url, [], None, True)

        state: dict = {}
        req = self.request(["p1/UCa"])
        with self.assertRaises(pub.PublishLimit):
            self.run_job(req, state, upload)
        rec = state["channels"]["p1/UCa"]
        self.assertEqual(rec["status"], pub.WAITING, "не ошибка канала, а ожидание AdsPower")
        self.assertEqual(rec["draft_url"], "https://youtu.be/draft1")
        self.assertNotIn("retries", state)
        FakeAds.up = True  # AdsPower снова открыт
        res = self.run_job(req, state, upload)
        self.assertEqual(seen, ["", "https://youtu.be/draft1"], "тот же черновик, без дубля")
        self.assertTrue(any(line.startswith("✓ Канал") for line in res["lines"]), res["lines"])

    def test_step_error_with_closed_browser_restarts_profile(self) -> None:
        """Браузер профиля закрылся, а упал «шаг» — это пропажа браузера: перезапуск, а не ошибка канала."""
        self._reset_ads()
        calls = []

        def upload(cdp, channel, job, cancel=None, progress=None, **kw):
            calls.append(1)
            if len(calls) == 1:
                FakeAds.browser_open = False
                raise youtube.StudioError("шаг «название»: нет элемента")
            FakeAds.browser_open = True
            return youtube.Uploaded("https://youtu.be/ok", [], None, True)

        with mock.patch.object(pub.time, "sleep", lambda s: None):
            res = self.run_job(self.request(["p1/UCa"]), {}, upload)
        self.assertEqual(len(calls), 2)
        self.assertTrue(any(line.startswith("✓ Канал") for line in res["lines"]), res["lines"])

    def test_real_step_error_with_live_browser_is_still_an_error(self) -> None:
        self._reset_ads()
        calls = []

        def upload(cdp, channel, job, cancel=None, progress=None, **kw):
            calls.append(1)
            raise youtube.StudioError("шаг «название»: нет элемента")

        state: dict = {}
        with self.assertRaises(pub.PublishLimit):
            self.run_job(self.request(["p1/UCa"]), state, upload)
        self.assertEqual(len(calls), 1, "браузер жив — профиль не перезапускаем")
        self.assertEqual(state["channels"]["p1/UCa"]["status"], pub.FAILED)
        self.assertEqual(state["retries"], 1, "обычная ошибка — обычный счёт повторов")

    def test_retry_continues_previous_video_not_duplicate(self) -> None:
        seen = []

        def upload(cdp, channel, job, cancel=None, progress=None, draft_url="", on_draft=None):
            seen.append(draft_url)
            if not draft_url:
                on_draft("https://youtu.be/first")  # Studio выдала ссылку — и тут всё упало
                raise youtube.StudioError("обрыв посреди загрузки")
            return youtube.Uploaded(draft_url, ["продолжил черновик прошлой попытки"], None, True)

        state: dict = {}
        req = self.request(["p1/UCa"])
        with self.assertRaises(pub.PublishLimit):
            self.run_job(req, state, upload)
        self.assertEqual(state["channels"]["p1/UCa"]["draft_url"], "https://youtu.be/first")
        res = self.run_job(req, state, upload)  # «Продолжить»
        self.assertEqual(seen, ["", "https://youtu.be/first"], "повтор — то же видео, без дубля")
        self.assertTrue(any("youtu.be/first" in line or "продолжил" in line for line in res["lines"]))

    def test_needs_user_waits_for_user(self) -> None:
        def upload(cdp, channel, job, cancel=None, progress=None, **kw):
            raise youtube.NeedsUser("в профиле не выполнен вход в Google")

        with self.assertRaises(RuntimeError) as err:
            self.run_job(self.request(["p1/UCa"]), {}, upload)
        self.assertNotIsInstance(err.exception, pub.PublishLimit)
        self.assertIn("ждёт тебя", str(err.exception))

    def test_passed_date_moves_to_next_free_day(self) -> None:
        got = []

        def upload(cdp, channel, job, cancel=None, progress=None, **kw):
            got.append(job.publish_at)
            return youtube.Uploaded("u", [], None, True)

        past = (datetime.now(schedule.MSK) - timedelta(days=2)).replace(microsecond=0)
        state = {"channels": {"p1/UCa": {"status": "failed", "publish_at": past.isoformat()}}}
        res = self.run_job(self.request(["p1/UCa"]), state, upload)
        self.assertGreater(got[0], datetime.now(schedule.MSK))
        self.assertTrue(any("прошла во время повторов" in line for line in res["lines"]))

    def test_limit_waits_until_reset(self) -> None:
        until = datetime.now().timestamp() + 3600

        def upload(cdp, channel, job, cancel=None, progress=None, **kw):
            raise youtube.UploadLimit("лимит загрузок на сегодня", until)

        with self.assertRaises(pub.PublishLimit) as err:
            self.run_job(self.request(["p1/UCa"]), {}, upload)
        self.assertEqual(err.exception.retry_at, until)

    def test_not_ready_does_not_open_profiles(self) -> None:
        ads = FakeAds()
        state: dict = {}
        with mock.patch.object(pub, "AdsPower", lambda *a, **kw: ads), mock.patch.object(youtube, "READY", False), \
             self.assertRaises(RuntimeError) as err:
            pub.run_publish(self.request(["p1/UCa"]), state, lambda **kw: state.update(kw))
        self.assertIn("следующем этапе", str(err.exception))
        self.assertEqual(ads.started, [])
        self.assertNotIn("materials", state, "обложка не потрачена")
        self.assertFalse(any(p.name.startswith(materials.USED_MARK) for p in Path(self.mat[0]).iterdir()))


class BrollWiringTests(Fixture):
    def test_publish_overrides_copies_setting(self) -> None:
        from server import runners

        broll = self.tmp / "футажи"
        broll.mkdir()
        (broll / "f.mp4").write_bytes(b"")
        cfg = {"broll_dir": str(broll), "speed_copies": True, "speed_copies_count": 5, "adspower_api_key": "K",
               "broll_publish": True, "broll_publish_channels": ["p1/UCa", "p2/UCc"],
               "broll_publish_thumbs": self.mat[0], "broll_publish_titles": self.mat[1], "broll_publish_descs": self.mat[2]}
        got = {}

        class Req:
            def __init__(self, **kw):
                got.update(kw)

        with mock.patch.object(runners, "_need_tts", lambda c, a: None), mock.patch.object(runners, "BrollRequest", Req):
            try:
                runners._broll(cfg, {"text": "Рассказ"}, {})
            except Exception:
                pass
        self.assertEqual(got.get("speed_copies"), 1, "2 канала → 1 копия, а не 5 из настроек")
        with mock.patch.object(runners, "_need_tts", lambda c, a: None), self.assertRaisesRegex(ValueError, "ни одного канала"):
            runners._broll({**cfg, "broll_publish_channels": []}, {"text": "Рассказ"}, {})


if __name__ == "__main__":
    unittest.main()


class CoauthorsToggleTest(unittest.TestCase):
    def test_toggle(self) -> None:
        from server.runners import _coauthors

        base = {"broll_publish_coauthors": ["p/A", "p/B"], "broll_publish_channels": ["p/B", "p/C"]}
        self.assertEqual(_coauthors(base, "broll_publish"), ["p/A"], "старые настройки: выбраны — включено; цель — не соавтор")
        self.assertEqual(_coauthors({**base, "broll_publish_coauthors_on": False}, "broll_publish"), [])
        self.assertEqual(_coauthors({**base, "broll_publish_coauthors_on": True}, "broll_publish"), ["p/A"])
        self.assertEqual(_coauthors({"broll_publish_coauthors": []}, "broll_publish"), [])


class ClockTest(unittest.TestCase):
    def test_12_and_24_hour_formats(self) -> None:
        from engine.publish.youtube import clock_of

        self.assertEqual(clock_of("2:00 PM"), (14, 0), "профиль с локалью США (13.11)")
        self.assertEqual(clock_of("14:00"), (14, 0))
        self.assertEqual(clock_of("12:30 AM"), (0, 30))
        self.assertEqual(clock_of("12:15 PM"), (12, 15))
        self.assertEqual(clock_of("4:45 pm"), (16, 45))
        self.assertIsNone(clock_of(""))


class StudioEnvTest(unittest.TestCase):
    def test_only_language_is_a_warning(self) -> None:
        from engine.publish.youtube import env_warnings

        self.assertEqual(env_warnings({"lang": "en-US", "hour12": True, "platform": "Windows"}), [],
                         "Windows-отпечаток и 12 ч программа понимает")
        self.assertEqual(len(env_warnings({"lang": "ru", "hour12": False, "platform": "Mac"})), 1)
        self.assertEqual(env_warnings({}), [])

    def test_saved_with_channels(self) -> None:
        from engine.publish import registry

        registry.save({"p": registry.ProfileEntry("p", "1", "Профиль")})
        registry.set_channels("p", [], studio={"lang": "en-US", "hour12": True, "platform": "Windows"})
        self.assertEqual(registry.load()["p"].studio["platform"], "Windows")
        registry.set_channels("p", [])
        self.assertEqual(registry.load()["p"].studio["lang"], "en-US", "без новых данных — прежние остаются")


class CoauthorFailTest(unittest.TestCase):
    """Соавтор не присоединился — итог выкладки поднимает предупреждение со ссылкой и что сделать руками."""

    def _co(self):
        ch = registry.Channel(id="UCco", name="Исповедь Сердца", handle="@ИсповедьСердца")
        return registry.Target(profile_id="p2", profile="№2 Исповедь", channel=ch)

    def test_warning_and_cleared_on_success(self):
        target = registry.Target(profile_id="p8", profile="№8 Сон",
                                 channel=registry.Channel(id="UCt", name="Истории Для Сна", handle="@Сон"))
        co = self._co()
        rec = {"status": pub.DONE, "url": "https://youtu.be/x", "title": "Две судьбы",
               "invites": {co.key: "https://studio.youtube.com/channel/UCco/collaboration/UCt"}}
        pub._coauthor_failed(rec, co, "Google просит подтвердить вход («Verify it's you»)")
        w = pub.coauthor_warnings([target], {target.key: rec})
        self.assertEqual(len(w), 1)
        self.assertIn("Исповедь Сердца", w[0])
        self.assertIn("НЕ присоединился", w[0])
        self.assertIn("collaboration/UCt", w[0])
        self.assertIn("Verify", w[0])
        self.assertIn("№2 Исповедь", w[0])
        pub._coauthor_failed(rec, co, "ещё раз")  # повтор не дублирует
        self.assertEqual(len(rec["coauthors_failed"]), 1)
        rec["coauthors_ok"] = [co.key]
        self.assertEqual(pub.coauthor_warnings([target], {target.key: rec}), [])

    def test_no_link_tells_how_to_invite(self):
        target = registry.Target(profile_id="p8", profile="№8", channel=registry.Channel(id="UCt", name="Сон"))
        co = self._co()
        rec = {"status": pub.DONE, "invites": {}}
        pub._coauthor_failed(rec, co, "соавторы не приглашены: нет кнопки")
        self.assertIn("пригласи соавтора", pub.coauthor_warnings([target], {target.key: rec})[0])

    def test_verify_gate(self):
        class Loc:
            def __init__(self, texts):
                self.texts = texts

            def all_inner_texts(self):
                return self.texts

        class Page:
            def __init__(self, texts):
                self.texts = texts

            def locator(self, _sel):
                return Loc(self.texts)

        youtube._verify_gate(Page(["You're invited to collaborate", ""]))
        with self.assertRaises(youtube.NeedsUser):
            youtube._verify_gate(Page(["Verify it’s you To continue, we need to confirm it’s really you."]))


class AcceptAllInvitesTest(unittest.TestCase):
    def test_title_in(self):
        acc = ["🌙 ЗАСЫПАЙТЕ ПОД САМУЮ ДОБРУЮ ИСТОРИЮ! Душевный жизненный рассказ на ночь"]
        self.assertTrue(pub._title_in("🌙 ЗАСЫПАЙТЕ ПОД САМУЮ ДОБРУЮ ИСТОРИЮ!  Душевный жизненный рассказ на ночь", acc))
        self.assertFalse(pub._title_in("ПОГРУЗИТЕСЬ В МИР СПОКОЙНОГО СНА!", acc))
        self.assertFalse(pub._title_in("", acc))

    def test_every_owner_pair_is_accepted_even_with_the_same_title(self):
        """Два канала-владельца одного человека, один соавтор, ОДИНАКОВОЕ название ролика: ссылка у каждой пары своя,
        принимать надо обе (раньше вторую пропускали «по названию» — она оставалась в «Pending acceptance»)."""
        calls = []

        def fake_accept(cdp, ch, link, *, title="", cancel=None):
            calls.append(link)
            return ["Один и тот же ролик с длинным названием"]

        co = registry.Target(profile_id="pc", profile="№3", channel=registry.Channel(id="UCco", name="Соавтор"))
        t1 = registry.Target(profile_id="p1", profile="№8", channel=registry.Channel(id="UC1", name="А"))
        t2 = registry.Target(profile_id="p1", profile="№8", channel=registry.Channel(id="UC2", name="Б"))
        results = {t1.key: {"status": pub.DONE, "title": "Один и тот же ролик с длинным названием", "invites": {co.key: "L1"}},
                   t2.key: {"status": pub.DONE, "title": "Один и тот же ролик с длинным названием", "invites": {co.key: "L2"}}}
        req = mock.Mock(adspower=("k", "u"), visible=False)
        ap = mock.MagicMock()
        ap.__enter__.return_value = ap
        with mock.patch.object(pub, "AdsPower", return_value=ap), \
                mock.patch.object(youtube, "accept_coauthor", side_effect=fake_accept):
            pub._accept_coauthors(req, [t1, t2], [co], results, lambda **_: None, None)
        self.assertEqual(calls, ["L1", "L2"], "каждая ссылка-приглашение открыта")
        for t in (t1, t2):
            self.assertIn(co.key, results[t.key]["coauthors_ok"])
        self.assertEqual(pub.coauthor_warnings([t1, t2], results), [])


class AcceptEdgeCasesTest(unittest.TestCase):
    def _run(self, fake):
        co = registry.Target(profile_id="pc", profile="№3", channel=registry.Channel(id="UCco", name="Соавтор"))
        ts = [registry.Target(profile_id="p1", profile="№8", channel=registry.Channel(id=f"UC{i}", name=f"К{i}"))
              for i in (1, 2)]
        results = {t.key: {"status": pub.DONE, "title": f"Длинное название ролика номер {i}", "invites": {co.key: "L"}}
                   for i, t in zip((1, 2), ts)}
        ap = mock.MagicMock()
        ap.__enter__.return_value = ap
        with mock.patch.object(pub, "AdsPower", return_value=ap), \
                mock.patch.object(youtube, "accept_coauthor", side_effect=fake) as acc:
            pub._accept_coauthors(mock.Mock(adspower=("k", "u"), visible=False), ts, [co], results,
                                  lambda **_: None, None)
        return ts, results, acc

    def test_verify_stops_and_flags_all(self):
        def fake(*a, **k):
            raise youtube.NeedsUser("Google просит подтвердить вход («Verify it's you»)")
        ts, results, acc = self._run(fake)
        self.assertEqual(acc.call_count, 2, "скрытое окно, потом обычное; пароль просят и там — второй ролик не мучаем")
        self.assertEqual(len(pub.coauthor_warnings(ts, results)), 2)

    def test_verify_in_hidden_window_retried_in_visible(self):
        calls = []

        def fake(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise youtube.NeedsUser("Google просит подтвердить вход («Verify it's you»)")
            return ["Длинное название ролика номер 1", "Длинное название ролика номер 2"]

        ts, results, acc = self._run(fake)
        self.assertEqual(pub.coauthor_warnings(ts, results), [], "в обычном окне приняли — без пометок")

    def test_other_video_accepted_is_not_success(self):
        ts, results, acc = self._run(lambda *a, **k: ["Совсем другой ролик с длинным названием"])
        w = pub.coauthor_warnings(ts, results)
        self.assertEqual(len(w), 2)
        self.assertIn("этого ролика среди ждущих нет", w[0])


class StudioTimeTest(unittest.TestCase):
    def test_dst_crossing(self):
        from datetime import timezone as tz_
        msk = schedule.MSK
        # сегодня в Берлине зима (GMT+1), выход — после перехода на летнее (GMT+2)
        now = datetime(2027, 3, 20, 12, 0, tzinfo=tz_.utc)
        at = datetime(2027, 3, 30, 17, 0, tzinfo=msk)
        got = youtube.studio_time(at, (1, 0), "Europe/Berlin", now=now)
        self.assertEqual((got.hour, got.minute), (16, 0))
        # пояс браузера не сходится с подсказкой Studio — верим подсказке
        got = youtube.studio_time(at, (3, 0), "Europe/Berlin", now=now)
        self.assertEqual((got.hour, got.minute), (17, 0))
        self.assertEqual(youtube.studio_time(at, (1, 0), "", now=now).hour, 15)
        self.assertEqual(youtube.studio_time(at, (1, 0), "Нет/Такого", now=now).hour, 15)


class ScheduledPendingTest(unittest.TestCase):
    class _Loc:
        def __init__(self, page):
            self.page = page

        @property
        def first(self):
            return self

        def inner_text(self, **_):
            return self.page.shown.pop(0) if len(self.page.shown) > 1 else self.page.shown[0]

        def wait_for(self, **_):
            return None

    class _Page:
        def __init__(self, shown):
            self.shown = list(shown)
            self.visits = 0

        def locator(self, _sel):
            return ScheduledPendingTest._Loc(self)

        def wait_for_timeout(self, _ms):
            return None

        def goto(self, *_a, **_k):
            self.visits += 1

    def test_waits_out_pending(self):
        page = self._Page(["Visibility Pending", "Visibility Pending", "Scheduled Nov 15, 2026 4:00 PM"])
        self.assertEqual(youtube._check_scheduled(page, "Nov 15, 2026", "https://youtu.be/x"), [])
        self.assertEqual(page.visits, 2)

    def test_still_pending_is_soft_note(self):
        notes = youtube._check_scheduled(self._Page(["Visibility Pending"]), "Nov 15, 2026", "https://youtu.be/x")
        self.assertEqual(len(notes), 1)
        self.assertIn("ещё обрабатывала", notes[0])

    def test_wrong_date_is_flagged(self):
        notes = youtube._check_scheduled(self._Page(["Scheduled Nov 16, 2026"]), "Nov 15, 2026", "https://youtu.be/x")
        self.assertIn("не вижу запланированной даты", notes[0])

    def test_russian_studio_date_with_narrow_space_matches(self):
        """Studio (ru) пишет «…2026\u202fг.», а ожидаемая строка — с обычным пробелом: это одна и та же дата."""
        shown = "Запланировано 20 нояб. 2026\u202fг., 19:00"
        self.assertEqual(youtube._check_scheduled(self._Page([shown]), "20 нояб. 2026 г.", "https://youtu.be/x"), [])


class SetDateTest(unittest.TestCase):
    """Дата публикации: принимаем по смыслу (любой язык и формат), при провале ввода — клик по дню в календаре,
    при полном провале — чистый экран и понятная ошибка."""

    class _Box:
        text = ""

        def inner_text(self, **_):
            return self.text

    class _Page:
        """Studio-заглушка. shown(typed, n) — что покажет поле даты после n-го ввода; parse — как «браузер» читает текст."""

        def __init__(self, shown, parse=None, pick=("ok",), popup=False):
            self.shown, self.pick, self.popup = shown, list(pick), popup
            self.parse = parse or (lambda t: "2026-11-20" if "20" in t and ("2026" in t) else "")
            self.typed, self.pick_calls = [], 0
            self.after_pick = None

        def evaluate(self, js, arg=None):
            if js is youtube._PARSE_DATE_JS:
                return self.parse(arg)
            if js is youtube._PICK_DAY_JS:
                self.pick_calls += 1
                return self.pick.pop(0) if self.pick else "scroll"
            return "20 нояб. 2026 г."  # _DATE_JS: дата в формате языка Studio

        def wait_for_timeout(self, _ms):
            return None

        def locator(self, _sel):
            page = self

            class L:
                first = type("F", (), {"is_visible": lambda self_: page.popup})()

            return L()

    def _run(self, page):
        box = self._Box()
        clicks, keys = [], []

        def el(_page, name, **_):
            if name == "date_trigger":
                n = len(page.typed)
                shown = page.shown(page.typed[-1] if page.typed else "", n)
                box.text = page.typed[-1] if shown == "ECHO" else (page.after_pick if shown == "PICKED" else shown)
                return box
            return object()

        def type_into(_page, _loc, text, _cancel):
            page.typed.append(text)
            page.popup = True

        def click(_p, loc):
            clicks.append(loc)
            if page.pick_calls and page.after_pick is None:
                page.after_pick = "20 нояб. 2026\u202fг."  # клик по дню в календаре выставил дату

        with mock.patch("engine.publish.studio_kit.el", el), mock.patch.object(youtube, "_type_into", type_into), \
                mock.patch.object(youtube.human, "click", click), \
                mock.patch.object(youtube.human, "press", lambda _p, key, *a, **k: keys.append(key)):
            err = None
            try:
                youtube._set_date(page, datetime(2026, 11, 20, 17, 0), None)
            except youtube.StudioError as exc:
                err = exc
        return page, clicks, keys, err

    def test_narrow_space_and_suffix_is_accepted_on_first_try(self):
        page, _c, _k, err = self._run(self._Page(lambda t, n: "20 нояб. 2026\u202fг."))
        self.assertIsNone(err)
        self.assertEqual(page.typed, ["20 нояб. 2026 г."], "второй формат не пробуем и календарь не ломаем")

    def test_accepted_by_meaning_even_if_studio_shows_another_format(self):
        """Studio показала «2026. 11. 20.», набирали «20 нояб. 2026 г.»: строки разные, дата одна — принимаем."""
        page, _c, _k, err = self._run(self._Page(lambda t, n: "2026. 11. 20.", parse=lambda t: "2026-11-20"))
        self.assertIsNone(err)
        self.assertEqual(len(page.typed), 1)

    def test_open_calendar_is_not_clicked_again(self):
        """Первый формат не подошёл — календарь остался открытым: второй раз вводим в него, по полю не кликаем."""
        page, clicks, _k, err = self._run(self._Page(lambda t, n: "Недопустимая" if n < 2 else "ECHO",
                                                     parse=lambda t: ""))
        self.assertIsNone(err)
        self.assertEqual(len(page.typed), 2)
        self.assertEqual(len(clicks), 1, "по полю даты кликнули один раз — повторный клик упирался в открытый календарь")

    def test_calendar_click_is_the_fallback_when_typing_never_works(self):
        """Ни один формат не принят (новый язык/формат Studio) — день выбирается кликом в календаре, с прокруткой."""
        page = self._Page(lambda t, n: "PICKED" if page_after(page) else "Недопустимая дата",
                          parse=lambda t: "2026-11-20" if "нояб" in t else "", pick=("scroll", "scroll", "ok"))
        page_after = lambda p: p.after_pick is not None  # noqa: E731
        page, clicks, keys, err = self._run(page)
        self.assertIsNone(err)
        self.assertEqual(len(page.typed), 3, "все три формата попробовали")
        self.assertEqual(page.pick_calls, 3, "месяц пролистали дважды, потом нашли день")
        self.assertNotIn("Escape", keys)

    def test_total_failure_closes_calendar_and_reports(self):
        page, _c, keys, err = self._run(self._Page(lambda t, n: "Недопустимая дата", parse=lambda t: "", pick=("disabled",)))
        self.assertIsNotNone(err)
        self.assertIn("не вписалась", str(err))
        self.assertIn("Escape", keys, "календарь не остаётся поверх окна")

    def test_norm_date(self):
        self.assertEqual(youtube.norm_date("20 нояб. 2026\u202fг."), youtube.norm_date("20 нояб. 2026 г."))
        self.assertEqual(youtube.norm_date("Nov 20, 2026"), "nov 20 2026")
        self.assertNotEqual(youtube.norm_date("20 нояб. 2026"), youtube.norm_date("21 нояб. 2026"))


class WalkToVisibilityTest(unittest.TestCase):
    """Шаги окна загрузки: без монетизации — «Далее» как раньше; с монетизацией — шаги проходит программа."""

    def _run(self, seq, *, steps=("DETAILS", "REVIEW")):
        from engine.publish import monetize
        from engine.publish.studio_kit import step as real_step

        it = iter(seq)
        page = mock.MagicMock()
        clicks = []
        calls = {"m": 0, "s": 0}

        def fake_m(p, cancel=None):
            calls["m"] += 1
            return ["монетизация: тест"]

        def fake_s(p, cancel=None):
            calls["s"] += 1
            return ["пригодность: тест"]

        with mock.patch.object(youtube, "_act", lambda cancel=None: None), \
                mock.patch.object(monetize, "steps", lambda p: list(steps)), \
                mock.patch.object(monetize, "active_step", lambda p: next(it)), \
                mock.patch.object(monetize, "monetization", fake_m), \
                mock.patch.object(monetize, "ad_suitability", fake_s), \
                mock.patch.object(youtube, "_check_dialog_problems", lambda p: None), \
                mock.patch.object(youtube, "_check_details_error", lambda p: None), \
                mock.patch("engine.publish.studio_kit.el", lambda p, key, **kw: clicks.append(key) or mock.MagicMock()), \
                mock.patch("engine.publish.studio_kit.step", lambda p, name: real_step.__wrapped__(p, name) if False else __import__("contextlib").nullcontext()), \
                mock.patch("engine.publish.studio_kit.diagnose", lambda *a, **k: "/tmp/снимок"):
            notes = youtube._walk_to_visibility(page, None)
        return notes, clicks, calls

    def test_channel_without_monetization(self):
        notes, clicks, calls = self._run(["DETAILS", "REVIEW", "REVIEW"])
        self.assertEqual(notes, [])
        self.assertEqual(clicks, ["next"], "одно «Далее» — как раньше")
        self.assertEqual(calls, {"m": 0, "s": 0})

    def test_monetized_channel_runs_both_steps(self):
        seq = ["DETAILS", "MONETIZATION", "MONETIZATION", "CONTENT_RATINGS", "CONTENT_RATINGS", "VIDEO_ELEMENTS",
               "VIDEO_ELEMENTS", "CHECKS", "CHECKS", "REVIEW", "REVIEW"]
        notes, clicks, calls = self._run(seq, steps=("DETAILS", "MONETIZATION", "CONTENT_RATINGS", "VIDEO_ELEMENTS",
                                                      "CHECKS", "REVIEW"))
        self.assertEqual(calls, {"m": 1, "s": 1})
        self.assertEqual(notes, ["монетизация: тест", "пригодность: тест"])
        self.assertEqual(len(clicks), 5)

    def test_unknown_step_stops_with_needs_user(self):
        with self.assertRaises(youtube.NeedsUser) as cm:
            self._run(["DETAILS", "SURPRISE", "SURPRISE"])
        self.assertIn("SURPRISE", str(cm.exception))

    def test_next_that_does_not_advance_is_an_error(self):
        with self.assertRaises(youtube.StudioError):
            self._run(["DETAILS", "DETAILS", "DETAILS"])


class MonetizeHelpersTest(unittest.TestCase):
    def test_is_on_ignores_popup_text_and_placeholder(self):
        from engine.publish import monetize

        def page_with(text):
            page = mock.MagicMock()
            page.locator.return_value.first.inner_text.return_value = text
            return page

        self.assertTrue(monetize._is_on(page_with("On\n")))
        self.assertFalse(monetize._is_on(page_with("Select")), "поле не заполнено")
        self.assertFalse(monetize._is_on(page_with("Off")))

    def test_placer_state(self):
        from engine.publish import monetize

        def page_with(texts):
            page = mock.MagicMock()
            page.locator.return_value.locator.return_value.all_inner_texts.return_value = texts
            return page

        self.assertEqual(monetize._placer_state(page_with(["Remove Ads"])), "remove")
        self.assertEqual(monetize._placer_state(page_with(["Auto-Placed", "Add"])), "place")
        self.assertEqual(monetize._placer_state(page_with([])), "")


class LiveProgressTest(unittest.TestCase):
    """Бар канала и общий бар двигаются на каждом шаге Studio (не только на загрузке файла), назад не идут."""

    def test_bars_advance_by_steps(self):
        from engine.publish import studio_kit

        t = registry.Target(profile_id="p", profile="№7", channel=registry.Channel(id="UC1", name="Канал"))
        seen = []
        live = pub._Live([t], {}, lambda **kw: None, lambda p, m: seen.append(p))
        self.addCleanup(studio_kit.set_reporter, None)
        live.start(t, "2026-11-15T16:15:00+03:00", 100, 0)
        pct = lambda: live.rows[t.key]["pct"]  # noqa: E731
        live._stage("название")
        a = pct()
        live._stage("монетизация: реклама каждые 20 с (NexLev)")
        b = pct()
        live._stage("название")  # шаг из начала (повтор) — назад не идёт
        self.assertEqual(pct(), b)
        live.file_progress(t, 0)(0.5, "Uploading 50%")
        c = pct()
        live._stage("конечная заставка и подсказка")
        d = pct()
        self.assertTrue(0 < a < b < c < d < 100, (a, b, c, d))
        self.assertEqual(seen, sorted(seen), "общий бар задачи тоже только вперёд")
        self.assertGreater(seen[-1], 0.05)


class LiveAfterUploadTest(unittest.TestCase):
    """Ролик загружен, идёт долгий шаг (ответы зрителям) — в очереди так и написано, со счётчиком; шаги Studio
    дописываются к нему, а не заменяют."""

    def test_long_step_labelled(self):
        from engine.publish import studio_kit

        t = registry.Target(profile_id="p", profile="№7", channel=registry.Channel(id="UC1", name="Канал"))
        msgs = []
        live = pub._Live([t], {}, lambda **kw: None, lambda p, m: msgs.append(m))
        self.addCleanup(studio_kit.set_reporter, None)
        live.start(t, "2026-11-15T16:15:00+03:00", 100, 0)
        live._stage("приглашение соавторов")
        live.set(t, pct=100)
        live.after_upload(t, "ответы на комментарии зрителей")
        self.assertIn("загружено ✓ — ответы на комментарии зрителей", msgs[-1])
        live.after_upload(t, "ответы на комментарии зрителей", "3 из 10, порция 2")
        live._stage("комментарий @Анна: «Привет из Минска»")
        self.assertIn("ответы на комментарии зрителей (3 из 10, порция 2): комментарий @Анна", msgs[-1])
        self.assertEqual(live.rows[t.key]["pct"], 100)
        live.finish(t, {"status": pub.DONE})
        self.assertEqual(live.rows[t.key]["stage"], "выложено ✓")
        self.assertEqual(live.phase, "")


class ChecksWaitTest(unittest.TestCase):
    def test_checks_done_words(self):
        for ok in ("Checks complete. No issues found.", "Checks complete. Copyright claim found. Issues found.",
                   "Проверка завершена"):
            self.assertTrue(youtube._checks_done(ok), ok)
        for bad in ("Uploading 100% ... 1 minute left", "Checks running…", "Processing will begin shortly", ""):
            self.assertFalse(youtube._checks_done(bad), bad)

    def test_waits_for_checks_then_returns(self):
        texts = iter(["Uploading 100%", "Checks running", "Checks complete. No issues found."])
        page = mock.MagicMock()
        with mock.patch.object(youtube, "_progress_text", lambda p: next(texts)):
            youtube._wait_checks(page, None, None)
        self.assertEqual(page.wait_for_timeout.call_count, 2)

    def test_still_checking_dialog_is_reported(self):
        with mock.patch.object(youtube, "_visible_dialogs",
                               lambda p: "We’re still checking your content We recommend taking another look. If you don’t fix"):
            self.assertIn("still checking", youtube._still_checking(mock.MagicMock()).lower())
        with mock.patch.object(youtube, "_visible_dialogs", lambda p: "Video scheduled | Your video will be public"):
            self.assertEqual(youtube._still_checking(mock.MagicMock()), "")


class AcceptListLoadsLateTest(unittest.TestCase):
    """14.11: список «Collaboration requests» открывался пустым и наполнялся позже — программа решала «ждущих нет»."""

    def _page(self, rows_after: int, names):
        from engine.publish import human as human_mod

        state = {"tick": 0, "clicked": False, "confirmed": False}
        page = mock.MagicMock()
        page.wait_for_timeout.side_effect = lambda ms: state.__setitem__("tick", state["tick"] + 1)
        confirm = mock.MagicMock()
        confirm.count.side_effect = lambda: 1 if state["clicked"] and not state["confirmed"] else 0
        confirm.is_visible.side_effect = lambda: state["clicked"] and not state["confirmed"]
        rows = mock.MagicMock()
        rows.count.side_effect = lambda: len(names) if state["tick"] >= rows_after else 0
        rows.nth.side_effect = lambda i: mock.MagicMock(**{"locator.return_value.first.get_attribute.return_value": names[i]})
        listing = mock.MagicMock()
        listing.count.return_value = 1
        listing.locator.return_value = rows

        def locator(sel):
            holder = mock.MagicMock()
            holder.all_inner_texts.return_value = []
            if "confirm-button" in sel:
                holder.first = confirm
            elif sel == youtube._COLLAB_LIST:
                holder.first = listing
            return holder

        page.locator.side_effect = locator

        def click(p, loc, **kw):
            state["confirmed" if loc is confirm else "clicked"] = True

        return page, state, mock.patch.object(human_mod, "click", click)

    def test_waits_for_rows_then_accepts(self):
        page, state, click = self._page(rows_after=6, names=["Ролик А", "Ролик Б"])
        with click, mock.patch.object(youtube, "_act", lambda c=None: None):
            got = youtube._accept_one(page, "https://studio/x", None)
        self.assertEqual(got, "Ролик А")
        self.assertGreaterEqual(state["tick"], 6, "ждал загрузки списка, а не сдался на первой секунде")

    def test_really_empty_list_returns_none(self):
        page, state, click = self._page(rows_after=10 ** 6, names=[])
        with click, mock.patch.object(youtube, "_act", lambda c=None: None):
            self.assertIsNone(youtube._accept_one(page, "https://studio/x", None))

    def test_already_accepted_names_are_skipped(self):
        page, state, click = self._page(rows_after=0, names=["Ролик А"])
        with click, mock.patch.object(youtube, "_act", lambda c=None: None):
            self.assertIsNone(youtube._accept_one(page, "https://studio/x", None, skip=["Ролик А"]))

    def test_click_blocked_by_google_verify_names_the_real_cause(self):
        """«Verify it's you» поверх «Accept»: клик падает по таймауту, а причиной должно стать окно Google."""
        page, state, _click = self._page(rows_after=0, names=["Ролик А"])
        base = page.locator.side_effect

        def locator(sel):
            holder = base(sel)
            if "dialog" in sel and "confirm-button" not in sel and sel != youtube._COLLAB_LIST:
                holder.all_inner_texts.return_value = ["Verify it's you To continue, we need to confirm it’s really you."]
            return holder

        page.locator.side_effect = locator

        def click(p, loc, **kw):
            raise TimeoutError("intercepts pointer events")

        from engine.publish import human as human_mod

        with mock.patch.object(human_mod, "click", click), mock.patch.object(youtube, "_act", lambda c=None: None):
            with self.assertRaises(youtube.NeedsUser):
                youtube._accept_one(page, "https://studio/x", None)


class FileUploadedCheckTest(unittest.TestCase):
    """14.11: ролик был в расписании, а файл не загрузился («Processing abandoned»), локальный файл удалён."""

    def _page(self, rows):
        page = mock.MagicMock()
        loc = mock.MagicMock()
        loc.count.return_value = len(rows)

        def nth(i):
            text, linked = rows[i]
            row = mock.MagicMock()
            row.inner_text.return_value = text
            row.locator.return_value.count.return_value = 1 if linked else 0
            return row

        loc.nth.side_effect = nth
        page.locator.return_value = loc
        return page

    def test_dead_row_without_link_is_found_by_title(self):
        page = self._page([("Другой ролик Scheduled", False),
                           ("🌙 ЭТА НОЧЬ СОЗДАНА ДЛЯ ОТДЫХА! Неспешный рассказ Processing abandoned Video failed to upload", False)])
        with self.assertRaises(youtube.StudioError) as cm:
            youtube._check_file_uploaded(page, "UC1", "https://youtu.be/LryrDctMrrg",
                                         "🌙 ЭТА НОЧЬ СОЗДАНА ДЛЯ ОТДЫХА! Неспешный рассказ под мягкий голос")
        self.assertIn("нет видеофайла", str(cm.exception))

    def test_healthy_row_passes(self):
        page = self._page([("Мой ролик Scheduled Nov 20, 2026", True)])
        youtube._check_file_uploaded(page, "UC1", "https://youtu.be/abcdefghijk", "Мой ролик")

    def test_no_row_found_does_not_block(self):
        youtube._check_file_uploaded(self._page([("Чужой ролик", True)]), "UC1", "https://youtu.be/abcdefghijk", "Мой ролик")


class PlaylistToggleTest(unittest.TestCase):
    def _box(self, checked_attr=None, aria=None):
        box = mock.MagicMock()
        box.get_attribute.return_value = checked_attr
        box.locator.return_value.first.get_attribute.return_value = aria
        return box

    def test_checked_state_read_from_attribute_or_inner_div(self):
        self.assertTrue(youtube._playlist_checked(self._box(checked_attr="")))
        self.assertTrue(youtube._playlist_checked(self._box(aria="true")))
        self.assertFalse(youtube._playlist_checked(self._box()))

    def test_already_selected_playlist_is_not_clicked_again(self):
        """Повтор по черновику: плейлист уже выбран — клик по нему снимал выбор."""
        clicks = []
        page = mock.MagicMock()
        boxes = mock.MagicMock()
        boxes.count.return_value = 1
        boxes.first = self._box(checked_attr="")
        page.locator.side_effect = lambda sel: boxes if "playlists-list" in sel else mock.MagicMock()
        from engine.publish import human as human_mod

        with mock.patch.object(youtube, "_act", lambda c=None: None), \
                mock.patch("engine.publish.studio_kit.el", lambda p, key, **kw: mock.MagicMock()), \
                mock.patch.object(human_mod, "click", lambda p, loc, **k: clicks.append(loc)):
            notes = youtube._pick_playlist(page, None)
        self.assertEqual(notes, [])
        self.assertNotIn(boxes.first, clicks, "выбранный плейлист не трогаем")


class SafeguardsTest(unittest.TestCase):
    """Подстраховки после сбоев 14.11: файл не удаляется без доказательства, плейлист проверяется, профиль не отбирают."""

    def test_duration_badge_is_proof(self):
        page = mock.MagicMock()
        row = mock.MagicMock()
        row.inner_text.return_value = "1:44:25 ПУСТЬ НОЧЬ УКРОЕТ ВАС Scheduled"
        row.locator.return_value.count.return_value = 1
        loc = mock.MagicMock()
        loc.count.return_value = 1
        loc.nth.return_value = row
        page.locator.return_value = loc
        self.assertTrue(youtube._check_file_uploaded(page, "UC1", "https://youtu.be/abcdefghijk", "ПУСТЬ НОЧЬ"))
        row.inner_text.return_value = "ПУСТЬ НОЧЬ УКРОЕТ ВАС Scheduled"  # длительности нет — не доказано
        self.assertFalse(youtube._check_file_uploaded(page, "UC1", "https://youtu.be/abcdefghijk", "ПУСТЬ НОЧЬ"))

    def test_playlist_audit_fixes_missing_playlist(self):
        from engine.publish import human as human_mod

        texts = iter(["Select", "Select", "Истории на ночь"])
        page = mock.MagicMock()
        page.locator.return_value.first.inner_text.side_effect = lambda **k: next(texts)
        job = mock.MagicMock(playlist_hint=["Истории на ночь"])
        with mock.patch.object(youtube, "_act", lambda c=None: None), \
                mock.patch.object(human_mod, "click", lambda *a, **k: None), \
                mock.patch.object(youtube, "_pick_playlist", lambda p, c, trigger=None, want=None, create="": []):
            notes = youtube._audit_playlist(page, "https://youtu.be/abcdefghijk", job, None)
        self.assertTrue(any("поставил" in n for n in notes), notes)

    def test_playlist_audit_silent_when_playlist_is_there(self):
        page = mock.MagicMock()
        page.locator.return_value.first.inner_text.return_value = "Истории на ночь"
        job = mock.MagicMock(playlist_hint=["Истории на ночь"])
        self.assertEqual(youtube._audit_playlist(page, "https://youtu.be/abcdefghijk", job, None), [])

    def test_profile_claimed_by_live_foreign_process_is_refused(self):
        import json as _json
        import subprocess
        import sys

        from engine.publish import adspower
        from engine.publish.store import publish_dir

        fresh_store(self)
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(sleeper.kill)
        f = adspower._lock_file("pX")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(_json.dumps({"pid": sleeper.pid, "at": 1}))
        ap = adspower.AdsPower("k", "http://localhost:1")
        with self.assertRaises(adspower.ProfileBusy):
            ap.start("pX")
        with self.assertRaises(adspower.ProfileBusy):
            ap.stop("pX")
        f.write_text(_json.dumps({"pid": 999999, "at": 1}))  # мёртвый процесс — отметка не мешает
        self.assertEqual(adspower._claimed_by_other("pX"), 0)
        f.write_text(_json.dumps({"pid": __import__("os").getpid(), "at": 1}))  # свой — тоже
        self.assertEqual(adspower._claimed_by_other("pX"), 0)


class CoverNeverBlocksPublishTest(Fixture):
    """Обложка не встала по любой причине — ролик всё равно выходит по расписанию, а в итогах — предупреждение."""

    def run_job(self, req, state, upload):
        with mock.patch.object(pub, "AdsPower", FakeAds), mock.patch.object(youtube, "READY", True), \
             mock.patch.object(youtube, "upload", upload):
            return pub.run_publish(req, state, lambda **v: state.update(v))

    def test_studio_cover_failure_is_warning_not_error(self) -> None:
        def upload(cdp, channel, job, cancel=None, progress=None, **kw):
            notes = [youtube.cover_not_set("Studio не приняла файл обложки")] if channel.id == "UCb" else []
            return youtube.Uploaded(f"https://youtu.be/{channel.id}", notes, None, True)

        state: dict = {}
        res = self.run_job(self.request(["p1/UCa", "p1/UCb"]), state, upload)
        self.assertEqual([r["status"] for r in state["live"]], ["done", "done"])
        cover = [w for w in res["warnings"] if youtube.COVER_NOT_SET in w]
        self.assertEqual(len(cover), 1)
        self.assertIn("Канал Б", cover[0])
        self.assertIn("https://youtu.be/UCb", cover[0])
        self.assertIn("поставь обложку сам", cover[0])

    def test_empty_cover_folder_publishes_without_cover(self) -> None:
        for f in Path(self.mat[0]).iterdir():
            f.unlink()
        seen = []

        def upload(cdp, channel, job, cancel=None, progress=None, **kw):
            seen.append(job.thumbnail)
            return youtube.Uploaded("u", [], None, True)

        state: dict = {}
        res = self.run_job(self.request(["p1/UCa"]), state, upload)
        self.assertEqual(seen, [None], "ролик ушёл без обложки")
        self.assertTrue(any(w.startswith(materials.COVER_MISSING) for w in res["warnings"]), res["warnings"])


class SetThumbnailTest(unittest.TestCase):
    """Шаг обложки в окне загрузки: любой сбой — предупреждение, а не ошибка всей выкладки."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.thumb = self.tmp / "обложка.jpg"
        self.thumb.write_bytes(b"x")
        self.page = mock.MagicMock()
        self.channel = registry.Channel("UCa", "Канал А", handle="@a")
        from engine.publish import studio_kit

        for p in (mock.patch.object(studio_kit, "el", lambda *a, **k: mock.MagicMock()),
                  mock.patch.object(studio_kit, "diagnose", lambda page, name, exc: self.tmp)):
            p.start()
            self.addCleanup(p.stop)

    def _run(self, choose=None, dialog: str = ""):
        with mock.patch.object(youtube.human, "choose_file", choose or (lambda *a, **k: "кнопкой")), \
                mock.patch.object(youtube, "_dialog_text", lambda page: dialog):
            return youtube._set_thumbnail(self.page, self.channel, self.thumb, None)

    def test_ok(self) -> None:
        self.assertEqual(self._run(), [])

    def test_button_missing_is_warning(self) -> None:
        def choose(*a, **k):
            raise RuntimeError("нет кнопки выбора обложки")

        notes = self._run(choose)
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith(youtube.COVER_NOT_SET))
        self.assertIn("нет кнопки выбора обложки", notes[0])

    def test_unverified_channel_and_rejected_file(self) -> None:
        self.assertIn("не подтверждён", self._run(dialog="Verify your account to upload custom thumbnails")[0])
        self.assertIn("не приняла", self._run(dialog="Thumbnail couldn't upload")[0])

    def test_missing_file(self) -> None:
        self.thumb.unlink()
        self.assertIn("файла обложки нет", youtube._set_thumbnail(self.page, self.channel, self.thumb, None)[0])

    def test_browser_loss_and_cancel_still_raise(self) -> None:
        from engine.core.cancel import CancelledError

        def lost(*a, **k):
            raise RuntimeError("Target closed")

        with self.assertRaises(RuntimeError):
            self._run(lost)

        def cancelled(*a, **k):
            raise CancelledError()

        with self.assertRaises(CancelledError):
            self._run(cancelled)


class AuditPlaylistErrorPathTest(unittest.TestCase):
    """Сбой при выставлении плейлиста не должен превращаться в NameError (LayoutError не был импортирован)."""

    def test_layout_error_is_reraised_not_name_error(self) -> None:
        from engine.publish.studio_kit import LayoutError

        page = mock.MagicMock()
        page.locator.return_value.first.inner_text.return_value = ""  # плейлиста на ролике нет
        job = youtube.UploadJob(file=Path("/x.mp4"), title="Т", description="Д", thumbnail=None,
                                publish_at=datetime(2030, 1, 1, tzinfo=schedule.MSK), playlist_hint=["Для вас"], coauthors=[])
        with mock.patch.object(youtube, "_pick_playlist", side_effect=LayoutError("нет поля")):
            with self.assertRaises(LayoutError):
                youtube._audit_playlist(page, "https://youtu.be/abcdefghijk", job, None)

    def test_other_errors_become_a_note(self) -> None:
        page = mock.MagicMock()
        page.locator.return_value.first.inner_text.return_value = ""
        job = youtube.UploadJob(file=Path("/x.mp4"), title="Т", description="Д", thumbnail=None,
                                publish_at=datetime(2030, 1, 1, tzinfo=schedule.MSK), playlist_hint=["Для вас"], coauthors=[])
        with mock.patch.object(youtube, "_pick_playlist", side_effect=RuntimeError("что-то странное")):
            notes = youtube._audit_playlist(page, "https://youtu.be/abcdefghijk", job, None)
        self.assertTrue(notes and "поставь в Studio" in notes[0])


class VisibilityTest(unittest.TestCase):
    """Ролики канала по доступу: открытые, ограниченные, по ссылке, запланированные, черновики — на любом языке Studio."""

    MONTHS = ["янв", "февр", "мар", "апр", "мая", "июн", "июл", "авг", "сент", "окт", "нояб", "дек"]
    TODAY = date(2026, 11, 20)

    def kind(self, cell: str, row: str = "") -> str:
        return youtube.visibility_of(cell, row, self.MONTHS, self.TODAY)

    def test_cells_in_english_and_russian(self) -> None:
        self.assertEqual(self.kind("Public"), "public")
        self.assertEqual(self.kind("Открытый доступ"), "public")
        self.assertEqual(self.kind("Private"), "private")
        self.assertEqual(self.kind("Ограниченный доступ"), "private")
        self.assertEqual(self.kind("Unlisted"), "unlisted")
        self.assertEqual(self.kind("Доступ по ссылке"), "unlisted")
        self.assertEqual(self.kind("Scheduled"), "scheduled")
        self.assertEqual(self.kind("Запланировано"), "scheduled")
        self.assertEqual(self.kind("Draft"), "draft")

    def test_fallback_to_row_text_and_future_date(self) -> None:
        self.assertEqual(self.kind("", "Ролик Private 10 нояб. 2026 г. 12"), "private")
        self.assertEqual(self.kind("", "Ролик Public 25 нояб. 2026 г. 12"), "scheduled")  # будущая дата
        self.assertEqual(self.kind("", "Ролик Public 10 нояб. 2026 г. 12"), "public")

    def test_count_videos_walks_all_pages(self) -> None:
        pages = [
            [{"cell": "Scheduled", "row": "a"}, {"cell": "Public", "row": "b"}],
            [{"cell": "Private", "row": "c"}, {"cell": "Unlisted", "row": "d"}, {"cell": "Public", "row": "e"}],
        ]

        class Next:
            def __init__(self, page): self.page = page
            @property
            def first(self): return self
            def count(self): return 1
            def get_attribute(self, _): return "true" if self.page.i >= len(pages) - 1 else "false"
            def click(self): self.page.i += 1

        class Page:
            i = 0
            def goto(self, *a, **k): pass
            def wait_for_selector(self, *a, **k): pass
            def wait_for_timeout(self, _): pass
            def evaluate(self, js): return self_months
            def eval_on_selector_all(self, sel, js): return pages[self.i]
            def locator(self, sel): return Next(self)

        self_months = self.MONTHS
        out = youtube.count_videos(Page(), "UC1")
        self.assertEqual(out, {"scheduled": 1, "public": 2, "unlisted": 1, "private": 1, "draft": 0})


class PlaylistDialogClosedTest(unittest.TestCase):
    """22.11: окно плейлистов закрылось само — 15 с ждали «Готово» и роняли выкладку канала."""

    def test_closed_dialog_is_not_an_error(self) -> None:
        page = mock.MagicMock()
        boxes = mock.MagicMock()
        boxes.count.return_value = 1
        done = mock.MagicMock()
        done.wait_for.side_effect = TimeoutError("Locator.wait_for: Timeout 5000ms exceeded")

        def locator(sel):
            return boxes if "checkbox" in sel else mock.MagicMock(last=done)

        page.locator.side_effect = locator
        clicks = []
        with mock.patch("engine.publish.studio_kit.el", return_value=mock.MagicMock()), \
             mock.patch.object(youtube, "_playlist_checked", return_value=True), \
             mock.patch.object(youtube.human, "click", lambda p, target, **kw: clicks.append(target)), \
             mock.patch.object(youtube, "_act", lambda cancel: None):
            notes = youtube._pick_playlist(page, None)
        self.assertEqual(notes, [])
        self.assertNotIn(done, clicks, "закрытое окно не закрываем повторно")


class RenamedChannelTest(unittest.TestCase):
    """Канал переименовали (название и @-адрес) в YouTube, а каналы профиля ещё не пересканированы."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        p = mock.patch.dict(os.environ, {"STORY_VIDEO_PUBLISH_DIR": str(self.tmp)})
        p.start()
        self.addCleanup(p.stop)

    def test_registry_updates_name_and_handle_everywhere(self) -> None:
        registry.sync([Profile(id="p1", number="1", name="Первый", group_name="")])
        registry.set_channels("p1", [registry.Channel(id="UCx", name="Старое", handle="@old", language="ru")])
        self.assertTrue(registry.update_identity("UCx", name="Новое", handle="@new", authuser=1))
        ch = registry.load()["p1"].channels[0]
        self.assertEqual((ch.name, ch.handle, ch.authuser, ch.language), ("Новое", "@new", 1, "ru"))
        self.assertFalse(registry.update_identity("UCx", name="Новое", handle="@new", authuser=1), "без изменений")

    def test_profile_rename_comes_from_adspower_sync(self) -> None:
        registry.sync([Profile(id="p1", number="1", name="Старое имя", group_name="А")])
        registry.set_channels("p1", [registry.Channel(id="UCx", name="Канал", handle="@x")])
        registry.sync([Profile(id="p1", number="5", name="Новое имя", group_name="Б")])
        p = registry.load()["p1"]
        self.assertEqual((p.number, p.name, p.group), ("5", "Новое имя", "Б"))
        self.assertEqual([c.id for c in p.channels], ["UCx"], "каналы профиля при этом не теряются")

    def test_switch_finds_renamed_channel_by_id(self) -> None:
        registry.sync([Profile(id="p1", number="1", name="Первый", group_name="")])
        ch = registry.Channel(id="UCx", name="Старое", handle="@old", authuser=0)
        registry.set_channels("p1", [ch])
        lists = {0: [{"name": "Другой", "handle": "@other", "gaia": "g0", "selected": False},
                     {"name": "Новое", "handle": "@new", "gaia": "g0", "selected": False}]}
        ids = {"@other": "UCother", "@new": "UCx"}
        page = mock.MagicMock()
        active = iter([False, True])
        with mock.patch.object(youtube, "_switcher_items", lambda page, a: lists.get(a, [])), \
             mock.patch.object(youtube, "channel_id", lambda page, h: ids[h]), \
             mock.patch.object(youtube, "_is_active", lambda page, cid: next(active)), \
             mock.patch.object(youtube, "pause", lambda cancel: None), \
             mock.patch.object(youtube.human, "click", lambda *a, **k: None):
            youtube.switch_to(page, ch)
        self.assertEqual((ch.name, ch.handle), ("Новое", "@new"))
        page.locator.assert_any_call("ytd-account-item-renderer")
        self.assertEqual(page.locator.return_value.nth.call_args[0][0], 1, "клик — по найденному по ID каналу")
        saved = registry.load()["p1"].channels[0]
        self.assertEqual((saved.name, saved.handle), ("Новое", "@new"), "учёт обновлён — в интерфейсе новое имя")


class ChosenPlaylistTest(unittest.TestCase):
    """22.11: на канале два плейлиста («Звезды», «истории») — ролики кладём в выбранный человеком «истории»."""

    def _run(self, labels, want, checked=()):
        page = mock.MagicMock()
        boxes = mock.MagicMock()
        boxes.count.return_value = len(labels)
        items = []
        for i, t in enumerate(labels):
            b = mock.MagicMock(name=t)
            b.evaluate.return_value = t
            b.get_attribute.return_value = "" if i in checked else None
            b.locator.return_value.first.get_attribute.return_value = "false"
            items.append(b)
        boxes.nth.side_effect = lambda i: items[i]
        done = mock.MagicMock()
        page.locator.side_effect = lambda sel: boxes if "playlists-list" in sel else mock.MagicMock(last=done)
        clicks = []

        def click(p, loc, **k):
            clicks.append(loc)
            if loc in items:
                loc.get_attribute.return_value = ""  # после клика галочка стоит

        with mock.patch.object(youtube, "_act", lambda c=None: None), \
                mock.patch("engine.publish.studio_kit.el", lambda p, key, **kw: mock.MagicMock()), \
                mock.patch.object(youtube.human, "click", click):
            notes = youtube._pick_playlist(page, None, want=want)
        return notes, [c for c in clicks if c in items], items

    def test_chosen_playlist_among_several(self):
        notes, clicked, items = self._run(["Звезды", "истории"], ["истории"])
        self.assertEqual((notes, clicked), ([], [items[1]]))

    def test_several_without_choice_selects_nothing(self):
        notes, clicked, _ = self._run(["истории", "historia"], ["истории", "historia"])
        self.assertEqual(clicked, [])
        self.assertIn("выбери плейлист канала", notes[0])

    def test_registry_keeps_choice_across_rescan(self):
        tmp = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"STORY_VIDEO_PUBLISH_DIR": tmp}):
            registry.sync([Profile(id="p", number="37", name="Душой нараспашку", group_name="")])
            registry.set_channels("p", [registry.Channel(id="UCz", name="Реальные Звезды", playlists=["Звезды", "истории"])])
            registry.set_playlist("UCz", "истории")
            registry.set_channels("p", [registry.Channel(id="UCz", name="Реальные Звезды", playlists=["Звезды", "истории"])])
            ch = registry.load()["p"].channels[0]
            self.assertEqual(ch.upload_playlists(), ["истории"])
            with self.assertRaises(ValueError):
                registry.set_playlist("UCz", "нет такого")


class FixAfterPublishTest(unittest.TestCase):
    """После выкладки без обложки / без плейлиста программа сама ставит «Доделать ролик»."""

    def test_fixes_planned_from_result_notes(self) -> None:
        from server.runners import fixes_for

        params = {"section": "slides", "make_cover": True, "source_url": "https://youtu.be/orig",
                  "folder": "/tmp/Камбоджа", "language": "ru", "run_dir": "/tmp/run"}
        results = {
            "p/UC1": {"status": "done", "url": "https://youtu.be/a",
                      "notes": [youtube.COVER_NOT_SET + " (нет связи) — ролик выложен без неё"]},
            "p/UC2": {"status": "done", "url": "https://youtu.be/b", "notes": ["всё хорошо"]},
            "p/UC3": {"status": "failed", "url": "", "notes": [youtube.COVER_NOT_SET]},
        }
        fixes = fixes_for(params, results, done=[], assign={"p/UC1": {"lang": "ru"}})
        self.assertEqual([f["channel"] for f in fixes], ["p/UC1"])
        self.assertEqual(fixes[0]["cover"]["source_url"], "https://youtu.be/orig")
        self.assertFalse(fixes[0]["playlist"])
        self.assertEqual(fixes_for(params, results, done=["p/UC1"]), [], "уже поставлена — второй раз не ставим")
        broll = fixes_for({**params, "section": "broll"}, results, done=[])
        self.assertEqual(broll, [], "обложки «Рассказов» берутся из твоих папок — сгенерировать нечем")

    def test_playlist_fix_only_when_one_playlist_is_known(self) -> None:
        from server.runners import fixes_for

        results = {"p/UC1": {"status": "done", "url": "https://youtu.be/a",
                             "notes": ["плейлист «истории» не стоит — поставь в Studio сам (https://youtu.be/a)"]}}
        one = registry.Target(profile_id="p", profile="№1", channel=registry.Channel(id="UC1", name="К", playlists=["истории"]))
        two = registry.Target(profile_id="p", profile="№1",
                              channel=registry.Channel(id="UC1", name="К", playlists=["истории", "Звезды"]))
        with mock.patch("engine.publish.registry.resolve", return_value=[one]):
            self.assertTrue(fixes_for({"section": "broll"}, results, done=[])[0]["playlist"])
        with mock.patch("engine.publish.registry.resolve", return_value=[two]):
            self.assertEqual(fixes_for({"section": "broll"}, results, done=[]), [], "плейлист не выбран — ставить нечего")


class FixVideoTest(unittest.TestCase):
    def _run(self, shown_before, shown_after, thumb=None, srcs=(set(), set())):
        page = mock.MagicMock()
        field = mock.MagicMock()
        state = {"saved": False}
        field.inner_text.side_effect = lambda timeout=0: shown_after if state["saved"] else shown_before
        page.locator.return_value.first = field
        save = mock.MagicMock()
        save.get_attribute.return_value = None
        src_iter = iter(srcs)

        def click(p, loc, **k):
            if loc is save:
                state["saved"] = True

        cm = mock.MagicMock()
        cm.__enter__.return_value = page
        with mock.patch.object(youtube, "studio_page", return_value=cm), \
             mock.patch.object(youtube, "switch_to", lambda *a, **k: None), \
             mock.patch.object(youtube, "goto", lambda *a, **k: None), \
             mock.patch.object(youtube, "_act", lambda c=None: None), \
             mock.patch.object(youtube, "_pick_playlist", return_value=[]) as pick, \
             mock.patch.object(youtube, "_thumb_srcs", lambda p: next(src_iter)), \
             mock.patch.object(youtube.human, "click", click), \
             mock.patch.object(youtube.human, "choose_file", return_value="кнопкой"), \
             mock.patch("engine.publish.studio_kit.el", lambda p, key, **kw: save):
            notes = youtube.fix_video("ws://x", registry.Channel(id="UC1", name="К", handle="@k"),
                                      "https://youtu.be/abcdefghijk", thumbnail=thumb, playlists=["истории"])
        return notes, pick

    def test_playlist_set_and_verified(self) -> None:
        notes, pick = self._run("Выберите плейлист", "истории")
        pick.assert_called_once()
        self.assertIn("плейлист «истории» поставлен", notes[0])

    def test_playlist_not_sticking_is_an_error(self) -> None:
        with self.assertRaises(youtube.StudioError):
            self._run("Выберите плейлист", "Выберите плейлист")

    def test_cover_set_and_checked_by_new_image(self) -> None:
        f = Path(tempfile.mkdtemp()) / "обложка.jpg"
        f.write_bytes(b"x")
        notes, pick = self._run("истории", "истории", thumb=f, srcs=({"old.jpg"}, {"new.jpg"}))
        pick.assert_not_called()  # плейлист уже стоит — не трогаем
        self.assertIn("обложка поставлена", notes[0])

    def test_nothing_to_do(self) -> None:
        notes, pick = self._run("истории", "истории")
        self.assertIn("доделывать нечего", notes[0])


class RuUploadLimitTest(unittest.TestCase):
    """23.11: русская Studio пишет о суточном лимите без слова «лимит», окно серое — шаг «название» падал по
    таймауту, а программа повторяла выкладку каждый час (10 попыток). Теперь это UploadLimit на сутки."""

    TEXT = ("Информация Название (обязательное поле) Я ПЛАКАЛА Загрузка недоступна. Загрузить это видео можно будет "
            "через 24 часа. Если вы хотите добавлять больше видео в день, пройдите проверку.")

    class _Loc:
        def __init__(self, text: str) -> None:
            self.text = text

        def inner_text(self, timeout: int = 0) -> str:
            return self.text

    class _Page:
        def __init__(self, text: str) -> None:
            self.text = text

        def locator(self, sel: str):
            return RuUploadLimitTest._Loc(self.text)

    def test_russian_limit_text_detected(self) -> None:
        with self.assertRaises(youtube.UploadLimit):
            youtube._check_dialog_problems(self._Page(self.TEXT))

    def test_ordinary_dialog_is_not_limit(self) -> None:
        youtube._check_dialog_problems(self._Page("Информация Название Описание Плейлисты Загрузка завершена"))

    def test_failed_step_becomes_limit(self) -> None:
        from engine.publish import studio_kit

        page = self._Page(self.TEXT)
        with mock.patch.object(studio_kit, "diagnose", side_effect=AssertionError("лимит — не снимок вёрстки")), \
                mock.patch.object(studio_kit, "no_connection", return_value=""):
            with self.assertRaises(youtube.UploadLimit):
                with studio_kit.step(page, "название"):
                    raise TimeoutError("Locator.click: Timeout 15000ms exceeded.")


class ForYouPlaylistTest(unittest.TestCase):
    """Нет ни одного плейлиста на канале — программа создаёт «Для вас» на языке роликов, один раз."""

    def test_names_by_language(self) -> None:
        self.assertEqual(registry.for_you("ru"), "Для вас")
        self.assertEqual(registry.for_you("it"), "Per te")
        self.assertEqual(registry.for_you("pt-BR"), "Para você")
        self.assertEqual(registry.for_you(""), "For you")
        self.assertEqual(registry.for_you("xx"), "For you")

    def test_only_when_channel_has_none(self) -> None:
        empty = registry.Channel(id="UC1", name="a", language="pl")
        self.assertEqual(empty.new_playlist(), "Dla Ciebie")
        self.assertEqual(empty.new_playlist("ro"), "Pentru tine", "язык ролика важнее языка канала")
        self.assertEqual(registry.Channel(id="UC2", name="b", playlists=["x"]).new_playlist("ru"), "")
        self.assertEqual(registry.Channel(id="UC3", name="c", playlists=["x", "y"]).new_playlist("ru"), "",
                         "несколько — не создаём")

    def test_created_note_is_parsed(self) -> None:
        notes = ["без обложки", f"{youtube.PLAYLIST_CREATED} «Per te» (на канале не было ни одного) — …"]
        self.assertEqual(youtube.created_playlist(notes), "Per te")
        self.assertEqual(youtube.created_playlist(["плейлистов на канале 2 — …"]), "")


class CoverVerifiedTest(unittest.TestCase):
    """23.11: на №37 обложка «выбиралась», но не дозагружалась — программа молчала. Теперь проверка по факту."""

    def _page(self, states):
        page = mock.MagicMock()
        seq = iter(states)
        page.evaluate.side_effect = lambda js, arg=None: next(seq, states[-1])
        return page

    def test_wait_reads_custom_thumb(self) -> None:
        self.assertTrue(youtube._wait_custom_thumb(self._page([{"src": "https://i9.ytimg.com/x.jpg"}]), timeout=0))
        self.assertFalse(youtube._wait_custom_thumb(self._page([{"src": ""}]), timeout=0))
        self.assertIsNone(youtube._wait_custom_thumb(self._page([None]), timeout=5), "блока нет — не ждём зря")

    def test_plain_copy_has_simple_name(self) -> None:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        src = d / "✓ Изображение ChatGPT 10 нояб. 2026 г., 11_47_55-9.png"
        src.write_bytes(b"png")
        out = youtube._plain_copy(src)
        self.assertRegex(out.name, r"^cover-[0-9a-f]{10}\.png$")
        self.assertEqual(out.read_bytes(), b"png")

    def _audit(self, states, put):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        thumb = d / "c.png"
        thumb.write_bytes(b"x")
        job = mock.MagicMock(thumbnail=thumb)
        page = self._page(states)
        with mock.patch.object(youtube, "goto"), \
                mock.patch.object(youtube, "_put_thumb_on_edit_page", return_value=put) as p, \
                mock.patch("engine.publish.studio_kit.step", lambda pg, name: __import__("contextlib").nullcontext()), \
                mock.patch.object(youtube, "THUMB_WAIT_SEC", 0):
            with mock.patch.object(youtube.time, "monotonic", side_effect=[0, 100] * 20):
                notes = youtube._audit_thumbnail(page, "https://youtu.be/abcdefghijk", job, None)
        return notes, p

    def test_audit_silent_when_cover_is_there(self) -> None:
        notes, put = self._audit([{"src": "https://i9.ytimg.com/c.jpg"}], True)
        self.assertEqual(notes, [])
        put.assert_not_called()

    def test_audit_puts_missing_cover(self) -> None:
        notes, put = self._audit([{"src": ""}, {"src": "https://i9.ytimg.com/c.jpg"}], True)
        put.assert_called_once()
        self.assertTrue(any("поставил на странице ролика" in n for n in notes), notes)

    def test_audit_failure_is_loud(self) -> None:
        notes, _ = self._audit([{"src": ""}], False)
        self.assertTrue(notes and notes[0].startswith(youtube.COVER_NOT_SET), notes)

    def test_fix_queued_with_same_file_for_ready_covers(self) -> None:
        from server.runners import fixes_for

        res = {"p/UC1": {"status": "done", "url": "https://youtu.be/abcdefghijk", "thumbnail": "/x/c.png",
                         "notes": [youtube.cover_not_set("не встала")]}}
        with mock.patch("engine.publish.registry.resolve", return_value=[]):
            out = fixes_for({"section": "broll"}, res, [])
        self.assertEqual(out, [{"channel": "p/UC1", "url": "https://youtu.be/abcdefghijk",
                                "cover": {"file": "/x/c.png"}, "playlist": False}])


class ThumbLimitTest(unittest.TestCase):
    """23.11, №37: «Развивайте свой канал… вскоре вы сможете добавлять больше своих значков в день» — суточный
    лимит своих обложек. Ролик выходит без обложки с пометкой, доделка ждёт сутки (retry_at), а не молчит."""

    RU = ("Развивайте свой канал Соблюдайте правила сообщества, публикуйте видео, и вскоре вы сможете добавлять "
          "больше своих значков в день. Подробнее… ОК")

    def _page(self, texts):
        page = mock.MagicMock()
        page.evaluate.return_value = texts
        return page

    def test_detected_ru_and_en(self) -> None:
        self.assertTrue(youtube.thumb_limit_text(self._page(["Сведения", self.RU])))
        self.assertTrue(youtube.thumb_limit_text(self._page(["Grow your channel … to add more custom thumbnails per day"])))
        self.assertEqual(youtube.thumb_limit_text(self._page(["Выберите плейлисты Звезды истории ОК"])), "")

    def test_limit_waits_a_day(self) -> None:
        with self.assertRaises(youtube.ThumbLimit) as cm:
            youtube._raise_if_thumb_limit(self._page([self.RU]))
        self.assertGreater(cm.exception.retry_at, __import__("time").time() + 23 * 3600)
        self.assertIsInstance(cm.exception, youtube.UploadLimit, "шаг Studio пропускает его как есть")
