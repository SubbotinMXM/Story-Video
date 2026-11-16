"""Кто именно эти люди: имя + контекст группы → однозначный человек (а не его тёзка).

Один запрос к точной модели на всю группу. Тёзки и однофамильцы отсеиваются по большинству: если
девять человек — советские артисты, десятый тоже советский артист, а не современный актёр с тем же
именем. Подсказки пользователя («Андрей Миронов (советский актёр)») — главнее всего. Если и по контексту
не понять, о ком речь, — doubt: программа спрашивает пользователя, а не берёт наугад.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from engine.core.logutil import log

_SYSTEM = """Ты редактор документальных роликов о знаменитостях и точно знаешь, кто есть кто.
Тебе дают список людей одной подборки (и иногда текст ролика). Для каждого определи, кто именно имеется в виду.
Правила:
- подборка обычно однородна: если большинство — советские артисты, то и остальные — советские артисты той же
  эпохи, а не современные тёзки и однофамильцы; так же со спортсменами, певцами, политиками и т.д.;
- уточнение в скобках после имени и уточнения пользователя — главнее всего;
- если человек с таким именем в этом контексте один — doubt пустой;
- doubt заполняй, только если даже с учётом контекста подходят два разных реальных человека: перечисли их
  коротко («советский актёр, 1941–1987 / российский актёр, род. 1975»).
Ответ только JSON:
{"people": [{"name": "имя как во входе", "who": "кто это: профессия и страна, коротко", "years": "годы жизни",
  "search": "запрос для поиска фото именно этого человека: полное имя + 1–3 уточняющих слова",
  "look": "как он выглядит на фото этой эпохи: пол, возраст в годы славы, характер снимков",
  "doubt": ""}],
 "group": "кто эта подборка в целом, одной фразой"}"""


@dataclass
class Person:
    name: str
    hint: str = ""  # уточнение пользователя из скобок
    who: str = ""
    years: str = ""
    search: str = ""
    look: str = ""
    doubt: str = ""

    def query(self) -> str:
        return self.search or " ".join(x for x in (self.name, self.who) if x)

    def describe(self) -> str:
        """Для проверки фото зрячей моделью: кто должен быть на фото."""
        bits = [self.name, self.who, self.years and f"({self.years})", self.look]
        return ", ".join(b for b in bits if b)


@dataclass
class Cast:
    people: list[Person]
    group: str = ""
    doubts: list[str] = field(default_factory=list)


_HINT = re.compile(r"^(.*?)\s*[(\[]([^)\]]+)[)\]]\s*$")
# «Marga Barbu — Марга Барбу», «Андрей Мартынов — старшина Васков»: после тире — уточнение, не имя
_DASH = re.compile(r"^(.*?\S)\s+[—–-]\s+(\S.*)$")


def split_hint(raw: str) -> tuple[str, str]:
    """«Андрей Миронов (советский актёр)» / «Андрей Миронов — советский актёр» → («Андрей Миронов», «советский актёр»)."""
    name, hint = raw.strip(), ""
    m = _HINT.match(name)
    if m and m.group(1).strip():
        name, hint = m.group(1).strip(), m.group(2).strip()
    m = _DASH.match(name)
    if m:
        name, hint = m.group(1).strip(), "; ".join(x for x in (m.group(2).strip(), hint) if x)
    return name, hint


def identify(names: list[str], *, llm: tuple[str, str, str] | None, context: str = "", notes: str = "") -> Cast:
    """Имена (можно с уточнениями в скобках) → кто они. Без LLM или при сбое — как есть, без сомнений."""
    people = [Person(*split_hint(n)) for n in names]
    if not people or not (llm and llm[0]):
        return Cast(people)
    from engine.slides.llm_client import LLMClient

    lines = [f"- {p.name}" + (f" (уточнение пользователя: {p.hint})" if p.hint else "") for p in people]
    user = "ЛЮДИ ПОДБОРКИ:\n" + "\n".join(lines)
    if notes.strip():
        user += f"\n\nУТОЧНЕНИЯ ПОЛЬЗОВАТЕЛЯ (главнее всего):\n{notes.strip()[:3000]}"
    if context.strip():
        user += f"\n\nТЕКСТ РОЛИКА (для контекста):\n{context.strip()[:12000]}"
    key, base, model = llm
    client = LLMClient(key, model, base)
    client.busy_wait = 120
    try:
        raw = client.complete(label="judge_identity", system=_SYSTEM, user=user)
    except Exception as exc:
        log(f"Кто есть кто: не получилось ({str(exc)[:120]}) — ищу по именам")
        return Cast(people)
    finally:
        client.close()
    data = _parse(raw)
    by_name = {str(x.get("name") or "").strip().lower(): x for x in data.get("people") or [] if isinstance(x, dict)}
    doubts: list[str] = []
    answers = [x for x in data.get("people") or [] if isinstance(x, dict)]
    for k, p in enumerate(people):
        x = by_name.get(p.name.lower())
        if x is None:  # нейросеть переписала имя по-своему — берём ответ по порядку
            x = answers[k] if len(answers) == len(people) else {}
        p.who, p.years = str(x.get("who") or "").strip(), str(x.get("years") or "").strip()
        p.search, p.look = str(x.get("search") or "").strip(), str(x.get("look") or "").strip()
        # уточнил пользователь — сомнений нет
        p.doubt = "" if p.hint else str(x.get("doubt") or "").strip()
        if p.doubt:
            doubts.append(f"{p.name}: {p.doubt}")
    cast = Cast(people, str(data.get("group") or "").strip(), doubts)
    log("Кто есть кто: " + (cast.group + " — " if cast.group else "")
        + "; ".join(f"{p.name} — {p.who or '?'}" for p in people))
    return cast


def doubt_message(doubts: list[str], where: str) -> str:
    return ("Не уверен, о ком именно речь — есть тёзки или однофамильцы:\n" + "\n".join(f"• {d}" for d in doubts)
            + f"\nУточни {where}, например «Андрей Миронов (советский актёр)», и запусти снова.")


def _parse(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
