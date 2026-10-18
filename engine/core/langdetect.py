"""Язык текста по частым словам — для языков, на которые переводим ролики.

Нужен до распознавания речи: чтобы не заказывать перевод на тот же язык,
на котором написан рассказ (это стоило бы денег и дало бы второй такой же ролик).
"""

from __future__ import annotations

import re
from collections import Counter

# служебные слова, которые почти не встречаются в других языках списка
_STOP: dict[str, frozenset[str]] = {
    "ru": frozenset("и в не на что он она они я с как это его но по из за от был была было так же уже только".split()),
    "en": frozenset("the and of to is was that it with for his her they he she had have this from were".split()),
    "es": frozenset("el los las del que y es por una con para se su lo como pero más fue era muy".split()),
    "de": frozenset("der die und das ist nicht ein eine ich sie mit auf den dem war sich auch es zu".split()),
    "fr": frozenset("le la les et est une des du que qui dans pour pas sur il elle était avec au".split()),
    "pl": frozenset("i w nie na się że to jest z do jak ale był była od po tak już jego".split()),
    "it": frozenset("il la di che è e per una non sono con gli della nel ma come anche era più".split()),
    "ro": frozenset("și în nu la că este cu pe un o din care era sau mai de lui ei fost".split()),
}

_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.UNICODE)
_CYRILLIC = re.compile(r"[а-яё]")


def detect_language(text: str) -> str | None:
    """Код языка (ru, en, es, de, fr, pl, it, ro) или None, если уверенности нет."""
    words = [w.lower() for w in _WORD.findall(text or "")][:5000]
    if len(words) < 5:
        return None
    if sum(1 for w in words if _CYRILLIC.search(w)) > len(words) / 2:
        # кириллица: русский, если это не украинский/болгарский и т.п.
        hits = sum(1 for w in words if w in _STOP["ru"])
        return "ru" if hits >= max(2, len(words) * 0.04) else None
    score: Counter[str] = Counter()
    for w in words:
        for lang, stop in _STOP.items():
            if lang != "ru" and w in stop:
                score[lang] += 1
    if not score:
        return None
    (best, n), *rest = score.most_common(2) + [("", 0)]
    second = rest[0][1]
    if n < max(2, len(words) * 0.04) or n < second * 1.3:
        return None
    return best
