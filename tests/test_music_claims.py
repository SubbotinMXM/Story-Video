"""Претензии YouTube на фоновую музыку: чёрный список, замена трека, распознавание на странице претензий."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import tempfile
import unittest
from unittest import mock

from engine.slides import music


CAT = {"groups": {"cinematic": "Кино", "ambient": "Эмбиент"}, "tracks": [
    {"id": "cold-journey", "title": "Cold Journey", "group": "cinematic", "file": "a.mp3", "duration": 1},
    {"id": "dream-freedom", "title": "Dream Freedom", "group": "cinematic", "file": "b.mp3", "duration": 1},
    {"id": "aquarium", "title": "Aquarium", "group": "ambient", "file": "c.mp3", "duration": 1},
]}


class MusicClaimsTest(unittest.TestCase):
    def setUp(self) -> None:
        d = tempfile.mkdtemp()
        for p in (mock.patch.dict("os.environ", {"STORY_VIDEO_PUBLISH_DIR": d}),
                  mock.patch.object(music, "catalog", lambda: CAT)):
            p.start()
            self.addCleanup(p.stop)

    def test_clean_track_used_as_is(self) -> None:
        self.assertEqual(music.usable("cold-journey"), ("cold-journey", ""))

    def test_claim_page_blocks_track_and_replaces_in_group(self) -> None:
        text = ("Claim overview What happened Copyright-protected content was found on this video "
                "Claim Type Cold Journey Copyright Melody or lyrics No impact on your video.")
        self.assertEqual(music.claims_in(text, video="https://youtu.be/x"), ["Cold Journey"])
        tid, note = music.usable("cold-journey")
        self.assertEqual(tid, "dream-freedom", "та же группа")
        self.assertIn("претензию YouTube", note)

    def test_nothing_clean_left(self) -> None:
        for t in CAT["tracks"]:
            music.mark_claimed(t["id"])
        self.assertEqual(music.usable("aquarium")[0], "")

    def test_group_pick_goes_through_all_tracks_before_repeating(self) -> None:
        cat = {"groups": {"war": "Военная"}, "hints": {}, "tracks": [
            {"id": f"w{i}", "title": f"W{i}", "group": "war", "file": "a.mp3", "duration": 1} for i in range(4)]}
        with mock.patch.object(music, "catalog", lambda: cat):
            first = [music.usable("group:war")[0] for _ in range(4)]
            self.assertEqual(sorted(first), ["w0", "w1", "w2", "w3"], "круг: все треки по разу")
            nxt = music.usable("group:war")[0]
            self.assertNotEqual(nxt, first[-1], "новый круг не начинается с последнего")
            tid, note = music.usable("group:war")
            self.assertIn("Военная", note)

    def test_group_skips_claimed_and_empty_group_means_no_music(self) -> None:
        cat = {"groups": {"war": "Военная"}, "hints": {}, "tracks": [
            {"id": "a", "title": "A", "group": "war", "file": "a.mp3", "duration": 1},
            {"id": "b", "title": "B", "group": "war", "file": "b.mp3", "duration": 1}]}
        with mock.patch.object(music, "catalog", lambda: cat):
            music.mark_claimed("a")
            self.assertEqual({music.usable("group:war")[0] for _ in range(5)}, {"b"})
            music.mark_claimed("b")
            tid, note = music.usable("group:war")
            self.assertEqual(tid, "")
            self.assertIn("нет подходящих", note)
            self.assertEqual(music.usable("group:нет-такой")[0], "")

    def test_publish_notes_claimed_music(self) -> None:
        from engine.publish import run as pub
        from engine.publish import youtube

        rec = {"url": "https://youtu.be/x", "notes": ["проверки YouTube нашли проблему — скорее всего претензию"]}
        with mock.patch.object(youtube, "music_claims", lambda cdp, url: ["Cold Journey"]):
            pub._note_music_claims(mock.Mock(cdp_url="ws://x"), rec)
        self.assertTrue(any("Cold Journey" in n for n in rec["notes"]))
        rec2 = {"url": "u", "notes": ["всё хорошо"]}
        with mock.patch.object(youtube, "music_claims", side_effect=AssertionError("без претензии не смотрим")):
            pub._note_music_claims(mock.Mock(cdp_url="ws://x"), rec2)


if __name__ == "__main__":
    unittest.main()
