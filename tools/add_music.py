"""Добавить скачанные из Фонотеки YouTube треки в assets/music (к уже готовым): подготовка + запись в catalog.json.

Запуск: .venv/bin/python tools/add_music.py <plan.json>
plan.json: [{"group", "title", "file" (путь к скачанному mp3), "artist"}], а также необязательные
"groups": {id: название} и "hints": {id: когда подходит} в виде объекта {"groups":…, "hints":…, "tracks":[…]}.
Каждый трек готовится как в prepare_music.py (одна громкость, бесшовный повтор по кругу).
Только инструменталы: перед добавлением — tools/check_vocals.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from prepare_music import OUT, loudness, prepare, slug  # noqa: E402

SOURCE = "YouTube Audio Library (Фонотека YouTube)"
LICENSE = "YouTube Audio Library licence — можно использовать в любых роликах, указывать автора не нужно"


def main() -> None:
    plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    cat_path = OUT / "catalog.json"
    cat = json.loads(cat_path.read_text(encoding="utf-8"))
    cat["groups"] = plan.get("groups") or cat["groups"]
    cat["hints"] = plan.get("hints") or cat.get("hints") or {}
    have = {t["id"] for t in cat["tracks"]}
    for item in plan["tracks"]:
        tid = slug(item["title"])
        if tid in have:
            print(f"уже есть: {item['title']}")
            continue
        name = f"{tid}.mp3"
        dur = prepare(Path(item["file"]), OUT / name)
        lufs, lra = loudness(OUT / name)
        print(f"{item['title']:32} {dur:6.1f}s  {lufs:6.1f} LUFS  LRA {lra:4.1f}")
        cat["tracks"].append({
            "id": tid, "title": item["title"], "group": item["group"], "file": name,
            "duration": round(dur, 1), "lufs": lufs, "lra": lra,
            "source": SOURCE, "license": LICENSE, "artist": item.get("artist", ""),
        })
    cat_path.write_text(json.dumps(cat, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
