"""Перевод озвучки VoiceGen: язык оригинала, заказ без двойной оплаты, ролики на языках."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.core.langdetect import detect_language
from engine.slides.ffmpeg_util import find_ffmpeg
from engine.slides.ffprobe_util import probe_duration
from engine.tts import get_provider
from engine.tts import voicegen as vg
from engine.tts import voicegen_dub as dub
from engine.tts.base import DubTrack

MP3 = b"ID3" + b"\0" * 4096
FF = find_ffmpeg()


def ff(*args: str) -> None:
    subprocess.run([FF, "-v", "error", "-y", *args], check=True)


class LanguageTests(unittest.TestCase):
    SAMPLES = {
        "ru": "Жил-был старик, и он каждый день ходил на реку, что была за деревней. Это было давно.",
        "en": "The old man went to the river every day, and he was happy with his life. It was long ago.",
        "es": "El viejo iba al río todos los días y era feliz con su vida. Fue hace mucho tiempo, pero lo recuerdo.",
        "de": "Der alte Mann ging jeden Tag zum Fluss, und er war mit seinem Leben zufrieden. Es ist lange her.",
        "fr": "Le vieil homme allait à la rivière chaque jour et il était heureux avec sa vie. Cela fait longtemps.",
        "pl": "Stary człowiek chodził nad rzekę każdego dnia i był zadowolony z życia. To było dawno temu, ale jest.",
        "it": "Il vecchio andava al fiume ogni giorno ed era felice della sua vita. È stato tanto tempo fa, ma non per me.",
        "ro": "Bătrânul mergea la râu în fiecare zi și era mulțumit de viața lui. A fost demult, dar nu este uitat.",
    }

    def test_all_offered_languages(self) -> None:
        for lang, text in self.SAMPLES.items():
            self.assertEqual(detect_language(text), lang, text)

    def test_unsure(self) -> None:
        self.assertIsNone(detect_language("Hola"))
        self.assertIsNone(detect_language("Старий чоловік щодня ходив до річки і був щасливий у своєму житті."))

    def test_source_language_prefers_whisper(self) -> None:
        from engine.slides.dub_stage import source_language

        self.assertEqual(source_language("es", self.SAMPLES["ru"]), "es")
        self.assertEqual(source_language("", self.SAMPLES["ru"]), "ru")
        self.assertEqual(source_language("", "???"), "auto")

    def test_field_options_and_normalize(self) -> None:
        p = get_provider("voicegen")
        spec = {f["key"]: f for f in p.describe()["fields"]}["translate"]
        self.assertEqual([o[0] for o in spec["options"]], ["ru", "en", "es", "de", "fr", "pl", "it", "ro"])
        self.assertEqual(p.dub_languages({"translate": ["ro", "zz", "en", "en"]}), ["en", "ro"])
        self.assertEqual(get_provider("lumean").dub_languages({"translate": ["en"]}), [])


class FakeDub:
    """VoiceGen с дубляжем: языки готовятся по одному за опрос."""

    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.resumes = 0
        self.status = "running"
        self.tracks: dict[str, str] = {}
        self.plan: list[tuple[str, str]] = []  # (язык, статус) — по одному шагу на опрос
        self.error = ""

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1/client")
        if path.startswith("/templates/"):
            return httpx.Response(200, json={"id": "t1", "name": "x", "voice_id": "v", "model_id": "sonic-preview",
                                             "voice_engine_settings": {"speed": 110, "volume": 1.2}})
        if path == "/dubbing" and req.method == "POST":
            body = json.loads(req.content)
            self.posts.append(body)
            self.tracks = {t["language"]: "queued" for t in body["target_languages"]}
            return httpx.Response(202, json={"ok": True, "job": self._job()})
        if path.endswith("/resume"):
            self.resumes += 1
            self.status = "running"
            return httpx.Response(200, json={"ok": True, "job": self._job()})
        if "/tracks/" in path:
            kind = path.rsplit("/", 1)[1]
            body = {"audio": MP3, "text": "Hello world. Bye.".encode(), "srt": b"1\n00:00:00,000 --> 00:00:01,000\nHello\n",
                    "timings": json.dumps({"words": [{"word": "Hello", "start": 0.1, "end": 0.5}, {"word": "world.", "start": 0.6, "end": 1.0}]}).encode()}[kind]
            return httpx.Response(200, content=body)
        if path.startswith("/dubbing/"):
            if self.plan:
                lang, st = self.plan.pop(0)
                if lang == "*":
                    self.status = st
                else:
                    self.tracks[lang] = st
            return httpx.Response(200, json={"ok": True, "job": self._job()})
        return httpx.Response(404, json={"detail": "?"})

    def _job(self) -> dict:
        tracks = [{"language": "ru", "is_original": True, "status": "done"}]
        tracks += [{"language": k, "is_original": False, "status": v} for k, v in self.tracks.items()]
        return {"job_id": "11111111-2222-3333-4444-555555555555", "status": self.status, "tracks": tracks,
                "current_language": "en", "current_language_progress": 40, "error_message": self.error}


class DubFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeDub()
        p = mock.patch.object(vg, "_TRANSPORT", httpx.MockTransport(self.fake))
        p.start()
        self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        dub.write_source(self.dir, task_id="task-1", template_id="t1", text_chars=1000)

    def start(self, targets, source="ru"):
        return dub.start("k", self.dir, source_language=source, targets=targets)

    def drain(self) -> list[DubTrack]:
        out = []
        for track in dub.iter_ready("k", self.dir, poll_sec=0.01):
            out.append(track)
            dub.mark_done(self.dir, track.language, f"/desk/{track.language}")
        return out

    def test_source_language_not_ordered_and_settings_from_template(self) -> None:
        self.assertEqual(self.start(["ru", "en", "es"]), ["en", "es"])
        body = self.fake.posts[0]
        self.assertEqual(body["source_task_id"], "task-1")
        self.assertEqual(body["source_language"], "ru")
        self.assertEqual([t["language"] for t in body["target_languages"]], ["en", "es"])
        self.assertEqual(body["model_id"], "sonic-3.6", "экспериментальная модель в дубляже недоступна")
        self.assertEqual((body["speed"], body["volume"]), (1.1, 1.2))
        self.assertNotIn("text", body, "оригинал не озвучивается и не оплачивается заново")

    def test_only_source_language_selected(self) -> None:
        self.assertEqual(self.start(["ru"]), [])
        self.assertEqual(self.fake.posts, [])

    def test_start_is_idempotent(self) -> None:
        self.start(["en"])
        self.assertEqual(self.start(["en", "de"]), ["en"], "повтор не создаёт второй заказ")
        self.assertEqual(len(self.fake.posts), 1)

    def test_no_source_or_too_old(self) -> None:
        (self.dir / dub.SOURCE_NAME).unlink()
        with self.assertRaises(dub.DubUnavailable):
            self.start(["en"])
        dub.write_source(self.dir, task_id="t", template_id="t1", text_chars=10)
        raw = json.loads((self.dir / dub.SOURCE_NAME).read_text())
        raw["created"] = time.time() - 73 * 3600
        (self.dir / dub.SOURCE_NAME).write_text(json.dumps(raw))
        with self.assertRaisesRegex(dub.DubUnavailable, "72"):
            self.start(["en"])

    def test_tracks_yielded_as_ready_with_assets(self) -> None:
        self.start(["en", "es"])
        self.fake.plan = [("en", "running"), ("en", "done"), ("es", "done"), ("*", "done")]
        tracks = self.drain()
        self.assertEqual([t.language for t in tracks], ["en", "es"])
        t = tracks[0]
        self.assertEqual(t.audio.read_bytes(), MP3)
        self.assertEqual(t.text.read_text(), "Hello world. Bye.")
        words = json.loads(t.timings.read_text())["words"]
        self.assertEqual([w["word"] for w in words], ["Hello", "world."])
        # «Продолжить»: готовые языки больше не скачиваются и не собираются
        self.assertEqual(self.drain(), [])

    def test_needs_attention_resumes_once_then_explains(self) -> None:
        self.start(["en"])
        self.fake.status = "needs_attention"
        self.fake.error = "insufficient balance"
        self.fake.plan = [("*", "needs_attention"), ("*", "needs_attention")]
        with self.assertRaisesRegex(vg.VoiceGenError, "insufficient balance.*Продолжить"):
            self.drain()
        self.assertEqual(self.fake.resumes, 1)

    def test_source_without_timings_is_warning(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/dubbing"):
                return httpx.Response(422, json={"detail": "Source needs complete subtitles; generate a new original with this endpoint"})
            return self.fake(req)

        with mock.patch.object(vg, "_TRANSPORT", httpx.MockTransport(handler)):
            with self.assertRaisesRegex(dub.DubUnavailable, "без таймингов"):
                self.start(["en"])

    def test_real_timings_format(self) -> None:
        raw = {"duration_seconds": 2.5, "words": [{"word": "Привет,", "start": 0.157, "end": 0.532}], "complete": True}
        self.assertEqual(dub.parse_timings(raw), [{"word": "Привет,", "start": 0.157, "end": 0.532}])

    def test_finished_without_language_is_error(self) -> None:
        self.start(["en"])
        self.fake.plan = [("en", "error"), ("*", "done")]
        with self.assertRaisesRegex(vg.VoiceGenError, "EN"):
            self.drain()

    def test_parse_timings_variants(self) -> None:
        self.assertEqual(dub.parse_timings([{"text": "a", "start_ms": 100, "end_ms": 300}]), [{"word": "a", "start": 0.1, "end": 0.3}])
        self.assertEqual(dub.parse_timings({"words": [{"word": "b", "start_time": 1, "end_time": 2}]})[0]["end"], 2.0)
        self.assertIsNone(dub.parse_timings({"weird": 1}))


class SynthesizeLeavesSourceTests(unittest.TestCase):
    def test_original_task_asks_timings_and_is_remembered(self) -> None:
        posts = []

        def handler(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if "/templates/" in path:
                return httpx.Response(200, json={"id": "t1", "name": "x", "voice_id": "v"})
            if path.endswith("/tasks") and req.method == "POST":
                posts.append(json.loads(req.content))
                return httpx.Response(200, json={"ok": True, "task": {"task_id": "abc", "status": "done"}})
            if path.endswith("/download"):
                return httpx.Response(200, content=MP3)
            return httpx.Response(200, json={"ok": True, "task": {"task_id": "abc", "status": "done"}})

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(vg, "_TRANSPORT", httpx.MockTransport(handler)):
            d = Path(tmp)
            vg.synthesize_resumable(api_key="k", text="Текст", template_id="t1", dest=d / "voice.mp3", state_file=d / "s.json", poll_sec=0.01)
            self.assertTrue(posts[0]["include_timestamps"], "без таймингов VoiceGen не переведёт озвучку")
            src = json.loads((d / dub.SOURCE_NAME).read_text())
            self.assertEqual((src["task_id"], src["template_id"]), ("abc", "t1"))


class DubRenderTests(unittest.TestCase):
    """Ролик на другом языке: тот же видеоряд, другой голос и субтитры, своя папка."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.run = root / "run"
        self.desk = root / "desktop"
        self.desk.mkdir()
        (self.run / "pool").mkdir(parents=True)
        for i in range(2):
            ff("-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration=4", str(self.run / "pool" / f"v{i}.mp4"))
        ff("-f", "lavfi", "-i", "sine=f=300:d=8", str(self.run / "voice.mp3"))
        slots = [{"kind": "video", "path": f"pool/v{i}.mp4", "start": i * 4.0, "end": i * 4.0 + 4, "duration": 4.0,
                  "provider": "x", "id": str(i)} for i in range(2)]
        (self.run / "timeline.json").write_text(json.dumps({"mode": "strict_sync", "slots": slots}), encoding="utf-8")
        (self.run / "script.txt").write_text("Оригинал.", encoding="utf-8")
        lang = self.run / dub.DUB_DIR / "en"
        lang.mkdir(parents=True)
        ff("-f", "lavfi", "-i", "sine=f=500:d=8", str(lang / "voice.mp3"))
        (lang / "script.txt").write_text("Hello world.", encoding="utf-8")
        (lang / "timings.json").write_text(json.dumps({"words": [{"word": "Hello", "start": 0.2, "end": 0.6}, {"word": "world.", "start": 0.7, "end": 1.2}]}))
        self.track = DubTrack(language="en", audio=lang / "voice.mp3", text=lang / "script.txt", timings=lang / "timings.json")
        for p in (mock.patch("engine.slides.publish.DESKTOP", self.desk), mock.patch("engine.slides.paths.DESKTOP", self.desk)):
            p.start()
            self.addCleanup(p.stop)

    def _req(self, **kw):
        from engine.slides.render_pipeline import RenderRequest

        return RenderRequest(run_dir=self.run, subtitles=True, copy_to_desktop=True, **kw)

    def test_dub_reuses_silent_video(self) -> None:
        from engine.slides import render_pipeline as rp
        from engine.slides.dub_stage import render_dub

        rp.run_render_stage(self._req(keep_silent=True))
        self.assertTrue((self.run / rp.SILENT_NAME).is_file())
        with mock.patch.object(rp, "_render_parallel", side_effect=AssertionError("кадры не пересобираются")):
            folder = render_dub(self._req(), self.track)
        self.assertTrue(folder.name.endswith("(EN)"), folder.name)
        self.assertAlmostEqual(probe_duration(folder / "ролик.mp4"), 8.0, delta=0.4)
        self.assertIn("Hello world.", (folder / "субтитры.srt").read_text(encoding="utf-8"))
        self.assertEqual((folder / "сценарий.txt").read_text(encoding="utf-8"), "Hello world.")
        self.assertFalse((self.track.audio.parent / "final.mp4").exists(), "копия ролика уже на Desktop")

    def test_three_languages_with_copies_and_strict_3min(self) -> None:
        """RU-оригинал + EN + DE, копии скорости: 3 папки по языкам, в каждой ролик и копия.

        Строгий ряд «первые 3 минуты» (начало по тексту + блок пула) рендерится один раз,
        переводы берут его без пересборки — порядок кадров у всех языков одинаковый.
        """
        from engine.slides import render_pipeline as rp
        from engine.slides.dub_stage import render_dub

        early = [{"kind": "video", "path": "pool/v0.mp4", "start": 0, "end": 3, "duration": 3.0, "provider": "x", "id": "a"}]
        block = [{"kind": "video", "path": "pool/v1.mp4", "start": 3, "end": 5, "duration": 2.0, "provider": "x", "id": "b"},
                 {"kind": "video", "path": "pool/v0.mp4", "start": 5, "end": 7, "duration": 2.0, "provider": "x", "id": "c"}]
        (self.run / "timeline.json").write_text(json.dumps(
            {"mode": "strict_sync", "variant": "3min", "slots": early, "loop_block": block, "loop_start": 3.0}), encoding="utf-8")
        de = self.run / dub.DUB_DIR / "de"
        de.mkdir(parents=True)
        ff("-f", "lavfi", "-i", "sine=f=700:d=8", str(de / "voice.mp3"))
        (de / "script.txt").write_text("Hallo Welt.", encoding="utf-8")
        de_track = DubTrack(language="de", audio=de / "voice.mp3", text=de / "script.txt")

        concats: list = []
        real = rp.concat_demuxer
        with mock.patch.object(rp, "concat_demuxer", lambda parts, dest: (concats.append([p.name for p in parts]), real(parts, dest))[1]):
            res = rp.run_render_stage(self._req(keep_silent=True, speed_copies=1, folder_language="ru"))
            folders = [res.desktop_video]
            for track in (self.track, de_track):
                folders.append(render_dub(self._req(speed_copies=1), track))
        self.assertEqual(len(concats), 1, "видеоряд собран один раз на все языки")
        self.assertEqual(concats[0][0], "seg_00000.mp4", "начало по тексту — первым")
        self.assertEqual(concats[0][1:3] * 2, concats[0][1:5], "дальше повторяется блок пула")
        self.assertEqual([f.name[-4:] for f in folders], ["(RU)", "(EN)", "(DE)"])
        for f in folders:
            names = sorted(p.name for p in f.glob("*.mp4"))
            self.assertEqual(names, ["99 процентов ролик.mp4", "ролик.mp4"], f.name)
            self.assertAlmostEqual(probe_duration(f / "ролик.mp4"), 8.0, delta=0.4)
        self.assertEqual((folders[2] / "сценарий.txt").read_text(encoding="utf-8"), "Hallo Welt.")
        self.assertEqual((folders[0] / "сценарий.txt").read_text(encoding="utf-8"), "Оригинал.")

    def test_dub_rebuilds_silent_if_missing(self) -> None:
        from engine.slides.dub_stage import render_dub

        folder = render_dub(self._req(), self.track)
        self.assertAlmostEqual(probe_duration(folder / "ролик.mp4"), 8.0, delta=0.4)

    def test_run_dub_stage_marks_and_cleans(self) -> None:
        from engine.slides import dub_stage, render_pipeline as rp

        (self.run / rp.SILENT_NAME).write_bytes(b"x")
        fake = mock.Mock()
        fake.dub_languages.return_value = ["en"]
        fake.start_dub.return_value = ["en"]
        fake.iter_dubs.return_value = iter([self.track])
        fake.mark_dub_done.side_effect = lambda d, lang, res: dub.mark_done(d, lang, res)
        with mock.patch.object(dub_stage, "get_provider", return_value=fake), \
             mock.patch.object(dub_stage, "render_dub", return_value=Path("/desk/X (EN)")):
            lines = dub_stage.run_dub_stage("voicegen", {}, self._req(), language="ru")
        self.assertEqual(lines, ["Ролик EN: /desk/X (EN)"])
        self.assertFalse((self.run / rp.SILENT_NAME).exists(), "видеоряд без звука удалён после переводов")
        fake.start_dub.assert_called_once()

    def test_unavailable_is_warning_not_failure(self) -> None:
        from engine.slides import dub_stage

        fake = mock.Mock()
        fake.dub_languages.return_value = ["en"]
        fake.start_dub.side_effect = dub.DubUnavailable("нет озвучки VoiceGen")
        with mock.patch.object(dub_stage, "get_provider", return_value=fake):
            lines = dub_stage.run_dub_stage("voicegen", {}, self._req(), language="ru")
        self.assertIn("нет озвучки VoiceGen", lines[0])


if __name__ == "__main__":
    unittest.main()


class BrollDubTests(unittest.TestCase):
    """Футажи: на каждый перевод — свой ролик; повторный запуск не начинает язык заново."""

    def test_each_language_gets_own_video(self) -> None:
        from server import runners
        from engine.broll.pipeline import BrollRequest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main = root / "main"
            (main / dub.DUB_DIR / "en").mkdir(parents=True)
            audio = main / dub.DUB_DIR / "en" / "voice.mp3"
            audio.write_bytes(MP3)
            text = main / dub.DUB_DIR / "en" / "script.txt"
            text.write_text("Hello.", encoding="utf-8")
            fake = mock.Mock()
            fake.iter_dubs.return_value = iter([DubTrack(language="en", audio=audio, text=text)])
            fake.mark_dub_done.side_effect = lambda d, lang, res: dub.mark_done(d, lang, res)
            calls = []

            def fake_broll(req, cancel=None, on_progress=None):
                calls.append(req)
                return mock.Mock(output_dir=root / "Рассказ (EN)")

            job = mock.Mock(state={})
            job.save_state.side_effect = lambda **kw: job.state.update(kw)
            (main / dub.STATE_NAME).write_text(json.dumps({"job_id": "j", "targets": ["en"], "localized": {}}))
            with mock.patch.object(runners, "get_provider", return_value=fake), \
                 mock.patch.object(runners, "start_dub", return_value=(["en"], "")), \
                 mock.patch.object(runners, "run_output_dir", lambda title: root / title), \
                 mock.patch.object(runners, "run_broll", fake_broll):
                (root / "Рассказ (EN)").mkdir()
                req = BrollRequest(broll_dir=root, story_text="Рассказ по-русски.", tts_provider="voicegen")
                lines = runners._broll_dubs(job, req, main, "Рассказ", cancel=mock.Mock(), progress=lambda p, m: None)
            self.assertEqual(lines, [f"Ролик EN: {root / 'Рассказ (EN)'}"])
            sub = calls[0]
            self.assertEqual((sub.source_audio, sub.story_text, sub.language), (audio, "Hello.", "en"))
            self.assertEqual(sub.title_hint, "Рассказ (EN)")
            self.assertEqual(job.state["dub_work"], {"en": str(root / "Рассказ (EN)")})
            self.assertFalse((main / dub.DUB_DIR).exists(), "служебные файлы перевода убраны")
            self.assertFalse((main / dub.STATE_NAME).exists())
