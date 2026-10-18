"""Очередь, настройки, ограничители, запрет сна, единственный экземпляр, потоки."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import contextvars
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.core import config as cfg_mod
from engine.core.cancel import CancelledError, CancelToken
from server import jobs as jobs_mod


def _wait(cond, timeout: float = 5.0) -> None:
    t0 = time.monotonic()
    while not cond():
        if time.monotonic() - t0 > timeout:
            raise AssertionError("не дождался")
        time.sleep(0.02)


class _ManagerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for p in (
            mock.patch.object(jobs_mod, "JOBS_FILE", self.root / "jobs.json"),
            mock.patch.object(jobs_mod, "DATA_DIR", self.root),
            mock.patch("server.keepawake.KeepAwake.hold", lambda self: None),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(time.sleep, 0.15)  # дать очереди дописать jobs.json


class JobManagerEdgeTests(_ManagerCase):
    def test_cancel_running_job_is_resumable(self) -> None:
        started = threading.Event()

        def builder(kind, params, state):
            def run(job, progress, cancel):
                started.set()
                while True:
                    cancel.check()
                    time.sleep(0.01)

            return "Долгая", run

        m = jobs_mod.JobManager(builder)
        job = m.add(m.create("x", {}))
        started.wait(3)
        self.assertTrue(m.cancel(job.id))
        _wait(lambda: job.status == jobs_mod.CANCELLED)
        self.assertTrue(job.public()["resumable"])

    def test_reset_running_job_refused(self) -> None:
        gate = threading.Event()

        def builder(kind, params, state):
            return "Т", lambda job, p, c: gate.wait(3) and {}

        m = jobs_mod.JobManager(builder)
        job = m.add(m.create("x", {}))
        _wait(lambda: job.status == jobs_mod.RUNNING)
        with self.assertRaisesRegex(ValueError, "останови"):
            m.reset(job.id)
        gate.set()
        _wait(lambda: job.status == jobs_mod.DONE)

    def test_cancel_queued_job(self) -> None:
        gate = threading.Event()

        def builder(kind, params, state):
            return "Т", lambda job, p, c: gate.wait(3) and {}

        with mock.patch.object(jobs_mod, "MAX_PARALLEL", 1):
            m = jobs_mod.JobManager(builder)
            first = m.add(m.create("x", {}))
            second = m.add(m.create("x", {}))
            _wait(lambda: first.status == jobs_mod.RUNNING)
            self.assertTrue(m.cancel(second.id))
            self.assertEqual(second.status, jobs_mod.CANCELLED)
            gate.set()
            _wait(lambda: first.status == jobs_mod.DONE)
            time.sleep(0.1)
            self.assertEqual(second.status, jobs_mod.CANCELLED, "отменённая не должна стартовать")

    def test_restart_marks_interrupted_jobs(self) -> None:
        data = [
            {"id": "a", "kind": "broll", "title": "A", "params": {}, "state": {"work_dir": "/x"}, "status": "running"},
            {"id": "b", "kind": "broll", "title": "B", "params": {}, "status": "queued"},
            {"id": "c", "kind": "broll", "title": "C", "params": {}, "status": "done"},
            {"broken": True},
        ]
        (self.root / "jobs.json").write_text(json.dumps(data), encoding="utf-8")
        m = jobs_mod.JobManager(lambda *a: ("T", lambda *b: {}))
        by_id = {j["id"]: j for j in m.list()}
        self.assertEqual(by_id["a"]["status"], "failed")
        self.assertIn("закрыто", by_id["a"]["error"])
        self.assertEqual(by_id["b"]["status"], "cancelled")
        self.assertTrue(by_id["a"]["resumable"] and by_id["b"]["resumable"])
        self.assertEqual(by_id["c"]["status"], "done")
        self.assertEqual(len(by_id), 3, "битая запись пропущена")
        self.assertEqual(m._find("a").state["work_dir"], "/x")

    def test_history_limit(self) -> None:
        with mock.patch.object(jobs_mod, "HISTORY_LIMIT", 2):
            m = jobs_mod.JobManager(lambda *a: ("T", lambda *b: {}))
            jobs = [m.add(m.create("x", {})) for _ in range(4)]
            _wait(lambda: all(j.status == jobs_mod.DONE for j in jobs))
            time.sleep(0.1)
            stored = json.loads((self.root / "jobs.json").read_text(encoding="utf-8"))
            self.assertEqual(len(stored), 2)

    def test_user_error_vs_crash_message(self) -> None:
        def builder(kind, params, state):
            def run(job, p, c):
                if params["user"]:
                    raise ValueError("Укажи папку")
                raise KeyError("boom")

            return "Т", run

        m = jobs_mod.JobManager(builder)
        a = m.add(m.create("x", {"user": True}))
        b = m.add(m.create("x", {"user": False}))
        _wait(lambda: a.status == jobs_mod.FAILED and b.status == jobs_mod.FAILED)
        self.assertEqual(a.error, "Укажи папку")
        self.assertTrue(b.error.startswith("KeyError"))


class RunInUseTests(_ManagerCase):
    def test_same_run_cannot_be_processed_twice(self) -> None:
        gate = threading.Event()

        def builder(kind, params, state):
            def run(job, progress, cancel):
                if kind == "slides_full":
                    job.save_state(run_dir="/runs/20261012_ролик")
                gate.wait(3)
                return {}

            return "Т", run

        m = jobs_mod.JobManager(builder)
        full = m.add(m.create("slides_full", {"text": "t"}))
        _wait(lambda: full.public()["run"] == "20261012_ролик")
        with self.assertRaisesRegex(ValueError, "уже в работе"):
            m.create("slides_resume", {"run": "20261012_ролик"})
        other = m.add(m.create("slides_render", {"run": "другой"}))  # другой запуск — можно
        gate.set()
        _wait(lambda: full.status == jobs_mod.DONE and other.status == jobs_mod.DONE)
        m.add(m.create("slides_resume", {"run": "20261012_ролик"}))  # освободился — можно


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg_path = root / "config.json"
        self.legacy = root / "legacy.json"
        for p in (
            mock.patch.object(cfg_mod, "DATA_DIR", root),
            mock.patch.object(cfg_mod, "CONFIG_PATH", self.cfg_path),
            mock.patch.object(cfg_mod, "LEGACY_CONFIG", self.legacy),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_first_run_imports_legacy(self) -> None:
        self.legacy.write_text(json.dumps({"api_key": "llm", "lumean_api_key": "lum", "strict_sync": True}), encoding="utf-8")
        cfg = cfg_mod.load_config()
        self.assertEqual(cfg["llm_api_key"], "llm")
        self.assertEqual(cfg["tts"]["lumean"]["api_key"], "lum")
        self.assertEqual(cfg["media_mode"], "strict_3min")
        self.assertTrue(self.cfg_path.is_file())

    def test_broken_file_gives_defaults(self) -> None:
        self.cfg_path.write_text("{не json", encoding="utf-8")
        self.assertEqual(cfg_mod.load_config()["media_mode"], "theme")

    def test_dead_model_cleared(self) -> None:
        self.cfg_path.write_text(json.dumps({"llm_model": "gpt-5.4"}), encoding="utf-8")
        self.assertEqual(cfg_mod.load_config()["llm_model"], "")

    def test_self_heal_from_legacy_when_key_wiped(self) -> None:
        self.legacy.write_text(json.dumps({"lumean_api_key": "real", "lumean_voices": [{"id": "v", "name": "Голос"}]}), encoding="utf-8")
        # файл перезаписан старой версией: плоские пустые lumean_*
        self.cfg_path.write_text(json.dumps({"lumean_api_key": "", "lumean_voices": []}), encoding="utf-8")
        cfg = cfg_mod.load_config()
        self.assertEqual(cfg["tts"]["lumean"]["api_key"], "real")
        self.assertEqual(cfg["tts"]["lumean"]["voice"]["items"][0]["name"], "Голос")

    def test_save_is_atomic_and_merges(self) -> None:
        cfg_mod.save_config({"subtitles": True})
        cfg_mod.save_config({"overlay_text": "канал"})
        cfg = cfg_mod.load_config()
        self.assertTrue(cfg["subtitles"])
        self.assertEqual(cfg["overlay_text"], "канал")
        self.assertFalse((self.cfg_path.with_suffix(".tmp")).exists())


class ThrottleTests(unittest.TestCase):
    def test_heavy_stage_serializes(self) -> None:
        from engine.core.throttle import heavy_stage

        inside: list[int] = []
        peak = [0]
        lock = threading.Lock()

        def work() -> None:
            with heavy_stage("Т"):
                with lock:
                    inside.append(1)
                    peak[0] = max(peak[0], len(inside))
                time.sleep(0.1)
                with lock:
                    inside.pop()

        th = [threading.Thread(target=work) for _ in range(3)]
        [t.start() for t in th]
        [t.join() for t in th]
        self.assertEqual(peak[0], 1)

    def test_waiting_is_cancellable(self) -> None:
        from engine.core.throttle import heavy_stage

        hold = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with heavy_stage("A"):
                hold.set()
                release.wait(3)

        t = threading.Thread(target=holder)
        t.start()
        hold.wait(3)
        token = CancelToken()
        threading.Timer(0.2, token.cancel).start()
        with self.assertRaises(CancelledError):
            with heavy_stage("B", token):
                pass
        release.set()
        t.join()

    def test_whisper_lock_released_on_error(self) -> None:
        from engine.slides import whisper_timings as wt

        with mock.patch.object(wt, "_transcribe_words", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                wt.transcribe_words(Path("/nope.mp3"), language="")
        self.assertTrue(wt._transcribe_lock.acquire(blocking=False))
        wt._transcribe_lock.release()


class ThreadsContextTests(unittest.TestCase):
    def test_context_reaches_pool_threads(self) -> None:
        from engine.core.threads import ThreadPoolExecutor

        var: contextvars.ContextVar[str] = contextvars.ContextVar("v", default="нет")
        var.set("задача-1")
        with ThreadPoolExecutor(2) as ex:
            self.assertEqual(ex.submit(var.get).result(), "задача-1")
            self.assertEqual(list(ex.map(lambda _: var.get(), range(3))), ["задача-1"] * 3)


class KeepAwakeTests(unittest.TestCase):
    def test_hold_release_idempotent(self) -> None:
        from server.keepawake import KeepAwake

        k = KeepAwake()
        if k._bin is None:
            self.skipTest("caffeinate есть только на macOS")
        k.hold()
        first = k._proc
        k.hold()  # второй раз не плодит процессы
        self.assertIs(k._proc, first)
        self.assertTrue(k.active)
        k.release()
        self.assertFalse(k.active)
        k.release()  # повторно — без ошибок
        self.assertIsNotNone(first.poll())


class SingleInstanceTests(unittest.TestCase):
    def test_second_instance_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code = (
                "import sys; sys.path.insert(0, %r);"
                "from unittest import mock; import engine.core.config as c;"
                "c.DATA_DIR = __import__('pathlib').Path(%r);"
                "import desktop; h = desktop._single_instance(); print(h is not None, flush=True)"
            ) % (str(ROOT), tmp)
            first = subprocess.Popen(
                [sys.executable, "-c", code + "; import time; time.sleep(2)"],
                stdout=subprocess.PIPE, text=True, cwd=ROOT,
            )
            line = first.stdout.readline().strip()
            second = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT)
            first.wait(5)
            third = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(line, "True")
        self.assertEqual(second.stdout.strip(), "False", second.stderr[-300:])
        self.assertEqual(third.stdout.strip(), "True", "после закрытия первого — снова можно")


if __name__ == "__main__":
    unittest.main()
