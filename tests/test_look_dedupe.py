"""Одинаковые на вид кадры (разные файлы) не попадают дважды и не стоят рядом."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.slides import look
from engine.slides.ffmpeg_util import find_ffmpeg


def _pic(path: Path, size=(800, 600), shape="circle") -> Path:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (800, 600), "white")
    d = ImageDraw.Draw(img)
    if shape == "circle":
        d.ellipse((200, 100, 600, 500), fill="black")
        d.rectangle((40, 40, 140, 560), fill="gray")
    elif shape == "bars":
        for x in range(0, 800, 100):
            d.rectangle((x, 0, x + 50, 600), fill="red")
    else:
        d.rectangle((0, 300, 800, 600), fill="navy")
        d.polygon([(400, 20), (100, 300), (700, 300)], fill="orange")
    img.resize(size).save(path, quality=70)
    return path


class LookTests(unittest.TestCase):
    def test_same_photo_two_sites_one_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = _pic(root / "site1.jpg")
            b = _pic(root / "site2.jpg", size=(400, 300))
            c = _pic(root / "other.jpg", shape="mountain")
            groups = look.look_groups([a, b, c])
            self.assertEqual(groups[a], groups[b])
            self.assertNotEqual(groups[a], groups[c])

    def test_same_clip_two_files_one_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ff = find_ffmpeg()
            for name, size in (("v1.mp4", "1280x720"), ("v2.mp4", "640x360")):
                subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:d=4", str(root / name)], check=True)
            subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", "mandelbrot=size=1280x720:rate=30", "-t", "4", str(root / "v3.mp4")], check=True)
            g = look.look_groups([root / "v1.mp4", root / "v2.mp4", root / "v3.mp4"])
            self.assertEqual(g[root / "v1.mp4"], g[root / "v2.mp4"])
            self.assertNotEqual(g[root / "v1.mp4"], g[root / "v3.mp4"])

    def test_strict_mix_rejects_seen_look(self) -> None:
        from engine.slides.source_mix import Budget, SourceStats
        from engine.slides.strict_media import MixState

        mix = MixState(subject="place", stats=SourceStats(), budget=Budget.for_beats(1))
        fp = 0b1010_1100
        mix.add_look(fp)
        self.assertTrue(mix.look_taken(fp ^ 0b11))  # отличается на 2 бита — тот же кадр
        self.assertFalse(mix.look_taken(fp ^ ((1 << 40) - 1)))

    def test_render_never_puts_lookalikes_side_by_side(self) -> None:
        import random

        from engine.core.spread import max_run, spread

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [_pic(root / "a.jpg"), _pic(root / "a_copy.jpg", size=(640, 480)),
                     _pic(root / "m.jpg", shape="mountain"), _pic(root / "b.jpg", shape="bars")]
            groups = look.look_groups(paths)
            self.assertEqual(len(set(groups.values())), 3)
            order = spread([i for _ in range(6) for i in range(4)], key=lambda i: groups[paths[i]], rng=random.Random(1))
            self.assertEqual(max_run([groups[paths[i]] for i in order]), 1, "одинаковое фото с двух сайтов не подряд")


if __name__ == "__main__":
    unittest.main()
