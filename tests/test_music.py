"""Фоновая музыка стоковых роликов: каталог, выбор, тихий ровный микс под голосом."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.slides import music
from engine.slides.ffmpeg_util import find_ffmpeg

FF = find_ffmpeg()


def _lufs(path: Path) -> float:
    err = subprocess.run([FF, "-hide_banner", "-i", str(path), "-af", "ebur128", "-f", "null", "-"], capture_output=True, text=True).stderr
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", err)[-1])


class CatalogTests(unittest.TestCase):
    def test_youtube_library_tracks_by_niche_categories(self) -> None:
        """Только Фонотека YouTube (на её треки не бывает претензий Content ID); категории по нишам, по 5 треков."""
        cat = music.catalog()
        self.assertEqual(len(cat["tracks"]), 30, len(cat["tracks"]))
        for t in cat["tracks"]:
            self.assertIn("YouTube Audio Library", t["source"])
            self.assertTrue((music.MUSIC_DIR / t["file"]).is_file())
            self.assertIn(t["group"], cat["groups"])
        for gid in cat["groups"]:
            self.assertEqual(len(music.group_tracks(gid)), 5, f"в категории «{gid}» должно быть 5 треков")
            self.assertTrue(cat["hints"].get(gid), f"у категории «{gid}» нет описания, для чего она")
        self.assertEqual(set(cat["groups"]), {"ambient", "war", "mystery", "ancient", "bio", "bright"})

    def test_no_generic_word_title_is_found_inside_other_words(self) -> None:
        self.assertEqual(music.claims_in("a leveling of the playing field", video=""), [], "«Level» — только целым словом")

    def test_same_loudness_for_all_tracks(self) -> None:
        levels = [t["lufs"] for t in music.catalog()["tracks"]]
        self.assertLess(max(levels) - min(levels), 2.0, levels)

    def test_credit_and_unknown(self) -> None:
        tid = music.catalog()["tracks"][0]["id"]
        self.assertIn("Фонотека YouTube", music.credit_line(tid))
        self.assertIsNone(music.track_path("нет-такого"))
        self.assertEqual(music.credit_line(""), "")


class MixTests(unittest.TestCase):
    def test_music_quiet_under_voice_one_encode(self) -> None:
        from engine.slides import ffprobe_util as fu
        from engine.slides import render_pipeline as rp

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:d=20", str(root / "silent.mp4")], check=True)
            subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=300:d=20", "-af", "loudnorm=I=-16", str(root / "voice.mp3")], check=True)
            (root / "subs.srt").write_text("1\n00:00:01,000 --> 00:00:03,000\nТекст\n", encoding="utf-8")
            track = music.track_path(music.catalog()["tracks"][0]["id"])
            encodes: list = []
            real = fu.run_ffmpeg
            with mock.patch.object(rp, "run_ffmpeg", lambda cmd, label="x": (encodes.append(label), real(cmd, label=label))[1]):
                rp.finish_video(rp.RenderRequest(run_dir=root, subtitles=True, music=track), silent=root / "silent.mp4",
                                voice=root / "voice.mp3", srt_path=root / "subs.srt", dest=root / "with.mp4", progress=lambda p, m: None)
            self.assertEqual(encodes, ["final"], "музыка — в том же одном проходе")
            # без голоса (тишина) — слышна только музыка: на ~20 дБ тише голоса
            subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "20", str(root / "mute.mp3")], check=True)
            rp.finish_video(rp.RenderRequest(run_dir=root, music=track), silent=root / "silent.mp4", voice=root / "mute.mp3",
                            srt_path=None, dest=root / "music_only.mp4", progress=lambda p, m: None)
            gap = _lufs(root / "voice.mp3") - _lufs(root / "music_only.mp4")
            self.assertTrue(21 <= gap <= 28, gap)  # музыка ~−24 дБ под голосом: тихий фон

    def test_runner_passes_selected_track(self) -> None:
        from server.runners import _render_flags

        tid = music.catalog()["tracks"][1]["id"]
        self.assertEqual(_render_flags({"music_track": tid})["music"], music.track_path(tid))
        self.assertIsNone(_render_flags({"music_track": ""})["music"])


if __name__ == "__main__":
    unittest.main()
