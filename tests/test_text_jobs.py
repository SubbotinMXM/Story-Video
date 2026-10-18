
import _isolate  # noqa: F401  (до импорта engine)
import tempfile
import unittest
from pathlib import Path

from engine.core.text_jobs import TextJob, load_text_jobs


class TextJobsTests(unittest.TestCase):
    def test_title_from_source(self) -> None:
        job = TextJob("hello world", source="My Story.txt")
        self.assertEqual(job.label, "My Story.txt")
        self.assertEqual(job.title_hint, "My Story")

    def test_load_multiple(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "a.txt"
            b = root / "b.md"
            a.write_text("story one", encoding="utf-8")
            b.write_text("story two", encoding="utf-8")
            jobs = load_text_jobs([a, b, root / "skip.bin"])
            self.assertEqual([j.text for j in jobs], ["story one", "story two"])
            self.assertEqual([j.source for j in jobs], ["a.txt", "b.md"])


if __name__ == "__main__":
    unittest.main()
