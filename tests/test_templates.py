"""Шаблоны запуска: что в них входит, запись/загрузка, «сделать N роликов» с каналов-конкурентов."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from test_api_server import ApiTestCase  # noqa: E402

from server import jobs as jobs_mod  # noqa: E402
from server import templates as tp  # noqa: E402

SLIDES = {
    "llm_api_key": "test-key",
    "slides_publish": True, "slides_publish_channels": ["p1/UC1", "p1/UC2", "p2/UC3", "p2/UC4"],
    "slides_publish_step": 2, "slides_publish_time": "18:00", "ai_gen_enabled": True, "media_mode": "strict_full",
    "rewrite_channels": {"broll": "https://youtube.com/@old", "slides": "https://youtube.com/@one https://youtube.com/@two"},
    "rewrite_prompt": {"broll": "default-broll", "slides": "default-slides"},
    "slides_publish_dates": ["2026-12-01"],
}


def video(vid: str, title: str = "") -> SimpleNamespace:
    return SimpleNamespace(id=vid, url=f"https://www.youtube.com/watch?v={vid}", title=title or vid)


def picker(per_channel: dict[str, list[str]]):
    """Подмена engine.rewrite.channels.pick: у каждого канала свой список «залетевших» роликов."""
    def pick(url, count, *, section, keys, jobs=None, exclude=None):
        ids = [v for v in per_channel[url] if v not in (exclude or set())][:count]
        return {"channel": url.rsplit("@", 1)[-1], "id": url, "videos": [video(v) for v in ids],
                "left": len(per_channel[url]) - len(ids), "total": len(per_channel[url]), "all": 10}
    return pick


class CaptureTests(ApiTestCase):
    def cfg(self, **extra):
        from engine.core.config import load_config

        self.client.patch("/api/settings", json={**SLIDES, "broll_publish_channels": ["p9/UC9"], "head_dir": "/x/head",
                                                 "analytics_view": {"group": "g1"}, **extra})
        return load_config()

    def test_slides_template_has_only_its_section_and_no_app_state(self) -> None:
        cap = tp.capture(self.cfg(), "slides")
        self.assertEqual(cap["slides_publish_channels"], SLIDES["slides_publish_channels"])
        self.assertEqual(cap["media_mode"], "strict_full")
        self.assertTrue(cap["ai_gen_enabled"])
        self.assertFalse([k for k in cap if k.startswith("broll_")], "настройки «Рассказов» в шаблон слайдов не входят")
        self.assertNotIn("head_dir", cap)
        self.assertNotIn("analytics_view", cap)
        self.assertNotIn("llm_api_key", cap, "ключи сервисов не копируются")
        self.assertNotIn("slides_publish_dates", cap, "календарные даты — не в шаблоне (даты задаются при запуске)")
        self.assertEqual(cap["rewrite_channels"], {"slides": SLIDES["rewrite_channels"]["slides"]}, "конкуренты — только своего раздела")

    def test_broll_template_is_the_mirror(self) -> None:
        cap = tp.capture(self.cfg(), "broll")
        self.assertEqual(cap["broll_publish_channels"], ["p9/UC9"])
        self.assertEqual(cap["head_dir"], "/x/head")
        self.assertFalse([k for k in cap if k.startswith("slides_")])
        for k in ("media_mode", "ai_gen_enabled", "music_track", "transition"):
            self.assertNotIn(k, cap)
        self.assertEqual(cap["rewrite_channels"], {"broll": "https://youtube.com/@old"})

    def test_apply_keeps_other_section_and_secrets(self) -> None:
        cfg = self.cfg(tts={"lumean": {"api_key": "СЕКРЕТ", "voice": "старый"}})
        tpl = {"settings": {"slides_publish_step": 3, "rewrite_channels": {"slides": "https://youtube.com/@new"},
                            "tts": {"lumean": {"voice": "из шаблона"}}}}
        patch = tp.patch_for(cfg, tpl)
        self.assertEqual(patch["rewrite_channels"], {"broll": "https://youtube.com/@old", "slides": "https://youtube.com/@new"})
        self.assertEqual(patch["tts"]["lumean"], {"voice": "из шаблона", "api_key": "СЕКРЕТ"}, "секрет озвучки не теряется")


class CrudTests(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client.patch("/api/settings", json=SLIDES)

    def make(self, name="Страны"):
        return self.client.post("/api/templates", json={"name": name, "section": "slides"})

    def test_create_list_summary(self) -> None:
        r = self.make()
        self.assertEqual(r.status_code, 200)
        t = r.json()
        self.assertEqual((t["name"], t["section"]), ("Страны", "slides"))
        s = t["summary"]
        self.assertEqual((s["publish"], s["step"], s["time"], s["ai"]), (True, 2, "18:00", True))
        self.assertEqual(len(s["channels"]), 4)
        self.assertEqual(s["competitors"], ["https://youtube.com/@one", "https://youtube.com/@two"])
        self.assertEqual([x["id"] for x in self.client.get("/api/templates").json()], [t["id"]])

    def test_name_rules(self) -> None:
        self.assertEqual(self.client.post("/api/templates", json={"name": "  ", "section": "slides"}).status_code, 400)
        self.assertEqual(self.client.post("/api/templates", json={"name": "X", "section": "нет"}).status_code, 400)
        self.make()
        dup = self.make("  страны ")
        self.assertEqual(dup.status_code, 400)
        self.assertIn("уже есть", dup.json()["detail"])
        self.assertEqual(self.client.post("/api/templates", json={"name": "Страны", "section": "broll"}).status_code, 200,
                         "то же имя в другом разделе — можно")

    def test_rename_copy_delete(self) -> None:
        t = self.make().json()
        self.assertEqual(self.client.patch(f"/api/templates/{t['id']}", json={"name": "Страны 2"}).json()["name"], "Страны 2")
        c1 = self.client.post(f"/api/templates/{t['id']}/copy").json()
        c2 = self.client.post(f"/api/templates/{t['id']}/copy").json()
        self.assertEqual((c1["name"], c2["name"]), ("Страны 2 (копия)", "Страны 2 (копия 2)"))
        self.assertEqual(self.client.delete(f"/api/templates/{c1['id']}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/templates/{c1['id']}").status_code, 404)
        self.assertEqual(self.client.patch("/api/templates/нет", json={"name": "а"}).status_code, 404)

    def test_edit_cycle_apply_then_save(self) -> None:
        t = self.make().json()
        self.client.patch("/api/settings", json={"slides_publish_channels": ["p1/UC1"], "slides_publish_step": 1})
        applied = self.client.post(f"/api/templates/{t['id']}/apply").json()
        self.assertEqual(applied["slides_publish_channels"], SLIDES["slides_publish_channels"], "загрузили шаблон в настройки")
        self.client.patch("/api/settings", json={"slides_publish_channels": ["p1/UC1"], "slides_publish_step": 3})
        saved = self.client.post(f"/api/templates/{t['id']}/save").json()
        self.assertEqual((saved["summary"]["channels"], saved["summary"]["step"]), (["p1/UC1"], 3), "«Сохранить в шаблон»")

    def test_file_survives_restart(self) -> None:
        self.make()
        self.assertEqual([x["name"] for x in tp.listing()], ["Страны"])
        raw = json.loads((self.root / "templates.json").read_text(encoding="utf-8"))
        self.assertEqual(raw[0]["name"], "Страны")


class LaunchTests(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client.patch("/api/settings", json=SLIDES)
        self.tpl = self.client.post("/api/templates", json={"name": "Страны", "section": "slides"}).json()
        # живые настройки потом меняются — запуск идёт по шаблону
        self.client.patch("/api/settings", json={"slides_publish_channels": ["zz/ZZ"], "slides_publish_step": 1,
                                                 "slides_publish_dates": ["2027-01-01"]})
        self.added: list = []
        add = lambda mgr, job: (self.added.append(job), job)[1]  # noqa: E731 — не запускаем рерайты по-настоящему
        # проверка входа настоящих задач (ключи озвучки, профили AdsPower) — отдельные тесты; здесь важны сами задачи шаблона
        create = lambda mgr, kind, params: jobs_mod.Job(kind=kind, title=kind, params=params, runner=None)  # noqa: E731
        patches = [mock.patch.object(jobs_mod.JobManager, "add", add), mock.patch.object(jobs_mod.JobManager, "create", create),
                   mock.patch("server.runners.royal_warning", lambda cfg, jobs: "")]  # без похода за ценами AI в сеть
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def launch(self, body=None, picks=None):
        picks = picks or {"https://youtube.com/@one": ["a1", "a2", "a3", "a4"], "https://youtube.com/@two": ["b1", "b2", "b3"]}
        with mock.patch("engine.rewrite.channels.pick", picker(picks)):
            return self.client.post(f"/api/templates/{self.tpl['id']}/launch", json=body or {"count": 5})

    def test_n_videos_channels_in_turn(self) -> None:
        r = self.launch({"count": 5})
        self.assertEqual(r.status_code, 200, r.text)
        urls = [j.params["url"].rsplit("=", 1)[-1] for j in self.added]
        self.assertEqual(urls, ["a1", "b1", "a2", "b2", "a3"], "каналы-конкуренты по очереди, всего N")
        rep = {c["url"]: c["taken"] for c in r.json()["channels"]}
        self.assertEqual(rep, {"https://youtube.com/@one": 3, "https://youtube.com/@two": 2})
        for j in self.added:
            self.assertEqual((j.kind, j.params["section"], j.params["template"]), ("rewrite", "slides", self.tpl["id"]))
            self.assertTrue(j.params["force"])

    def test_job_runs_by_template_not_by_live_settings(self) -> None:
        self.launch({"count": 2})
        snap = self.added[0].params["_settings"]
        self.assertEqual(snap["slides_publish_channels"], SLIDES["slides_publish_channels"])
        self.assertEqual(snap["slides_publish_step"], 2)
        self.assertEqual(snap["ai_gen_enabled"], True)
        live = self.client.get("/api/settings").json()
        self.assertEqual(live["slides_publish_channels"], ["zz/ZZ"], "текущие настройки запуск не трогает")
        self.assertEqual(live["slides_publish_dates"], ["2027-01-01"], "и календарь в настройках не очищает")

    def test_start_date_and_step_make_dates(self) -> None:
        from engine.publish import schedule

        start = date.today() + timedelta(days=3)
        r = self.launch({"count": 3, "start": start.isoformat(), "step": 2})
        self.assertEqual(r.status_code, 200, r.text)
        plan = self.added[0].params["publish_plan"]
        self.assertTrue(all(j.params["publish_plan"] == plan for j in self.added), "один план на весь запуск")
        got = [schedule.take_plan_date(plan)[0] for _ in range(3)]
        self.assertEqual(got, [start, start + timedelta(days=2), start + timedelta(days=4)])
        self.assertEqual(schedule.plan_step(plan), 2)

    def test_without_start_date_follows_last_scheduled_with_template_step(self) -> None:
        from engine.publish import schedule

        self.launch({"count": 2})
        plan = self.added[0].params["publish_plan"]
        self.assertEqual(schedule.take_plan_date(plan)[0], None, "без даты — «после последнего запланированного»")
        self.assertEqual(schedule.plan_step(plan), 2, "шаг — из шаблона")

    def test_channels_override_only_for_this_launch_or_saved(self) -> None:
        self.launch({"count": 1, "channels": ["p1/UC1"]})
        self.assertEqual(self.added[0].params["_settings"]["slides_publish_channels"], ["p1/UC1"])
        self.assertEqual(self.client.get("/api/templates").json()[0]["summary"]["channels"], SLIDES["slides_publish_channels"],
                         "шаблон не изменился")
        self.launch({"count": 1, "channels": ["p1/UC2"], "save_channels": True})
        self.assertEqual(self.client.get("/api/templates").json()[0]["summary"]["channels"], ["p1/UC2"], "сохранили в шаблон")

    def test_errors(self) -> None:
        self.assertEqual(self.launch({"count": 1, "channels": []}).status_code, 400, "выкладка включена, а каналов нет")
        past = (date.today() - timedelta(days=1)).isoformat()
        self.assertEqual(self.launch({"count": 1, "start": past}).status_code, 400)
        self.assertEqual(self.launch({"count": 1, "start": "завтра"}).status_code, 400)
        self.assertEqual(self.client.post("/api/templates/нет/launch", json={"count": 1}).status_code, 404)
        self.assertEqual(self.launch({"count": 3}, picks={"https://youtube.com/@one": [], "https://youtube.com/@two": []}).status_code, 400,
                         "брать больше нечего")
        self.assertFalse(self.added, "ничего не поставлено")

    def test_fewer_videos_than_asked_is_reported(self) -> None:
        r = self.launch({"count": 5}, picks={"https://youtube.com/@one": ["a1"], "https://youtube.com/@two": ["b1"]})
        self.assertEqual(len(self.added), 2)
        self.assertTrue(any("2 из 5" in n for n in r.json()["notes"]))

    def test_template_without_competitors(self) -> None:
        self.client.patch("/api/settings", json={"rewrite_channels": {"broll": "", "slides": ""}})
        t = self.client.post("/api/templates", json={"name": "Пустой", "section": "slides"}).json()
        r = self.client.post(f"/api/templates/{t['id']}/launch", json={"count": 1})
        self.assertEqual(r.status_code, 400)
        self.assertIn("каналов-конкурентов", r.json()["detail"])

    def test_publish_off_still_makes_videos(self) -> None:
        self.client.patch("/api/settings", json={"slides_publish": False, "slides_publish_channels": [],
                                                 "rewrite_channels": SLIDES["rewrite_channels"]})
        t = self.client.post("/api/templates", json={"name": "Без выкладки", "section": "slides"}).json()
        with mock.patch("engine.rewrite.channels.pick", picker({"https://youtube.com/@one": ["a1"], "https://youtube.com/@two": []})):
            r = self.client.post(f"/api/templates/{t['id']}/launch", json={"count": 1})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(any("выключена" in n for n in r.json()["notes"]))
        self.assertNotIn("publish_plan", self.added[0].params)


if __name__ == "__main__":
    unittest.main()
