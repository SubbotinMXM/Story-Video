"""Расшифровка ролика, скопированная руками (YouTube «Показать текст видео», .srt, .vtt) → чистый текст.

Убираем всё служебное: таймкоды («0:05», «1:02:03», «[00:01]», «00:00:01,000 --> 00:00:04,000»),
номера блоков субтитров, заголовки WEBVTT, строки-подписи длительности («2 seconds», «1 минута 5 секунд»),
пометки [Музыка]/[Аплодисменты]. Время внутри самой речи («в 10:30 утра») не трогаем —
режем только таймкоды в начале строки.
"""

from __future__ import annotations

import re

from engine.rewrite.source import _clean

_CLOCK = r"\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?"
# строка целиком — таймкод / диапазон субтитров / номер блока
_ONLY_TIME = re.compile(rf"^[\[(]?{_CLOCK}[\])]?(?:\s*-->\s*{_CLOCK}.*)?$")
_ONLY_NUMBER = re.compile(r"^\d{1,5}$")
# таймкод в начале строки перед текстом: «0:05 текст», «[00:01:02] текст», «(1:05) — текст»
_LEAD_TIME = re.compile(rf"^[\[(]?{_CLOCK}[\])]?\s*[-–—:|]?\s+")
_HEADERS = re.compile(r"^(?:WEBVTT|Kind:|Language:|NOTE\b|STYLE\b|Transcript\b|Расшифровка\b|Текст видео\b)", re.I)
# подпись длительности, которую YouTube копирует вместе с таймкодом
_UNITS = (r"(?:seconds?|minutes?|hours?|секунд[аы]?|минут[аы]?|час(?:а|ов)?|"
          r"segundos?|minutos?|horas?|sekunden?|minuten?|stunden?|secondes?|heures?|sekund[ay]?|minut[ay]?|godzin[ay]?)")
_DURATION = re.compile(rf"^(?:\d+\s+{_UNITS}[,\s]*(?:и|and|y|und|et|i)?\s*)+$", re.I)


def clean_transcript(raw: str) -> str:
    """Расшифровка → сплошной текст без служебных пометок."""
    out: list[str] = []
    for line in (raw or "").replace("﻿", "").splitlines():
        line = line.strip()
        if not line or _HEADERS.match(line) or _ONLY_TIME.match(line) or _ONLY_NUMBER.match(line) or _DURATION.match(line):
            continue
        line = _LEAD_TIME.sub("", line)
        line = re.sub(r"<[^>]+>", "", line)  # теги vtt: <c>, <00:00:01.000>
        if not line.strip():
            continue
        if out and out[-1] == line:  # авто-субтитры повторяют строку
            continue
        out.append(line)
    return _clean(" ".join(out))
