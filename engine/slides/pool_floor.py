"""Нижняя граница разнообразия кадров: в ролике не меньше 80 РАЗНЫХ кадров (фото, видео, сгенерированные).

Пул, в котором нашлось 8 картинок, раньше молча повторялся до конца ролика (×74) и уходил на выкладку.
Теперь:
1. пул добирается по ТЕМЕ ролика (предметы, устройства, места, процессы — без имени героя), несколько кругов;
2. нехватку закрывают AI-кадры (в пределах денежного лимита ролика);
3. всё равно меньше нормы — сборка останавливается с понятной причиной, ролик не рендерится и не выкладывается.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from engine.core.jsonish import json_dict
from engine.core.logutil import log
from engine.slides.media_pipeline import MediaError
from engine.slides.paths import TIMELINE_NAME

# сколько разных кадров в ролике — не меньше (жёсткая граница) и сколько целимся набрать
MIN_UNIQUE_FRAMES = 80
TOP_UP_ROUNDS = 3
TOPIC_QUERIES = 45

_SYSTEM = """\
You write search queries for still photos and illustrations for a narrated documentary.
The footage must show THE TOPIC of the film: its objects, devices, components, diagrams, machines, places,
workplaces, materials, processes, historical scenes and the world around the story. Return ONLY JSON:
{"queries": ["..."]}

Rules:
- Every query is 3-8 English words and names something concrete a camera or an illustrator can show.
- NEVER put a person's name in a query: photos of one person are rare, and the film already has them.
  A person may appear only as a role ("1970s electronics engineer at a workbench").
- Cover the WHOLE script, not its first paragraph: each query is a different thing from a different part of the story.
- Mix real objects, close-ups, macro details, wide shots of places, vintage photographs of the era,
  diagrams / schematics / blueprints, and everyday scenes that explain the idea.
- Keep the film's domain in each query so a word cannot drift to another meaning.
- Do not repeat or rephrase the queries already used (listed in the request).
"""


def topic_queries(plan, script: str, client, *, used: list[str], count: int = TOPIC_QUERIES) -> list[str]:
    """Запросы по теме ролика без имён людей. Пусто — LLM не ответила (круг пропускается)."""
    text = re.sub(r"\s+", " ", script or "").strip()
    user = (
        f"FILM: {plan.context.brief or plan.summary}\nEra: {plan.context.era}\nPlace: {plan.context.place}\n"
        f"Looks like: {'; '.join(plan.context.visual_markers[:10])}\n"
        f"Already used (do not repeat): {json.dumps(used[-40:], ensure_ascii=False)}\n\n"
        f"SCRIPT (whole narration, abridged):\n{_spread_text(text)}\n\nWrite {count} different queries."
    )
    try:
        raw = client.complete(label="topic_queries", system=_SYSTEM, user=user)
        data = json_dict(raw)
    except Exception as exc:  # сбой LLM не должен ронять сборку: решит граница в конце
        log(f"Добор по теме: запросы не получились ({str(exc)[:120]})")
        return []
    names = _names_of(plan)
    seen = {q.lower() for q in used}
    out: list[str] = []
    for q in data.get("queries") or []:
        q = " ".join(str(q or "").split()).strip('"')
        if not q or q.lower() in seen or any(n in q.lower() for n in names):
            continue
        seen.add(q.lower())
        out.append(q)
    return out[:count]


def _names_of(plan) -> list[str]:
    """Имена и фамилии героев (в нижнем регистре): запросы по теме их не содержат."""
    out: list[str] = []
    for p in plan.context.key_people:
        out.extend(w.lower() for w in str(p.get("name") or "").split() if len(w) >= 4)
    for raw in plan.context.characters:
        for part in re.split(r"[,()]", str(raw)):
            words = [w for w in part.split() if len(w) >= 4 and w[:1].isupper()]
            out.extend(w.lower() for w in words)
    return out


def _spread_text(text: str, limit: int = 9000) -> str:
    """Сценарий целиком, но короче лимита: куски вразбивку, чтобы запросы покрывали весь рассказ."""
    if len(text) <= limit:
        return text
    step = limit // 6
    stride = (len(text) - step) // 5
    return " […] ".join(text[i * stride:i * stride + step] for i in range(6))


def top_up_images(
    images: list[dict], plan, script: str, *, req, http, clients, rng, pool: Path, trials: Path, frames: Path,
    cancel, progress, judge, exclude: list[dict] | None, target: int, pct_lo: float, pct_hi: float,
) -> list[dict]:
    """Картинок меньше цели — добор по теме ролика (до TOP_UP_ROUNDS кругов с новыми запросами)."""
    from engine.slides.theme_media import _fill_kind, _llm

    used = list(plan.image_queries)
    span = pct_hi - pct_lo
    for rnd in range(TOP_UP_ROUNDS):
        if len(images) >= target:
            break
        if cancel:
            cancel.check()
        client = _llm(req)
        if cancel:
            cancel.register(client)
        try:
            queries = topic_queries(plan, script, client, used=used)
        finally:
            client.close()
        if not queries:
            break
        used.extend(queries)
        progress(pct_lo + span * rnd / TOP_UP_ROUNDS,
                 f"Картинок {len(images)} из {target} — добор по теме ролика, круг {rnd + 1}: {len(queries)} запросов")
        before = len(images)
        images, _rej = _fill_kind(
            http, clients, plan, rng=rng, pool=pool, trials=trials, frames=frames, kind="photo", queries=queries,
            req=req, cancel=cancel, progress=progress, pct_lo=pct_lo + span * rnd / TOP_UP_ROUNDS,
            pct_hi=pct_lo + span * (rnd + 1) / TOP_UP_ROUNDS, already=images, need=target, exclude=exclude, judge=judge,
        )
        log(f"Добор по теме, круг {rnd + 1}: +{len(images) - before}, всего {len(images)} из {target}")
    return images


def unique_frames(raw: dict) -> int:
    """Сколько РАЗНЫХ кадров в таймлайне (по файлу): слоты начала, блок после начала, пул фото и видео."""
    paths: set[str] = set()
    for key in ("slots", "loop_block", "images", "videos"):
        for it in raw.get(key) or []:
            if isinstance(it, dict) and it.get("path"):
                paths.add(str(it["path"]))
    return len(paths)


def min_frames(duration: float) -> int:
    """Норма для ролика такой длины: 80 разных кадров на ролик от 20 минут, у короткого — пропорционально."""
    if duration <= 0:
        return MIN_UNIQUE_FRAMES
    return max(10, round(MIN_UNIQUE_FRAMES * min(1.0, duration / 1200.0)))


def check_timeline(run_dir: Path) -> int:
    """Последняя страховка перед рендером, для любого режима подбора: мало разных кадров — не собираем ролик."""
    path = Path(run_dir) / TIMELINE_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    n = unique_frames(raw)
    try:
        from engine.slides import checkpoint as cp

        need = min_frames(float(cp.load(Path(run_dir)).duration or 0))
    except Exception:
        need = MIN_UNIQUE_FRAMES
    if n < need:
        raise MediaError(
            f"Разных кадров в ролике {n} — нужно не меньше {need}. Ролик с повторами одних и тех же "
            "картинок не собираю и не выкладываю. Нажми «Продолжить» — подбор кадров повторится "
            "(можно включить AI-кадры или добавить ключи стоков)."
        )
    return n
