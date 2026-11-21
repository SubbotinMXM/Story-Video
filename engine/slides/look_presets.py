"""Разное оформление от ролика к ролику: стиль субтитров, цветовая подача, ритм смены кадров.

Всё выбирается по «ключу» ролика (имя его папки запуска): у одного ролика (и его переводов) выбор всегда
один и тот же, у разных роликов — разный. Так каналы не выглядят конвейером, а пересборка не меняет вид.
"""

from __future__ import annotations

import hashlib
import random

# ---------- субтитры ----------
# цвета ASS: &HAABBGGRR (AA — прозрачность, 00 — непрозрачно). BorderStyle=3 — сплошная плашка под текстом.
SUBS_PRESETS: list[dict[str, str]] = [
    {"name": "Классика", "FontName": "Arial", "FontSize": "26", "PrimaryColour": "&H0000FFFF", "OutlineColour": "&H00000000",
     "BorderStyle": "1", "Outline": "3", "Shadow": "0", "Alignment": "2", "MarginV": "16"},
    {"name": "Белые с тенью", "FontName": "Georgia", "FontSize": "26", "PrimaryColour": "&H00FFFFFF", "OutlineColour": "&H00000000",
     "BorderStyle": "1", "Outline": "2", "Shadow": "2", "Alignment": "2", "MarginV": "18"},
    {"name": "Тёмная плашка", "FontName": "Arial", "FontSize": "22", "Bold": "1", "PrimaryColour": "&H00FFFFFF",
     "OutlineColour": "&H99000000", "BorderStyle": "3", "Outline": "5", "Shadow": "0", "Alignment": "2", "MarginV": "20"},
    {"name": "Плакатные", "FontName": "Impact", "FontSize": "28", "PrimaryColour": "&H00FFFFFF", "OutlineColour": "&H00000000",
     "BorderStyle": "1", "Outline": "3", "Shadow": "0", "Alignment": "2", "MarginV": "14"},
    {"name": "Светло-жёлтые", "FontName": "Verdana", "FontSize": "23", "Bold": "1", "PrimaryColour": "&H0080F0FF",
     "OutlineColour": "&H00202020", "BorderStyle": "1", "Outline": "3", "Shadow": "1", "Alignment": "2", "MarginV": "16"},
    {"name": "Синяя плашка", "FontName": "Trebuchet MS", "FontSize": "23", "PrimaryColour": "&H00FFFFFF",
     "OutlineColour": "&HB0602010", "BorderStyle": "3", "Outline": "5", "Shadow": "0", "Alignment": "2", "MarginV": "22"},
    {"name": "Тёплые", "FontName": "Tahoma", "FontSize": "25", "Bold": "1", "PrimaryColour": "&H0040A8FF",
     "OutlineColour": "&H00000000", "BorderStyle": "1", "Outline": "3", "Shadow": "0", "Alignment": "2", "MarginV": "16"},
    {"name": "Курсив слева", "FontName": "Georgia", "FontSize": "26", "Italic": "1", "PrimaryColour": "&H00FFFFFF",
     "OutlineColour": "&H00000000", "BorderStyle": "1", "Outline": "2", "Shadow": "2", "Alignment": "1", "MarginL": "60",
     "MarginV": "22"},
]

# ---------- цветовая подача (мягко: чтобы кадры одного ролика были «из одного фильма») ----------
GRADES: list[tuple[str, str]] = [
    ("без обработки", ""),
    ("тёплая", "colorbalance=rs=0.04:rm=0.03:bs=-0.04:bm=-0.03,eq=saturation=1.05"),
    ("холодная", "colorbalance=rs=-0.03:rm=-0.02:bs=0.05:bm=0.03,eq=saturation=0.97"),
    ("контрастная", "eq=contrast=1.08:saturation=1.08:gamma=0.98"),
    ("выцветшая", "eq=contrast=0.94:saturation=0.88:brightness=0.01"),
    ("кино", "colorbalance=rs=-0.03:bs=0.04:rh=0.04:bh=-0.03,eq=contrast=1.05"),
]

_MOTION_MODES = 6


def _h(key: str, salt: str) -> int:
    return int(hashlib.md5(f"{salt}:{key}".encode("utf-8")).hexdigest()[:8], 16)


def subs_preset(key: str) -> dict[str, str]:
    """Стиль субтитров ролика. Пустой ключ — «Классика» (как было)."""
    return SUBS_PRESETS[_h(key, "subs") % len(SUBS_PRESETS)] if key else SUBS_PRESETS[0]


def subs_force_style(key: str) -> str:
    """Строка для subtitles=…:force_style='…' (запятые экранированы для фильтра ffmpeg)."""
    p = subs_preset(key)
    return "\\,".join(f"{k}={v}" for k, v in p.items() if k != "name")


def grade_of(key: str) -> tuple[str, str]:
    """(название, фильтр ffmpeg) цветовой подачи ролика. Пустой ключ — без обработки."""
    return GRADES[_h(key, "grade") % len(GRADES)] if key else GRADES[0]


def grade_filter(key: str) -> str:
    return grade_of(key)[1]


def motion_mode(seed: int) -> int:
    """Вид движения фото (наезд, отъезд, пролёты, дрейф); соседние кадры всегда разные:
    чётные берут из первой тройки видов, нечётные — из второй, внутри тройки выбор случайный."""
    return random.Random(f"m{seed}").randrange(3) + (3 if seed % 2 else 0)


def rhythm_durations(n: int, hold: float, rng: random.Random) -> list[float]:
    """Длины кадров «как в кино»: серии быстрых и медленных планов, в среднем hold (общий объём не меняется)."""
    if n <= 0:
        return []
    paces = [0.6, 0.8, 1.0, 1.25, 1.55]
    out: list[float] = []
    last = -1.0
    while len(out) < n:
        pace = rng.choice([p for p in paces if p != last])
        last = pace
        for _ in range(rng.randint(2, 5)):
            out.append(hold * pace * rng.uniform(0.92, 1.08))
    out = out[:n]
    mean = sum(out) / n
    scale = hold / mean if mean > 0 else 1.0
    lo, hi = hold * 0.5, hold * 1.7
    return [round(min(hi, max(lo, d * scale)), 3) for d in out]
