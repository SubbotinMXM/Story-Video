"""Интерфейс и сервер говорят об одном: поля настроек и ответы API совпадают."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.core.config import DEFAULTS

API_TS = (ROOT / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")


def ts_type_fields(name: str) -> set[str]:
    m = re.search(rf"export type {name} = \{{(.*?)\n\}};", API_TS, re.S)
    assert m, name
    return set(re.findall(r"^\s{2}(\w+)\??:", m.group(1), re.M))


class ContractTests(unittest.TestCase):
    def test_settings_fields_match_server(self) -> None:
        ui = ts_type_fields("Settings")
        self.assertEqual(ui - set(DEFAULTS), set(), "интерфейс читает поля, которых сервер не отдаёт")
        self.assertEqual(set(DEFAULTS) - ui, set(), "сервер отдаёт поля, о которых интерфейс не знает")

    def test_ui_never_writes_unknown_settings(self) -> None:
        written: set[str] = set()
        for f in (ROOT / "web" / "src").rglob("*.tsx"):
            for m in re.finditer(r"update\(\{\s*([a-z_]+)\s*:", f.read_text(encoding="utf-8")):
                written.add(m.group(1))
        self.assertEqual(written - set(DEFAULTS), set())

    def test_job_fields_match_server(self) -> None:
        from server.jobs import Job

        job = Job(kind="x", title="t", params={}, runner=None).public()
        self.assertEqual(ts_type_fields("Job") - set(job), set())

    def test_meta_fields_match_server(self) -> None:
        from fastapi.testclient import TestClient

        from server.app import app

        meta = TestClient(app).get("/api/meta").json()
        self.assertEqual(ts_type_fields("Meta") - set(meta), set())

    def test_ui_job_kinds_exist_on_server(self) -> None:
        from server.runners import _BUILDERS

        kinds: set[str] = set()
        for f in (ROOT / "web" / "src").rglob("*.tsx"):
            text = f.read_text(encoding="utf-8")
            kinds |= set(re.findall(r'kind: "([a-z_]+)",\s*params', text))
            # SlidesPage: start("slides_full") → kind переменной
            kinds |= set(re.findall(r'start\("([a-z_]+)"\)', text))
        self.assertTrue(kinds)
        self.assertEqual(kinds - set(_BUILDERS), set())


if __name__ == "__main__":
    unittest.main()
