"""API новых разделов: голосования, поиск ниш, выбор «Чем переписывать» (только Claude/Codex)."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import shutil
import unittest
from unittest import mock

from test_api_server import ApiTestCase

from engine.publish import polls, registry
from engine.publish.adspower import Profile
from engine.publish.store import publish_dir


class SectionsApiTests(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        shutil.rmtree(publish_dir(), ignore_errors=True)
        self.addCleanup(shutil.rmtree, publish_dir(), True)
        registry.sync([Profile("p1", "1", "Истории")])
        registry.set_channels("p1", [registry.Channel("UCa", "Канал А", handle="@a", language="ru")])

    def test_meta_offers_only_subscriptions(self) -> None:
        engines = self.client.get("/api/meta").json()["rewrite_engines"]
        self.assertEqual(set(engines), {"claude_code", "codex"})
        self.assertEqual(self.client.get("/api/settings").json()["rewrite_engine"], "claude_code")

    def test_polls_generate(self) -> None:
        got = [polls.Poll("Вопрос?", ["Да", "Нет"])]
        with mock.patch.object(polls, "channel_titles", return_value=("Канал А", ["Ролик 1"])), \
                mock.patch.object(polls, "generate", return_value=got) as g:
            r = self.client.post("/api/polls/generate", json={"channel": "p1/UCa", "count": 99})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["polls"], [{"question": "Вопрос?", "options": ["Да", "Нет"]}])
        self.assertEqual(g.call_args.args[2], polls.COUNT_MAX)  # количество ограничено
        self.assertEqual(g.call_args.kwargs["language"], "ru")

    def test_polls_generate_errors_are_readable(self) -> None:
        self.assertEqual(self.client.post("/api/polls/generate", json={"channel": ""}).status_code, 400)
        with mock.patch.object(polls, "channel_titles", side_effect=RuntimeError("квота")):
            r = self.client.post("/api/polls/generate", json={"channel": "p1/UCa"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("квота", r.json()["detail"])

    def test_polls_history_newest_first(self) -> None:
        polls.remember("UCa", polls.Poll("Первый?", ["а", "б"]), None, "published")
        polls.remember("UCa", polls.Poll("Второй?", ["а", "б"]), None, "scheduled")
        items = self.client.get("/api/polls/history", params={"channel": "p1/UCa"}).json()["items"]
        self.assertEqual([i["question"] for i in items], ["Второй?", "Первый?"])

    def test_polls_job_validation(self) -> None:
        code, body = self.add_job("polls", {"channel": ""})
        self.assertEqual(code, 400, body)
        with mock.patch("server.runners._s", lambda cfg, k: "key" if k == "adspower_api_key" else ""):
            from server import runners

            title, _run = runners._polls_job({"adspower_api_key": "key"},
                                             {"channel": "p1/UCa", "count": 500, "step_days": 999}, {})
        self.assertIn("30 шт.", title)
        self.assertIn("каждые 60 дн.", title)

    def test_niches_runs_and_evergreen(self) -> None:
        body = self.client.get("/api/niches/runs").json()
        self.assertEqual(body["runs"], [])
        self.assertEqual(len(body["evergreen"]), 40)
        self.assertTrue(all(n["keywords"] for n in body["evergreen"]))

    def test_niches_delete_run_and_channel(self) -> None:
        from engine.research import niches

        niches.save({"id": "r1", "niche": "А", "at": 1, "channels": [{"id": "UC1"}, {"id": "UC2"}]})
        niches.save({"niche": "Старый без id", "at": 2.5, "channels": []})
        r = self.client.delete("/api/niches/runs/r1/channels/UC1")
        self.assertEqual(r.status_code, 200)
        run = next(x for x in r.json()["runs"] if x["id"] == "r1")
        self.assertEqual([c["id"] for c in run["channels"]], ["UC2"])
        self.assertEqual(self.client.delete("/api/niches/runs/r1/channels/UC1").status_code, 404)
        old = next(x for x in r.json()["runs"] if x["niche"] == "Старый без id")
        r = self.client.delete(f"/api/niches/runs/{old['id']}")
        self.assertEqual([x["id"] for x in r.json()["runs"]], ["r1"])
        self.assertEqual(self.client.delete("/api/niches/runs/nope").status_code, 404)

    def test_niches_job_needs_niche_and_keys(self) -> None:
        code, _ = self.add_job("niches", {"niche": "", "keywords": []})
        self.assertEqual(code, 400)
        code, body = self.add_job("niches", {"niche": "Истории"})
        self.assertEqual(code, 400)  # нет ключа YouTube API
        self.assertIn("YouTube", str(body))


if __name__ == "__main__":
    unittest.main()
