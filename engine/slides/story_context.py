"""Глобальный контекст сюжета для релевантных сток-запросов."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from engine.core.jsonish import json_object
from engine.slides.llm_client import LLMClient
from engine.core.logutil import log


CONTEXT_SYSTEM = """\
You extract VISUAL production context from a narration script for stock footage search.
Return ONLY valid JSON (no markdown).

Schema:
{
  "era": "short English era cue, e.g. '3rd century BC Ancient Greece' or 'modern day' or '1940s WWII'",
  "place": "short place cue, e.g. 'Syracuse Sicily' or 'generic city'",
  "title": "1-3 words in the SCRIPT language: the subject of the film, used as a folder name. Person if there is one (Архимед), otherwise the topic (Афины, природа)",
  "characters": ["main people/roles in English, historically accurate names if known"],
  "visual_style": "one of: historical_reenactment | documentary_archive | nature | modern_lifestyle | science | abstract",
  "subject": "one of: person | place | nature | history | other. person = the film is mainly about one or several specific real people (a biography of a celebrity or historical figure, or stories about several celebrities one after another); place = a country, city, region, building, or a nation / people; nature = animals, landscapes, natural phenomena; history = historical events or an era without one main hero; other = anything else",
  "must_include": ["2-6 English keywords that MUST bias every search (era/place/identity)"],
  "avoid": ["4-10 English anachronism keywords to avoid, e.g. smartphone, modern kitchen, yogurt, jeans, car"],
  "historical": true/false,
  "brief": "2-3 English sentences: what or who the film is about, where (country, regions, cities) and when",
  "visual_markers": ["5-10 English visual cues that make a frame recognizably belong to THIS story's place and era: architecture, script on signs, typical people and clothing, landscape and climate, vehicles, landmarks"],
  "wrong_look": ["3-8 English descriptions of frames that would be WRONG for this story: other countries or cultures that are easy to confuse with it, wrong era, e.g. 'European alpine village', 'Chinese signage', 'modern cars' for a medieval story"],
  "person_era": "only for subject=person: photo if real photos or videos of this person exist (born after ~1830: actors, singers, politicians, athletes of the 19th–21st century, living or recently died); pre_photo if they lived before photography (Caesar, Archimedes, Peter the Great); empty string otherwise"
}

Rules:
- historical=true ONLY if the whole story is set in the past and present-day footage would be wrong
  (Archimedes, Caesar, WWII, an actor who died in 1990). If the story reaches the present day
  (a country or people today, a living star, "from ancient times to now"), historical=false:
  modern, high-quality footage is preferred
- For historical=true: avoid MUST list modern lifestyle cues; must_include MUST lock era/place
- Stories about an INVENTION, technology, a chip, a program, a material or a scientific idea (even when an engineer is
  its central figure) are NOT biographies: subject=history (or other), NOT person, and historical=false — photos of the
  same components, circuits, devices, workplaces and diagrams exist today and fit perfectly. Put the decade only into
  "era"; "avoid" lists only things that visibly break the story (e.g. smartphones). subject=person is only for a
  film about a celebrity's life as a personality (career, relationships, scandals).
- Be concrete, English only, short phrases
"""


# Слово с несколькими значениями («гусеница»: гусеница танка или насекомое) читается по теме ролика, а не по самому
# частому смыслу. Правило идёт во все промпты, которые пишут запросы или оценивают кадры, — в любом режиме подбора.
AMBIGUITY_RULE = """\
- READ EVERY WORD IN THE SENSE OF THIS FILM'S TOPIC, never by its most common meaning. A word with several meanings
  (caterpillar: tank track or insect; mouse: computer device or animal; bow: weapon or ribbon; crane: machine or bird;
  mole: animal or spy; drone: aircraft or bee) always means the one that fits the film's brief, its domain and the
  neighbouring lines. In a film about tanks "гусеница" / "caterpillar" is the chain track of a tank, never the insect.
  Keep the film's domain in the query ("tank caterpillar track close-up", not "caterpillar") so the search cannot
  drift to another meaning."""

JUDGE_AMBIGUITY_RULE = """\
Words of the narration have the meaning of THIS film's topic. A candidate that shows ANOTHER meaning of an ambiguous
word (an insect for "caterpillar" in a film about tanks, an animal for "mouse" in a film about computers, a vegetable
for "bow" in a medieval film) does not belong to the film: context 0-3 and reject "wrong meaning of the word"."""


_PRESENT = re.compile(r"\b(present|modern|today|contemporary|current|nowadays|now)\b", re.I)
_YEAR = re.compile(r"(?<!\d)(1[0-9]{3}|20[0-9]{2})(?!\d)")


def reaches_present(era: str) -> bool:
    """Эпоха доходит до наших дней: «1947-present», «modern day», «1960s-2020s»."""
    if _PRESENT.search(era or ""):
        return True
    years = [int(y) for y in _YEAR.findall(era or "")]
    return bool(years) and max(years) >= 2005


@dataclass
class StoryContext:
    era: str = ""
    place: str = ""
    title: str = ""
    characters: list[str] = field(default_factory=list)
    visual_style: str = "documentary_archive"
    # о чём ролик: person | place | nature | history | other (см. source_mix)
    subject: str = ""
    must_include: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    historical: bool = False
    # для ролика про человека: photo — есть его фото/видео; pre_photo — только статуи и картины
    person_era: str = ""
    # «паспорт ролика» для проверки кадров: о чём/где/когда, приметы своего места и чужого
    brief: str = ""
    visual_markers: list[str] = field(default_factory=list)
    wrong_look: list[str] = field(default_factory=list)

    def clip_positive(self, query: str) -> str:
        parts = [query.strip()]
        if self.era:
            parts.append(self.era)
        if self.place:
            parts.append(self.place)
        if self.historical:
            parts.append("historical period accurate costume setting")
            parts.append("not modern not contemporary lifestyle")
        return ", ".join(p for p in parts if p)

    def clip_negative(self) -> str:
        base = list(self.avoid) if self.avoid else []
        if self.historical:
            base.extend(
                [
                    "modern people",
                    "smartphone",
                    "contemporary clothing",
                    "supermarket",
                    "yogurt",
                    "kitchen appliance",
                    "car interior",
                    "laptop",
                    "social media",
                ]
            )
        # уникальные, короткий промпт
        seen: set[str] = set()
        out: list[str] = []
        for w in base:
            key = w.lower().strip()
            if key and key not in seen:
                seen.add(key)
                out.append(w.strip())
        return ", ".join(out[:12])

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict | None) -> StoryContext:
        if not isinstance(raw, dict):
            return cls()
        chars = raw.get("characters") or []
        must = raw.get("must_include") or []
        avoid = raw.get("avoid") or []
        return cls(
            era=str(raw.get("era") or "").strip(),
            place=str(raw.get("place") or "").strip(),
            title=str(raw.get("title") or "").strip(),
            characters=[str(x).strip() for x in chars if str(x).strip()],
            visual_style=str(raw.get("visual_style") or "documentary_archive").strip(),
            subject=str(raw.get("subject") or "").strip().lower(),
            must_include=[str(x).strip() for x in must if str(x).strip()],
            avoid=[str(x).strip() for x in avoid if str(x).strip()],
            # история, доходящая до наших дней, — не «историческая»: современное в приоритете
            historical=bool(raw.get("historical")) and not reaches_present(str(raw.get("era") or "")),
            person_era=str(raw.get("person_era") or "").strip().lower()
            if str(raw.get("person_era") or "").strip().lower() in ("photo", "pre_photo")
            else "",
            brief=str(raw.get("brief") or "").strip(),
            visual_markers=_strs(raw.get("visual_markers")),
            wrong_look=_strs(raw.get("wrong_look")),
        )


def _strs(raw: object) -> list[str]:
    return [str(x).strip() for x in raw if str(x).strip()] if isinstance(raw, list) else []


def extract_story_context(
    script_text: str,
    client: LLMClient,
    *,
    label: str = "story_context",
) -> StoryContext:
    text = re.sub(r"\s+", " ", (script_text or "").strip())
    if not text:
        return StoryContext()
    # не раздувать токены
    snippet = text[:12000]
    user = (
        "Narration script:\n\n"
        f"{snippet}\n\n"
        "Extract visual production context JSON."
    )
    try:
        raw = client.complete(label=label, system=CONTEXT_SYSTEM, user=user)
        data = _parse_json_obj(raw)
        ctx = StoryContext.from_dict(data)
        log(
            f"StoryContext: historical={ctx.historical} era={ctx.era!r} "
            f"place={ctx.place!r} must={ctx.must_include} avoid={ctx.avoid[:5]}"
        )
        return ctx
    except Exception as exc:
        log(f"StoryContext fail, empty: {exc}")
        return StoryContext()


def _parse_json_obj(raw: str) -> dict:
    data = json_object(raw)
    if data is None:
        raise ValueError(f"no JSON object: {(raw or '')[:200]!r}")
    return data
