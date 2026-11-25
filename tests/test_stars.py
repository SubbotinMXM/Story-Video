"""Ролик про людей (любой режим): разметка по героям, кадры только своего героя, главы по героям, тёзки."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from engine.slides import stars_media as st

CAST = json.dumps({
    "people": True,
    "stars": [
        {"id": 1, "name": "Юрий Никулин", "latin": "Yuri Nikulin", "search": "Юрий Никулин", "kind": "photo", "years": "1921–1997",
         "works": ["Бриллиантовая рука", "Кавказская пленница"]},
        {"id": 2, "name": "Андрей Миронов", "latin": "Andrei Mironov", "search": "Андрей Миронов", "kind": "photo"},
        {"id": 3, "name": "Упомянут мимоходом", "latin": "x", "search": "x", "kind": "photo"},
        {"id": 0, "name": "плохой id"},
    ],
    "parts": [{"i": 0, "stars": []}, {"i": 2, "stars": [1]}, {"i": 3, "stars": [1]}, {"i": 5, "stars": [2]},
              {"i": 8, "stars": [9]}, {"i": 9, "stars": []}],
}, ensure_ascii=False)


def _person_plan():
    from engine.slides.story_context import StoryContext
    from engine.slides.theme_plan import ThemePlan

    return ThemePlan(summary="Актёры", context=StoryContext(subject="person", characters=["Yuri Nikulin"]))


PERSON_PLAN = _person_plan()


def sents(n: int = 10, step: float = 30.0) -> list[tuple[float, str]]:
    return [(i * step, f"фраза {i}") for i in range(n)]


def photo(path: Path, seed: int) -> Path:
    rng = random.Random(seed)
    img = Image.new("RGB", (16, 9))
    img.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(16 * 9)])
    img.resize((900, 1200)).save(path, "JPEG")
    return path


class CastTests(unittest.TestCase):
    def test_parse_merges_and_drops_unknown(self) -> None:
        cast = st._parse_cast(CAST, 10)
        self.assertEqual([s.name for s in cast.stars], ["Юрий Никулин", "Андрей Миронов"], "без героя, у которого нет своего блока")
        self.assertEqual(cast.parts, [(0, ()), (2, (1,)), (5, (2,)), (8, ())], "подряд один состав — один блок; неизвестный id — общее")
        self.assertEqual(cast.stars[0].years, "1921–1997")
        self.assertEqual(cast.stars[0].works, ["Бриллиантовая рука", "Кавказская пленница"])
        self.assertTrue(cast.people)

    def test_parse_old_single_star_format_and_pairs(self) -> None:
        raw = json.dumps({"stars": [{"id": 1, "name": "А Б"}, {"id": 2, "name": "В Г"}],
                          "parts": [{"i": 0, "star": 1}, {"i": 4, "stars": [1, 2]}]})
        self.assertEqual(st._parse_cast(raw, 10).parts, [(0, (1,)), (4, (1, 2))])

    def test_part_times(self) -> None:
        times = st.part_times([(0, ()), (2, (1,)), (5, (2,)), (8, ())], sents(), 300.0)
        self.assertEqual(times, [(0.0, 60.0, ()), (60.0, 150.0, (1,)), (150.0, 240.0, (2,)), (240.0, 300.0, ())])

    def test_cast_tries_other_model(self) -> None:
        bad = mock.MagicMock()
        bad.complete.side_effect = RuntimeError("503")
        good = mock.MagicMock()
        good.complete.return_value = CAST
        with mock.patch("engine.slides.llm_client.LLMClient", side_effect=[bad, good]):
            cast = st.plan_cast(("k", "u", "m"), sents())
        self.assertEqual(len(cast.stars), 2)
        self.assertTrue(cast.parts)

    def test_prompt_has_times_rules_and_hint(self) -> None:
        text = st._prompt(sents(5, 60.0), duration=300.0, hint="ПОДСКАЗКА: на обложке 3 чел.")
        self.assertIn("[2] 2:00 фраза 2", text, "у фраз есть метки времени — по ним LLM судит о длине блоков")
        self.assertIn("ПОДСКАЗКА: на обложке 3 чел.", text)
        self.assertIn(f"НЕ БОЛЬШЕ {st.MAX_CHAPTERS_TOTAL}", text)
        self.assertIn("works", text)


class ShapeTests(unittest.TestCase):
    """Блоки приводятся к виду, пригодному для глав и подбора кадров."""

    def S(self):  # 20 фраз по минуте — 20 минут; самый короткий блок героя — 100 с
        return sents(20, 60.0)

    def test_general_in_the_middle_goes_to_previous_hero(self) -> None:
        got = st.shape_parts([(0, (1,)), (5, ()), (6, (2,)), (12, ())], self.S(), 1200.0)
        self.assertEqual(got, [(0, (1,)), (6, (2,)), (12, ())], "общий кусок посреди — часть героя; заключение остаётся")

    def test_short_blocks_become_one_duet_block(self) -> None:
        # А и Б по минуте, друг за другом: разница в 60 с между главами — бессмыслица, это один блок на двоих
        got = st.shape_parts([(0, ()), (1, (1,)), (2, (2,)), (3, (1,)), (4, (2,)), (10, (3,)), (15, ())], self.S(), 1200.0)
        self.assertEqual(got, [(0, ()), (1, (1, 2)), (4, (2,)), (10, (3,)), (15, ())])
        times = st.part_times(got, self.S(), 1200.0)
        for t0, t1, ids in times:
            if ids:
                self.assertGreaterEqual(t1 - t0, st.min_block_sec(1200.0))

    def test_block_count_capped(self) -> None:
        parts = [(i, (i + 1,)) for i in range(20)]
        got = st.shape_parts(parts, self.S(), 1200.0, max_total=6)
        self.assertLessEqual(len(got), 6)
        self.assertEqual(got[0][0], 0)
        covered = {x for _i, ids in got for x in ids}
        self.assertEqual(covered, set(range(1, 21)), "никто из героев не потерян — они в составе общих блоков")

    def test_tiny_intro_and_outro_join_neighbours(self) -> None:
        s = [(0.0, "a"), (5.0, "b"), (200.0, "c"), (400.0, "d"), (405.0, "e")]
        got = st.shape_parts([(0, ()), (1, (1,)), (2, (2,)), (4, ())], s, 410.0)
        self.assertEqual(got, [(0, (1,)), (2, (2,))], "вступление в 5 с и заключение в 5 с — не главы")

    def test_one_hero_stays_one_block(self) -> None:
        self.assertEqual(st.shape_parts([(0, (1,)), (7, (1,))], self.S(), 1200.0), [(0, (1,))])

    def test_min_block_scales_with_length(self) -> None:
        self.assertEqual(st.min_block_sec(300), 45.0)
        self.assertEqual(st.min_block_sec(1800), 100.0)


class SlotsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stars = [st.Star(1, "Никулин", "Nikulin", "Никулин"), st.Star(2, "Миронов", "Mironov", "Миронов")]
        self.media = {
            1: [{"path": f"pool/s01/p{k}.jpg", "kind": "image", "star": 1} for k in range(40)]
            + [{"path": f"pool/s01/v{k}.mp4", "kind": "video", "star": 1, "duration": 8.0} for k in range(2)],
            2: [{"path": f"pool/s02/p{k}.jpg", "kind": "image", "star": 2} for k in range(40)]
            + [{"path": "pool/s02/v0.mp4", "kind": "video", "star": 2, "duration": 8.0}],
        }
        self.parts = [(0.0, 30.0, ()), (30.0, 150.0, (1,)), (150.0, 270.0, (2,)), (270.0, 300.0, ())]

    def test_each_block_only_its_star_no_gaps_and_border_exact(self) -> None:
        slots = st.build_slots(self.parts, self.media, self.stars, rng=random.Random(1))
        for s in slots:
            if 30.0 <= s["start"] < 150.0:
                self.assertIn("/s01/", s["path"], "на блоке про Никулина — только он")
            if 150.0 <= s["start"] < 270.0:
                self.assertIn("/s02/", s["path"])
        for a, b in zip(slots, slots[1:]):
            self.assertAlmostEqual(a["end"], b["start"], places=2, msg="без дыр")
        self.assertEqual(slots[0]["start"], 0.0)
        self.assertEqual(slots[-1]["end"], 300.0)
        self.assertTrue(any(s["start"] == 150.0 for s in slots), "смена героя — ровно на границе блока")
        self.assertTrue(all(s["duration"] <= st.VIDEO_MAX for s in slots if s["kind"] == "video"))

    def test_no_frame_repeats_anywhere_when_material_is_enough(self) -> None:
        slots = st.build_slots(self.parts, self.media, self.stars, rng=random.Random(2))
        paths = [s["path"] for s in slots]
        self.assertEqual(len(paths), len(set(paths)), "ни одного повтора на весь ролик, пока кадров хватает")

    def test_videos_spread_through_the_block(self) -> None:
        slots = st.build_slots([(0.0, 240.0, (1,))], self.media, self.stars, rng=random.Random(3))
        kinds = [s["kind"] for s in slots]
        self.assertEqual(kinds.count("video"), 2)
        first, second = [k for k, v in enumerate(kinds) if v == "video"]
        self.assertGreater(second - first, 3, "видео не кучкой, а вразбивку")
        self.assertGreater(first, 0)

    def test_pair_block_shows_both_heroes(self) -> None:
        slots = st.build_slots([(0.0, 120.0, (1, 2))], self.media, self.stars, rng=random.Random(1))
        owners = {s["path"].split("/")[1] for s in slots}
        self.assertEqual(owners, {"s01", "s02"})

    def test_intro_and_outro_use_leftovers_not_used_frames(self) -> None:
        slots = st.build_slots(self.parts, self.media, self.stars, rng=random.Random(4))
        edge = [s["path"] for s in slots if s["start"] < 30.0 or s["start"] >= 270.0]
        body = {s["path"] for s in slots if 30.0 <= s["start"] < 270.0}
        self.assertTrue(edge)
        self.assertFalse(set(edge) & body)

    def test_small_pool_slows_down_instead_of_repeating_in_a_row(self) -> None:
        media = {1: [{"path": f"pool/s01/p{k}.jpg", "kind": "image", "star": 1} for k in range(10)]}
        slots = st.build_slots([(0.0, 60.0, (1,))], media, self.stars, rng=random.Random(1))
        self.assertEqual(len({s["path"] for s in slots}), len(slots), "10 фото на минуту — хватает без повторов")
        self.assertGreaterEqual(slots[0]["duration"], st.IMAGE_HOLD)
        tiny = {1: media[1][:3]}
        slots = st.build_slots([(0.0, 60.0, (1,))], tiny, self.stars, rng=random.Random(1))
        for a, b in zip(slots, slots[1:]):
            self.assertNotEqual(a["path"], b["path"], "одинаковые кадры подряд")
        self.assertEqual(slots[-1]["end"], 60.0)

    def test_star_without_photos_gets_neutral_frames_not_someone_else(self) -> None:
        media = {1: self.media[1], 2: []}
        neutral = [{"path": f"pool/neutral/n{k}.jpg", "kind": "image", "star": 0} for k in range(3)]
        slots = st.build_slots([(0.0, 30.0, (2,))], media, self.stars, rng=random.Random(1), neutral=neutral)
        self.assertTrue(slots)
        self.assertTrue(all(x["path"].startswith("pool/neutral/") for x in slots), "чужое лицо вместо героя — нельзя")

    def test_broll_spread_between_blocks_each_once(self) -> None:
        broll = [{"path": f"pool/broll/b{k}.mp4", "kind": "video", "duration": 7.0, "star": 0} for k in range(3)]
        parts = [(0.0, 100.0, (1,)), (100.0, 200.0, (2,)), (200.0, 300.0, (1,))]
        slots = st.build_slots(parts, self.media, self.stars, rng=random.Random(1), broll=broll)
        opened = [s["path"] for s in slots if "/broll/" in s["path"]]
        self.assertEqual(sorted(opened), sorted(b["path"] for b in broll), "каждое атмосферное видео — один раз")
        blocks = {0 if s["start"] < 100 else 1 if s["start"] < 200 else 2 for s in slots if "/broll/" in s["path"]}
        self.assertEqual(blocks, {0, 1, 2}, "по блокам, а не все в один")
        self.assertEqual(st.foreign_slots(slots, parts, self.stars), [], "атмосферные видео — не «чужой герой»")

    def test_foreign_slots_detected(self) -> None:
        slots = [{"start": 10.0, "star": "Миронов"}, {"start": 20.0, "star": "Никулин"}, {"start": 30.0, "star": ""}]
        bad = st.foreign_slots(slots, [(0.0, 60.0, (1,))], self.stars)
        self.assertEqual([b["star"] for b in bad], ["Миронов"])

    def test_real_layout_never_shows_foreign_hero(self) -> None:
        slots = st.build_slots(self.parts, self.media, self.stars, rng=random.Random(5))
        self.assertEqual(st.foreign_slots(slots, self.parts, self.stars), [])


class DedupeTests(unittest.TestCase):
    def test_same_photo_of_two_heroes_stays_with_the_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "pool").mkdir()
            photo(run / "pool" / "a.jpg", 1)
            (run / "pool" / "b.jpg").write_bytes((run / "pool" / "a.jpg").read_bytes())
            photo(run / "pool" / "c.jpg", 2)
            media = {1: [{"path": "pool/a.jpg", "kind": "image"}], 2: [{"path": "pool/b.jpg", "kind": "image"},
                                                                         {"path": "pool/c.jpg", "kind": "image"}]}
            self.assertEqual(st.dedupe_media(media, run, [1, 2]), 1)
            self.assertEqual([it["path"] for it in media[2]], ["pool/c.jpg"])
            self.assertTrue((run / "pool" / "a.jpg").exists())
            self.assertFalse((run / "pool" / "b.jpg").exists())


class QueriesAndTargetsTests(unittest.TestCase):
    def test_variants_use_works_from_the_text(self) -> None:
        star = st.Star(1, "Михаил Боярский", "Mikhail Boyarsky", "Михаил Боярский", works=["Д'Артаньян и три мушкетёра"])
        q = st.variant_queries(star, "Михаил Боярский")
        self.assertIn("Михаил Боярский в фильме Д'Артаньян и три мушкетёра", q, "кадры из фильма — по смыслу рассказа")
        self.assertIn("Михаил Боярский в молодости", q)
        plan = st._star_plan(PERSON_PLAN, star, star.search)
        self.assertIn("Михаил Боярский в фильме Д'Артаньян и три мушкетёра", plan.image_queries)

    def test_photo_and_video_targets(self) -> None:
        self.assertEqual(st.photo_targets(1200, 1), (100, 10), "один герой — фото на весь ролик, много видео")
        self.assertEqual(st.photo_targets(270, 8), (45, 2))
        self.assertEqual(st.photo_targets(60, 8)[1], 0)
        total_videos = 8 * st.photo_targets(270, 8)[1]
        self.assertGreaterEqual(total_videos, 10)
        eight = 8 * st.photo_targets(270, 8)[0]
        self.assertGreaterEqual(eight, 80, "на ролик из 8 героев — не меньше 80 фото в запасе")


class TopUpTests(unittest.TestCase):
    def test_topup_queries_are_different_words(self) -> None:
        star = st.Star(1, "Олег Даль", "Oleg Dal", "Олег Даль", works=["Экипаж"])
        q = st.topup_queries(star, "Олег Даль")
        self.assertIn("Олег Даль 1970-е", q)
        self.assertIn("Олег Даль Экипаж фото", q)
        self.assertEqual(len(q), len(set(q)))

    def test_floors_scale_with_length(self) -> None:
        self.assertEqual(st.photo_floor(1800), 100)
        self.assertEqual(st.video_floor(1800), 12)
        self.assertEqual(st.photo_floor(300), 30)
        self.assertGreaterEqual(st.video_floor(300), 3)


class ChaptersTests(unittest.TestCase):
    TL = {
        "stars": [{"id": 1, "name": "Юрий Никулин", "latin": "Yuri Nikulin"}, {"id": 2, "name": "Андрей Миронов", "latin": "Andrei Mironov"},
                  {"id": 3, "name": "Олег Басилашвили", "latin": "Oleg Basilashvili"}],
        "parts": [{"start": 0, "stars": []}, {"start": 40, "stars": [1]}, {"start": 300, "stars": [2, 3]},
                  {"start": 500, "stars": [1, 2, 3]}, {"start": 700, "stars": []}],
    }

    def test_by_block_with_duets_intro_and_outro(self) -> None:
        self.assertEqual(st.star_chapters(self.TL, "ru"), [
            (0.0, "Вступление"), (40.0, "Юрий Никулин"), (300.0, "Андрей Миронов и Олег Басилашвили"),
            (500.0, "Никулин, Миронов и Басилашвили"), (700.0, "Заключение"),
        ])

    def test_latin_names_for_other_languages(self) -> None:
        self.assertEqual(st.star_chapters(self.TL, "en")[1], (40.0, "Yuri Nikulin"))
        self.assertEqual(st.star_chapters(self.TL, "en")[2][1], "Andrei Mironov and Oleg Basilashvili")
        self.assertEqual(st.star_chapters(self.TL, "de")[0], (0.0, "Einleitung"))

    def test_old_timeline_format_still_works(self) -> None:
        tl = {"stars": self.TL["stars"][:2], "parts": [{"start": 0, "star": 0}, {"start": 40, "star": 1}, {"start": 300, "star": 2}]}
        self.assertEqual([t for _s, t in st.star_chapters(tl, "ru")], ["Вступление", "Юрий Никулин", "Андрей Миронов"])

    def test_many_names_in_one_title_are_shortened(self) -> None:
        tl = {"stars": [{"id": i, "name": f"Имя Фамилия{i}"} for i in range(1, 6)],
              "parts": [{"start": 0, "stars": [1, 2, 3, 4, 5]}]}
        self.assertEqual(st.star_chapters(tl, "ru")[0][1], "Фамилия1, Фамилия2 и другие")


class TimelineMatchTests(unittest.TestCase):
    def test_people_timeline_fits_any_mode(self) -> None:
        from engine.slides.resume import timeline_matches

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "pool").mkdir()
            (run / "timeline.json").write_text(json.dumps({"mode": "strict_sync", "variant": "stars", "slots": [{"x": 1}]}))
            self.assertTrue(timeline_matches(run, strict=False))
            self.assertTrue(timeline_matches(run, strict=True))
            self.assertTrue(timeline_matches(run, strict=True, full=True))


class StageTests(unittest.TestCase):
    def setUp(self) -> None:
        # маленькие фикстуры: норма «80 фото на ролик» проверяется в test_pool_floor
        p = unittest.mock.patch.object(st, "MIN_PHOTOS_PER_VIDEO", 1)
        p.start()
        self.addCleanup(p.stop)

    def test_stage_writes_timeline_and_uses_only_own_photos(self) -> None:
        from engine.slides.media_pipeline import MediaRequest, MediaSourcesConfig
        from engine.slides.theme_plan import ThemePlan

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "script.txt").write_text(" ".join(f"Фраза {i}." for i in range(10)), encoding="utf-8")
            (run / "timings.json").write_text(json.dumps({"words": [{"word": f"Фраза {i}.", "start": i * 30.0, "end": i * 30 + 1.0} for i in range(10)]}))

            def fake_star(http, clients, plan, star, *, seconds, run_dir, cancel, heroes=2):
                d = run_dir / "pool" / f"s{star.id:02d}"
                d.mkdir(parents=True, exist_ok=True)
                imgs = []
                for k in range(3):
                    photo(d / f"p{k}.jpg", star.id * 10 + k)
                    imgs.append({"path": f"pool/s{star.id:02d}/p{k}.jpg", "star": star.id, "provider": "google", "id": f"{star.id}{k}"})
                return [], imgs

            good = mock.MagicMock()
            good.complete.return_value = CAST
            with mock.patch("engine.slides.llm_client.LLMClient", return_value=good), \
                 mock.patch.object(st, "ensure_theme_plan", lambda *a: PERSON_PLAN), \
                 mock.patch.object(st, "collect_star", fake_star), \
                 mock.patch.object(st, "probe_duration", lambda p: 300.0), \
                 mock.patch("engine.slides.judge.make_judge", lambda *a: None), \
                 mock.patch.object(st, "_build_clients", lambda keys: {}):
                res = st.collect_people_timeline(MediaRequest(run_dir=run, keys=MediaSourcesConfig(), llm_api_key="k", seed=1))
            tl = json.loads(res.timeline_file.read_text(encoding="utf-8"))
            self.assertEqual(tl["variant"], "stars")
            for s in tl["slots"]:
                if 60 <= s["start"] < 150:
                    self.assertIn("/s01/", s["path"])
                if 150 <= s["start"] < 240:
                    self.assertIn("/s02/", s["path"])
            self.assertFalse((run / "thumbnail.jpg").exists(), "превью — отдельно, на вкладке «Превью»")
            self.assertTrue((run / "stars.json").is_file(), "разметка сохранена — «Продолжить» не спросит LLM снова")
            self.assertEqual(tl["parts"][1]["stars"], [1])
            self.assertEqual([p["star"] for p in tl["parts"]], [0, 1, 2, 0])
            report = json.loads((run / "stars_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["blocks"], 4)
            self.assertTrue(report["warnings"], "мало фото — в отчёте есть предупреждение, его видно в логе")



class RobustnessTests(unittest.TestCase):
    """Сбои по дороге не должны ронять сборку или оставлять фоновую работу."""
    def setUp(self) -> None:
        # маленькие фикстуры: норма «80 фото на ролик» проверяется в test_pool_floor
        p = unittest.mock.patch.object(st, "MIN_PHOTOS_PER_VIDEO", 1)
        p.start()
        self.addCleanup(p.stop)


    def stage(self, fake_star, *, judge=None, broll=None):
        from engine.slides.media_pipeline import MediaRequest, MediaSourcesConfig

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run = Path(tmp.name)
        (run / "script.txt").write_text(" ".join(f"Фраза {i}." for i in range(10)), encoding="utf-8")
        (run / "timings.json").write_text(json.dumps({"words": [{"word": f"Фраза {i}.", "start": i * 30.0, "end": i * 30 + 1.0} for i in range(10)]}))
        good = mock.MagicMock()
        good.complete.return_value = CAST
        patches = [mock.patch("engine.slides.llm_client.LLMClient", return_value=good),
                   mock.patch.object(st, "ensure_theme_plan", lambda *a: PERSON_PLAN),
                   mock.patch.object(st, "collect_star", fake_star),
                   mock.patch.object(st, "probe_duration", lambda p: 300.0),
                   mock.patch("engine.slides.judge.make_judge", lambda *a: judge),
                   mock.patch.object(st, "_build_clients", lambda keys: {}),
                   mock.patch.object(st, "broll_videos", broll or (lambda *a, **k: []))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        res = st.collect_people_timeline(MediaRequest(run_dir=run, keys=MediaSourcesConfig(), llm_api_key="k", seed=1))
        return run, res

    @staticmethod
    def own_photos(run_dir, star, n=3):
        d = run_dir / "pool" / f"s{star.id:02d}"
        d.mkdir(parents=True, exist_ok=True)
        out = []
        for k in range(n):
            photo(d / f"p{k}.jpg", star.id * 10 + k)
            out.append({"path": f"pool/s{star.id:02d}/p{k}.jpg", "star": star.id, "provider": "google", "id": f"{star.id}{k}"})
        return out

    def test_one_hero_search_failure_does_not_kill_the_stage(self) -> None:
        def fake(http, clients, plan, star, *, seconds, run_dir, cancel, heroes=2):
            if star.id == 2:
                raise RuntimeError("Bing упал")
            return [], self.own_photos(run_dir, star)

        run, res = self.stage(fake)
        self.assertIsNotNone(res, "один герой без фото — ролик всё равно собирается")
        tl = json.loads(res.timeline_file.read_text(encoding="utf-8"))
        names = {s["star"] for s in tl["slots"]}
        self.assertIn("Юрий Никулин", names)
        self.assertNotIn("Андрей Миронов", names, "чужого лица вместо героя нет")

    def test_judge_failure_keeps_photos(self) -> None:
        judge = mock.MagicMock()
        judge.enabled = True
        judge.pool.return_value.rate.side_effect = RuntimeError("LLM 503")

        def fake(http, clients, plan, star, *, seconds, run_dir, cancel, heroes=2):
            return [], self.own_photos(run_dir, star)

        run, res = self.stage(fake, judge=judge)
        self.assertIsNotNone(res)
        report = json.loads((run / "stars_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["heroes"]["Юрий Никулин"]["photos"], 3, "проверка не ответила — фото не потеряны")

    def test_background_video_search_is_stopped_when_stage_ends_early(self) -> None:
        import threading

        seen = {}

        def slow_broll(http, clients, run_dir, need, *, cancel=None, queries=None, stop=None):
            seen["stop"] = stop
            stop.wait(5)  # ждёт сигнала остановки
            seen["stopped"] = stop.is_set()
            return []

        run, res = self.stage(lambda *a, **k: ([], []), broll=slow_broll)
        self.assertIsNone(res, "ни у одного героя нет фото — подбор по режиму")
        self.assertTrue(seen["stop"].is_set(), "этап закончился — фоновому поиску видео сказали остановиться")
        self.assertTrue(seen["stopped"])
        self.assertFalse(any(t.name.startswith("ThreadPoolExecutor") and t.is_alive() and "broll" in t.name for t in threading.enumerate()))

    def test_stop_broll_helper_waits_for_the_thread(self) -> None:
        import threading
        import time

        from concurrent.futures import ThreadPoolExecutor

        stop = threading.Event()
        bg = ThreadPoolExecutor(max_workers=1)
        bg.submit(lambda: stop.wait(5))
        t = time.monotonic()
        st._stop_broll(bg, stop, wait_sec=3.0)
        self.assertLess(time.monotonic() - t, 2.0)
        self.assertTrue(stop.is_set())
        st._stop_broll(None, threading.Event())  # без фонового поиска — не падает

    def test_broll_videos_honours_stop_flag(self) -> None:
        import threading

        stop = threading.Event()
        stop.set()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("engine.slides.media_pipeline._search_source", side_effect=AssertionError("после остановки не ищем")):
                got = st.broll_videos(mock.MagicMock(), {}, Path(tmp), 5, stop=stop)
        self.assertEqual(got, [])

if __name__ == "__main__":
    unittest.main()


def _run(tmp: str) -> Path:
    run = Path(tmp)
    (run / "script.txt").write_text(" ".join(f"Фраза {i}." for i in range(10)), encoding="utf-8")
    (run / "timings.json").write_text(json.dumps({"words": [{"word": f"Фраза {i}.", "start": i * 30.0, "end": i * 30 + 1.0} for i in range(10)]}))
    return run


class AutoPeopleTests(unittest.TestCase):
    """Логика «Звёзд» сама включается в любом режиме, если ролик про людей."""
    def setUp(self) -> None:
        # маленькие фикстуры: норма «80 фото на ролик» проверяется в test_pool_floor
        p = unittest.mock.patch.object(st, "MIN_PHOTOS_PER_VIDEO", 1)
        p.start()
        self.addCleanup(p.stop)


    def _req(self, run: Path):
        from engine.slides.media_pipeline import MediaRequest, MediaSourcesConfig

        return MediaRequest(run_dir=run, keys=MediaSourcesConfig(), llm_api_key="k", seed=1)

    def test_not_about_people_skips_cast(self) -> None:
        from engine.slides.story_context import StoryContext
        from engine.slides.theme_plan import ThemePlan

        with tempfile.TemporaryDirectory() as tmp:
            run = _run(tmp)
            plan = ThemePlan(summary="Природа", context=StoryContext(subject="nature"))
            llm = mock.MagicMock()
            with mock.patch("engine.slides.llm_client.LLMClient", return_value=llm), \
                 mock.patch.object(st, "ensure_theme_plan", lambda *a: plan), \
                 mock.patch.object(st, "probe_duration", lambda p: 300.0):
                self.assertIsNone(st.collect_people_timeline(self._req(run)))
            llm.complete.assert_not_called()  # разметка по героям — лишний запрос, не делаем

    def test_no_heroes_answer_is_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _run(tmp)
            llm = mock.MagicMock()
            llm.complete.return_value = '{"stars": [], "parts": []}'
            with mock.patch("engine.slides.llm_client.LLMClient", return_value=llm), \
                 mock.patch.object(st, "ensure_theme_plan", lambda *a: PERSON_PLAN), \
                 mock.patch.object(st, "probe_duration", lambda p: 300.0):
                self.assertIsNone(st.collect_people_timeline(self._req(run)))
                self.assertIsNone(st.collect_people_timeline(self._req(run)))
            self.assertEqual(llm.complete.call_count, 1, "«героев нет» — одна модель, один раз, потом из stars.json")

    def test_no_photos_falls_back_to_mode(self) -> None:
        from engine.slides import media_pipeline as mp

        with tempfile.TemporaryDirectory() as tmp:
            run = _run(tmp)
            llm = mock.MagicMock()
            llm.complete.return_value = CAST
            seen = {}

            def theme(req, **kw):
                seen["people"] = req.people
                return "theme"

            with mock.patch("engine.slides.llm_client.LLMClient", return_value=llm), \
                 mock.patch.object(st, "ensure_theme_plan", lambda *a: PERSON_PLAN), \
                 mock.patch.object(st, "collect_star", lambda *a, **kw: ([], [])), \
                 mock.patch.object(st, "probe_duration", lambda p: 300.0), \
                 mock.patch("engine.slides.judge.make_judge", lambda *a: None), \
                 mock.patch.object(st, "_build_clients", lambda keys: {}), \
                 mock.patch("engine.slides.theme_media.collect_theme_pool", theme):
                self.assertEqual(mp.run_media_stage(self._req(run)), "theme")
            self.assertFalse(seen["people"], "те же поиски героя второй раз не повторяем")

    def test_strict_mode_also_by_heroes(self) -> None:
        from dataclasses import replace

        from engine.slides import media_pipeline as mp

        with tempfile.TemporaryDirectory() as tmp:
            run = _run(tmp)
            with mock.patch.object(st, "collect_people_timeline", lambda req, **kw: "people"), \
                 mock.patch("engine.slides.strict_media.collect_strict_timeline", lambda req, **kw: "strict"):
                self.assertEqual(mp.run_media_stage(replace(self._req(run), strict_sync=True)), "people")

    def test_history_with_several_names_asks_cast_but_not_people_is_skipped(self) -> None:
        """Ролик про съёмки/скандалы LLM пометил «история»: людей в нём несколько — спрашиваем разметку, и если
        это не про личностей, подбираем по режиму."""
        from engine.slides.story_context import StoryContext
        from engine.slides.theme_plan import ThemePlan

        with tempfile.TemporaryDirectory() as tmp:
            run = _run(tmp)
            plan = ThemePlan(summary="Съёмки", context=StoryContext(subject="history", characters=["Боярский", "Смехов", "Дуров"]))
            llm = mock.MagicMock()
            llm.complete.return_value = json.dumps({"people": False, "stars": [{"id": 1, "name": "Кто-то"}],
                                                    "parts": [{"i": 0, "stars": [1]}]}, ensure_ascii=False)
            with mock.patch("engine.slides.llm_client.LLMClient", return_value=llm), \
                 mock.patch.object(st, "ensure_theme_plan", lambda *a: plan), \
                 mock.patch.object(st, "probe_duration", lambda p: 300.0):
                self.assertIsNone(st.collect_people_timeline(self._req(run)))
            self.assertEqual(llm.complete.call_count, 1)

    def test_history_with_one_name_does_not_ask(self) -> None:
        from engine.slides.story_context import StoryContext
        from engine.slides.theme_plan import ThemePlan

        with tempfile.TemporaryDirectory() as tmp:
            run = _run(tmp)
            plan = ThemePlan(summary="Война", context=StoryContext(subject="history", characters=["Сталин"]))
            llm = mock.MagicMock()
            with mock.patch("engine.slides.llm_client.LLMClient", return_value=llm), \
                 mock.patch.object(st, "ensure_theme_plan", lambda *a: plan), \
                 mock.patch.object(st, "probe_duration", lambda p: 300.0):
                self.assertIsNone(st.collect_people_timeline(self._req(run)))
            llm.complete.assert_not_called()

    def test_history_about_people_goes_by_heroes(self) -> None:
        from engine.slides.story_context import StoryContext
        from engine.slides.theme_plan import ThemePlan

        with tempfile.TemporaryDirectory() as tmp:
            run = _run(tmp)
            plan = ThemePlan(summary="Съёмки", context=StoryContext(subject="history", characters=["Никулин", "Миронов"]))
            llm = mock.MagicMock()
            llm.complete.return_value = CAST

            def fake_star(http, clients, plan, star, *, seconds, run_dir, cancel, heroes=2):
                d = run_dir / "pool" / f"s{star.id:02d}"
                d.mkdir(parents=True, exist_ok=True)
                imgs = []
                for k in range(3):
                    photo(d / f"p{k}.jpg", star.id * 10 + k)
                    imgs.append({"path": f"pool/s{star.id:02d}/p{k}.jpg", "star": star.id, "provider": "google", "id": f"{star.id}{k}"})
                return [], imgs

            with mock.patch("engine.slides.llm_client.LLMClient", return_value=llm), \
                 mock.patch.object(st, "ensure_theme_plan", lambda *a: plan), \
                 mock.patch.object(st, "collect_star", fake_star), \
                 mock.patch.object(st, "probe_duration", lambda p: 300.0), \
                 mock.patch("engine.slides.judge.make_judge", lambda *a: None), \
                 mock.patch.object(st, "_build_clients", lambda keys: {}):
                res = st.collect_people_timeline(self._req(run))
            self.assertIsNotNone(res, "«история» про нескольких людей — по героям, а не по теме")

    def test_old_cache_format_is_recomputed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "stars.json"
            f.write_text(json.dumps({"stars": [{"id": 1, "name": "А", "latin": "A", "search": "А"}], "parts": [[0, 1]], "n": 10}))
            self.assertIsNone(st._load_cast(f, 10), "разметка старого вида (без v) не годится")

    def test_cover_hint_names_people_from_original_cover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "source.json").write_text(json.dumps({"url": "https://youtu.be/x"}))
            seen = {}

            def ask(llm, label, system, user, image):
                seen["label"] = label
                return {"names": ["Олег Даль", "Валентин Гафт"]}

            with mock.patch("engine.publish.cover.youtube_original", lambda url: ("Скандалисты", b"img", "ru")), \
                 mock.patch("engine.publish.cover._ask_image", ask):
                hint = st._cover_hint(run, self._req(run))
            self.assertIn("2 чел.", hint)
            self.assertIn("Олег Даль", hint)
            with mock.patch("engine.publish.cover.youtube_original", side_effect=AssertionError("второй раз не качаем")):
                self.assertEqual(st._cover_hint(run, self._req(run)), hint, "обложка разобрана один раз")

    def test_person_video_without_cast_stops_instead_of_silent_theme_mode(self) -> None:
        from engine.slides.media_pipeline import MediaError

        with tempfile.TemporaryDirectory() as tmp:
            run = _run(tmp)
            with mock.patch("engine.slides.llm_client.LLMClient", side_effect=RuntimeError("503")), \
                 mock.patch.object(st, "ensure_theme_plan", lambda *a: PERSON_PLAN), \
                 mock.patch.object(st, "probe_duration", lambda p: 300.0):
                with self.assertRaises(MediaError) as err:
                    st.collect_people_timeline(self._req(run))
            self.assertIn("Продолжить", str(err.exception))

    def test_no_source_no_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(st._cover_hint(Path(tmp), self._req(Path(tmp))), "")

    def test_one_hero_keeps_normal_chapters(self) -> None:
        from engine.slides.render_pipeline import _preset_chapters

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            tl = {"variant": "stars", "stars": [{"id": 1, "name": "Ванга"}], "parts": [{"start": 0, "star": 1}]}
            (run / "timeline.json").write_text(json.dumps(tl, ensure_ascii=False), encoding="utf-8")
            self.assertIsNone(_preset_chapters(run, "ru"), "один герой — главы по смыслу, а не «Ванга»")

    def test_two_heroes_without_three_chapters_use_normal_chapters(self) -> None:
        from engine.slides.render_pipeline import _people_limits, _preset_chapters

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            tl = {"variant": "stars", "stars": [{"id": 1, "name": "А Б"}, {"id": 2, "name": "В Г"}],
                  "parts": [{"start": 0, "stars": [1]}, {"start": 300, "stars": [2]}]}
            (run / "timeline.json").write_text(json.dumps(tl, ensure_ascii=False), encoding="utf-8")
            self.assertIsNone(_preset_chapters(run, "ru"), "2 главы YouTube не примет — по смыслу, но не больше 10")
            self.assertEqual(_people_limits(run), {"cap": 10, "gap": 90.0})
            tl["parts"].append({"start": 600, "stars": []})
            (run / "timeline.json").write_text(json.dumps(tl, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(len(_preset_chapters(run, "ru")), 3)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_people_limits(Path(tmp)), {})


class NamesakeTests(unittest.TestCase):
    """Тёзки: лицо героя берём по точным запросам (имя + кто + годы), а не по всем фото с именем."""

    def test_anchor_queries(self) -> None:
        star = st.Star(1, "Ванга", "Baba Vanga", "Ванга", "photo", "1911–1996", "болгарская ясновидящая")
        self.assertEqual(st.anchor_queries(star, "Ванга"), [
            "Ванга болгарская ясновидящая", "Ванга болгарская ясновидящая фото", "Ванга 1911–1996",
            "Ванга болгарская ясновидящая 1911–1996",
        ])
        self.assertEqual(st.anchor_queries(st.Star(2, "X", "X", "X"), "X"), [])

    def test_hero_face_from_exact_queries_only(self) -> None:
        import numpy as np

        from engine.slides import person_media as pm
        from engine.slides.theme_plan import ThemePlan
        from engine.slides.stocks_base import StockCandidate

        star = st.Star(1, "Ванга", "Baba Vanga", "Ванга", "photo", "1911–1996", "болгарская ясновидящая")
        anchors = st.anchor_queries(star, "Ванга")
        vanga, man = np.array([1.0, 0.0]), np.array([0.0, 1.0])

        def search(clients, source, queries, **kw):
            if source != "google":
                return []
            out = []
            for k, q in enumerate(queries):
                c = StockCandidate(provider="google", id=f"{k}", kind="image", query=q, preview_url="", download_url=f"http://x/{k}.jpg",
                                   width=1000, height=800)

                out.append(c)
            return out

        calls: list[int] = []

        def find_hero(per_image):
            calls.append(len(per_image))
            return vanga

        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp)
            (pool / "t").mkdir()
            (pool / "f").mkdir()

            def download(http, url, path, retries=0, **kw):
                photo(path, int(url.rsplit("/", 1)[1].split(".")[0]))
                return True

            qs, _ = pm.person_query_set(st._star_plan(ThemePlan(summary="s"), star, "Ванга"), "Ванга", "photo")
            exact = {pm.quote_name(q, "Ванга") for q in anchors}

            def embed(path):
                k = int(path.stem.split("_")[1])
                return [vanga if qs[k] in exact else man]

            with mock.patch.object(pm, "_search", search), mock.patch.object(pm, "_download", download), \
                 mock.patch("engine.slides.theme_media._image_short_side", lambda p: 900), \
                 mock.patch.object(pm.faces, "face_embeddings", embed), \
                 mock.patch.object(pm.faces, "find_hero", find_hero):
                _v, images, _rv, _ri = pm.collect_person_pool(
                    None, {}, st._star_plan(ThemePlan(summary="s"), star, "Ванга"), name="Ванга", kind="photo",
                    rng=random.Random(1), pool=pool, trials=pool / "t", frames=pool / "f", cancel=None,
                    progress=lambda *a: None, pct_lo=0, pct_hi=1, image_need=30, video_need=0, anchors=anchors)
        self.assertEqual(calls, [len(anchors)], "лицо — только по фото из точных запросов")
        self.assertEqual(len(images), len(anchors), "фото «мужика» по голому имени отсеяны")
        self.assertTrue(all(any(a.split()[1] in im["query"] for a in anchors) for im in images))
