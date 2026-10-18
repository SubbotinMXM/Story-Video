"""Подготовка фоновой музыки: assets/music/*.mp3 + catalog.json.

Каждый трек:
1. без тишины в начале и в конце;
2. бесшовный для повтора по кругу — конец плавно перетекает в начало (кроссфейд 3 с),
   поэтому при зацикливании на весь ролик нет щелчка и провала громкости на стыке;
3. одна громкость для всех треков и ровная по всей длине: тихие места подняты
   (dynaudnorm), общий уровень — EBU R128 −16 LUFS, LRA 5.

Запуск: .venv/bin/python tools/prepare_music.py <папка с исходниками> <plan.json>
plan.json: [{"group", "title", "file", "license"}] (исходники — FreePD, CC0).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.slides.ffmpeg_util import find_ffmpeg  # noqa: E402
from engine.slides.ffprobe_util import probe_duration  # noqa: E402

OUT = ROOT / "assets" / "music"
CROSSFADE = 3.0
TARGET = "I=-16:LRA=5:TP=-1.5"
GROUPS = {
    "calm": "Спокойная, атмосферная",
    "epic": "Эпическая, кинематографичная",
    "energetic": "Энергичная, вдохновляющая",
    "ethnic": "Этническая: Восток, пустыня, кочевники",
    "mystery": "Задумчивая, таинственная",
}


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stderr


def loudness(path: Path) -> tuple[float, float]:
    """(интегральная громкость LUFS, разброс LRA) готового файла."""
    err = run([find_ffmpeg(), "-hide_banner", "-i", str(path), "-af", "ebur128", "-f", "null", "-"])
    i = float(re.findall(r"I:\s+(-?[\d.]+) LUFS", err)[-1])
    lra = float(re.findall(r"LRA:\s+([\d.]+) LU", err)[-1])
    return i, lra


def full_sound_span(path: Path, *, drop_db: float = 6.0) -> tuple[float, float]:
    """Где трек звучит в полную силу: без тихого вступления и затухания в конце.

    Иначе стык при повторе склеивает два тихих места — слышен провал громкости.
    """
    err = run([find_ffmpeg(), "-hide_banner", "-i", str(path), "-af", "ebur128", "-f", "null", "-"])
    level = float(re.findall(r"I:\s+(-?[\d.]+) LUFS", err)[-1])
    points = [(float(t), float(m)) for t, m in re.findall(r"t:\s*([\d.]+)\s+TARGET.*?M:\s*(-?[\d.]+)", err)]
    loud = [t for t, m in points if m >= level - drop_db]
    if not loud:
        return 0.0, probe_duration(path)
    # момент M — окно 0,4 с, заканчивающееся в t
    return max(0.0, loud[0] - 0.4), loud[-1]


def prepare(src: Path, dest: Path) -> float:
    ff = find_ffmpeg()
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "raw.wav"
        trimmed = Path(tmp) / "t.wav"
        run([ff, "-y", "-i", str(src), "-ar", "44100", "-ac", "2", str(raw)])
        start, end = full_sound_span(raw)
        run([ff, "-y", "-i", str(raw), "-af", f"atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS", str(trimmed)])
        dur = probe_duration(trimmed)
        c = CROSSFADE
        graph = (
            "[0:a]asplit=3[a][b][c];"
            f"[a]atrim=start={dur - c:.3f}:end={dur:.3f},asetpts=PTS-STARTPTS[tail];"
            f"[b]atrim=start=0:end={c:.3f},asetpts=PTS-STARTPTS[head];"
            f"[c]atrim=start={c:.3f}:end={dur - c:.3f},asetpts=PTS-STARTPTS[body];"
            f"[tail][head]acrossfade=d={c:.3f}:c1=tri:c2=tri[seam];"
            "[seam][body]concat=n=2:v=0:a=1[loop];"
            # тихие места трека поднимаются (окно ~1 с, до +18 дБ), затем одна громкость для всех
            f"[loop]dynaudnorm=f=1000:g=31:m=8:p=0.9,loudnorm={TARGET}[out]"
        )
        run([ff, "-y", "-i", str(trimmed), "-filter_complex", graph, "-map", "[out]",
             "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", str(dest)])
    return probe_duration(dest)


def main() -> None:
    src_dir, plan_path = Path(sys.argv[1]), Path(sys.argv[2])
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    catalog = []
    for item in plan:
        name = f"{slug(item['title'])}.mp3"
        dur = prepare(src_dir / item["file"], OUT / name)
        lufs, lra = loudness(OUT / name)
        print(f"{item['title']:32} {dur:6.1f}s  {lufs:6.1f} LUFS  LRA {lra:4.1f}")
        catalog.append({
            "id": slug(item["title"]),
            "title": item["title"],
            "group": item["group"],
            "file": name,
            "duration": round(dur, 1),
            "lufs": lufs,
            "lra": lra,
            "source": "FreePD.com (archive.org/details/abook-music-freepd-cc0)",
            "license": item.get("license") or "https://creativecommons.org/publicdomain/zero/1.0/",
        })
    (OUT / "catalog.json").write_text(
        json.dumps({"groups": GROUPS, "tracks": catalog}, ensure_ascii=False, indent=1), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
