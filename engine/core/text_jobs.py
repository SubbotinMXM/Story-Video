"""Очередь текстов: один файл / вставка = один job → один прогон пайплайна."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

TEXT_EXTS = {".txt", ".md", ".text"}


@dataclass(frozen=True)
class TextJob:
    text: str
    source: str | None = None  # имя файла или None (paste)

    @property
    def label(self) -> str:
        if self.source:
            return Path(self.source).name
        flat = " ".join(self.text.split())
        if not flat:
            return "пустой"
        return flat[:48] + ("…" if len(flat) > 48 else "")

    @property
    def title_hint(self) -> str:
        if self.source:
            return Path(self.source).stem
        flat = " ".join(self.text.split())
        return (flat[:60] if flat else "ролик")


def load_text_jobs(paths: list[Path]) -> list[TextJob]:
    jobs: list[TextJob] = []
    for path in paths:
        if not path.is_file() or path.suffix.lower() not in TEXT_EXTS:
            continue
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            continue
        jobs.append(TextJob(text=text, source=path.name))
    return jobs
