"""HTTP API: настройки, проверка входа задач, очередь, запуски, файлы, фронтенд.

Всё в отдельной временной папке: настройки/очередь/запуски пользователя не трогаются.
"""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from engine.core import config as cfg_mod
from server import app as app_mod
from server import jobs as jobs_mod
from server import runners as runners_mod


class ApiTestCase(unittest.TestCase):
    """Каждый тест — с чистыми настройками, очередью и папкой запусков."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        (root / "runs").mkdir()
        patches = [
            mock.patch.object(cfg_mod, "DATA_DIR", root),
            mock.patch.object(cfg_mod, "CONFIG_PATH", root / "config.json"),
            mock.patch.object(cfg_mod, "LEGACY_CONFIG", root / "no_legacy.json"),
            mock.patch.object(jobs_mod, "DATA_DIR", root),
            mock.patch.object(jobs_mod, "JOBS_FILE", root / "jobs.json"),
            mock.patch.object(app_mod, "RUNS_DIR", root / "runs"),
            mock.patch.object(runners_mod, "RUNS_DIR", root / "runs"),
            mock.patch("server.keepawake.KeepAwake.hold", lambda self: None),
            # «Рабочий стол» — тоже во временной папке
            mock.patch("engine.broll.output.desktop_dir", lambda: root / "desktop"),
            mock.patch("engine.slides.paths.DESKTOP", root / "desktop"),
            mock.patch("engine.slides.publish.DESKTOP", root / "desktop"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        # свой менеджер очереди на тест; перед очисткой — дождаться фоновых задач
        jobs_mod.manager = None
        self.addCleanup(setattr, jobs_mod, "manager", None)
        self.addCleanup(self._drain)
        self.client = TestClient(app_mod.app)

    def _drain(self) -> None:
        mgr = jobs_mod.manager
        for _ in range(250):
            if mgr is None or not mgr.busy():
                time.sleep(0.15)  # очередь дописывает jobs.json после смены статуса
                return
            time.sleep(0.02)

    def add_job(self, kind: str, params: dict) -> tuple[int, dict | list]:
        r = self.client.post("/api/jobs", json=[{"kind": kind, "params": params}])
        return r.status_code, r.json()


class MetaSettingsTests(ApiTestCase):
    def test_meta(self) -> None:
        m = self.client.get("/api/meta").json()
        self.assertEqual(m["llm_models"][0], "gpt-6-luna", "модели — от дешёвых к дорогим")
        self.assertEqual(m["llm_models"][-1], "gpt-6-astra")
        self.assertEqual(m["llm_default_model"], "", "по умолчанию — авто по цене")
        self.assertEqual([p["id"] for p in m["tts_providers"]], ["lumean", "voicegen"])
        fields = {f["key"]: f for f in m["tts_providers"][0]["fields"]}
        self.assertTrue(fields["template"]["hidden"])
        self.assertTrue(fields["voice"]["quick"] and fields["speed"]["quick"])
        self.assertEqual(m["max_copies"], 12)

    def test_settings_defaults_and_patch(self) -> None:
        s = self.client.get("/api/settings").json()
        self.assertEqual(s["media_mode"], "theme")
        self.assertEqual(s["tts_provider"], "lumean")
        self.assertIn("lumean", s["tts"])  # у сервиса полный набор полей
        self.assertEqual(s["tts"]["lumean"]["speed"], 1.0)
        s2 = self.client.patch("/api/settings", json={"subtitles": True, "media_mode": "strict_full"}).json()
        self.assertTrue(s2["subtitles"])
        self.assertEqual(s2["media_mode"], "strict_full")
        raw = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        self.assertTrue(raw["subtitles"])

    def test_unknown_setting_ignored(self) -> None:
        s = self.client.patch("/api/settings", json={"hack": 1}).json()
        self.assertNotIn("hack", s)

    def test_bad_media_mode_falls_back(self) -> None:
        s = self.client.patch("/api/settings", json={"media_mode": "wrong"}).json()
        self.assertEqual(s["media_mode"], "theme")


class JobValidationTests(ApiTestCase):
    """Ошибки входа должны отдаваться сразу (400), а не падать в очереди."""

    def setUp(self) -> None:
        super().setUp()
        self.broll = self.root / "broll"
        self.broll.mkdir()
        (self.broll / "f.mp4").write_bytes(b"")  # папка футажей с видео
        self.audio = self.root / "a.mp3"
        self.audio.write_bytes(b"x" * 2000)

    def test_unknown_kind(self) -> None:
        code, body = self.add_job("nope", {})
        self.assertEqual(code, 400)
        self.assertIn("Неизвестный тип", body["detail"])

    def test_broll_needs_text_or_audio(self) -> None:
        code, body = self.add_job("broll", {"text": ""})
        self.assertEqual(code, 400)
        self.assertIn("текст", body["detail"])

    def test_broll_needs_tts_key(self) -> None:
        code, body = self.add_job("broll", {"text": "рассказ"})
        self.assertEqual(code, 400)
        self.assertIn("API key", body["detail"])

    def test_broll_needs_folder(self) -> None:
        code, body = self.add_job("broll", {"text": "", "audio": str(self.audio)})
        self.assertEqual(code, 400)
        self.assertIn("папку футажей", body["detail"])

    def test_broll_missing_audio_file(self) -> None:
        code, body = self.add_job("broll", {"text": "", "audio": "/нет/файла.mp3"})
        self.assertEqual(code, 400)
        self.assertIn("не найден", body["detail"])

    def test_subscribe_without_file(self) -> None:
        self.client.patch("/api/settings", json={"broll_dir": str(self.broll), "subscribe": True})
        code, body = self.add_job("broll", {"text": "", "audio": str(self.audio)})
        self.assertEqual(code, 400)
        self.assertIn("подписаться", body["detail"])

    def test_copies_bounds(self) -> None:
        self.client.patch("/api/settings", json={"broll_dir": str(self.broll), "speed_copies": True, "speed_copies_count": 13})
        code, body = self.add_job("broll", {"text": "", "audio": str(self.audio)})
        self.assertEqual(code, 400)
        self.assertIn("от 1 до 12", body["detail"])

    def test_slides_need_llm_key(self) -> None:
        code, body = self.add_job("slides_full", {"text": "", "audio": str(self.audio)})
        self.assertEqual(code, 400)
        self.assertIn("LLM", body["detail"])

    def test_copies_job_validation(self) -> None:
        code, body = self.add_job("copies", {"video": str(self.audio), "count": 0})
        self.assertEqual(code, 400)
        code, body = self.add_job("copies", {"video": "", "count": 1})
        self.assertEqual(code, 400)

    def test_run_name_cannot_escape_runs_dir(self) -> None:
        code, body = self.add_job("slides_render", {"run": "../../etc"})
        self.assertEqual(code, 400)

    def test_batch_is_all_or_nothing(self) -> None:
        r = self.client.post(
            "/api/jobs",
            json=[{"kind": "copies", "video": "x", "params": {"video": str(self.audio), "count": 1}}, {"kind": "nope", "params": {}}],
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.get("/api/jobs").json(), [])

    def test_valid_broll_is_queued_and_text_hidden(self) -> None:
        self.client.patch("/api/settings", json={"broll_dir": str(self.broll)})
        with mock.patch.object(runners_mod, "run_broll", side_effect=RuntimeError("stop")):
            code, body = self.add_job("broll", {"text": "", "audio": str(self.audio), "source": "x.txt"})
            self.assertEqual(code, 200, body)
            # дождаться конца задачи, пока имитация ещё на месте
            for _ in range(200):
                if self.client.get("/api/jobs").json()[0]["status"] == "failed":
                    break
                time.sleep(0.02)
        self.assertNotIn("text", body[0]["params"])


class QueueApiTests(ApiTestCase):
    def _copies_job(self) -> str:
        video = self.root / "v.mp4"
        video.write_bytes(b"x" * 2000)
        code, body = self.add_job("copies", {"video": str(video), "count": 1})
        self.assertEqual(code, 200, body)
        return body[0]["id"]

    def _wait(self, job_id: str, status: str) -> dict:
        for _ in range(200):
            job = next((j for j in self.client.get("/api/jobs").json() if j["id"] == job_id), None)
            if job and job["status"] == status:
                return job
            time.sleep(0.05)
        self.fail(f"задача не дошла до {status}")

    def test_failed_job_resume_and_reset(self) -> None:
        with mock.patch.object(runners_mod, "write_speed_copies", side_effect=RuntimeError("ffmpeg упал")):
            jid = self._copies_job()
            job = self._wait(jid, "failed")
        self.assertTrue(job["resumable"])
        self.assertIn("ffmpeg упал", job["error"])
        with mock.patch.object(runners_mod, "write_speed_copies", return_value=[]):
            self.assertEqual(self.client.post(f"/api/jobs/{jid}/resume").status_code, 200)
            job = self._wait(jid, "done")
        self.assertEqual(job["attempts"], 2)
        # готовую задачу продолжить нельзя
        self.assertEqual(self.client.post(f"/api/jobs/{jid}/resume").status_code, 400)
        self.assertTrue(self.client.post(f"/api/jobs/{jid}/reset").json()["ok"])
        self.assertEqual(self.client.get("/api/jobs").json(), [])

    def test_clear_removes_only_done(self) -> None:
        with mock.patch.object(runners_mod, "write_speed_copies", return_value=[]):
            done = self._copies_job()
            self._wait(done, "done")
        with mock.patch.object(runners_mod, "write_speed_copies", side_effect=RuntimeError("x")):
            failed = self._copies_job()
            self._wait(failed, "failed")
        self.client.post("/api/jobs/clear")
        left = [j["id"] for j in self.client.get("/api/jobs").json()]
        self.assertEqual(left, [failed], "упавшая задача должна остаться — её можно продолжить")

    def test_unknown_job_ids(self) -> None:
        self.assertEqual(self.client.post("/api/jobs/nope/resume").status_code, 400)
        self.assertFalse(self.client.post("/api/jobs/nope/reset").json()["ok"])
        self.assertFalse(self.client.post("/api/jobs/nope/cancel").json()["ok"])


class RunsFilesSpaTests(ApiTestCase):
    def test_runs_listing(self) -> None:
        run = self.root / "runs" / "20261012_100000_тест"
        run.mkdir()
        (run / "script.txt").write_text("Текст   сценария\nвторая строка", encoding="utf-8")
        (run / "voice.mp3").write_bytes(b"x")
        runs = self.client.get("/api/runs").json()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["stage"], "tts_done")
        self.assertEqual(runs[0]["snippet"], "Текст сценария вторая строка")
        self.assertFalse(runs[0]["has_final"])

    def test_check_path(self) -> None:
        r = self.client.post("/api/check-path", json={"path": str(self.root)}).json()
        self.assertEqual(r, {"exists": True, "is_dir": True, "is_file": False})
        r = self.client.post("/api/check-path", json={"path": "   "}).json()
        self.assertFalse(r["exists"])

    def test_reveal_missing(self) -> None:
        self.assertEqual(self.client.post("/api/reveal", json={"path": "/нет/такого"}).status_code, 404)

    def test_spa_and_api_404(self) -> None:
        self.assertEqual(self.client.get("/api/nope").status_code, 404)
        r = self.client.get("/some/page")
        self.assertIn(r.status_code, (200, 503))  # 503 — если фронтенд не собран
        if r.status_code == 200:
            self.assertIn("<div id=\"root\">", r.text)

    def test_spa_does_not_serve_files_outside_dist(self) -> None:
        r = self.client.get("/../engine/core/config.py")
        self.assertNotIn("DEFAULTS", r.text)

    def test_ping_unknown_and_missing_key(self) -> None:
        self.assertEqual(self.client.post("/api/ping/nope").status_code, 404)
        r = self.client.post("/api/ping/tts:lumean").json()
        self.assertFalse(r["ok"])
        self.assertIn("API key", r["message"])
        r = self.client.post("/api/ping/llm").json()
        self.assertFalse(r["ok"])


class EventsTests(ApiTestCase):
    def test_snapshot_first(self) -> None:
        import asyncio

        from server.events import hub

        async def first_event() -> dict:
            req = mock.Mock()
            req.is_disconnected = mock.AsyncMock(return_value=False)
            resp = await app_mod.events(req)
            chunk = await resp.body_iterator.__anext__()
            await resp.body_iterator.aclose()
            return json.loads(chunk.removeprefix("data: ").strip())

        hub.logs.clear()
        hub.publish("log", {"line": "hello", "job_id": None})
        ev = asyncio.run(first_event())
        self.assertEqual(ev["type"], "snapshot")
        self.assertEqual(ev["jobs"], [])
        self.assertEqual(ev["logs"][-1]["line"], "hello")


if __name__ == "__main__":
    unittest.main()


class SwitchNoticeTest(unittest.TestCase):
    """Автосмена инструмента в задаче: в карточку (повтор — счётчиком) и одно событие на смену."""

    def test_switch_goes_to_job_card_and_event(self):
        from engine.core import switches
        from engine.core.logutil import current_job
        from server import jobs as jm

        mgr = jm.JobManager.__new__(jm.JobManager)
        import threading

        mgr._lock = threading.RLock()
        job = jm.Job(kind="rewrite", title="Рерайт: тест", params={}, runner=None)
        mgr._jobs = [job]
        mgr._last_emit = {}
        events = []
        with mock.patch.object(jm.hub, "publish", lambda kind, data: events.append((kind, data))), \
                mock.patch.object(mgr, "_save", lambda: None):
            switches.set_callback(mgr._on_switch)
            self.addCleanup(switches.set_callback, None)
            tok = current_job.set(job.id)
            try:
                switches.report("рерайт", "claude_code", "codex", "кончился лимит подписки")
                switches.report("рерайт", "claude_code", "codex", "кончился лимит подписки")
            finally:
                current_job.reset(tok)
        card = job.public()["switches"]
        self.assertEqual(len(card), 1)
        self.assertEqual(card[0]["times"], 2)
        self.assertEqual((card[0]["from"], card[0]["to"]), ("Claude", "Codex (ChatGPT)"))
        toasts = [d for k, d in events if k == "switch"]
        self.assertEqual(len(toasts), 1, "повтор той же смены — без второго уведомления")
        self.assertEqual(toasts[0]["title"], "Рерайт: тест")
