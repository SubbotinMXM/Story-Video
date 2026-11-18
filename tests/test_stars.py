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
    "stars": [
        {"id": 1, "name": "Юрий Никулин", "latin": "Yuri Nikulin", "search": "Юрий Никулин", "kind": "photo", "years": "1921–1997"},
        {"id": 2, "name": "Андрей Миронов", "latin": "Andrei Mironov", "search": "Андрей Миронов", "kind": "photo"},
        {"id": 3, "name": "Упомянут мимоходом", "latin": "x", "search": "x", "kind": "photo"},
        {"id": 0, "name": "плохой id"},
    ],
    "parts": [{"i": 0, "star": 0}, {"i": 2, "star": 1}, {"i": 3, "star": 1}, {"i": 5, "star": 2}, {"i": 8, "star": 9}, {"i": 9, "star": 0}],
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
        stars, parts = st._parse_cast(CAST, 10)
        self.assertEqual([s.name for s in stars], ["Юрий Никулин", "Андрей Миронов"], "без героя, у которого нет своей части")
        self.assertEqual(parts, [(0, 0), (2, 1), (5, 2), (8, 0)], "подряд один герой — одна часть; неизвестный id — общее")
        self.assertEqual(stars[0].years, "1921–1997")

    def test_part_times(self) -> None:
        times = st.part_times([(0, 0), (2, 1), (5, 2), (8, 0)], sents(), 300.0)
        self.assertEqual(times, [(0.0, 60.0, 0), (60.0, 150.0, 1), (150.0, 240.0, 2), (240.0, 300.0, 0)])

    def test_cast_tries_other_model(self) -> None:
        bad = mock.MagicMock()
        bad.complete.side_effect = RuntimeError("503")
        good = mock.MagicMock()
        good.complete.return_value = CAST
        with mock.patch("engine.slides.llm_client.LLMClient", side_effect=[bad, good]):
            stars, parts = st.plan_cast(("k", "u", "m"), sents())
        self.assertEqual(len(stars), 2)
        self.assertTrue(parts)


class SlotsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stars = [st.Star(1, "Никулин", "Nikulin", "Никулин"), st.Star(2, "Миронов", "Mironov", "Миронов")]
        self.media = {
            1: [{"path": f"pool/s01/p{k}.jpg", "kind": "image", "star": 1} for k in range(4)],
            2: [{"path": f"pool/s02/p{k}.jpg", "kind": "image", "star": 2} for k in range(3)]
            + [{"path": "pool/s02/v0.mp4", "kind": "video", "star": 2, "duration": 8.0}],
        }

    def test_each_part_only_its_star_no_repeats_in_a_row(self) -> None:
        parts = [(0.0, 20.0, 0), (20.0, 80.0, 1), (80.0, 140.0, 2), (140.0, 150.0, 0)]
        slots = st.build_slots(parts, self.media, self.stars, rng=random.Random(1))
        for s in slots:
            if 20.0 <= s["start"] < 80.0:
                self.assertIn("/s01/", s["path"], "на части про Никулина — только он")
            if 80.0 <= s["start"] < 140.0:
                self.assertIn("/s02/", s["path"])
        for a, b in zip(slots, slots[1:]):
            self.assertNotEqual(a["path"], b["path"], "одинаковые кадры подряд")
            self.assertAlmostEqual(a["end"], b["start"], places=2, msg="без дыр")
        self.assertEqual(slots[-1]["end"], 150.0)
        self.assertTrue(any(s["start"] == 80.0 for s in slots), "смена героя — ровно на границе части")
        video = [s for s in slots if s["kind"] == "video"]
        self.assertTrue(all(s["duration"] <= st.VIDEO_MAX for s in video))

    def test_general_part_rotates_all_stars(self) -> None:
        slots = st.build_slots([(0.0, 40.0, 0)], self.media, self.stars, rng=random.Random(1))
        owners = [s["path"].split("/")[1] for s in slots]
        self.assertIn("s01", owners)
        self.assertIn("s02", owners)
        self.assertTrue(all(a != b for a, b in zip(owners, owners[1:])), "по очереди")

    def test_star_without_photos_gets_neutral_frames_not_someone_else(self) -> None:
        media = {1: self.media[1], 2: []}
        neutral = [{"path": f"pool/neutral/n{k}.jpg", "kind": "image", "star": 0} for k in range(3)]
        slots = st.build_slots([(0.0, 30.0, 2)], media, self.stars, rng=random.Random(1), neutral=neutral)
        self.assertTrue(slots)
        self.assertTrue(all(x["path"].startswith("pool/neutral/") for x in slots), "чужое лицо вместо героя — нельзя")


class ChaptersTests(unittest.TestCase):
    TL = {
        "stars": [{"id": 1, "name": "Юрий Никулин", "latin": "Yuri Nikulin"}, {"id": 2, "name": "Андрей Миронов", "latin": "Andrei Mironov"}],
        "parts": [{"start": 0, "star": 0}, {"start": 40, "star": 1}, {"start": 200, "star": 0},
                  {"start": 215, "star": 1}, {"start": 300, "star": 2}, {"start": 500, "star": 0}],
    }

    def test_by_star_with_intro_and_outro(self) -> None:
        self.assertEqual(st.star_chapters(self.TL, "ru"), [
            (0.0, "Вступление"), (40.0, "Юрий Никулин"), (300.0, "Андрей Миронов"), (500.0, "Заключение"),
        ])

    def test_latin_names_for_other_languages(self) -> None:
        self.assertEqual(st.star_chapters(self.TL, "en")[1], (40.0, "Yuri Nikulin"))
        self.assertEqual(st.star_chapters(self.TL, "de")[0], (0.0, "Einleitung"))


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


if __name__ == "__main__":
    unittest.main()


def _run(tmp: str) -> Path:
    run = Path(tmp)
    (run / "script.txt").write_text(" ".join(f"Фраза {i}." for i in range(10)), encoding="utf-8")
    (run / "timings.json").write_text(json.dumps({"words": [{"word": f"Фраза {i}.", "start": i * 30.0, "end": i * 30 + 1.0} for i in range(10)]}))
    return run


class AutoPeopleTests(unittest.TestCase):
    """Логика «Звёзд» сама включается в любом режиме, если ролик про людей."""

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

    def test_one_hero_gets_whole_video_and_more_photos(self) -> None:
        self.assertEqual(st.photo_targets(1200, 1)[0], 80, "один герой — фото на весь ролик, меньше повторов")
        self.assertEqual(st.photo_targets(1200, 8)[0], 30)
        self.assertGreater(st.photo_targets(1200, 1)[1], st.photo_targets(1200, 8)[1])

    def test_one_hero_keeps_normal_chapters(self) -> None:
        from engine.slides.render_pipeline import _preset_chapters

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            tl = {"variant": "stars", "stars": [{"id": 1, "name": "Ванга"}], "parts": [{"start": 0, "star": 1}]}
            (run / "timeline.json").write_text(json.dumps(tl, ensure_ascii=False), encoding="utf-8")
            self.assertIsNone(_preset_chapters(run, "ru"), "один герой — главы по смыслу, а не «Ванга»")


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

            def download(http, url, path, retries=0):
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
