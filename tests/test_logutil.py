"""Журнал не растёт бесконечно; тяжёлые команды идут с пониженным приоритетом; кадр клипа достаётся одним способом."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.core import logutil


class RotateTests(unittest.TestCase):
    def test_big_log_moves_to_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            path.write_text("x" * 100, encoding="utf-8")
            (Path(tmp) / "app.log.1").write_text("old", encoding="utf-8")
            with mock.patch.object(logutil, "LOG_PATH", path), mock.patch.object(logutil, "MAX_LOG_BYTES", 50), \
                    mock.patch.object(logutil, "_writes", 0):
                logutil.log("новая строка")
            self.assertEqual((Path(tmp) / "app.log.1").read_text(encoding="utf-8"), "x" * 100, "прошлый .1 заменён")
            self.assertIn("новая строка", path.read_text(encoding="utf-8"))
            self.assertLess(path.stat().st_size, 100)

    def test_small_log_stays(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            with mock.patch.object(logutil, "LOG_PATH", path), mock.patch.object(logutil, "_writes", 0):
                logutil.log("а")
                logutil.log("б")
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)
            self.assertFalse((Path(tmp) / "app.log.1").exists())


class PriorityTests(unittest.TestCase):
    def test_render_command_is_niced(self) -> None:
        from engine.slides.ffmpeg_util import low_priority

        cmd = low_priority(["ffmpeg", "-i", "a"])
        self.assertEqual(cmd[-3:], ["ffmpeg", "-i", "a"])
        self.assertIn("nice", cmd[0])

    def test_run_ffmpeg_uses_it(self) -> None:
        from engine.slides import ffprobe_util

        seen = []
        done = mock.MagicMock(returncode=0)
        with mock.patch.object(ffprobe_util, "run_cancellable", lambda cmd, **kw: seen.append(cmd) or done):
            ffprobe_util.run_ffmpeg(["ffmpeg", "-y"])
        self.assertIn("nice", seen[0][0])


class GrabFrameTests(unittest.TestCase):
    def test_first_frame_tries_next_second(self) -> None:
        from engine.slides import ffprobe_util

        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd[cmd.index("-ss") + 1])
            if len(calls) == 2:  # на 2-й секунде кадра нет (короткий клип), с начала — есть
                Path(cmd[-1]).write_bytes(b"x" * 100)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ffprobe_util.subprocess, "run", fake_run):
            self.assertTrue(ffprobe_util.grab_first_frame(Path("clip.mp4"), Path(tmp) / "f.jpg"))
        self.assertEqual(calls, ["2.00", "0.00"])

    def test_missing_ffmpeg_is_not_a_crash(self) -> None:
        from engine.slides import ffprobe_util

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ffprobe_util.subprocess, "run", side_effect=OSError("нет")):
            self.assertFalse(ffprobe_util.grab_frame(Path("clip.mp4"), Path(tmp) / "f.jpg"))


if __name__ == "__main__":
    unittest.main()


class TransitionTests(unittest.TestCase):
    def test_calm_is_unchanged_black_dip(self) -> None:
        from engine.slides.render_segment import transition_fades

        self.assertEqual(transition_fades("calm", 7, 4.0, 0.5), (0.25, 0.25, "black", "black"))

    def test_flash_color_matches_across_a_cut(self) -> None:
        from engine.slides.render_segment import transition_fades

        for seed in range(6):  # уход кадра N гаснет в цвет, из которого появляется кадр N+1
            out_color = transition_fades("flash", seed, 4.0, 0.5)[3]
            in_color = transition_fades("flash", seed + 1, 4.0, 0.5)[2]
            self.assertEqual(out_color, in_color)
        self.assertNotEqual(transition_fades("flash", 0, 4.0, 0.5)[3], transition_fades("flash", 1, 4.0, 0.5)[3])

    def test_short_image_keeps_fades_inside(self) -> None:
        from engine.slides.render_segment import transition_fades

        fin, fout, _a, _b = transition_fades("flash", 3, 0.6, 0.5)
        self.assertLessEqual(fin + fout, 0.6)

    def test_filter_uses_style_colors(self) -> None:
        from engine.slides.render_segment import image_motion_filter

        flash, _ = image_motion_filter(duration=4, seed=2, fade_sec=0.5, w=1920, h=1080, fps=30, transition="flash")
        calm, _ = image_motion_filter(duration=4, seed=2, fade_sec=0.5, w=1920, h=1080, fps=30)
        self.assertIn("color=white", flash)
        self.assertNotIn("color=white", calm)
        self.assertIn("color=black", calm)


class StopKillsProcessTests(unittest.TestCase):
    """«Стоп» обрывает внешнюю программу сразу, а не после того как она доработает."""

    def _stopper(self, token, delay: float = 0.3) -> None:
        import threading

        threading.Timer(delay, token.cancel).start()

    def test_run_cancellable_killed_by_stop(self) -> None:
        import time

        from engine.core.cancel import CancelledError, CancelToken, current_cancel, run_cancellable

        token = CancelToken()
        current_cancel.set(token)
        self.addCleanup(current_cancel.set, None)
        self._stopper(token)
        t0 = time.monotonic()
        with self.assertRaises(CancelledError):
            run_cancellable(["sleep", "30"])
        self.assertLess(time.monotonic() - t0, 5, "sleep 30 оборвался по «Стоп»")
        self.assertEqual(token._closables, [], "после процесса в токене ничего не осталось")

    def test_already_stopped_does_not_even_start(self) -> None:
        from engine.core.cancel import CancelledError, CancelToken, current_cancel, run_cancellable

        token = CancelToken()
        token.cancel()
        current_cancel.set(token)
        self.addCleanup(current_cancel.set, None)
        with self.assertRaises(CancelledError):
            run_cancellable(["sleep", "30"])

    def test_normal_run_and_timeout_like_subprocess_run(self) -> None:
        import subprocess

        from engine.core.cancel import run_cancellable

        done = run_cancellable(["echo", "привет"], text=True)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "привет"))
        self.assertEqual(run_cancellable(["sh", "-c", "exit 3"]).returncode, 3)
        with self.assertRaises(subprocess.TimeoutExpired):
            run_cancellable(["sleep", "5"], timeout=0.2)

    def test_run_ffmpeg_stops_and_concat_does_not_fall_back(self) -> None:
        import time

        from engine.core.cancel import CancelledError, CancelToken, current_cancel
        from engine.slides import ffprobe_util

        token = CancelToken()
        current_cancel.set(token)
        self.addCleanup(current_cancel.set, None)
        self._stopper(token)
        t0 = time.monotonic()
        with self.assertRaises(CancelledError):
            ffprobe_util.run_ffmpeg(["sleep", "30"], label="тест")
        self.assertLess(time.monotonic() - t0, 5)

        calls = []

        def stop(cmd, **kw):
            calls.append(cmd)
            raise CancelledError("Остановлено пользователем")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ffprobe_util, "run_ffmpeg", stop):
            parts = [Path(tmp) / "a.mp4", Path(tmp) / "b.mp4"]
            for p in parts:
                p.write_bytes(b"x")
            with self.assertRaises(CancelledError):
                ffprobe_util.concat_demuxer(parts, Path(tmp) / "out.mp4")
        self.assertEqual(len(calls), 1, "после «Стоп» перекодирование-запас не запускается")

    def test_progress_reader_killed(self) -> None:
        """Сборка рассказа (compositor) читает вывод ffmpeg построчно — тоже обрывается."""
        import time

        from engine.compositor import pipeline
        from engine.core.cancel import CancelledError, CancelToken, current_cancel

        token = CancelToken()
        current_cancel.set(token)
        self.addCleanup(current_cancel.set, None)
        self._stopper(token)
        t0 = time.monotonic()
        with mock.patch.object(pipeline, "low_priority", lambda cmd: ["sh", "-c", "echo start >&2; exec sleep 30"]):
            with self.assertRaises(CancelledError):
                pipeline._run_ffmpeg(["ffmpeg"], 100.0, None)
        self.assertLess(time.monotonic() - t0, 5)
