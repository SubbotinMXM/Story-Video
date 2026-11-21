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

    def test_job_collects_tokens_of_its_own_llm_calls(self) -> None:
        from engine.core import usage
        from engine.slides.llm_client import _usage_line

        def resp(i, c, o):
            u = mock.MagicMock(prompt_tokens=i, completion_tokens=o)
            u.prompt_tokens_details.cached_tokens = c
            return mock.MagicMock(usage=u)

        def builder(kind, params, state):
            def run(job, progress, cancel):
                _usage_line(resp(1000, 400, 50), "rewrite_part_1")
                _usage_line(resp(2000, 0, 100), "rewrite_review")
                _usage_line(resp(500, 100, 10), "judge_pool")
                _usage_line(resp(500, 100, 10), "judge_8")
                return {}
            return "Токены", run

        m = jobs_mod.JobManager(builder)
        job = m.add(m.create("x", {}))
        _wait(lambda: job.status == jobs_mod.DONE)
        tokens = job.public()["tokens"]
        self.assertEqual((tokens["calls"], tokens["in"], tokens["cached"], tokens["out"]), (4, 4000, 600, 170))
        by = {r["name"]: r for r in tokens["by"]}
        self.assertEqual(by["Проверка кадров"]["calls"], 2)
        self.assertEqual(by["Рерайт"]["in"], 1000)
        self.assertEqual(by["Проверка рерайта"]["out"], 100, "проверка рерайта — отдельно от рерайта")
        usage.record("x", 1, 0, 1)  # вне задачи — тихо, без ошибок
        self.assertIsNone(usage.summary({}))

    def _drain(self, kinds: list[str], min_sec: float | None) -> list[tuple]:
        """Ставит задачи, ждёт пустую очередь, возвращает отправленные уведомления."""
        seen: list[tuple] = []

        gate = threading.Event()  # задачи стартуют, когда все уже в очереди (иначе первая успеет закончиться одна)

        def builder(kind, params, state):
            def run(job, progress, cancel):
                gate.wait(3)
                if kind == "bad":
                    raise RuntimeError("сломалось")
                return {}
            return f"Задача {kind}", run

        patches = [mock.patch("server.notify.notify", lambda title, text: seen.append((title, text))),
                   mock.patch.object(jobs_mod, "AUTO_RETRY_MIN", ())]
        if min_sec is not None:
            patches.append(mock.patch.object(jobs_mod.JobManager, "NOTICE_MIN_SEC", min_sec))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        m = jobs_mod.JobManager(builder)
        jobs = [m.add(m.create(k, {})) for k in kinds]
        gate.set()
        _wait(lambda: all(j.status in (jobs_mod.DONE, jobs_mod.FAILED) for j in jobs))
        time.sleep(0.2)
        return seen

    def test_notice_once_when_queue_drains(self) -> None:
        seen = self._drain(["a", "b", "bad"], min_sec=0.0)
        self.assertEqual(len(seen), 1, "одно уведомление на всю пачку")
        self.assertEqual(seen[0][0], "Story Video: Готово 2 из 3", "итог — по отчёту: сколько из скольких")
        self.assertIn("исправить автоматически", seen[0][1])

    def test_quick_jobs_do_not_notify(self) -> None:
        self.assertEqual(self._drain(["a", "b"], min_sec=None), [], "секундные задачи — без уведомления")

    def test_notice_texts_and_off_switch(self) -> None:
        from server import notify

        self.assertEqual(notify.queue_message(1, 0, "Ролик"), ("Story Video: готово", "Задача готова. Последняя: Ролик"))
        self.assertEqual(notify.queue_message(0, 2, "")[0], "Story Video: ошибка")
        with mock.patch.object(notify.subprocess, "run") as run:
            notify.notify("т", "х")  # в тестах STORY_VIDEO_NO_NOTIFY включён
            time.sleep(0.1)
        run.assert_not_called()

    def test_stop_feedback_is_not_overwritten_by_progress(self) -> None:
        """«Стоп» нажат, шаг ещё доделывается: прогресс не затирает «Останавливаю…» и видно флаг stopping."""
        in_step = threading.Event()
        proceed = threading.Event()

        def builder(kind, params, state):
            def run(job, progress, cancel):
                in_step.set()
                proceed.wait(3)
                progress(0.5, "Рендер 50%")  # код, который ещё не заметил «Стоп»
                cancel.check()
                return {}
            return "Шаг", run

        m = jobs_mod.JobManager(builder)
        job = m.add(m.create("x", {}))
        in_step.wait(3)
        self.assertFalse(job.public()["stopping"])
        self.assertIsNone(job.public()["stop_at"])
        m.cancel(job.id)
        self.assertTrue(job.public()["stopping"])
        self.assertLess(abs(job.public()["stop_at"] - time.time()), 5, "время нажатия «Стоп» — для счётчика секунд")
        proceed.set()
        _wait(lambda: job.status == jobs_mod.CANCELLED)
        self.assertNotIn("Рендер 50%", " ".join([job.message]))
        self.assertFalse(job.public()["stopping"], "остановлена — флаг снят")

    def test_stop_kills_ffmpeg_inside_a_job(self) -> None:
        """Настоящий долгий процесс в задаче умирает по «Стоп» сразу (токен задачи доходит до run_ffmpeg)."""
        from engine.slides import ffprobe_util

        started = threading.Event()

        def builder(kind, params, state):
            def run(job, progress, cancel):
                started.set()
                ffprobe_util.run_ffmpeg(["sleep", "30"], label="долгий")
                return {}
            return "Долгий", run

        m = jobs_mod.JobManager(builder)
        job = m.add(m.create("x", {}))
        started.wait(3)
        time.sleep(0.2)
        t0 = time.monotonic()
        m.cancel(job.id)
        _wait(lambda: job.status == jobs_mod.CANCELLED, timeout=5)
        self.assertLess(time.monotonic() - t0, 3)

    def test_scheduled_analytics_runs_right_after_current_upload(self) -> None:
        """Идёт заливка, в очереди ещё ролики; аналитика по расписанию встаёт перед ними, заливку не прерывает."""
        gate = threading.Event()
        order: list[str] = []

        def builder(kind, params, state):
            def run(job, p, c):
                order.append(params.get("n") or kind)
                if params.get("n") == "первая":
                    gate.wait(3)
                return {}
            return kind, run

        m = jobs_mod.JobManager(builder)
        first = m.add(m.create("publish", {"n": "первая"}))
        _wait(lambda: first.status == jobs_mod.RUNNING)
        m.add(m.create("publish", {"n": "вторая"}))
        m.add(m.create("publish", {"n": "третья"}))
        sync = m.add(m.create("analytics", {"auto": True}))
        m.move(sync.id, "top")
        self.assertEqual(order, ["первая"], "идущая заливка не тронута")
        gate.set()
        _wait(lambda: len(order) == 4)
        self.assertEqual(order, ["первая", "analytics", "вторая", "третья"])

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

    def test_upload_lane_runs_beside_render(self) -> None:
        """Выкладка не ждёт монтажа (и монтаж — выкладки): у неё своя полоса, по одной задаче."""
        gate = threading.Event()
        started: list[str] = []

        def builder(kind, params, state):
            def run(job, p, c):
                started.append(kind)
                gate.wait(3)
                return {}
            return kind, run

        with mock.patch.object(jobs_mod, "MAX_PARALLEL", 1):
            m = jobs_mod.JobManager(builder)
            m.add(m.create("broll", {}))
            m.add(m.create("publish", {}))
            m.add(m.create("publish", {}))
            _wait(lambda: len(started) == 2)
            time.sleep(0.2)
            snapshot = list(started)
            gate.set()
            _wait(lambda: len(started) == 3)
        self.assertEqual(sorted(snapshot), ["broll", "publish"], "вторая выкладка ждёт первую")

    def test_retry_at_from_exception_requeues_for_later(self) -> None:
        class Limit(RuntimeError):
            retry_at = time.time() + 3600

        def builder(kind, params, state):
            def run(job, p, c):
                raise Limit("лимит загрузок YouTube — продолжу позже")
            return kind, run

        m = jobs_mod.JobManager(builder)
        job = m.add(m.create("publish", {}))
        _wait(lambda: job.state.get("retry_at") is not None and job.status == jobs_mod.QUEUED)
        self.assertGreater(job.state["retry_at"], time.time() + 3000)
        self.assertIn("лимит", job.message)
        m.cancel(job.id)

    def test_move_changes_which_queued_job_runs_next(self) -> None:
        """Приоритет: поднятая задача берётся следующей; работающую не двигаем."""
        gate = threading.Event()
        ran: list[str] = []

        def builder(kind, params, state):
            def run(job, p, c):
                ran.append(kind)
                gate.wait(3)
                return {}
            return kind, run

        with mock.patch.object(jobs_mod, "MAX_PARALLEL", 1):
            m = jobs_mod.JobManager(builder)
            first = m.add(m.create("a", {}))
            _wait(lambda: first.status == jobs_mod.RUNNING)
            m.add(m.create("b", {}))
            c = m.add(m.create("c", {}))
            d = m.add(m.create("d", {}))
            with self.assertRaisesRegex(ValueError, "ждут"):
                m.move(first.id, "down")
            self.assertTrue(m.move(d.id, "top"))
            self.assertTrue(m.move(c.id, "up"))  # d, c, b
            self.assertTrue(m.move(c.id, "top"))  # c, d, b
            self.assertFalse(m.move(c.id, "up"), "и так первая")
            queued = [j["title"] for j in m.list() if j["status"] == jobs_mod.QUEUED]
            gate.set()
            _wait(lambda: len(ran) == 4)
        self.assertEqual(ran, ["a", "c", "d", "b"])
        self.assertEqual(queued, ["c", "d", "b"])

    def test_follow_up_goes_right_after_its_parent(self) -> None:
        """Рерайт ссылки 1 готов → её ролик встаёт сразу за ней, впереди рерайтов ссылок 2 и 3."""
        gate = threading.Event()

        def builder(kind, params, state):
            return kind, lambda job, p, c: gate.wait(3) and {}

        with mock.patch.object(jobs_mod, "MAX_PARALLEL", 1):
            m = jobs_mod.JobManager(builder)
            first = m.add(m.create("rewrite-1", {}))
            m.add(m.create("rewrite-2", {}))
            m.add(m.create("rewrite-3", {}))
            m.add(m.create("video-1", {}), after=first.id)
            order = [j["title"] for j in m.list()][-4:]
            gate.set()
        self.assertEqual(order, ["rewrite-1", "video-1", "rewrite-2", "rewrite-3"])

    def test_transient_failure_is_retried_automatically(self) -> None:
        """Обрыв сети / 522 у VoiceGen: задача сама встаёт в очередь с паузой и продолжает с места падения."""
        calls = []

        def builder(kind, params, state):
            def run(job, p, c):
                calls.append(dict(job.state))
                if len(calls) == 1:
                    job.save_state(step="озвучка готова")
                    raise RuntimeError("VoiceGen 522: Cloudflare could not establish a TCP connection")
                return {"summary": "ок"}
            return "Ролик", run

        with mock.patch.object(jobs_mod, "AUTO_RETRY_MIN", (0.0005,)):  # ~0.03 с вместо минут
            m = jobs_mod.JobManager(builder)
            job = m.add(m.create("x", {}))
            _wait(lambda: job.status == jobs_mod.DONE, timeout=5)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].get("step"), "озвучка готова", "повтор — с места падения")

    def test_settings_error_is_not_retried(self) -> None:
        def builder(kind, params, state):
            def run(job, p, c):
                raise ValueError("Укажи папку футажей")
            return "Ролик", run

        m = jobs_mod.JobManager(builder)
        job = m.add(m.create("x", {}))
        _wait(lambda: job.status == jobs_mod.FAILED)
        time.sleep(0.2)
        self.assertEqual(job.status, jobs_mod.FAILED, "ошибку настроек повтор не исправит — ждём человека")

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

    def test_builtin_rewrite_engine_becomes_claude(self) -> None:
        """Встроенная нейросеть — только запасная: сохранённый выбор «llm» становится Claude, Codex остаётся."""
        self.cfg_path.write_text(json.dumps({"rewrite_engine": "llm"}), encoding="utf-8")
        self.assertEqual(cfg_mod.load_config()["rewrite_engine"], "claude_code")
        self.assertEqual(cfg_mod.save_config({"rewrite_engine": "codex"})["rewrite_engine"], "codex")
        self.assertEqual(cfg_mod.load_config()["rewrite_engine"], "codex")

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
        self.assertIsNotNone(k._activity, "App Nap выключен на время работы")
        k.hold()  # второй раз не плодит процессы
        self.assertIs(k._proc, first)
        self.assertTrue(k.active)
        k.release()
        self.assertFalse(k.active)
        self.assertIsNone(k._activity, "очередь пуста — App Nap снова разрешён")
        k.release()  # повторно — без ошибок
        self.assertIsNotNone(first.poll())

    def test_schedule_keeps_mac_awake_between_jobs(self) -> None:
        """18.11: очередь опустела в 04:08 — Mac уснул, VPN порвался, аналитика по расписанию в 06:02 без сети.
        Пока включено расписание, конец работы очереди сон не разрешает."""
        from server.keepawake import KeepAwake

        k = KeepAwake()
        if k._bin is None:
            self.skipTest("caffeinate есть только на macOS")
        k.hold("schedule")
        k.hold()  # пришла задача
        k.release()  # очередь пуста
        self.assertTrue(k.active, "расписание включено — не спим")
        k.release("schedule")  # расписание выключили
        self.assertFalse(k.active)


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


class RewriteModelTest(unittest.TestCase):
    def test_own_rewrite_model_or_common(self) -> None:
        from server.runners import _rewrite_llm

        base = {"llm_api_key": "k", "llm_base_url": "https://x/v1", "llm_model": "gpt-a"}
        self.assertEqual(_rewrite_llm(base)[2], "gpt-a", "пусто — как в разделе LLM")
        self.assertEqual(_rewrite_llm({**base, "rewrite_llm_model": " gpt-b "})[2], "gpt-b")
        self.assertIsNone(_rewrite_llm({"rewrite_llm_model": "gpt-b"}), "без ключа — нет нейросети")


class AnalyticsGroupsConfigTest(unittest.TestCase):
    def test_groups_and_view_normalized(self):
        from engine.core.config import _normalize

        cfg = _normalize({"analytics_groups": [
            {"id": "g1", "name": " Рассказы ", "channels": ["UC1", "UC1", "", "UC2"]},
            {"id": "g1", "name": "дубль"}, {"id": "", "name": "без id"}, {"id": "g2", "name": ""}, "мусор",
        ], "analytics_view": {"sort": "subscribers", "лишнее": 1}})
        self.assertEqual(cfg["analytics_groups"], [{"id": "g1", "name": "Рассказы", "channels": ["UC1", "UC2"]}])
        self.assertEqual(cfg["analytics_view"], {"group": "", "profile": "", "sort": "subscribers", "dir": "desc"})
        kept = _normalize({"analytics_view": {"profile": "p12", "group": "g1"}})["analytics_view"]
        self.assertEqual((kept["profile"], kept["group"]), ("p12", "g1"), "фильтр по профилю сохраняется")
        multi = _normalize({"analytics_view": {"profile": "p12,p3", "group": "g1,g2,none"}})["analytics_view"]
        self.assertEqual((multi["profile"], multi["group"]), ("p12,p3", "g1,g2,none"), "можно выбрать несколько групп и профилей")
        self.assertEqual(_normalize({})["analytics_groups"], [])
