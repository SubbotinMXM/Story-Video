"""Настоящие ролики из крошечных тестовых файлов: ffmpeg, Whisper, рендер, сборка.

Внешние сервисы не нужны. Рабочий стол подменён временной папкой.
"""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.slides.ffmpeg_util import find_ffmpeg
from engine.slides.ffprobe_util import has_audio_stream, probe_duration, probe_video_size

FF = find_ffmpeg()
HAS_SAY = shutil.which("say") is not None


def ff(*args: str) -> None:
    subprocess.run([FF, "-v", "error", "-y", *args], check=True)


def make_clip(path: Path, src: str = "testsrc2", size: str = "640x360", sec: float = 4, audio: bool = False) -> Path:
    args = ["-f", "lavfi", "-i", f"{src}=size={size}:rate=30"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=f=440", "-shortest"]
    ff(*args, "-t", str(sec), "-pix_fmt", "yuv420p", str(path))
    return path


def make_green_sub(path: Path) -> Path:
    """Анимация подписки: зелёный фон, красная плашка видна только с 1 по 2 с."""
    ff(
        "-f", "lavfi", "-i", "color=c=0x60FB01:size=640x360:rate=30:d=3",
        "-f", "lavfi", "-i", "sine=f=880:d=3",
        "-vf", "drawbox=x=200:y=150:w=240:h=60:color=red:t=fill:enable='between(t,1,2)'",
        "-pix_fmt", "yuv420p", "-shortest", str(path),
    )
    return path


def make_voice(path: Path, text: str, voice: str = "Milena") -> Path:
    aiff = path.with_suffix(".aiff")
    subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
    ff("-i", str(aiff), str(path))
    return path


TEXT_RU = "Закройте глаза и представьте себе город. Огромный, шумный, живой. Небоскрёбы упираются в облака."


@unittest.skipUnless(HAS_SAY, "нужна macOS-утилита say для тестовой озвучки")
class BrollIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        clips = self.root / "clips"
        clips.mkdir()
        for i, src in enumerate(("testsrc2", "mandelbrot", "rgbtestsrc")):
            make_clip(clips / f"c{i}.mp4", src)
        for name in ("intro", "outro"):
            (self.root / name).mkdir()
            make_clip(self.root / name / "x.mp4", "smptebars", sec=2, audio=True)
        self.voice = make_voice(self.root / "voice.mp3", TEXT_RU)
        self.sub = make_green_sub(self.root / "sub.mp4")
        p = mock.patch("engine.broll.output.desktop_dir", lambda: self.root / "desk")
        p.start()
        self.addCleanup(p.stop)

    def _req(self, **kw):
        from engine.broll.pipeline import BrollRequest

        base = dict(
            broll_dir=self.root / "clips",
            story_text=TEXT_RU,
            source_audio=self.voice,
            title_hint="тест",
            subtitles=True,
            intro_dir=self.root / "intro",
            outro_dir=self.root / "outro",
            subscribe=True,
            subscribe_path=self.sub,
            speed_copies=1,
            work_dir=self.root / "desk" / "work",
            overlay_text="Канал",
        )
        base.update(kw)
        return BrollRequest(**base)

    def test_full_broll_video(self) -> None:
        from engine.broll.pipeline import run_broll

        with mock.patch("engine.broll.pipeline.burn_subtitles", side_effect=AssertionError("субтитры — в проходе сборки, без второго пережатия")):
            res = run_broll(self._req())
        names = sorted(p.name for p in res.output_dir.iterdir())
        self.assertIn("тест.mp4", names)
        self.assertIn("99 процентов тест.mp4", names)
        for junk in ("subs.srt", "timings.json", "script_origin.txt", ".progress.json", ".intro_sec"):
            self.assertNotIn(junk, names, "в папке только ролики, озвучка, текст и главы")
        voice = probe_duration(self.voice)
        total = probe_duration(res.video)
        self.assertAlmostEqual(total, voice + 4, delta=1.0)  # интро 2 + рассказ + аутро 2
        self.assertEqual(probe_video_size(res.video), (1280, 720), "футажи — не ниже 720p, вместе с интро и аутро")
        self.assertTrue(has_audio_stream(res.video))
        self.assertTrue(res.output_dir.name.startswith("тест_"))

    def test_eight_pieces_split_evenly(self) -> None:
        from engine.compositor import pipeline as cp

        calls = {}
        real = cp.pick_broll

        def spy(folder, rng):
            got = real(folder, rng)
            calls["n"] = len(got)
            return got

        with mock.patch.object(cp, "pick_broll", spy):
            from engine.broll.pipeline import run_broll

            run_broll(self._req(subtitles=False, subscribe=False, speed_copies=0, intro_dir=None, outro_dir=None))
        self.assertEqual(calls["n"], 8)

    def test_resume_skips_done_stages(self) -> None:
        from engine.broll import pipeline as bp

        with mock.patch.object(bp, "write_speed_copies", side_effect=RuntimeError("сбой копий")):
            with self.assertRaises(RuntimeError):
                bp.run_broll(self._req())
        stages = json.loads((self.root / "desk" / "work" / ".progress.json").read_text())
        # интро, аутро, подписка и субтитры — в одном проходе со сборкой
        self.assertTrue({"compose", "subs", "intro", "outro", "subscribe"} <= set(stages), stages)
        with mock.patch.object(bp, "build_and_run", side_effect=AssertionError("монтаж повторно")), \
             mock.patch.object(bp, "transcribe_words", side_effect=AssertionError("whisper повторно")):
            res = bp.run_broll(self._req())
        self.assertTrue(res.video.is_file())


@unittest.skipUnless(HAS_SAY, "нужна macOS-утилита say")
class BrollAudioOnlyResumeTests(unittest.TestCase):
    """Ролик из готового аудио без текста: продолжение после сбоя через сборщик задач."""

    def test_resume_audio_only_job(self) -> None:
        from engine.core import config as cfg_mod
        from server import runners

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clips").mkdir()
            make_clip(root / "clips" / "c.mp4")
            voice = make_voice(root / "моё аудио.mp3", TEXT_RU)
            cfg = dict(cfg_mod.DEFAULTS, broll_dir=str(root / "clips"))
            state: dict = {}
            job = mock.MagicMock()
            job.state = state
            job.save_state = lambda **kw: state.update(kw)
            with mock.patch("engine.broll.output.desktop_dir", lambda: root / "desk"), \
                 mock.patch.object(runners, "load_config", lambda: cfg):
                title, run = runners.build("broll", {"text": "", "audio": str(voice)}, state)
                with mock.patch("engine.broll.pipeline.build_and_run", side_effect=RuntimeError("сбой монтажа")):
                    with self.assertRaises(RuntimeError):
                        run(job, lambda p, m: None, None)
                voice.unlink()  # исходник могли убрать — озвучка уже в рабочей папке
                title2, run2 = runners.build("broll", {"text": "", "audio": str(voice)}, state)
                res = run2(job, lambda p, m: None, None)
            self.assertTrue(Path(res["video"]).is_file())
            self.assertEqual(title2, title)


class SlidesRenderIntegrationTests(unittest.TestCase):
    """Рендер слайдов из готового списка кадров + выгрузка папки ролика."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        run = self.root / "run"
        (run / "pool").mkdir(parents=True)
        self.run = run
        make_clip(run / "pool" / "beat_0000_youtube.mp4", "testsrc2", "1920x1080", 4)
        make_clip(run / "pool" / "beat_0001_pexels.mp4", "mandelbrot", "1280x720", 4)
        ff("-f", "lavfi", "-i", "smptebars=size=1200x800", "-frames:v", "1", str(run / "pool" / "beat_0002_google.jpg"))
        ff("-f", "lavfi", "-i", "sine=f=300:d=9", str(run / "voice.mp3"))
        (run / "script.txt").write_text("Один два три четыре пять шесть", encoding="utf-8")
        words = [{"word": w, "start": i * 1.4, "end": i * 1.4 + 1.2, "probability": 1} for i, w in enumerate("Один два три четыре пять шесть".split())]
        (run / "timings.json").write_text(json.dumps({"words": words, "language": "ru", "duration": 9}), encoding="utf-8")
        slots = [
            {"kind": "video", "path": "pool/beat_0000_youtube.mp4", "start": 0, "end": 3, "duration": 3, "provider": "youtube", "id": "abc", "source_title": "Клип", "source_author": "Автор"},
            {"kind": "video", "path": "pool/beat_0001_pexels.mp4", "start": 3, "end": 6, "duration": 3, "provider": "pexels", "id": "1"},
            {"kind": "image", "path": "pool/beat_0002_google.jpg", "start": 6, "end": 9, "duration": 3, "provider": "google", "id": "2"},
        ]
        (run / "timeline.json").write_text(json.dumps({"mode": "strict_sync", "variant": "full", "fade_sec": 0.5, "slots": slots}), encoding="utf-8")
        self.sub = make_green_sub(self.root / "sub.mp4")
        for p in (
            mock.patch("engine.slides.publish.DESKTOP", self.root / "desk"),
            mock.patch("engine.slides.paths.DESKTOP", self.root / "desk"),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_render_and_export(self) -> None:
        from engine.slides.render_pipeline import RenderRequest, run_render_stage

        res = run_render_stage(
            RenderRequest(run_dir=self.run, subtitles=True, subscribe=True, subscribe_path=self.sub, copy_to_desktop=True, seed=1)
        )
        self.assertAlmostEqual(res.duration, 9, delta=0.6)
        self.assertEqual(probe_video_size(res.video_file), (1920, 1080))
        folder = res.desktop_video
        self.assertIsNotNone(folder)
        names = sorted(p.name for p in folder.iterdir())
        self.assertEqual(names, ["главы.txt", "озвучка.mp3", "ролик.mp4", "сценарий.txt"], "субтитры и источники в папку не нужны")
        # ролик короткий — глав нет, но автора нарезки YouTube указать обязательно
        self.assertIn("Автор — «Клип» — https://youtu.be/abc", (folder / "главы.txt").read_text(encoding="utf-8"))
        credits = (self.run / "credits.txt").read_text(encoding="utf-8")
        self.assertIn("«Клип» — Автор — https://www.youtube.com/watch?v=abc", credits)
        self.assertFalse((self.run / "segments").exists(), "сегменты удалены")

    def test_theme_pool_render(self) -> None:
        from engine.slides.render_pipeline import RenderRequest, run_render_stage

        (self.run / "timeline.json").write_text(
            json.dumps(
                {
                    "mode": "theme_pool",
                    "image_hold_sec": 3,
                    "images": [{"path": "pool/beat_0002_google.jpg"}],
                    "videos": [{"path": "pool/beat_0001_pexels.mp4", "duration": 3}],
                }
            ),
            encoding="utf-8",
        )
        res = run_render_stage(RenderRequest(run_dir=self.run, copy_to_desktop=False, seed=1))
        self.assertAlmostEqual(res.duration, 9, delta=0.6)


class NoEffectsTests(unittest.TestCase):
    def test_footage_used_as_is(self) -> None:
        from engine.compositor import pipeline as cp

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "broll").mkdir()
            make_clip(root / "broll" / "a.mp4", "testsrc2", "640x360", 2)
            ff("-f", "lavfi", "-i", "sine=f=300:d=3", str(root / "a.mp3"))
            seen: list = []
            for seed in range(12):
                with mock.patch.object(cp, "_run_ffmpeg", lambda cmd, total, prog: seen.append(cmd[cmd.index("-filter_complex") + 1])):
                    cp.build_and_run(cp.ComposeRequest(audio=root / "a.mp3", broll_dir=root / "broll", head_dir=None, text="",
                                                       output=root / "o.mp4", seed=seed, subscribe=False, subscribe_path=None))
            for graph in seen:
                for fx in ("noise=", "vignette", "eq=", "gblur"):
                    self.assertNotIn(fx, graph, "никаких эффектов поверх футажей")


class OnePassFinishTests(unittest.TestCase):
    """Субтитры, интро, аутро и подписка — одним кодированием, звук не съезжает."""

    def _probe_streams(self, path: Path) -> dict:
        from engine.slides.ffmpeg_util import find_ffprobe

        out = subprocess.run([find_ffprobe(), "-v", "error", "-show_entries", "stream=codec_type,duration",
                              "-of", "json", str(path)], capture_output=True, text=True, check=True)
        return {s["codec_type"]: float(s["duration"]) for s in json.loads(out.stdout)["streams"]}

    def test_slides_finish_single_encode(self) -> None:
        from engine.slides import render_pipeline as rp
        from engine.slides import ffprobe_util as fu

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "intro").mkdir()
            (root / "outro").mkdir()
            make_clip(root / "intro" / "i.mp4", "testsrc2", "640x360", 2, audio=True)
            make_clip(root / "outro" / "o.mp4", "testsrc2", "854x480", 2)
            make_clip(root / "silent.mp4", "testsrc2", "1920x1080", 4)
            ff("-f", "lavfi", "-i", "sine=f=300:d=4", str(root / "voice.mp3"))
            (root / "subs.srt").write_text("1\n00:00:00,500 --> 00:00:02,000\nПривет\n", encoding="utf-8")
            sub = make_green_sub(root / "sub.mp4")
            encodes: list = []
            real = fu.run_ffmpeg

            def counting(cmd, label="ffmpeg"):
                if "libx264" in cmd:
                    encodes.append(label)
                return real(cmd, label=label)

            req = rp.RenderRequest(run_dir=root, subscribe=True, subscribe_path=sub, subtitles=True,
                                   intro_dir=root / "intro", outro_dir=root / "outro", seed=3)
            with mock.patch.object(rp, "run_ffmpeg", counting):
                out = rp.finish_video(req, silent=root / "silent.mp4", voice=root / "voice.mp3",
                                      srt_path=root / "subs.srt", dest=root / "final.mp4", progress=lambda p, m: None)
            self.assertEqual(len(encodes), 1, "весь ролик кодируется один раз")
            self.assertEqual(probe_video_size(out), (1920, 1080))
            streams = self._probe_streams(out)
            self.assertAlmostEqual(streams["video"], 8.0, delta=0.15)
            self.assertAlmostEqual(streams["audio"], streams["video"], delta=0.15, msg="звук и картинка одной длины")

    def test_mixed_color_range_does_not_cut_video(self) -> None:
        """Видео (yuv420p) и фото (yuvj420p) в одном ряду: ролик не обрывается на стыке.

        Раньше смена формата посреди видеоряда перезапускала граф единого прохода,
        и часовой ролик выходил длиной 2,5 минуты.
        """
        from engine.slides import render_pipeline as rp
        from engine.slides.ffprobe_util import concat_demuxer

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parts = []
            for i in range(6):
                seg = root / f"s{i}.mp4"
                if i == 5:  # фото уже после подписки — как в часовом ролике
                    ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:d=1", "-frames:v", "1", str(root / "p.jpg"))
                    ff("-loop", "1", "-i", str(root / "p.jpg"), "-t", "2", "-r", "30", "-pix_fmt", "yuvj420p",
                       "-color_range", "pc", "-colorspace", "bt470bg", "-s", "1280x720",
                       "-c:v", "libx264", "-video_track_timescale", "15360", str(seg))
                else:
                    ff("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:d=2", "-pix_fmt", "yuv420p",
                       "-c:v", "libx264", "-video_track_timescale", "15360", str(seg))
                parts.append(seg)
            concat_demuxer(parts, root / "silent.mp4")
            ff("-f", "lavfi", "-i", "sine=f=300:d=12", str(root / "voice.mp3"))
            (root / "subs.srt").write_text("1\n00:00:00,500 --> 00:00:09,000\nПривет\n", encoding="utf-8")
            req = rp.RenderRequest(run_dir=root, subscribe=True, subscribe_path=make_green_sub(root / "sub.mp4"), subtitles=True, seed=1)
            out = rp.finish_video(req, silent=root / "silent.mp4", voice=root / "voice.mp3", srt_path=root / "subs.srt",
                                  dest=root / "final.mp4", progress=lambda p, m: None)
            streams = self._probe_streams(out)
            self.assertAlmostEqual(streams["video"], 12.0, delta=0.2)
            self.assertAlmostEqual(streams["audio"], 12.0, delta=0.2)

    def test_image_segments_use_standard_range(self) -> None:
        from engine.slides.ffmpeg_util import find_ffprobe
        from engine.slides.render_segment import render_image_canvas

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ff("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:d=1", "-frames:v", "1", str(root / "p.jpg"))
            render_image_canvas(media=root / "p.jpg", duration=1.0, dest=root / "s.mp4")
            fmt = subprocess.run([find_ffprobe(), "-v", "error", "-show_entries", "stream=pix_fmt", "-of", "csv=p=0",
                                  str(root / "s.mp4")], capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(fmt, "yuv420p", "сегмент с фото — в том же формате, что видео")

    def test_footage_single_encode(self) -> None:
        from engine.compositor import pipeline as cp

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for d in ("broll", "intro", "outro"):
                (root / d).mkdir()
            make_clip(root / "broll" / "b.mp4", "testsrc2", "854x480", 3)
            make_clip(root / "intro" / "i.mp4", "testsrc2", "640x360", 2, audio=True)
            make_clip(root / "outro" / "o.mp4", "testsrc2", "640x360", 2)
            ff("-f", "lavfi", "-i", "sine=f=300:d=5", str(root / "a.mp3"))
            (root / "subs.srt").write_text("1\n00:00:00,500 --> 00:00:02,000\nПривет\n", encoding="utf-8")
            runs: list = []
            real = cp._run_ffmpeg
            with mock.patch.object(cp, "_run_ffmpeg", lambda cmd, total, prog: (runs.append(cmd), real(cmd, total, prog))[1]):
                res = cp.build_and_run(cp.ComposeRequest(
                    audio=root / "a.mp3", broll_dir=root / "broll", head_dir=None, text="Канал", output=root / "o.mp4",
                    seed=1, subscribe=True, subscribe_path=make_green_sub(root / "sub.mp4"),
                    intro_dir=root / "intro", outro_dir=root / "outro", srt=root / "subs.srt"))
            self.assertEqual(len(runs), 1, "футажи, субтитры, интро, аутро и подписка — один проход")
            self.assertIsNotNone(res.intro)
            streams = self._probe_streams(res.output)
            self.assertAlmostEqual(streams["video"], 9.0, delta=0.15)
            self.assertAlmostEqual(streams["audio"], streams["video"], delta=0.15)


class CornerTests(unittest.TestCase):
    def test_head_top_right_text_top_left(self) -> None:
        from engine.compositor import pipeline as cp

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for d in ("broll", "head"):
                (root / d).mkdir()
            make_clip(root / "broll" / "b.mp4", "testsrc2", "640x360", 2)
            make_clip(root / "head" / "h.mp4", "testsrc2", "320x320", 2)
            ff("-f", "lavfi", "-i", "sine=f=300:d=2", str(root / "a.mp3"))
            for seed in range(5):
                graphs: list = []
                with mock.patch.object(cp, "_run_ffmpeg", lambda cmd, total, prog: graphs.append(cmd[cmd.index("-filter_complex") + 1])):
                    res = cp.build_and_run(cp.ComposeRequest(audio=root / "a.mp3", broll_dir=root / "broll", head_dir=root / "head",
                                                             text="Канал", output=root / "o.mp4", seed=seed, subscribe=False, subscribe_path=None))
                self.assertEqual((res.head_corner, res.text_corner), ("tr", "tl"))
                self.assertIn("overlay=x=W-w-", graphs[0], "голова у правого края")


class DiskFullTests(unittest.TestCase):
    def test_clear_message_when_disk_is_full(self) -> None:
        import shutil as sh

        from engine.slides import ffprobe_util as fu

        usage = sh.disk_usage("/")._replace(free=1024**3)
        with mock.patch("shutil.disk_usage", return_value=usage):
            with self.assertRaisesRegex(RuntimeError, "Не хватило места на диске: свободно 1.0 ГБ"):
                fu.run_ffmpeg([FF, "-v", "error", "-f", "lavfi", "-i", "nosuchfilter", "/tmp/x.mp4"], label="t")

    def test_normal_error_kept_when_space_ok(self) -> None:
        import shutil as sh

        from engine.slides import ffprobe_util as fu

        usage = sh.disk_usage("/")._replace(free=100 * 1024**3)
        with mock.patch("shutil.disk_usage", return_value=usage), self.assertRaisesRegex(RuntimeError, "t failed"):
            fu.run_ffmpeg([FF, "-v", "error", "-f", "lavfi", "-i", "nosuchfilter", str(Path(tempfile.gettempdir()) / "x.mp4")], label="t")


class IntroOutroSizeTests(unittest.TestCase):
    """Интро/аутро подгоняются под ролик, а не ролик под них."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.root / "intro").mkdir()
        make_clip(self.root / "intro" / "small.mp4", "testsrc2", "640x360", 2, audio=True)

    def _glue(self, main_size: str) -> Path:
        import random

        from engine.compositor.pipeline import _maybe_append_outro, _maybe_prepend_intro

        main = make_clip(self.root / f"main_{main_size}.mp4", "testsrc2", main_size, 3, audio=True)
        _maybe_prepend_intro(main, self.root / "intro", rng=random.Random(1), on_progress=None)
        _maybe_append_outro(main, self.root / "intro", rng=random.Random(1), on_progress=None)
        return main

    def test_stock_video_stays_1080p(self) -> None:
        main = self._glue("1920x1080")
        self.assertEqual(probe_video_size(main), (1920, 1080), "1080p не падает до размера интро")
        self.assertAlmostEqual(probe_duration(main), 7.0, delta=0.5)

    def test_never_below_720p(self) -> None:
        self.assertEqual(probe_video_size(self._glue("854x480")), (1280, 720))


class StrictRenderOrderTests(unittest.TestCase):
    """Строгий ряд монтируется строго по порядку текста; блок пула — один раз и повтором."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.run = Path(self.tmp.name) / "run"
        (self.run / "pool").mkdir(parents=True)
        self.addCleanup(self.tmp.cleanup)
        for i in range(4):
            make_clip(self.run / "pool" / f"v{i}.mp4", "testsrc2", "1280x720", 3)
        for i in range(3):
            ff("-f", "lavfi", "-i", f"mandelbrot=size=1280x720:start_scale={i + 1}", "-frames:v", "1", str(self.run / "pool" / f"i{i}.jpg"))
        ff("-f", "lavfi", "-i", "sine=f=300:d=40", str(self.run / "voice.mp3"))

    def _slot(self, kind, name, start, dur):
        return {"kind": kind, "path": f"pool/{name}", "start": start, "end": start + dur, "duration": dur, "provider": "x", "id": name}

    def test_strict_order_kept(self) -> None:
        from engine.slides import render_pipeline as rp

        slots = [self._slot("video", f"v{i}.mp4", i * 2.0, 2.0) for i in range(4)] + [self._slot("image", f"i{i}.jpg", 8 + i * 2.0, 2.0) for i in range(3)]
        (self.run / "timeline.json").write_text(json.dumps({"mode": "strict_sync", "slots": slots}), encoding="utf-8")
        seen: list = []
        with mock.patch.object(rp, "concat_demuxer", lambda parts, dest: seen.extend(parts)), \
             mock.patch.object(rp, "mux_av", side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                rp.run_render_stage(rp.RenderRequest(run_dir=self.run, copy_to_desktop=False, seed=3))
        self.assertEqual([p.name for p in seen], [f"seg_{i:05d}.mp4" for i in range(7)], "порядок текста не трогаем")

    def test_loop_block_rendered_once_and_repeated(self) -> None:
        from engine.slides import render_pipeline as rp

        early = [self._slot("video", f"v{i}.mp4", i * 3.0, 3.0) for i in range(2)]
        block = [self._slot("image", "i0.jpg", 6, 3), self._slot("video", "v2.mp4", 9, 3), self._slot("image", "i1.jpg", 12, 3)]
        (self.run / "timeline.json").write_text(
            json.dumps({"mode": "strict_sync", "variant": "3min", "slots": early, "loop_block": block, "loop_start": 6.0}),
            encoding="utf-8",
        )
        seen: list = []
        real = rp.concat_demuxer
        with mock.patch.object(rp, "concat_demuxer", lambda parts, dest: (seen.extend(parts), real(parts, dest))[1]):
            res = rp.run_render_stage(rp.RenderRequest(run_dir=self.run, copy_to_desktop=False, seed=1, keep_segments=True))
        rendered = sorted(p.name for p in (self.run / "segments").iterdir())
        self.assertEqual(len(rendered), 5, "сегментов: 2 начала + 3 блока — блок рендерится один раз")
        names = [p.name for p in seen]
        self.assertEqual(names[:2], ["seg_00000.mp4", "seg_00001.mp4"])
        self.assertEqual(names[2:5] * 4, names[2:14], "дальше блок повторяется")
        self.assertAlmostEqual(res.duration, 40.0, delta=0.6)


class MuxAndSubscribeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _video_md5(self, path: Path) -> str:
        out = subprocess.run([FF, "-v", "error", "-i", str(path), "-map", "0:v", "-c", "copy", "-f", "md5", "-"], capture_output=True, text=True, check=True)
        return out.stdout.strip()

    def test_mux_without_overlays_copies_video(self) -> None:
        from engine.slides.render_mux import mux_av

        v = make_clip(self.root / "v.mp4", sec=3)
        ff("-i", str(v), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(self.root / "v264.mp4"))
        ff("-f", "lavfi", "-i", "sine=d=3", str(self.root / "a.mp3"))
        out = mux_av(video=self.root / "v264.mp4", voice=self.root / "a.mp3", dest=self.root / "o.mp4")
        self.assertEqual(self._video_md5(out), self._video_md5(self.root / "v264.mp4"), "видео должно копироваться без пережатия")
        self.assertTrue(has_audio_stream(out))

    def test_speed_copies(self) -> None:
        from engine.slides.speed_copies import write_speed_copies

        v = make_clip(self.root / "ролик.mp4", sec=4, audio=True)
        written = write_speed_copies(v, self.root, 2)
        self.assertEqual(sorted(p.name for p in written), ["101 процент ролик.mp4", "99 процентов ролик.mp4"])
        slow = probe_duration(self.root / "99 процентов ролик.mp4")
        fast = probe_duration(self.root / "101 процент ролик.mp4")
        self.assertGreater(slow, fast)
        self.assertAlmostEqual(slow, 4 / 0.99, delta=0.15)


@unittest.skipUnless(HAS_SAY, "нужна macOS-утилита say")
class WhisperIntegrationTests(unittest.TestCase):
    def test_autodetect_two_languages(self) -> None:
        from engine.slides.whisper_timings import transcribe_words

        with tempfile.TemporaryDirectory() as tmp:
            ru = make_voice(Path(tmp) / "ru.mp3", TEXT_RU)
            es = make_voice(Path(tmp) / "es.mp3", "Cuando Isaac Newton murió, Inglaterra despidió a uno de sus genios.", "Mónica")
            r1 = transcribe_words(ru, language="")
            r2 = transcribe_words(es, language="")
        self.assertEqual((r1.language, r2.language), ("ru", "es"))
        self.assertTrue(all(w.end >= w.start for w in r1.words))
        self.assertGreater(len(r1.words), 8)


if __name__ == "__main__":
    unittest.main()
