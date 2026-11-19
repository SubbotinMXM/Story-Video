"""«Что нового»: файл версий changelog.json и окно после обновления (что показывать, когда, один раз)."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.core import changelog as cl


def write(tmp: Path, releases: list) -> Path:
    p = tmp / "changelog.json"
    p.write_text(json.dumps({"releases": releases}, ensure_ascii=False), encoding="utf-8")
    return p


def rel(version: str, *titles: str) -> dict:
    return {"version": version, "date": "2026-11-19", "title": f"Выпуск {version}",
            "items": [{"tag": "new", "title": t, "text": "подробности"} for t in (titles or ("что-то",))]}


class RealFileTests(unittest.TestCase):
    """Сам changelog.json, который уедет в репозиторий: ошибка в нём — окно не покажется у всех."""

    def test_file_is_valid_and_newest_first(self) -> None:
        raw = json.loads(cl.FILE.read_text(encoding="utf-8"))
        rels = raw["releases"]
        self.assertTrue(rels, "хотя бы один выпуск")
        versions = [r["version"] for r in rels]
        self.assertEqual(len(versions), len(set(versions)), "версии не повторяются")
        key = lambda v: tuple(int(x) for x in v.split("."))  # noqa: E731
        self.assertEqual(versions, sorted(versions, key=key, reverse=True), "новые выпуски — сверху")
        for r in rels:
            self.assertRegex(r["version"], r"^\d+\.\d+\.\d+$")
            self.assertRegex(r["date"], r"^\d{4}-\d{2}-\d{2}$")
            self.assertTrue(r["title"].strip())
            self.assertTrue(r["items"], f"в выпуске {r['version']} нет изменений")
            for it in r["items"]:
                self.assertIn(it["tag"], cl.TAGS)
                self.assertTrue(it["title"].strip() and it["text"].strip(), f"пустое изменение в {r['version']}")
                self.assertLess(len(it["title"]), 90, "заголовок — коротко")
        # весь файл проходит чистку без потерь: ничего не пропущено как «битое»
        self.assertEqual(len(cl.releases()), len(rels))
        for r, clean in zip(rels, cl.releases()):
            self.assertEqual(len(r["items"]), len(clean["items"]))

    def test_current_version_is_the_top_release(self) -> None:
        self.assertEqual(cl.current(), json.loads(cl.FILE.read_text(encoding="utf-8"))["releases"][0]["version"])


class UnseenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = write(Path(self.tmp.name), [rel("0.4.0"), rel("0.3.0"), rel("0.2.0"), rel("0.1.0")])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def versions(self, seen: str) -> list[str]:
        return [r["version"] for r in cl.unseen(seen, self.path)]

    def test_first_launch_shows_only_the_latest(self) -> None:
        self.assertEqual(self.versions(""), ["0.4.0"])

    def test_up_to_date_shows_nothing(self) -> None:
        self.assertEqual(self.versions("0.4.0"), [])

    def test_update_shows_everything_since_last_seen(self) -> None:
        self.assertEqual(self.versions("0.3.0"), ["0.4.0"])
        self.assertEqual(self.versions("0.1.0"), ["0.4.0", "0.3.0", "0.2.0"], "человек пропустил два обновления — увидит оба")

    def test_unknown_old_version_shows_latest_ones(self) -> None:
        self.assertEqual(self.versions("9.9.9"), ["0.4.0", "0.3.0", "0.2.0", "0.1.0"])

    def test_long_gap_is_capped(self) -> None:
        many = write(Path(self.tmp.name), [rel(f"0.{n}.0") for n in range(12, 0, -1)])
        got = cl.unseen("0.1.0", many)
        self.assertEqual(len(got), cl.MAX_SHOWN)
        self.assertEqual(got[0]["version"], "0.12.0")

    def test_missing_or_broken_file_means_no_window(self) -> None:
        self.assertEqual(cl.unseen("", Path(self.tmp.name) / "нет.json"), [])
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text("{не json", encoding="utf-8")
        self.assertEqual(cl.unseen("", bad), [])
        self.assertEqual(cl.current(bad), "")

    def test_bad_entries_are_skipped_not_fatal(self) -> None:
        p = write(Path(self.tmp.name), [{"version": "", "items": []}, "мусор", {"version": "0.5.0", "items": []},
                                        {"version": "0.4.0", "items": [{"title": ""}, {"title": "ок", "tag": "странный"}]},
                                        rel("0.4.0", "дубль")])
        got = cl.releases(p)
        self.assertEqual([r["version"] for r in got], ["0.4.0"], "без изменений и повторы отброшены")
        self.assertEqual(got[0]["items"], [{"tag": "new", "title": "ок", "text": ""}], "незнакомый тег — «новое»")


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from engine.core import config as cfg_mod
        from server import app as app_mod

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.changelog = write(root, [rel("0.3.0", "Первое", "Второе"), rel("0.2.0")])
        patches = [mock.patch.object(cfg_mod, "DATA_DIR", root), mock.patch.object(cfg_mod, "CONFIG_PATH", root / "config.json"),
                   mock.patch.object(cl, "FILE", self.changelog)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.client = TestClient(app_mod.app)

    def test_shown_once_then_not_until_next_release(self) -> None:
        first = self.client.get("/api/whats-new").json()
        self.assertEqual(first["version"], "0.3.0")
        self.assertEqual([r["version"] for r in first["unseen"]], ["0.3.0"])
        self.assertEqual([r["version"] for r in first["history"]], ["0.3.0", "0.2.0"])
        self.assertEqual(self.client.post("/api/whats-new/seen").json(), {"version": "0.3.0"})
        self.assertEqual(self.client.get("/api/whats-new").json()["unseen"], [], "после «Понятно» окна больше нет")
        # вышло обновление — окно снова, и только с новым
        write(Path(self.tmp.name), [rel("0.4.0", "Новое"), rel("0.3.0"), rel("0.2.0")])
        again = self.client.get("/api/whats-new").json()
        self.assertEqual([r["version"] for r in again["unseen"]], ["0.4.0"])

    def test_seen_does_not_touch_other_settings(self) -> None:
        self.client.patch("/api/settings", json={"overlay_text": "мой канал"})
        self.client.post("/api/whats-new/seen")
        self.assertEqual(self.client.get("/api/settings").json()["overlay_text"], "мой канал")

    def test_no_changelog_no_crash(self) -> None:
        with mock.patch.object(cl, "FILE", Path(self.tmp.name) / "нет.json"):
            data = self.client.get("/api/whats-new").json()
            self.assertEqual((data["version"], data["unseen"], data["history"]), ("", [], []))
            self.assertEqual(self.client.post("/api/whats-new/seen").json(), {"version": ""})


class FrontendWiredTests(unittest.TestCase):
    def test_dialog_is_mounted_and_marks_seen(self) -> None:
        app = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
        self.assertIn("WhatsNewDialog", app)
        self.assertIn("api.whatsNewSeen()", app, "закрытие окна запоминается")
        self.assertIn("setNewsOpen(\"history\")", app, "историю версий можно открыть позже")


if __name__ == "__main__":
    unittest.main()
