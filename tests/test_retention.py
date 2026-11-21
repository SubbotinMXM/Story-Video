"""Автоочистка: старые завершённые карточки и снимки уходят, идущие и свежие остаются."""

from __future__ import annotations

import _isolate  # noqa: F401

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.core import retention
from server.jobs import CANCELLED, DONE, FAILED, QUEUED, RUNNING, Job, JobManager


class RetentionTest(unittest.TestCase):
    def test_purge_old_keeps_active_and_fresh(self) -> None:
        mgr = JobManager(lambda kind, params, state: ("t", lambda *a: {}))
        mgr._jobs.clear()  # файл очереди общий для тестов: карточки соседних тестов не нужны
        old = time.time() - 6 * 86400
        for status, created, finished in [(DONE, old, old), (FAILED, old, old), (CANCELLED, old, None),
                                          (RUNNING, old, None), (QUEUED, old, None), (FAILED, time.time(), time.time())]:
            j = Job(kind="x", title=status, params={}, runner=None, status=status, created=created)
            j.finished = finished
            mgr._jobs.append(j)
        self.assertEqual(mgr.purge_old(5), 3)
        self.assertEqual(sorted(j.status for j in mgr._jobs), sorted([RUNNING, QUEUED, FAILED]))

    def test_sweep_files(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            data = Path(d)
            (data / "publish" / "errors" / "old").mkdir(parents=True)
            (data / "publish" / "errors" / "new").mkdir()
            (data / "app.log.1").write_text("x")
            past = time.time() - 6 * 86400
            os.utime(data / "publish" / "errors" / "old", (past, past))
            os.utime(data / "app.log.1", (past, past))
            self.assertEqual(retention.sweep_files(data), 2)
            self.assertTrue((data / "publish" / "errors" / "new").is_dir())
            self.assertFalse((data / "publish" / "errors" / "old").exists())


if __name__ == "__main__":
    unittest.main()
