"""Три режима подбора: по смыслу, строго 3 мин, строго весь ролик."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.slides import strict_media as sm
from engine.slides.resume import timeline_matches
from engine.slides.source_mix import Budget, SourceStats
from engine.slides.whisper_timings import WordTiming
from server.runners import _media_mode


def _words(total_sec: float, step: float = 0.5) -> list[WordTiming]:
    out, t, i = [], 0.0, 0
    while t < total_sec:
        w = f"слово{i}." if i % 6 == 5 else f"слово{i}"
        out.append(WordTiming(word=w, start=t, end=t + step * 0.9, probability=1.0))
        t += step
        i += 1
    return out


class BeatsTests(unittest.TestCase):
    def test_full_mode_frame_every_3_to_5s(self) -> None:
        """Начало — кадр каждые 3–5 с; дальше куски текста 8–10 с, по 2 кадра на кусок."""
        words = _words(400)
        beats = sm.with_shots(sm.split_beats(words, 400, late_min=sm.FULL_BEAT_MIN, late_max=sm.FULL_BEAT_MAX))
        early = [b for b in beats if b.start < sm.HEAD_SEC]
        late = [b for b in beats if b.start >= sm.HEAD_SEC][:-2]  # хвост может быть короче
        self.assertEqual(sm.HEAD_SEC, 120.0)
        self.assertTrue(all(3 <= b.duration <= 5.6 for b in early), [b.duration for b in early])
        self.assertTrue(all(3.9 <= b.duration <= 5.3 for b in late), [b.duration for b in late])
        # два кадра одного куска — один текст, разный номер кадра
        pairs = [b for b in beats if b.start >= sm.HEAD_SEC]
        self.assertEqual([b.shot for b in pairs[:4]], [0, 1, 0, 1])
        self.assertEqual(pairs[0].text, pairs[1].text)

    def test_default_split_unchanged(self) -> None:
        words = _words(400)
        self.assertEqual(
            [(b.start, b.end) for b in sm.split_beats(words, 400)],
            [(b.start, b.end) for b in sm.split_beats(words, 400, late_min=sm.BEAT_MIN, late_max=sm.BEAT_MAX)],
        )


class HoleFillingTests(unittest.TestCase):
    """Ни один кусок не выпадает — иначе всё после дыры съезжает раньше текста."""

    def test_late_holes_filled_by_any_kind(self) -> None:
        B = sm.Beat
        beats = [B(0, 6, "a"), B(6, 12, "b"), B(180, 192, "c"), B(192, 204, "d"), B(204, 216, "e")]
        v = lambda p: {"kind": "video", "path": p, "start": 0, "end": 1, "duration": 1}
        i = lambda p: {"kind": "image", "path": p, "start": 0, "end": 1, "duration": 1}
        for slots in ([v("a"), v("b"), v("c"), None, v("e")], [v("a"), v("b"), None, None, None]):
            out = sm._fill_holes(beats, slots)
            self.assertEqual(len(out), len(beats))
        # в начале дыру сначала закрываем видео (если есть), а не фото
        out = sm._fill_holes(beats, [v("a"), None, i("c"), i("d"), v("e")])
        self.assertEqual(out[1]["kind"], "video")
        # видео нет совсем — подойдёт и фото (раньше начало было только из видео)
        out = sm._fill_holes(beats, [i("a"), None, i("c"), i("d"), i("e")])
        self.assertEqual(out[1]["kind"], "image")


class ModeMappingTests(unittest.TestCase):
    def test_runner_flags(self) -> None:
        self.assertEqual(_media_mode({"media_mode": "theme"}), {"strict_sync": False, "strict_full": False})
        self.assertEqual(_media_mode({"media_mode": "strict_3min"}), {"strict_sync": True, "strict_full": False})
        self.assertEqual(_media_mode({"media_mode": "strict_full"}), {"strict_sync": True, "strict_full": True})

    def test_resume_requires_same_variant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "timeline.json").write_text(json.dumps({"mode": "strict_sync", "slots": [{}]}))
            self.assertTrue(timeline_matches(run, strict=True))  # старый запуск = «3 мин»
            self.assertFalse(timeline_matches(run, strict=True, full=True))
            (run / "timeline.json").write_text(json.dumps({"mode": "strict_sync", "variant": "full", "slots": [{}]}))
            self.assertTrue(timeline_matches(run, strict=True, full=True))
            self.assertFalse(timeline_matches(run, strict=True, full=False))


class MixedPickTests(unittest.TestCase):
    def _pick(self, results: dict[bool, dict | None], index: int = 7):
        calls: list[tuple[bool, str]] = []

        def fake_try(_http, _clients, _plan, _beat, query, *, prefer_video, **k):
            calls.append((prefer_video, query))
            return results[prefer_video], 0

        mix = sm.MixState(subject="other", stats=SourceStats(), budget=Budget())
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sm, "_try_kind", fake_try):
            pool = Path(tmp) / "pool"
            pool.mkdir()
            hit, _r, _w = sm._pick_mixed(
                None, {}, None, sm.Beat(200, 212, "t"),
                video_query="vq", image_query="iq", thematic=False,
                index=index, trials=pool, frames=pool, pool=pool, mix=mix,
            )
        return hit, calls

    def test_alternates_video_and_photo_first(self) -> None:
        strong = {"fit": 8, "weak": False, "path": "pool/a", "provider": "pexels"}
        _hit, even = self._pick({True: strong, False: strong}, index=8)
        _hit, odd = self._pick({True: strong, False: strong}, index=9)
        self.assertEqual(even, [(True, "vq")])
        self.assertEqual(odd, [(False, "iq")])

    def test_weak_first_tries_other_and_takes_better(self) -> None:
        weak_v = {"fit": 3, "weak": True, "path": "pool/v.mp4", "provider": "youtube"}
        weak_i = {"fit": 5, "weak": True, "path": "pool/i.jpg", "provider": "google"}
        hit, calls = self._pick({True: weak_v, False: weak_i}, index=8)
        self.assertEqual([c[0] for c in calls], [True, False])
        self.assertEqual(hit["fit"], 5)

    def test_no_video_falls_back_to_photo(self) -> None:
        photo = {"fit": 6, "weak": False, "path": "pool/i.jpg", "provider": "google"}
        hit, calls = self._pick({True: None, False: photo}, index=8)
        self.assertEqual([c[0] for c in calls], [True, False])
        self.assertIs(hit, photo)


class YoutubeSafetyTests(unittest.TestCase):
    def test_risky_titles(self) -> None:
        from engine.slides.source_youtube import _risky

        self.assertTrue(_risky("Avengers Official Trailer 2026", "Marvel Entertainment"))
        self.assertTrue(_risky("Lyon vs PSG highlights", "Sports"))
        self.assertTrue(_risky("Song (Official Music Video)", "ArtistVEVO"))
        self.assertFalse(_risky("Shenzhen City aerial view", "Miyakoshi Holdings"))
        self.assertFalse(_risky("What Really Happened Inside the Colosseum?", "Ancient Rome Live"))
        self.assertTrue(_risky("People flee in panic as skyscraper wobbles", "News 360 Tv"))
        self.assertTrue(_risky("16 dead in flash flood in Qinghai", "中国新闻社"))

    def test_credits_file(self) -> None:
        from engine.slides.publish import write_credits

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            slots = [
                {"provider": "youtube", "id": "abc", "source_title": "Shenzhen aerial", "source_author": "Miyakoshi"},
                {"provider": "youtube", "id": "abc", "source_title": "Shenzhen aerial", "source_author": "Miyakoshi"},
                {"provider": "pexels", "id": "1"},
                {"provider": "google", "id": "2", "fallback": True},
            ]
            (run / "timeline.json").write_text(json.dumps({"mode": "strict_sync", "slots": slots}), encoding="utf-8")
            text = write_credits(run).read_text(encoding="utf-8")
            self.assertIn("«Shenzhen aerial» — Miyakoshi — https://www.youtube.com/watch?v=abc", text)
            self.assertEqual(text.count("watch?v=abc"), 1)
            self.assertIn("Pexels 1", text)
            self.assertNotIn("Google", text)  # повтор-заглушка не считается


if __name__ == "__main__":
    unittest.main()
