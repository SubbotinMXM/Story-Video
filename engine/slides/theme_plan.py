"""Один раз читаем весь текст → тема + запросы на пул, без привязки к фразам."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from engine.core.jsonish import json_dict
from engine.slides.llm_client import LLMClient
from engine.core.logutil import log
from engine.slides.story_context import StoryContext, extract_story_context

THEME_NAME = "theme.json"

QUERY_SYSTEM = """\
You plan a documentary B-roll POOL for an entire narration.
The footage is NOT cut to individual sentences. It only has to match the story as a whole:
the person, the era, the place, and what they did.

Return ONLY JSON:
{
  "summary": "one English sentence: who / when / where / what the film is about",
  "video_queries": ["12 to 16 short English video search queries"],
  "image_queries": ["12 to 16 short English still-image search queries"]
}

Rules:
- English only, 4–10 words each
- Every query keeps the film's country/region/city and era ("Japanese fishing village Okinawa",
  not "fishing village"): a frame from another country is useless for this film
- For places, cities, nature: prefer professional cinematic footage words ("aerial drone", "cinematic 4K",
  "timelapse", "golden hour") — not vlogs, news or amateur clips
- If characters[] has a real person, the film is ABOUT that person: at least 12 of 16
  image queries and 4 of 16 video queries must show THEM, with their full name in the query.
  * Person who lived in the age of photography (after ~1850: actors, singers, politicians,
    athletes, scientists of the 20th–21st century): REAL PHOTOS of that person only —
    "<name> photo", "<name> portrait photo", "<name> 1970s", "<name> on stage",
    "<name> interview", "<name> with <colleague>". NEVER statues, busts, paintings,
    engravings or "ancient" — the internet has real photos of them.
  * Person from before photography (Archimedes, Caesar, Peter the Great): statue, bust,
    portrait painting, engraving, illustration.
  Only the remaining queries are the city, places, objects, era.
- If there is NO main person (nature, a city, a general topic), do not invent one.
  Show the place and the subject.
- If historical=true, NEVER write modern scenes (no smartphones, cars, offices, yogurt, jeans)
- Good: "Archimedes marble statue" (ancient)
- Good: "Silviu Stanculescu actor photo" (20th century)
- Bad: "Silviu Stanculescu bronze bust museum" (20th-century person — use photos)
- Good: "Archimedes writing manuscript painting"
- Good: "ancient Greek temple ruins Sicily"
- Bad: "Syracuse harbor aerial" as the majority of a biography
- Bad: "modern engineer at computer"
"""


@dataclass
class ThemePlan:
    context: StoryContext = field(default_factory=StoryContext)
    summary: str = ""
    video_queries: list[str] = field(default_factory=list)
    image_queries: list[str] = field(default_factory=list)

    def clip_text(self) -> str:
        bits = [self.summary, self.context.era, self.context.place]
        bits.extend(self.context.characters[:3])
        bits.extend(self.context.must_include[:4])
        return ", ".join(b.strip() for b in bits if b and b.strip())

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "video_queries": self.video_queries,
            "image_queries": self.image_queries,
            "story_context": self.context.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> ThemePlan:
        if not isinstance(raw, dict):
            return cls()
        return cls(
            context=StoryContext.from_dict(raw.get("story_context")),
            summary=str(raw.get("summary") or "").strip(),
            video_queries=_str_list(raw.get("video_queries")),
            image_queries=_str_list(raw.get("image_queries")),
        )


def build_theme_plan(script_text: str, client: LLMClient) -> ThemePlan:
    text = re.sub(r"\s+", " ", (script_text or "").strip())
    if not text:
        raise ValueError("Пустой текст для темы")
    ctx = extract_story_context(text, client, label="theme_context")
    user = (
        "STORY CONTEXT:\n"
        + json.dumps(ctx.to_dict(), ensure_ascii=False)
        + "\n\nSCRIPT:\n"
        + text[:7000]
        + "\n\nWrite the B-roll pool queries."
    )
    raw = client.complete(label="theme_queries", system=QUERY_SYSTEM, user=user)
    data = _parse_obj(raw)
    plan = ThemePlan(
        context=ctx,
        summary=str(data.get("summary") or "").strip() or ctx.clip_positive(""),
        video_queries=_str_list(data.get("video_queries"))[:16],
        image_queries=_str_list(data.get("image_queries"))[:16],
    )
    if len(plan.video_queries) < 4:
        plan.video_queries = _fallback_queries(ctx, kind="video")
    if len(plan.image_queries) < 6:
        plan.image_queries = _fallback_queries(ctx, kind="image")
    _bias_main_character(plan)
    log(
        f"ThemePlan: {plan.summary!r} videos={len(plan.video_queries)} "
        f"images={len(plan.image_queries)} historical={ctx.historical}"
    )
    return plan


def save_theme(path: Path, plan: ThemePlan) -> None:
    path.write_text(
        json.dumps(plan.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def ensure_theme_plan(run_dir: Path, script_text: str, client: LLMClient) -> ThemePlan:
    """theme.json уже есть (стадия «тема») — берём его, иначе спрашиваем LLM."""
    path = run_dir / THEME_NAME
    plan = load_theme(path)
    if plan is not None:
        return plan
    plan = build_theme_plan(script_text, client)
    save_theme(path, plan)
    return plan


def load_theme(path: Path) -> ThemePlan | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    plan = ThemePlan.from_dict(raw if isinstance(raw, dict) else None)
    if not plan.video_queries and not plan.image_queries:
        return None
    return plan


_YEAR = re.compile(r"(?<!\d)(1[0-9]{3}|20[0-9]{2})(?!\d)")
_CENTURY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\s+century", re.I)
_MODERN = re.compile(r"\b(modern|contemporary|present|today|current|recent)\b", re.I)


def person_era(era: str) -> str:
    """photo — человек эпохи фотографии (после ~1850); painting — 19 век; ancient — раньше."""
    text = era or ""
    years = [int(y) for y in _YEAR.findall(text)]
    if years:
        last = max(years)
        return "photo" if last >= 1870 else "painting" if last >= 1800 else "ancient"
    centuries = [int(c) for c in _CENTURY.findall(text)]
    if centuries:
        last = max(centuries)
        return "photo" if last >= 20 else "painting" if last == 19 else "ancient"
    if _MODERN.search(text):
        return "photo"
    return "ancient" if re.search(r"\b(BC|BCE|ancient|antiquity|medieval)\b", text, re.I) else "photo"


def _decades(era: str) -> list[str]:
    years = sorted({int(y) for y in _YEAR.findall(era or "") if int(y) >= 1870})
    out: list[str] = []
    for y in years:
        d = f"{y // 10 * 10}s"
        if d not in out:
            out.append(d)
    return out[:3]


def hero_era(ctx: StoryContext) -> str:
    """Эпоха героя: ответ LLM (photo / pre_photo), иначе — по годам в строке эпохи."""
    if ctx.person_era == "photo":
        return "photo"
    if ctx.person_era == "pre_photo":
        guess = person_era(ctx.era)
        return guess if guess != "photo" else "ancient"
    return person_era(ctx.era)


def person_queries(name: str, era: str, place: str, kind: str | None = None) -> tuple[list[str], list[str]]:
    """Запросы на изображения героя: фото для людей XX–XXI века, статуи — для древних."""
    kind = kind or person_era(era)
    city = (place or "").split(",")[0].strip()
    if kind == "photo":
        images = [
            name,
            f"{name} photo",
            f"{name} portrait photo",
            f"{name} archive photo",
            f"{name} young",
            f"{name} interview",
            f"{name} on stage",
            *[f"{name} {d}" for d in _decades(era)],
            *([f"{name} {city}"] if city else []),
        ]
        videos = [f"{name} interview", f"{name} archive footage", f"{name} documentary", name]
    elif kind == "painting":
        images = [
            f"{name} portrait",
            f"{name} photograph",
            f"{name} portrait painting",
            f"{name} engraving",
            f"{name} lithograph",
            f"statue of {name}",
            name,
        ]
        videos = [f"{name} portrait", f"{name} documentary", f"statue of {name}"]
    else:
        images = [
            f"{name} marble statue",
            f"{name} bronze bust museum",
            f"{name} portrait painting",
            f"{name} engraving illustration",
            f"{name} sculpture close up",
            f"statue of {name}",
            f"{name} writing manuscript painting",
            f"{name} teaching students painting",
            f"{name} ancient portrait",
            f"bust of {name}",
        ]
        videos = [f"{name} statue", f"statue of {name}", f"{name} marble sculpture", f"{name} portrait"]
    return images, videos


def _bias_main_character(plan: ThemePlan) -> None:
    """Если ролик про человека, запросы на его изображения идут первыми."""
    name = _main_name(plan.context.characters)
    if not name:
        return
    kind = hero_era(plan.context)
    images, videos = person_queries(name, plan.context.era, plan.context.place, kind)
    if kind == "photo":
        # от LLM для человека эпохи фото не берём «статуи/бюсты» — интернет полон его фото
        bad = re.compile(r"\b(statue|bust|sculpture|marble|bronze|engraving|ancient)\b", re.I)
        plan.image_queries = [q for q in plan.image_queries if not (name.lower() in q.lower() and bad.search(q))]
        plan.video_queries = [q for q in plan.video_queries if not (name.lower() in q.lower() and bad.search(q))]
    plan.image_queries = _prepend_unique(images, plan.image_queries)[:20]
    plan.video_queries = _prepend_unique(videos, plan.video_queries)[:18]
    log(f"ThemePlan: герой {name!r} ({kind}) — его изображения впереди")


def _main_name(characters: list[str]) -> str:
    for raw in characters:
        name = " ".join(str(raw or "").split())
        if 2 <= len(name) <= 40 and len(name.split()) <= 4:
            return name
    return ""


def _prepend_unique(head: list[str], tail: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for q in [*head, *tail]:
        key = q.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(q.strip())
    return out


def _fallback_queries(ctx: StoryContext, *, kind: str) -> list[str]:
    base = " ".join(
        x for x in (ctx.era, ctx.place, " ".join(ctx.characters[:2])) if x
    ).strip()
    if not base:
        base = "historical documentary"
    tails = (
        ["portrait", "city street", "landscape", "workshop", "harbor", "crowd"]
        if kind == "video"
        else [
            "portrait painting",
            "city ruins",
            "landscape",
            "artifact",
            "map",
            "statue",
            "harbor",
            "manuscript",
        ]
    )
    return [f"{base} {t}".strip() for t in tails]


def _str_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        q = str(item or "").strip().strip('"').strip("'")
        if q and q not in out:
            out.append(q)
    return out


_parse_obj = json_dict
