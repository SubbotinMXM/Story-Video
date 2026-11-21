"""Обновление программы: проверка, подтягивание, ожидание свободной очереди, расписание, перезапуск."""

from __future__ import annotations

import _isolate  # noqa: F401

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import updater
from engine.publish.schedule import MSK


def git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", "HOME": str(cwd)})


class Busy:
    def __init__(self, busy: bool) -> None:
        self._b = busy
        self.saved = 0

    def busy(self) -> bool:
        return self._b

    def _save(self) -> None:
        self.saved += 1


class UpdaterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.remote, self.app, self.other = root / "remote.git", root / "app", root / "other"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.remote)], check=True, capture_output=True)
        subprocess.run(["git", "clone", str(self.remote), str(self.app)], check=True, capture_output=True)
        (self.app / "a.txt").write_text("1")
        git(self.app, "add", "."); git(self.app, "commit", "-m", "первый"); git(self.app, "push", "origin", "HEAD:main")
        git(self.app, "branch", "--set-upstream-to=origin/main")
        subprocess.run(["git", "clone", str(self.remote), str(self.other)], check=True, capture_output=True)
        (self.other / "a.txt").write_text("2")
        (self.other / "changelog.json").write_text(json.dumps({"releases": [{"version": "9.9.9", "title": "Новое"}]}))
        git(self.other, "add", "."); git(self.other, "commit", "-m", "0.9: новое"); git(self.other, "push", "origin", "HEAD:main")
        self._st = mock.patch.object(updater, "STATE_FILE", "update_state_test.json")
        self._st.start()
        self.addCleanup(self._st.stop)
        self.addCleanup(lambda: (updater.clear_pending()))

    def test_status_sees_new_commits(self) -> None:
        st = updater.status(self.app, fetch=True)
        self.assertTrue(st["ok"], st)
        self.assertEqual(st["behind"], 1)
        self.assertEqual(st["commits"], ["0.9: новое"])
        self.assertEqual(st["version"], "9.9.9")

    def test_pull_updates_files_and_conflict_is_explained(self) -> None:
        updater.status(self.app, fetch=True)
        (self.app / "a.txt").write_text("мои правки")
        with self.assertRaises(updater.UpdateError) as ctx:
            updater.pull(self.app)
        self.assertIn("правки", str(ctx.exception))
        git(self.app, "checkout", "a.txt")
        updater.pull(self.app)
        self.assertEqual((self.app / "a.txt").read_text(), "2")
        self.assertEqual(updater.status(self.app)["behind"], 0)

    def test_not_a_repo_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            st = updater.status(Path(d))
            self.assertFalse(st["ok"])

    def test_pending_waits_for_idle_queue(self) -> None:
        calls: list[str] = []
        st = {"ok": True, "behind": 1, "version": "9", "current": "x", "checked_at": 1.0, "error": ""}
        updater.set_pending("update", "user")
        busy, idle = Busy(True), Busy(False)
        with mock.patch.object(updater, "status", lambda **k: st), mock.patch.object(updater, "pull", lambda *a, **k: ""):
            self.assertNotEqual(updater.tick(busy, {}, restart_fn=lambda: calls.append("r"), status_fn=lambda **k: st), "applied")
            self.assertEqual(calls, [], "пока идёт работа — не перезапускаем")
            self.assertEqual(updater.tick(idle, {}, restart_fn=lambda: calls.append("r"), status_fn=lambda **k: st), "applied")
        self.assertEqual(calls, ["r"])
        self.assertEqual(idle.saved, 1)
        self.assertEqual(updater.pending(), {})

    def test_failed_update_is_not_retried_forever(self) -> None:
        updater.set_pending()
        with mock.patch.object(updater, "apply_and_restart", side_effect=updater.UpdateError("нет связи")):
            self.assertEqual(updater.tick(Busy(False), {}, restart_fn=lambda: None, status_fn=lambda **k: {"ok": True, "behind": 0, "checked_at": 9e12}), "error")
        self.assertEqual(updater.pending(), {})
        self.assertEqual(updater.state()["last_error"], "нет связи")

    def test_schedule_marks_pending_once_a_day(self) -> None:
        setting = {"enabled": True, "time": "04:00"}
        cfg = {"update_auto": setting}
        before = datetime(2026, 11, 20, 3, 0, tzinfo=MSK)
        after = datetime(2026, 11, 20, 4, 5, tzinfo=MSK)
        self.assertFalse(updater.due(setting, before))  # включили до слота — слот сегодня ещё впереди
        self.assertTrue(updater.due(setting, after))
        st = {"ok": True, "behind": 2, "version": "9", "current": "x", "checked_at": 1.0, "error": ""}
        with mock.patch.object(updater, "datetime") as dt:
            dt.now.return_value = after
            updater.tick(Busy(True), cfg, restart_fn=lambda: None, status_fn=lambda **k: st)
        self.assertEqual(updater.pending().get("source"), "auto")
        self.assertFalse(updater.due(setting, after), "второй раз в тот же день — нет")

    def test_restart_script_waits_for_old_process(self) -> None:
        with mock.patch.object(updater.subprocess, "Popen") as popen:
            updater.restart(pid=4242, argv=["--browser"], repo=Path("/x y"), exit_after=None)
        script = popen.call_args[0][0][2]
        self.assertIn("kill -0 4242", script)
        self.assertIn("exec ./run.sh '--browser'", script)
        self.assertIn("'/x y'", script)

    def test_normalize(self) -> None:
        self.assertEqual(updater.normalize({"enabled": 1, "time": "7:5"}), {"enabled": True, "time": "04:00"})
        self.assertEqual(updater.normalize({"time": "23:30"})["time"], "23:30")


if __name__ == "__main__":
    unittest.main()
