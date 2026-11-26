"""Ролик не собирается, если в нём меньше 80 РАЗНЫХ кадров; пул добирается по теме, а не по имени героя."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import tempfile
import random
import unittest
from pathlib import Path

from engine.slides import pool_floor, theme_plan
from engine.slides.media_pipeline import MediaError
from engine.slides.story_context import StoryContext


def _write(run: Path, **kw) -> None:
    (run / "timeline.json").write_text(json.dumps(kw), encoding="utf-8")


def _items(n: int, prefix: str = "pool/p") -> list[dict]:
    return [{"path": f"{prefix}{i}.jpg"} for i in range(n)]


class GateTests(unittest.TestCase):
    def test_eight_pictures_repeated_forever_is_rejected(self) -> None:
        # так выглядел ролик про LM317: 28 кадров начала (8 разных) + блок из тех же 8
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            head = [{"path": f"pool/p{i % 8}.jpg"} for i in range(28)]
            _write(run, slots=head, loop_block=_items(8))
            with self.assertRaises(MediaError) as ctx:
                pool_floor.check_timeline(run)
            self.assertIn("не меньше 80", str(ctx.exception))

    def test_four_photos_repeated_80_times_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            _write(run, slots=[{"path": f"pool/p{i % 4}.jpg"} for i in range(322)])
            with self.assertRaises(MediaError):
                pool_floor.check_timeline(run)

    def test_enough_different_frames_pass(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            _write(run, slots=_items(28, "pool/h"), loop_block=_items(60))
            self.assertEqual(pool_floor.check_timeline(run), 88)

    def test_theme_pool_counts_images_and_videos(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            _write(run, images=_items(75), videos=_items(10, "pool/v"))
            self.assertEqual(pool_floor.check_timeline(run), 85)

    def test_short_video_needs_proportionally_less(self) -> None:
        self.assertEqual(pool_floor.min_frames(2400), 80)
        self.assertEqual(pool_floor.min_frames(600), 40)


class _Client:
    def __init__(self, answer: dict) -> None:
        self.answer = answer

    def complete(self, **_kw) -> str:
        return json.dumps(self.answer)


class TopicQueriesTests(unittest.TestCase):
    def _plan(self) -> theme_plan.ThemePlan:
        ctx = StoryContext(era="1970s", place="California", characters=["Bob Dobkin, engineer"], brief="A voltage regulator")
        return theme_plan.ThemePlan(context=ctx, summary="regulator", image_queries=["Bob Dobkin photo"])

    def test_queries_never_contain_the_heros_name_or_repeat(self) -> None:
        client = _Client({"queries": ["Bob Dobkin portrait", "TO-220 voltage regulator macro", "heat sink fins close-up",
                                      "to-220 voltage regulator macro", "Bob Dobkin photo"]})
        got = pool_floor.topic_queries(self._plan(), "script text", client, used=["Bob Dobkin photo"])
        self.assertEqual(got, ["TO-220 voltage regulator macro", "heat sink fins close-up"])

    def test_llm_failure_gives_no_queries(self) -> None:
        class Broken:
            def complete(self, **_kw):
                raise RuntimeError("down")

        self.assertEqual(pool_floor.topic_queries(self._plan(), "x", Broken(), used=[]), [])


class HeroQueriesTests(unittest.TestCase):
    def _plan(self, subject: str) -> theme_plan.ThemePlan:
        ctx = StoryContext(era="1970s", place="California", characters=["Bob Dobkin"], subject=subject, person_era="photo")
        return theme_plan.ThemePlan(context=ctx, image_queries=[f"voltage regulator {i}" for i in range(12)],
                                    video_queries=["circuit board macro"])

    def test_tech_story_keeps_topic_queries_in_front(self) -> None:
        plan = self._plan("history")
        theme_plan._bias_main_character(plan)
        self.assertEqual(sum("Bob Dobkin" in q for q in plan.image_queries), 0)
        self.assertGreaterEqual(sum("regulator" in q for q in plan.image_queries), 12)

    def test_biography_still_leads_with_the_person(self) -> None:
        plan = self._plan("person")
        theme_plan._bias_main_character(plan)
        self.assertGreaterEqual(sum("Bob Dobkin" in q for q in plan.image_queries), 8)


class FocusTests(unittest.TestCase):
    def _plan(self, **raw) -> theme_plan.ThemePlan:
        base = {"era": "1970s", "place": "California", "characters": ["Bob Dobkin"], "subject": "history"}
        ctx = StoryContext.from_dict({**base, **raw})
        return theme_plan.ThemePlan(context=ctx, image_queries=[f"voltage regulator {i}" for i in range(14)],
                                    video_queries=["circuit board macro"])

    def test_context_fields_agree(self) -> None:
        mixed = StoryContext.from_dict({"subject": "person", "focus": "mixed", "historical": True})
        self.assertEqual((mixed.subject, mixed.focus), ("history", "mixed"))
        biography = StoryContext.from_dict({"subject": "other", "focus": "person"})
        self.assertEqual(biography.subject, "person")
        self.assertEqual(StoryContext.from_dict({"subject": "person"}).focus, "")  # старый запуск — по subject

    def test_thing_and_creator_gets_both(self) -> None:
        plan = self._plan(focus="mixed", key_people=[{"name": "Hans Camenzind", "search": "Hans Camenzind Signetics engineer"}])
        theme_plan._bias_main_character(plan)
        people = [q for q in plan.image_queries if "Camenzind" in q]
        self.assertEqual(len(people), 6, "треть пула — сам человек")
        self.assertTrue(all("Signetics engineer" in q or q == "Hans Camenzind Signetics engineer" for q in people))
        self.assertGreaterEqual(sum("regulator" in q for q in plan.image_queries), 14, "остальное — по теме")
        self.assertTrue(any("Camenzind" in q for q in plan.video_queries))

    def test_several_creators_share_the_person_queries(self) -> None:
        plan = self._plan(focus="mixed", key_people=[{"name": "Ann One"}, {"name": "Bob Two"}, {"name": "Cy Three"}])
        theme_plan._bias_main_character(plan)
        for name in ("Ann One", "Bob Two", "Cy Three"):
            self.assertEqual(sum(name in q for q in plan.image_queries), 2, name)

    def test_biography_and_topic(self) -> None:
        bio = self._plan(focus="person", key_people=[{"name": "Bob Dobkin"}], person_era="photo")
        theme_plan._bias_main_character(bio)
        self.assertGreaterEqual(sum("Bob Dobkin" in q for q in bio.image_queries), 8)
        topic = self._plan(focus="topic")
        theme_plan._bias_main_character(topic)
        self.assertEqual(sum("Bob Dobkin" in q for q in topic.image_queries), 0)

    def test_hero_names_cover_all_key_people(self) -> None:
        ctx = StoryContext.from_dict({"focus": "mixed", "key_people": [{"name": "Ann One"}, {"name": "Bob Two"}]})
        self.assertEqual(theme_plan.hero_names(ctx), ["ann one", "bob two"])
        self.assertEqual(theme_plan.hero_names(StoryContext.from_dict({"characters": ["Bob Dobkin"]})), ["bob dobkin"])

    def test_context_reads_the_whole_script(self) -> None:
        from engine.slides import story_context

        seen: list[str] = []

        class Client:
            def complete(self, *, label, system, user, **_kw):
                seen.append(user)
                return '{"focus": "mixed", "person_share": 35, "key_people": [{"name": "Hans Camenzind"}]}'

        text = ("Начало. " * 2000) + "СЕРЕДИНА МОНТАЖ " + ("конец. " * 5000) + "ФИНАЛ ИСТОРИИ"
        ctx = story_context.extract_story_context(text, Client())
        self.assertIn("ФИНАЛ ИСТОРИИ", seen[0], "последняя часть сценария тоже прочитана")
        self.assertGreater(len(seen[0]), 30000)
        self.assertEqual((ctx.focus, ctx.person_share), ("mixed", 35))



class JudgeCompactTests(unittest.TestCase):
    def test_compact_answer_is_parsed_in_order(self) -> None:
        from engine.slides import judge

        got = judge._parse('{"r": [[7, 6, 8, ""], [2, 3, 5, "wrong country"], [9, 9, 9]]}', 3)
        self.assertEqual([(v.context, v.beat, v.quality, v.reject) for v in got],
                         [(7, 6, 8, ""), (2, 3, 5, "wrong country"), (9, 9, 9, "")])
        self.assertTrue(got[0].ok and not got[1].ok)

    def test_old_keyed_answer_still_works(self) -> None:
        from engine.slides import judge

        got = judge._parse('{"items": [{"n": 1, "context": 7, "beat": 6, "quality": 8, "reject": ""}]}', 2)
        self.assertEqual(got[0].context, 7)
        self.assertEqual(got[1].reject, "нет оценки", "кандидат без оценки не берётся")

    def test_prompt_asks_for_the_short_form(self) -> None:
        from engine.slides import judge

        self.assertIn('{"r":', judge._SYSTEM)


class FileDefectCacheTests(unittest.TestCase):
    def test_defective_file_is_not_sent_to_the_judge_twice(self) -> None:
        import os
        from unittest import mock

        from engine.slides import judge_cache, theme_media
        from engine.slides.judge import Verdict
        from engine.slides.stocks_base import StockCandidate

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["STORY_VIDEO_JUDGE_CACHE_DIR"] = tmp
            self.addCleanup(os.environ.pop, "STORY_VIDEO_JUDGE_CACHE_DIR", None)
            judge_cache.reset_for_tests()
            self.addCleanup(judge_cache.reset_for_tests)
            img = Path(tmp) / "a.jpg"
            img.write_bytes(b"some image bytes")
            cand = StockCandidate(provider="google", id="1", kind="photo", query="q", preview_url="p", download_url="u", width=1, height=1)
            judge = mock.MagicMock()
            judge.pool.return_value.rate.return_value = Verdict(2, 2, 3, "watermark")
            self.assertIsNone(theme_media._judge_pool_item(judge, img, cand, prefer_video=False))
            self.assertEqual(judge.pool.return_value.rate.call_count, 1)
            self.assertIsNone(theme_media._judge_pool_item(judge, img, cand, prefer_video=False))
            self.assertEqual(judge.pool.return_value.rate.call_count, 1, "второй раз — без запроса к нейросети")

    def test_wrong_country_is_not_remembered(self) -> None:
        import os
        from unittest import mock

        from engine.slides import judge_cache, theme_media
        from engine.slides.judge import Verdict
        from engine.slides.stocks_base import StockCandidate

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["STORY_VIDEO_JUDGE_CACHE_DIR"] = tmp
            self.addCleanup(os.environ.pop, "STORY_VIDEO_JUDGE_CACHE_DIR", None)
            judge_cache.reset_for_tests()
            self.addCleanup(judge_cache.reset_for_tests)
            img = Path(tmp) / "b.jpg"
            img.write_bytes(b"other image")
            cand = StockCandidate(provider="google", id="2", kind="photo", query="q", preview_url="p", download_url="u", width=1, height=1)
            judge = mock.MagicMock()
            judge.pool.return_value.rate.return_value = Verdict(1, 1, 5, "wrong country")
            theme_media._judge_pool_item(judge, img, cand, prefer_video=False)
            theme_media._judge_pool_item(judge, img, cand, prefer_video=False)
            self.assertEqual(judge.pool.return_value.rate.call_count, 2, "чужая страна зависит от ролика — проверяем заново")



class CheapJudgeAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        import os

        from engine.slides import judge_audit

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["STORY_VIDEO_JUDGE_CACHE_DIR"] = self.tmp.name
        self.addCleanup(os.environ.pop, "STORY_VIDEO_JUDGE_CACHE_DIR", None)
        judge_audit.reset_for_tests()
        self.addCleanup(judge_audit.reset_for_tests)
        self.audit = judge_audit

    def test_cheap_model_until_there_is_enough_evidence(self) -> None:
        from engine.slides import llm_models

        self.assertTrue(self.audit.use_light())
        self.assertEqual(llm_models.vision_tier("judge_pool"), "light")
        self.assertEqual(llm_models.vision_tier("judge_5"), "smart", "кадры под фразу — всегда умная")
        self.assertEqual(llm_models.vision_tier("judge_ai"), "smart")

    def test_good_agreement_keeps_the_cheap_model(self) -> None:
        self.audit.record([(True, True)] * 20 + [(False, False)] * 12 + [(False, True)] * 4 + [(True, False)] * 2)
        self.assertEqual(self.audit.stats()["compared"], 38)
        self.assertTrue(self.audit.use_light())

    def test_cheap_model_letting_bad_frames_through_is_switched_off(self) -> None:
        from engine.slides import llm_models

        self.audit.record([(True, True)] * 20 + [(True, False)] * 8 + [(False, False)] * 10)
        self.assertFalse(self.audit.use_light(), "ложных принятий 21% — возвращаемся на умную")
        self.assertEqual(llm_models.vision_tier("judge_pool"), "smart")

    def test_stats_survive_restart(self) -> None:
        self.audit.record([(True, False)] * 40)
        self.audit.reset_for_tests()
        self.assertEqual(self.audit.stats()["compared"], 40)

    def test_every_eighth_sheet_is_double_checked(self) -> None:
        picks = [self.audit.should_sample() for _ in range(17)]
        self.assertEqual([i for i, p in enumerate(picks) if p], [0, 8, 16])

    def test_sampled_sheet_uses_the_smart_answer(self) -> None:
        from unittest import mock

        from engine.slides.judge import PoolBatcher, Verdict

        cheap = [Verdict(8, 8, 8, ""), Verdict(8, 8, 8, "")]
        smart = [Verdict(8, 8, 8, ""), Verdict(1, 1, 1, "wrong subject")]
        judge = mock.MagicMock()
        judge.client.model = ""
        judge.client.base_url, judge.client.api_key = "https://x/v1", "k"
        judge.rate.return_value = smart
        with mock.patch("engine.slides.llm_models.chain", return_value=["big-model"]):
            got = PoolBatcher(judge)._audit([Path("a"), Path("b")], cheap)
        self.assertEqual(got, smart)
        self.assertEqual(judge.rate.call_args.kwargs["model"], "big-model")
        self.assertEqual(self.audit.stats()["false_accept"], 1)

    def test_own_model_in_settings_is_not_audited(self) -> None:
        from unittest import mock

        from engine.slides.judge import PoolBatcher, Verdict

        judge = mock.MagicMock()
        judge.client.model = "my-model"
        cheap = [Verdict(8, 8, 8, "")]
        self.assertEqual(PoolBatcher(judge)._audit([Path("a")], cheap), cheap)
        judge.rate.assert_not_called()


class VideoSecondPassTests(unittest.TestCase):
    """26.11: сеть была забита, клипы не скачались — в пуле 0 видео. Теперь малое число видео — второй заход."""

    def test_few_videos_trigger_second_pass_and_budget_starts_after_search(self) -> None:
        from unittest import mock

        from engine.slides import theme_media as tm

        calls: list[tuple[str, int]] = []

        def fake_fill(*a, **k):
            calls.append((k["kind"], len(k.get("already") or [])))
            if k["kind"] == "video":
                return ([{"path": f"v{len(calls)}.mp4"}] * (3 if len(calls) == 2 else 0)), 0
            return [], 0

        plan = mock.Mock()
        plan.video_queries, plan.image_queries = ["q"], ["q"]
        req = mock.Mock(source_mix=None)
        with mock.patch.object(tm, "_fill_kind", fake_fill), mock.patch.object(tm, "_google_need", lambda *a: 0), \
                mock.patch.object(tm, "_stocks_need", lambda *a, **k: 0, create=True):
            try:
                tm.gather_pool(None, {}, plan, rng=random.Random(1), pool=Path("."), trials=Path("."), frames=Path("."),
                               req=req, cancel=None, progress=lambda *a: None, pct_lo=0, pct_hi=1,
                               image_need=0, video_need=12, video_search_sec=1.0)
            except Exception:
                pass  # дальше — картинки, их здесь не проверяем
        self.assertEqual([c for c in calls if c[0] == "video"][:2], [("video", 0), ("video", 0)],
                         "второй заход по видео сделан")


class FrameLinesTests(unittest.TestCase):
    def test_counts_photos_videos_ai_and_sources_once_per_file(self) -> None:
        import json
        import tempfile

        from engine.slides.paths import TIMELINE_NAME
        from engine.slides.pool_floor import frame_lines, frame_stats

        raw = {
            "slots": [{"kind": "image", "path": "a.jpg", "provider": "google"},
                      {"kind": "video", "path": "v1.mp4", "provider": "pexels"}],
            "loop_block": [{"kind": "image", "path": "a.jpg", "provider": "google"},
                           {"kind": "image", "path": "b.jpg", "provider": "pexels"},
                           {"kind": "image", "path": "c.png", "provider": "ai"},
                           {"kind": "video", "path": "v2.mp4", "provider": "pixabay"}],
        }
        st = frame_stats(raw)
        self.assertEqual((st["total"], st["photos"], st["videos"], st["ai"]), (5, 2, 2, 1))
        self.assertEqual(st["sources"]["pexels"], 2)
        d = Path(tempfile.mkdtemp())
        (d / TIMELINE_NAME).write_text(json.dumps(raw), encoding="utf-8")
        lines = frame_lines(d)
        self.assertIn("5 разных — фото 2, видео 2, AI-кадров 1", lines[0])
        self.assertIn("pexels 2", lines[1])
        self.assertEqual(frame_lines(Path(tempfile.mkdtemp())), [], "нет таймлайна — строк нет")


if __name__ == "__main__":
    unittest.main()
