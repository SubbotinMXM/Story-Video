"""Запас памяти для Mac: программа никогда не занимает оперативку целиком (8, 16, 32 ГБ — всё равно)."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from engine.core import memguard as mg

GB = mg.GB


def _mac(total_gb: float, free_pct: int, pressure: int = 1):
    vals = {"hw.memsize": int(total_gb * GB), "kern.memorystatus_level": free_pct,
            "kern.memorystatus_vm_pressure_level": pressure}
    return mock.patch.object(mg, "_sysctl", lambda name, ttl=2.0: vals[name])


class ReserveTests(unittest.TestCase):
    def setUp(self) -> None:
        p = mock.patch.dict("os.environ", {"STORY_VIDEO_NO_MEMGUARD": ""})
        p.start()
        self.addCleanup(p.stop)

    def test_reserve_scales_with_ram_but_never_below_minimum(self) -> None:
        for total, want in ((8, 2.5), (16, 4), (32, 8), (64, 16)):
            with _mac(total, 50):
                self.assertAlmostEqual(mg.reserve() / GB, want, places=1, msg=f"{total} ГБ")

    def test_tight_when_new_work_would_eat_the_reserve(self) -> None:
        with _mac(16, 50):  # свободно 8 ГБ, запас 4
            self.assertFalse(mg.tight(mg.HEAVY_NEED), "8 − 3 = 5 > 4: ролик помещается")
            self.assertTrue(mg.tight(5 * GB))
        with _mac(16, 30):  # свободно 4,8 ГБ
            self.assertTrue(mg.tight(mg.HEAVY_NEED))
            self.assertFalse(mg.tight(0))
        with _mac(32, 40):  # свободно 12,8, запас 8
            self.assertFalse(mg.tight(mg.HEAVY_NEED))
        with _mac(8, 60):  # свободно 4,8, запас 2,5
            self.assertTrue(mg.tight(mg.HEAVY_NEED), "на 8 ГБ второй ролик не влезает")
        with _mac(16, 80, pressure=2):
            self.assertFalse(mg.tight(0), "«warn» при забитой подкачке держится часами — решает свободная память")
        with _mac(16, 80, pressure=4):
            self.assertTrue(mg.tight(0), "macOS на грани — новое не начинаем")

    def test_workers_shrink_with_free_memory(self) -> None:
        with _mac(16, 50):  # 8 − 4 = 4 ГБ на ffmpeg по 0,7 → 5, но не больше 4
            self.assertEqual(mg.workers(4, mg.FFMPEG_NEED), 4)
        with _mac(16, 30):  # 4,8 − 4 = 0,8 → 1
            self.assertEqual(mg.workers(4, mg.FFMPEG_NEED), 1)
        with _mac(16, 80, pressure=2):
            self.assertEqual(mg.workers(4, mg.FFMPEG_NEED), 4)
        with _mac(16, 80, pressure=4):
            self.assertEqual(mg.workers(4, mg.FFMPEG_NEED), 1)

    def test_critical(self) -> None:
        with _mac(16, 10):  # 1,6 < 4 / 2
            self.assertTrue(mg.critical())
        with _mac(16, 40):
            self.assertFalse(mg.critical())

    def test_off_in_tests_by_default(self) -> None:
        with mock.patch.dict("os.environ", {"STORY_VIDEO_NO_MEMGUARD": "1"}), _mac(8, 1, pressure=4):
            self.assertFalse(mg.tight(10 * GB))
            self.assertFalse(mg.critical())
            self.assertEqual(mg.workers(4, GB), 4)


class RenderTests(unittest.TestCase):
    def test_render_runs_fewer_ffmpeg_when_memory_is_short(self) -> None:
        from engine.slides import render_pipeline as rp

        live = 0
        peak = 0
        lock = threading.Lock()

        def seg():
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(0.05)
            with lock:
                live -= 1
            return Path("x")

        jobs = [(Path(f"s{i}"), seg, f"кусок {i}") for i in range(8)]
        with mock.patch.object(mg, "workers", return_value=1), mock.patch.object(mg, "critical", return_value=False):
            rp._render_parallel(jobs, cancel=None, progress=lambda p, m: None)
        self.assertEqual(peak, 1)
        peak = 0
        with mock.patch.object(mg, "workers", return_value=4), mock.patch.object(mg, "critical", return_value=False):
            rp._render_parallel(jobs, cancel=None, progress=lambda p, m: None)
        self.assertGreater(peak, 1)


class QueueTests(unittest.TestCase):
    def test_upload_waits_at_most_ten_minutes_for_memory(self) -> None:
        from server import jobs as jobs_mod

        m = object.__new__(jobs_mod.JobManager)
        m._jobs, m._last_emit = [], {}
        job = jobs_mod.Job(kind="publish", title="Выкладка", params={}, runner=None)
        with mock.patch.object(mg, "tight", return_value=True), mock.patch.object(mg, "describe", return_value="мало"):
            now = time.time()
            self.assertFalse(m._mem_ok(job, now))
            self.assertIn("память", job.message)
            self.assertFalse(m._mem_ok(job, now + 60))
            self.assertFalse(m._mem_ok(job, now + jobs_mod.MEM_WAIT_UPLOAD - 1))
            self.assertTrue(m._mem_ok(job, now + jobs_mod.MEM_WAIT_UPLOAD + 1), "выкладка ждёт не дольше 3 мин")
        with mock.patch.object(mg, "tight", return_value=False):
            self.assertTrue(m._mem_ok(job, now))
        self.assertNotIn("mem_wait_since", job.state)

    def test_first_video_waits_only_when_memory_is_critical(self) -> None:
        from server import jobs as jobs_mod

        m = object.__new__(jobs_mod.JobManager)
        m._jobs, m._last_emit = [], {}
        job = jobs_mod.Job(kind="slides_full", title="Ролик", params={}, runner=None)
        with mock.patch.object(mg, "tight", return_value=True), mock.patch.object(mg, "critical", return_value=False):
            self.assertTrue(m._mem_ok(job, time.time()), "память заняли другие программы — ждать бесполезно")
        with mock.patch.object(mg, "critical", return_value=True), mock.patch.object(mg, "describe", return_value="мало"):
            self.assertFalse(m._mem_ok(job, time.time()))
        other = jobs_mod.Job(kind="slides_full", title="Первый", params={}, runner=None, status=jobs_mod.RUNNING)
        m._jobs = [other]
        with mock.patch.object(mg, "tight", return_value=True), mock.patch.object(mg, "critical", return_value=False), \
             mock.patch.object(mg, "describe", return_value="мало"):
            self.assertFalse(m._mem_ok(job, time.time() + 3600), "второй ждёт, пока наш первый освободит память")

    def test_light_jobs_never_wait(self) -> None:
        from server import jobs as jobs_mod

        m = object.__new__(jobs_mod.JobManager)
        m._jobs = []
        job = jobs_mod.Job(kind="rewrite", title="Рерайт", params={}, runner=None)
        with mock.patch.object(mg, "tight", return_value=True):
            self.assertTrue(m._mem_ok(job, time.time()))


class ClipTests(unittest.TestCase):
    def test_model_unloaded_when_last_stage_ends(self) -> None:
        from engine.slides import clip_ranker as cr

        cr._bundle = {"torch": None, "model": object()}
        cr._features[(1, 2)] = object()
        cr.hold_clip()
        cr.hold_clip()
        cr.release_clip()
        self.assertIsNotNone(cr._bundle, "второй ролик ещё подбирает кадры")
        cr.release_clip()
        self.assertIsNone(cr._bundle)
        self.assertEqual(cr._features, {})


if __name__ == "__main__":
    unittest.main()
