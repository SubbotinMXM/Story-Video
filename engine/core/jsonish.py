"""JSON-объект из ответа нейросети: она оборачивает его в ```json, пишет «Вот ответ:» до и пояснения после."""

from __future__ import annotations

import json
import re

_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)


def json_object(raw: str | None) -> dict | None:
    """Первый JSON-объект из текста. Нет объекта или он битый — None."""
    text = (raw or "").strip()
    fenced = _FENCE.search(text)
    for chunk in ([fenced.group(1)] if fenced else []) + [text]:
        start, end = chunk.find("{"), chunk.rfind("}")
        if start < 0 or end <= start:
            continue
        try:
            data = json.loads(chunk[start:end + 1])
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def json_dict(raw: str | None) -> dict:
    """То же, но «не вышло» — пустой словарь (ответ можно разбирать дальше без проверок на None)."""
    return json_object(raw) or {}
