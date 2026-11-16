"""Языки каналов: разводка версий ролика по каналам, предохранитель от чужого языка, перевод материалов."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)
from _human import setUpModule, tearDownModule  # noqa: F401

import unittest
from pathlib import Path
from unittest import mock

from engine.publish import localize, registry
from engine.publish import run as pub
from engine.publish import youtube
from test_publish import FakeAds, Fixture


class LanguageRunTests(Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.es = self.tmp / "ролик ES"
        self.es.mkdir()
        self.es_files = []
        for pct in (100, 99):
            f = self.es / f"{pct}.mp4"
            f.write_bytes(b"v")
            self.es_files.append((str(f), pct))
        self.uploads: list[tuple[str, str, str]] = []

    def upload(self, cdp, channel, job, cancel=None, progress=None, **kw):
        self.uploads.append((channel.id, Path(job.file).parent.name, job.title))
        return youtube.Uploaded(f"https://youtu.be/{channel.id}", [])

    def run_job(self, req, state):
        def save(**values):
            state.update(values)

        with mock.patch.object(pub, "AdsPower", FakeAds), mock.patch.object(youtube, "READY", True), \
             mock.patch.object(youtube, "upload", self.upload):
            return pub.run_publish(req, state, save)

    def versions_request(self, channels):
        prepared = []

        def prepare(version, base, *, cancel=None, progress=None):
            prepared.append((version.language, base is not None))
            return {"title": f"Название {version.language.upper()}", "thumbnail": "", "description": "d"}

        req = self.request(channels, versions=[pub.Version("ru", self.folder, self.files[:2]),
                                               pub.Version("es", self.es, self.es_files)])
        req.prepare = prepare
        return req, prepared

    def test_each_channel_gets_its_language(self) -> None:
        registry.set_language("UCa", "ru")
        registry.set_language("UCb", "es")
        req, prepared = self.versions_request(["p1/UCa", "p1/UCb"])
        res = self.run_job(req, {})
        self.assertEqual(sorted(self.uploads), [("UCa", "ролик", "Название RU"), ("UCb", "ролик ES", "Название ES")])
        self.assertEqual(prepared, [("ru", False), ("es", True)], "материалы ES — переводом с основной")
        self.assertIn("Выложено на 2", res["summary"])

    def test_channel_without_language_waits_then_continues(self) -> None:
        registry.set_language("UCa", "ru")
        req, _ = self.versions_request(["p1/UCa", "p1/UCb"])
        state: dict = {}
        with self.assertRaisesRegex(RuntimeError, "не указан язык"):
            self.run_job(req, state)
        self.assertEqual([u[0] for u in self.uploads], ["UCa"])
        registry.set_language("UCb", "es")  # указал язык → «Продолжить»
        self.run_job(req, state)
        self.assertEqual([u[0] for u in self.uploads], ["UCa", "UCb"])
        self.assertEqual(self.uploads[1][1], "ролик ES")

    def test_language_without_version_skipped(self) -> None:
        registry.set_language("UCa", "ru")
        registry.set_language("UCb", "de")
        req, _ = self.versions_request(["p1/UCa", "p1/UCb"])
        res = self.run_job(req, {})
        self.assertEqual([u[0] for u in self.uploads], ["UCa"])
        self.assertTrue(any("ролика на DE нет" in line for line in res["lines"]))

    def test_single_version_wrong_language_not_uploaded(self) -> None:
        registry.set_language("UCa", "ru")
        registry.set_language("UCb", "es")
        req = self.request(["p1/UCa", "p1/UCb"], language="ru")
        state: dict = {}
        with self.assertRaisesRegex(RuntimeError, "НЕ заливаю"):
            self.run_job(req, state)
        self.assertEqual([u[0] for u in self.uploads], ["UCa"], "испанский канал не получил русский ролик")
        self.assertNotIn("publish_at", state["channels"]["p1/UCb"], "день на чужом канале не занят")
        registry.set_language("UCb", "ru")  # канал сменил нишу на русский → «Продолжить»
        self.run_job(req, state)
        self.assertEqual([u[0] for u in self.uploads], ["UCa", "UCb"])

    def test_unknown_languages_upload_as_before(self) -> None:
        req = self.request(["p1/UCa", "p1/UCb"])
        self.run_job(req, {})
        self.assertEqual(len(self.uploads), 2)


class RegistryLanguageTests(Fixture):
    def test_language_kept_on_rescan_and_auto_suggested(self) -> None:
        registry.set_language("UCa", "pl")
        registry.set_channels("p1", [registry.Channel("UCa", "Канал А", titles_language="es"),
                                     registry.Channel("UCb", "Канал Б", titles_language="es")])
        chans = {c.id: c for c in registry.load()["p1"].channels}
        self.assertEqual((chans["UCa"].language, chans["UCa"].language_auto), ("pl", False), "твой язык не трогаем")
        self.assertEqual((chans["UCb"].language, chans["UCb"].language_auto), ("es", True), "подсказка по названиям")

    def test_language_change_everywhere(self) -> None:
        registry.set_language("UCa", "IT")
        langs = {p.id: [c.language for c in p.channels if c.id == "UCa"] for p in registry.load().values()}
        self.assertEqual(langs, {"p1": ["it"], "p2": ["it"]}, "один канал в двух профилях — язык общий")


class LocalizeTests(unittest.TestCase):
    def test_no_text_same_cover(self) -> None:
        notes: list[str] = []
        img = Path(__file__)  # любой файл
        self.assertEqual(localize.cover_for(None, None, str(img), [], "es", Path("/tmp/x"), notes), str(img))
        self.assertEqual(notes, [])

    def test_text_without_key_note(self) -> None:
        notes: list[str] = []
        out = localize.cover_for(None, None, str(Path(__file__)), ["ВЬЕТНАМ"], "es", Path("/tmp/x"), notes)
        self.assertEqual(out, str(Path(__file__)))
        self.assertTrue(notes and "RoyalTechno" in notes[0])

    def test_localize_translates_title_and_description(self) -> None:
        mat = {"title": "Вьетнам для взрослых", "description": "0:00 Начало", "thumbnail": "", "lines": []}
        with mock.patch.object(localize, "translate_text", lambda llm, t, d, lang: ("Vietnam para adultos", "0:00 Inicio")):
            out = localize.localize(mat, "es", llm=("k", "u", "m"), cs=None, work=Path("/tmp/x"), force=True)
        self.assertEqual((out["title"], out["description"]), ("Vietnam para adultos", "0:00 Inicio"))
        self.assertEqual(mat["title"], "Вьетнам для взрослых", "основные материалы не трогаем")

    def test_same_language_untouched(self) -> None:
        mat = {"title": "Как живут люди в деревне и что они делают", "description": "Это история о том, как они живут и что было",
               "thumbnail": ""}
        with mock.patch.object(localize, "translate_text", side_effect=AssertionError("не нужен перевод")):
            self.assertIs(localize.localize(mat, "ru", llm=("k",), cs=None, work=Path("/tmp/x"), force=False), mat)


class PreviewTests(Fixture):
    def test_preview_lines(self) -> None:
        from server.runners import routing_preview

        registry.set_language("UCa", "ru")
        registry.set_language("UCc", "de")
        lines = routing_preview(["p1/UCa", "p1/UCb", "p2/UCc"],
                                [{"language": "ru", "files": []}, {"language": "es", "files": []}])
        text = "\n".join(lines)
        self.assertIn("Ролик RU → ", text)
        self.assertIn("Ролик ES → нет каналов", text)
        self.assertIn("не указан язык", text)
        self.assertIn("ролика на DE нет", text)


if __name__ == "__main__":
    unittest.main()
