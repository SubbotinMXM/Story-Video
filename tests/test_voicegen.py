"""VoiceGen: шаблоны из бота, озвучка без двойной оплаты, «Обновить» через API."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.core.cancel import CancelledError, CancelToken
from engine.tts import get_provider
from engine.tts import voicegen as vg

MP3 = b"ID3" + b"\0" * 4096

TEMPLATES = [
    {"id": "t1", "name": "Бабкины истории", "voice_id": "v1", "voice_display_name": "Cornelius",
     "voice_engine": "cartesia", "voice_engine_settings": {"speed": 1.1}, "settings_preset": "standard",
     "split_mode": "chunk", "chunk_size": 3000, "delay_between_chunks": 0.5},
    {"id": "t2", "name": "Спокойный", "voice_id": "v2", "voice_engine": "cartesia",
     "voice_engine_settings": {"speed": 100}, "settings_preset": "stable", "split_mode": "chunk",
     "chunk_size": 3000, "delay_between_chunks": 0.5},
]


class FakeVoiceGen:
    """Мини-сервер VoiceGen: задачи проходят queued → running → done (или error)."""

    def __init__(self, *, fail_first: bool = False) -> None:
        self.tasks: dict[str, dict] = {}
        self.by_client: dict[str, str] = {}
        self.posts: list[dict] = []
        self.fail_first = fail_first
        self.auth: set[str] = set()

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.auth.add(req.headers.get("authorization", ""))
        path = req.url.path.removeprefix("/api/v1/client")
        if path == "/templates":
            return httpx.Response(200, json=TEMPLATES)
        if path.startswith("/templates/"):
            tpl = next((t for t in TEMPLATES if t["id"] == path.rsplit("/", 1)[1]), None)
            return httpx.Response(200, json=tpl) if tpl else httpx.Response(404, json={"detail": "not found"})
        if path == "/me":
            return httpx.Response(200, json={"free_generation_remaining_chars": 5000, "has_active_subscription": True})
        if path == "/tasks" and req.method == "POST":
            body = json.loads(req.content)
            self.posts.append(body)
            cid = body["client_task_id"]
            if cid in self.by_client:  # идемпотентность по client_task_id
                return httpx.Response(200, json={"ok": True, "task": self.tasks[self.by_client[cid]]})
            tid = f"task{len(self.tasks) + 1}"
            status_seq = ["running", "error"] if self.fail_first and not self.tasks else ["running", "done"]
            self.tasks[tid] = {"task_id": tid, "status": "queued", "progress": 0, "_seq": status_seq}
            self.by_client[cid] = tid
            return httpx.Response(200, json={"ok": True, "task": self.tasks[tid]})
        if path.startswith("/tasks/") and path.endswith("/download"):
            return httpx.Response(200, content=MP3)
        if path.startswith("/tasks/"):
            task = self.tasks.get(path.split("/")[2])
            if task is None:
                return httpx.Response(404, json={"detail": "not found"})
            if task["_seq"]:
                task["status"] = task["_seq"].pop(0)
                task["progress"] = 100 if task["status"] == "done" else 50
                if task["status"] == "error":
                    task["error_message"] = "provider down"
            return httpx.Response(200, json={"ok": True, "task": task})
        return httpx.Response(404, json={"detail": "?"})


class VoiceGenTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeVoiceGen()
        self._use(self.fake)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _use(self, fake: FakeVoiceGen) -> None:
        p = mock.patch.object(vg, "_TRANSPORT", httpx.MockTransport(fake))
        p.start()
        self.addCleanup(p.stop)

    def synth(self, **kw) -> Path:
        return vg.synthesize_resumable(
            api_key="k", text="Текст рассказа", template_id="t1", dest=self.dir / "voice.mp3",
            state_file=self.dir / "tts_order.json", poll_sec=0.01, **kw,
        )


class ProviderTests(VoiceGenTestCase):
    def test_refresh_labels_and_keeps_selection(self) -> None:
        p = get_provider("voicegen")
        opts = p.normalize({"api_key": "k", "template": {"items": [{"id": "t2", "name": "старое"}, {"id": "gone"}], "selected": "t2"}})
        value = p.refreshed(opts, "template")
        self.assertEqual([i["id"] for i in value["items"]], ["t1", "t2"])
        self.assertEqual(value["items"][0]["name"], "Бабкины истории · Cornelius · 1.10×")
        self.assertEqual(value["items"][1]["name"], "Спокойный")  # 100% = обычная скорость, не пишем
        self.assertEqual(value["selected"], "t2")
        # выбранный шаблон удалён в боте → выбор переходит на первый
        opts["template"]["selected"] = "gone"
        self.assertEqual(p.refreshed(opts, "template")["selected"], "t1")

    def test_check_needs_key_and_templates(self) -> None:
        p = get_provider("voicegen")
        with self.assertRaisesRegex(ValueError, "API key"):
            p.check(p.normalize({}))
        with self.assertRaisesRegex(ValueError, "Обновить"):
            p.check(p.normalize({"api_key": "k"}))
        p.check(p.normalize({"api_key": "k", "template": {"items": [{"id": "t1", "name": "x"}]}}))

    def test_refresh_needs_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "API key"):
            get_provider("voicegen").refresh({}, "template")

    def test_ping(self) -> None:
        msg = get_provider("voicegen").ping({"api_key": "k"})
        self.assertIn("шаблонов: 2", msg)
        self.assertIn("Bearer k", self.fake.auth)


class SynthesizeTests(VoiceGenTestCase):
    def test_happy_path(self) -> None:
        out = self.synth()
        self.assertEqual(out.read_bytes(), MP3)
        self.assertFalse((self.dir / "tts_order.json").exists())
        body = self.fake.posts[0]
        # шаблон разворачивается в явные настройки: с template_id VoiceGen выключает тайминги
        self.assertNotIn("template_id", body)
        self.assertEqual(body["voice_id"], "v1")
        self.assertEqual(body["voice_engine_settings"], {"speed": 1.1})
        self.assertEqual(body["chunk_size"], 3000)
        self.assertNotIn("settings_preset", body, "пресет сбросил бы громкость и скорость шаблона")
        self.assertTrue(body["include_timestamps"])
        self.assertEqual(body["timestamps_granularity"], "word")
        self.assertEqual(body["result_format"], "single")

    def test_resume_does_not_create_second_task(self) -> None:
        self.fake.tasks["old"] = {"task_id": "old", "status": "running", "progress": 40, "_seq": ["done"]}
        import hashlib
        (self.dir / "tts_order.json").write_text(json.dumps({
            "provider": "voicegen", "client_task_id": "c", "task_id": "old",
            "text_sha": hashlib.sha256("Текст рассказа".encode()).hexdigest(), "template_id": "t1",
        }))
        self.synth()
        self.assertEqual(self.fake.posts, [])

    def test_lost_create_response_reuses_client_task_id(self) -> None:
        import hashlib
        (self.dir / "tts_order.json").write_text(json.dumps({
            "provider": "voicegen", "client_task_id": "same",
            "text_sha": hashlib.sha256("Текст рассказа".encode()).hexdigest(), "template_id": "t1",
        }))
        self.synth()
        self.assertEqual([b["client_task_id"] for b in self.fake.posts], ["same"])

    def test_other_template_starts_fresh(self) -> None:
        (self.dir / "tts_order.json").write_text(json.dumps({"provider": "voicegen", "task_id": "x", "template_id": "t2"}))
        self.synth()
        self.assertEqual(len(self.fake.posts), 1)

    def test_failed_task_is_recreated_once(self) -> None:
        fake = FakeVoiceGen(fail_first=True)
        self._use(fake)
        self.synth()
        self.assertEqual(len(fake.posts), 2)
        self.assertNotEqual(fake.posts[0]["client_task_id"], fake.posts[1]["client_task_id"])

    def test_cancel_keeps_task_for_resume(self) -> None:
        token = CancelToken()
        token.cancel()
        with self.assertRaises(CancelledError):
            self.synth(cancel=token)
        self.assertEqual(self.fake.posts, [])

    def test_percent_settings_become_multipliers(self) -> None:
        body = vg.task_payload("x", {"voice_id": "v", "voice_engine_settings": {"speed": 100, "volume": 200}}, "c")
        self.assertEqual(body["voice_engine_settings"], {"speed": 1.0, "volume": 2.0})

    def test_deleted_template(self) -> None:
        with self.assertRaisesRegex(vg.VoiceGenError, "Обновить"):
            vg.synthesize_resumable(api_key="k", text="t", template_id="gone", dest=self.dir / "v.mp3",
                                    state_file=self.dir / "s.json", poll_sec=0.01)

    def test_bad_key_message(self) -> None:
        self._use(lambda req: httpx.Response(401, json={"detail": "Invalid API key"}))
        with self.assertRaisesRegex(vg.VoiceGenError, "ключ VoiceGen неверный"):
            self.synth()


class RefreshApiTests(VoiceGenTestCase):
    def test_refresh_endpoint_saves_templates(self) -> None:
        from fastapi.testclient import TestClient

        from engine.core import config as cfg_mod
        from server import app as app_mod

        root = self.dir
        for p in (
            mock.patch.object(cfg_mod, "DATA_DIR", root),
            mock.patch.object(cfg_mod, "CONFIG_PATH", root / "config.json"),
            mock.patch.object(cfg_mod, "LEGACY_CONFIG", root / "no_legacy.json"),
        ):
            p.start()
            self.addCleanup(p.stop)
        client = TestClient(app_mod.app)
        r = client.post("/api/tts/voicegen/refresh/template")
        self.assertEqual(r.status_code, 400)
        self.assertIn("API key", r.json()["detail"])
        client.patch("/api/settings", json={"tts_provider": "voicegen", "tts": {"voicegen": {"api_key": "k"}}})
        r = client.post("/api/tts/voicegen/refresh/template")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 2)
        saved = json.loads((root / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["tts"]["voicegen"]["template"]["selected"], "t1")
        self.assertEqual(saved["tts"]["voicegen"]["api_key"], "k")
        self.assertEqual(client.post("/api/tts/voicegen/refresh/api_key").status_code, 400)
        self.assertEqual(client.post("/api/tts/nope/refresh/template").status_code, 400)


if __name__ == "__main__":
    unittest.main()
