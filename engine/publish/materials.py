"""Обложка, название и описание ролика для выкладки — по кругу, без повторов.

Обложки: папка картинок; использованная получает пометку «✓ » в имени файла. Все помечены —
круг пройден: пометки снимаются, начинается новый круг.
Названия и описания: текстовые файлы с пунктами «1. …», «2. …» (описание — до следующего номера,
может быть многострочным). Какие номера уже были — помнит программа (data/publish/used.json), файлы
не трогаются. Название и описание выбираются независимо друг от друга.
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass
from pathlib import Path

from engine.core.logutil import log
from engine.publish.store import LOCK, read_json, write_json

USED_MARK = "✓ "
COVER_MISSING = "⚠ Обложки нет"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
USED_FILE = "used.json"
_ITEM = re.compile(r"^\s*(\d+)\s*[.)]\s*(.*)$")


@dataclass(frozen=True)
class Materials:
    thumbnail: str  # путь к обложке (уже с пометкой «✓ »); пусто — обложки нет (ролик выйдет без неё)
    title: str
    description: str
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        out: dict = {"thumbnail": self.thumbnail, "title": self.title, "description": self.description}
        if self.warnings:
            out["warnings"] = list(self.warnings)
        return out


def parse_numbered(text: str) -> dict[int, str]:
    """«1. Текст…» → {1: "Текст…"}; пункт продолжается до следующей строки с номером."""
    items: dict[int, list[str]] = {}
    current: int | None = None
    for line in (text or "").replace("\r", "").split("\n"):
        m = _ITEM.match(line)
        if m:
            current = int(m.group(1))
            items[current] = [m.group(2).rstrip()]
        elif current is not None:
            items[current].append(line.rstrip())
    return {n: "\n".join(lines).strip() for n, lines in items.items() if "\n".join(lines).strip()}


def read_numbered(path: Path, what: str) -> dict[int, str]:
    if not path.is_file():
        raise ValueError(f"Нет файла {what}: {path}")
    items = parse_numbered(path.read_text(encoding="utf-8", errors="replace"))
    if not items:
        raise ValueError(f"В файле {what} нет пунктов вида «1. …»: {path}")
    return items


def thumbnails(folder: Path) -> list[Path]:
    if not folder.is_dir():
        raise ValueError(f"Нет папки обложек: {folder}")
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS and not p.name.startswith(".")]
    if not files:
        raise ValueError(f"В папке обложек нет картинок (jpg, png, webp): {folder}")
    return sorted(files)


def check(thumbs_dir: str, titles_file: str, descs_file: str) -> None:
    """Проверка при запуске (до рерайта и озвучки): папка и файлы есть и не пустые."""
    for raw, what in ((thumbs_dir, "Папка обложек"), (titles_file, "Файл названий"), (descs_file, "Файл описаний")):
        if not str(raw or "").strip():
            raise ValueError(f"Выкладка на YouTube: не указан путь — {what}")
    thumbnails(Path(thumbs_dir).expanduser())
    read_numbered(Path(titles_file).expanduser(), "названий")
    read_numbered(Path(descs_file).expanduser(), "описаний")


def check_descs(descs_file: str) -> None:
    """«Переделать конкурента»: обложка и название генерируются — нужен только файл описаний."""
    if not str(descs_file or "").strip():
        raise ValueError("Выкладка на YouTube: не указан путь — Файл описаний")
    read_numbered(Path(descs_file).expanduser(), "описаний")


def pick_description(descs_file: str, *, rng: random.Random | None = None) -> str:
    """Одно описание из файла — случайное, без повторов до конца круга (тот же учёт, что у pick)."""
    rng = rng or random.Random()
    with LOCK:
        path = Path(descs_file).expanduser()
        return _pick_item(path, read_numbered(path, "описаний"), rng)


def pick(thumbs_dir: str, titles_file: str, descs_file: str, *, rng: random.Random | None = None) -> Materials:
    """Обложка + название + описание для одного ролика (на всех его каналах — одни и те же)."""
    rng = rng or random.Random()
    warnings: list[str] = []
    with LOCK:
        try:
            thumb = str(_pick_thumbnail(Path(thumbs_dir).expanduser(), rng))
        except (ValueError, OSError) as exc:
            # обложки нет — ролик всё равно выходит (название, описание, дата), а в итогах — предупреждение
            thumb = ""
            warnings.append(f"{COVER_MISSING} ({exc}) — ролик выложен без обложки, поставь её сам в Studio")
            log(f"Выкладка: обложку взять не из чего ({exc}) — выкладываю без неё")
        titles_path, descs_path = Path(titles_file).expanduser(), Path(descs_file).expanduser()
        title = _pick_item(titles_path, read_numbered(titles_path, "названий"), rng)
        desc = _pick_item(descs_path, read_numbered(descs_path, "описаний"), rng)
    log(f"Выкладка: обложка «{Path(thumb).name if thumb else '—'}», название «{title[:60]}»")
    return Materials(thumb, title, desc, tuple(warnings))


def _pick_thumbnail(folder: Path, rng: random.Random) -> Path:
    files = thumbnails(folder)
    fresh = [p for p in files if not p.name.startswith(USED_MARK)]
    if not fresh:
        # круг пройден — пометки прочь, все обложки снова в деле
        log(f"Обложки: круг из {len(files)} пройден — начинаю новый")
        fresh = []
        for p in files:
            target = p.with_name(p.name[len(USED_MARK):])
            if target.exists():  # такое имя уже занято — оставляем как есть, но берём в круг
                fresh.append(p)
                continue
            p.rename(target)
            fresh.append(target)
    chosen = rng.choice(fresh)
    if chosen.name.startswith(USED_MARK):
        return chosen
    marked = chosen.with_name(USED_MARK + chosen.name)
    chosen.rename(marked)
    return marked


def _pick_item(path: Path, items: dict[int, str], rng: random.Random) -> str:
    key = _pool_key(path)
    used_all = read_json(USED_FILE, {})
    used = {int(n) for n in used_all.get(key, []) if str(n).isdigit()}
    fresh = [n for n in items if n not in used]
    if not fresh:
        log(f"{path.name}: круг из {len(items)} пройден — начинаю новый")
        used, fresh = set(), list(items)
    n = rng.choice(fresh)
    used_all[key] = sorted(used | {n})
    write_json(USED_FILE, used_all)
    return items[n]


def _pool_key(path: Path) -> str:
    """Учёт привязан к файлу (по полному пути): разные файлы названий — разные круги."""
    resolved = str(path.resolve())
    return hashlib.sha1(resolved.encode()).hexdigest()[:12] + ":" + path.name
