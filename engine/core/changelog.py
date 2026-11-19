"""«Что нового»: список версий из changelog.json в корне проекта.

Каждый выпуск (то, что пушится в репозиторий) — запись сверху файла: версия, дата, заголовок, короткое резюме
и список изменений для людей простым языком. После обновления приложение один раз показывает окно «Что нового»
со всем, что вышло с версии, которую человек уже видел (но не больше MAX_SHOWN выпусков). Какую версию человек
видел — в его data/config.json (last_seen_version); сам файл версий от пользователей не зависит и едет с репозиторием.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from engine.core.config import PROJECT_ROOT

FILE = PROJECT_ROOT / "changelog.json"
MAX_SHOWN = 5  # за один раз — не больше стольких выпусков (человек давно не обновлялся — остальное в «Истории версий»)
TAGS = ("new", "better", "fix")  # новое / улучшено / исправлено


def _clean_item(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    title = " ".join(str(raw.get("title") or "").split())
    text = " ".join(str(raw.get("text") or "").split())
    if not title:
        return None
    tag = str(raw.get("tag") or "new").strip().lower()
    return {"tag": tag if tag in TAGS else "new", "title": title, "text": text}


def _clean_release(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    version = str(raw.get("version") or "").strip()
    items = [i for i in (_clean_item(x) for x in raw.get("items") or []) if i]
    if not version or not items:
        return None
    return {
        "version": version,
        "date": str(raw.get("date") or "").strip(),
        "title": " ".join(str(raw.get("title") or "").split()) or f"Версия {version}",
        "summary": " ".join(str(raw.get("summary") or "").split()),
        "items": items,
    }


def releases(path: Path | None = None) -> list[dict[str, Any]]:
    """Все выпуски, новые сверху. Битый файл или запись — пропускаются (окно просто не покажется)."""
    try:
        raw = json.loads((path or FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw.get("releases") if isinstance(raw, dict) else []:
        rel = _clean_release(item)
        if rel and rel["version"] not in seen:
            seen.add(rel["version"])
            out.append(rel)
    return out


def current(path: Path | None = None) -> str:
    """Версия приложения — самый свежий выпуск."""
    rel = releases(path)
    return rel[0]["version"] if rel else ""


def unseen(last_seen: str, path: Path | None = None) -> list[dict[str, Any]]:
    """Выпуски, которых человек ещё не видел (новые сверху).
    Ничего не видел раньше (первый запуск или версия до появления «Что нового») — только самый свежий.
    Видел версию, которой уже нет в файле — тоже последние MAX_SHOWN."""
    rel = releases(path)
    if not rel:
        return []
    seen = (last_seen or "").strip()
    if not seen:
        return rel[:1]
    versions = [r["version"] for r in rel]
    if seen == versions[0]:
        return []
    if seen in versions:
        return rel[: versions.index(seen)][:MAX_SHOWN]
    return rel[:MAX_SHOWN]
