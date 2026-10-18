"""Продолжение с места падения: озвучка без повторной оплаты, строгий подбор, очередь."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import shutil
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

from engine.core import lumean
from engine.core.lumean import LumeanError, LumeanOrderFailed, synthesize_resumable


class FakeLumean:
    """Подмена LumeanClient: считает заказы, умеет «падать» на ожидании."""

    created = 0
    fail_wait = False
    fail_status = False

    def __init__(self, api_key: str) -> None:
        self._last_tts_speed = 1.0
        self._last_tts_model = ""

    def close(self) -> None:
        pass

    def resolve_template_id(self, tid: str) -> str:
        return tid

    def create_tts_order(self, **kw) -> str:
        FakeLumean.created += 1
        self._last_tts_speed = 1.1
        self._last_tts_model = "eleven_v3"
        return f"order{FakeLumean.created}"

    def wait_order(self, order_id: str, **kw) -> dict:
        if FakeLumean.fail_status:
            FakeLumean.fail_status = False
            raise LumeanOrderFailed("Заказ завершился со статусом failed")
        if FakeLumean.fail_wait:
            raise LumeanError("Таймаут ожидания заказа")
        return {"id": order_id}

    def download_order_audio(self, order: dict, dest: Path) -> Path:
        # скорость/модель должны быть восстановлены из сохранённого заказа
        assert self._last_tts_speed == 1.1 and self._last_tts_model == "eleven_v3"
        dest.write_bytes(b"x" * 5000)
        return dest


class SynthesizeResumableTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeLumean.created = 0
        FakeLumean.fail_wait = False
        FakeLumean.fail_status = False
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        patcher = mock.patch.object(lumean, "LumeanClient", FakeLumean)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def _call(self, text: str = "привет") -> Path:
        return synthesize_resumable(
            api_key="k",
            text=text,
            template_id="t",
            voice_id="v",
            speed=1.1,
            dest=self.dir / "voice.mp3",
            state_file=self.dir / "tts_order.json",
        )

    def test_retry_reuses_order_no_double_payment(self) -> None:
        FakeLumean.fail_wait = True
        with self.assertRaises(LumeanError):
            self._call()
        self.assertEqual(FakeLumean.created, 1)
        self.assertTrue((self.dir / "tts_order.json").is_file())
        self.assertFalse((self.dir / "voice.mp3").exists())

        FakeLumean.fail_wait = False
        self._call()
        self.assertEqual(FakeLumean.created, 1, "второй заказ создан — двойная оплата")
        self.assertTrue((self.dir / "voice.mp3").is_file())
        self.assertFalse((self.dir / "tts_order.json").exists())

    def test_failed_order_is_replaced(self) -> None:
        (self.dir / "tts_order.json").write_text(
            json.dumps({"order_id": "old", "text_sha": _sha("привет"), "voice_id": "v", "speed": 1.1, "model": "eleven_v3"})
        )
        FakeLumean.fail_status = True
        self._call()
        self.assertEqual(FakeLumean.created, 1)

    def test_changed_text_makes_new_order(self) -> None:
        (self.dir / "tts_order.json").write_text(
            json.dumps({"order_id": "old", "text_sha": _sha("другой"), "voice_id": "v"})
        )
        self._call()
        self.assertEqual(FakeLumean.created, 1)


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class StrictProgressTests(unittest.TestCase):
    def test_roundtrip_keeps_only_existing_files(self) -> None:
        from engine.slides.strict_media import Beat, _load_progress, _save_progress

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "pool").mkdir()
            (run / "pool" / "beat_0000_youtube.mp4").write_bytes(b"v")
            beats = [Beat(0, 6, "раз"), Beat(6, 12, "два"), Beat(12, 18, "три")]
            slots = [
                {"path": "pool/beat_0000_youtube.mp4"},
                {"path": "pool/beat_0001_youtube.mp4"},  # файла нет
                None,
            ]
            _save_progress(run, beats, ["q1", "q2", "q3"], slots, "3min")
            got = _load_progress(run, beats, "3min")
            assert got is not None
            self.assertEqual(got["queries"], ["q1", "q2", "q3"])
            self.assertEqual(list(got["slots"]), [0])
            # другие тайминги — прошлый прогресс не подходит
            self.assertIsNone(_load_progress(run, beats[:2], "3min"))
            # прогресс «первые 3 мин» не подходит для режима «весь ролик»
            self.assertIsNone(_load_progress(run, beats, "full"))


class JobManagerResumeTests(unittest.TestCase):
    def test_fail_persist_resume_reset(self) -> None:
        from server import jobs as jobs_mod

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work = tmp_path / "work"
            calls: list[dict] = []

            def builder(kind, params, state):
                def run(job, progress, cancel):
                    calls.append(dict(job.state))
                    if "work_dir" not in job.state:
                        work.mkdir()
                        job.save_state(work_dir=str(work), owned_dir=str(work))
                    if len(calls) == 1:
                        raise ValueError("ключ протух")
                    return {"summary": "ok"}

                return "Тест", run

            with mock.patch.object(jobs_mod, "JOBS_FILE", tmp_path / "jobs.json"), mock.patch.object(
                jobs_mod, "DATA_DIR", tmp_path
            ):
                m = jobs_mod.JobManager(builder)
                job = m.add(m.create("broll", {"text": "t"}))
                _wait(lambda: job.status == jobs_mod.FAILED)
                self.assertEqual(job.error, "ключ протух")
                self.assertTrue(job.public()["resumable"])

                # после «перезапуска приложения» задача на месте, с состоянием
                m2 = jobs_mod.JobManager(builder)
                again = m2._find(job.id)
                assert again is not None
                self.assertEqual(again.status, jobs_mod.FAILED)
                self.assertEqual(again.state["work_dir"], str(work))

                m2.resume(job.id)
                _wait(lambda: again.status == jobs_mod.DONE)
                self.assertEqual(calls[-1]["work_dir"], str(work), "продолжение не получило рабочую папку")
                self.assertEqual(again.attempts, 2)

            with mock.patch.object(jobs_mod, "JOBS_FILE", tmp_path / "jobs2.json"), mock.patch.object(
                jobs_mod, "DATA_DIR", tmp_path
            ):
                calls.clear()
                shutil.rmtree(work)
                work_b = work
                m3 = jobs_mod.JobManager(builder)
                j = m3.add(m3.create("broll", {}))
                _wait(lambda: j.status == jobs_mod.FAILED)
                self.assertTrue(work_b.is_dir())
                m3.reset(j.id)
                self.assertFalse(work_b.exists(), "сброс не удалил незаконченную папку")
                self.assertIsNone(m3._find(j.id))


class ParallelJobsTests(unittest.TestCase):
    def test_two_run_together_third_waits_and_logs_are_attributed(self) -> None:
        from engine.core.logutil import log
        from engine.core.threads import ThreadPoolExecutor
        from server import jobs as jobs_mod
        from server.events import hub

        running_now: list[int] = []
        peak = [0]
        lock = threading.Lock()
        gate = threading.Event()

        def builder(kind, params, state):
            def run(job, progress, cancel):
                with lock:
                    running_now.append(1)
                    peak[0] = max(peak[0], len(running_now))
                # строка из фонового потока должна попасть в журнал этой задачи
                with ThreadPoolExecutor(1) as ex:
                    ex.submit(log, f"marker {params['n']}").result()
                gate.wait(5)
                with lock:
                    running_now.pop()
                return {"summary": "ok"}

            return f"Ролик {params['n']}", run

        seen: list[dict] = []
        orig = hub.publish
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            jobs_mod, "JOBS_FILE", Path(tmp) / "jobs.json"
        ), mock.patch.object(jobs_mod, "DATA_DIR", Path(tmp)), mock.patch.object(
            hub, "publish", lambda kind, data: (seen.append({"kind": kind, **data}), orig(kind, data))
        ):
            m = jobs_mod.JobManager(builder)
            jobs = [m.add(m.create("broll", {"n": i})) for i in range(3)]
            _wait(lambda: peak[0] == 2)  # оба реально внутри работы одновременно
            self.assertEqual(jobs[2].status, jobs_mod.QUEUED, "третий должен ждать")
            gate.set()
            _wait(lambda: all(j.status == jobs_mod.DONE for j in jobs))
        self.assertEqual(peak[0], 2)
        for j in jobs:
            n = j.params["n"]
            marks = [e for e in seen if e["kind"] == "log" and f"marker {n}" in e["line"]]
            self.assertTrue(marks and all(e["job_id"] == j.id for e in marks), (n, marks))


def _wait(cond, timeout: float = 5.0) -> None:
    t0 = time.monotonic()
    while not cond():
        if time.monotonic() - t0 > timeout:
            raise AssertionError("не дождался")
        time.sleep(0.02)


if __name__ == "__main__":
    unittest.main()
