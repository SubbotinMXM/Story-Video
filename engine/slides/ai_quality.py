"""AI-кадры по смыслу ролика: описание пишет LLM, результат проверяет зрячая LLM.

1. Описание (prompt) — не «запрос + Setting: Japan», а сцена этого момента рассказа,
   которая явно принадлежит миру ролика: страна/регион, эпоха, приметы из «паспорта ролика»
   (архитектура, вывески, одежда, природа), без того, что для ролика чужое.
2. Готовый кадр смотрит проверка (judge): не та страна/эпоха, брак, артефакты — одна
   повторная генерация с исправленным описанием; снова мимо — кадр не ставим (остаётся живой).

Описания и итог проверки пишутся в ai_prompts.json: «Продолжить» после сбоя берёт те же
описания — генератор узнаёт задачу и не берёт деньги второй раз.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from engine.core.jsonish import json_dict
from engine.core.cancel import CancelledError
from engine.core.logutil import log
from engine.core.threads import ThreadPoolExecutor
from engine.slides.story_context import AMBIGUITY_RULE

PROMPTS_NAME = "ai_prompts.json"
_BATCH = 12
_CHECK_WORKERS = 6

_SYSTEM = """\
You write prompts for an AI image/video generator that makes B-roll for a documentary film.
For each numbered item write ONE English prompt (40-80 words) for a photorealistic shot that shows
that narration moment AND unmistakably belongs to the film's world from the FILM PASSPORT:
name the country/region/city and era, and include 2-4 concrete visual markers of that place
(architecture, script on signs, typical clothing, landscape, climate, vehicles).
Describe a concrete subject, setting, time of day and light; for VIDEO add exactly one slow smooth
camera move (slow dolly-in, gentle pan, drone flyover); for PHOTO add composition and lens.
If the moment is abstract, show a concrete symbolic scene from the film's world.
Never: text, captions, logos, watermarks, split screen, readable signs (only blurred background signage),
anything from "Wrong for this film", anachronisms.
Return ONLY JSON: {"prompts": ["...", "..."]} — same count and order as the items.
"""
_SYSTEM = _SYSTEM.replace("Return ONLY JSON:", AMBIGUITY_RULE + "\nReturn ONLY JSON:", 1)


def _hash(*parts: str) -> str:
    return hashlib.sha256("␟".join(parts).encode("utf-8")).hexdigest()[:16]


class AiQuality:
    """Описания и проверка AI-кадров одного ролика. judge — engine.slides.judge.Judge."""

    def __init__(self, judge, run_dir: Path) -> None:
        self.judge = judge
        self.path = run_dir / PROMPTS_NAME
        self._lock = threading.Lock()
        try:
            self.cache: dict = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.cache = {}

    def _save(self) -> None:
        with self._lock:
            self.path.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")

    # ---- описания ----

    def prompts(self, items: list[dict]) -> list[str | None]:
        """items: {"key", "kind", "moment", "around", "query"} → описание на каждый (None — не вышло)."""
        out: list[str | None] = [None] * len(items)
        todo: list[int] = []
        for i, it in enumerate(items):
            rec = self.cache.get(it["key"]) or {}
            if rec.get("src") == self._src(it) and rec.get("prompt"):
                out[i] = rec["prompt"]
            else:
                todo.append(i)
        for start in range(0, len(todo), _BATCH):
            chunk = todo[start : start + _BATCH]
            got = self._ask([items[i] for i in chunk])
            for i, prompt in zip(chunk, got):
                if prompt:
                    out[i] = prompt
                    self.cache[items[i]["key"]] = {"src": self._src(items[i]), "prompt": prompt}
        if todo:
            self._save()
        return out

    @staticmethod
    def _src(it: dict) -> str:
        return _hash(it.get("kind", ""), it.get("moment", ""), it.get("around", ""), it.get("query", ""))

    def _ask(self, items: list[dict]) -> list[str]:
        lines = []
        for n, it in enumerate(items, start=1):
            around = f"\n   (context around: {it['around'][:300]})" if it.get("around") else ""
            lines.append(f"{n}. {it['kind'].upper()}: «{(it.get('moment') or it.get('query') or '')[:400]}»{around}")
        user = f"FILM PASSPORT:\n{self.judge.brief}\n\nITEMS:\n" + "\n".join(lines)
        try:
            raw = self.judge.client.complete(label="ai_prompts", system=_SYSTEM, user=user)
        except CancelledError:
            raise
        except Exception as exc:
            log(f"AI-кадры: LLM не написал описания ({exc}) — беру простые")
            return []
        data = json_dict(raw)
        got = [str(x).strip() for x in (data.get("prompts") or []) if isinstance(x, str)]
        return got[: len(items)]

    # ---- проверка готового кадра ----

    def check(self, tasks: list, texts: dict[str, str], *, data_keys: set[str] | None = None) -> dict[str, str]:
        """Готовые кадры глазами LLM. → {key задачи: причина отказа} для неподошедших.
        Проверка недоступна — отказов нет (кадр ставится, как раньше)."""
        if self.judge is None:
            return {}
        data_keys = data_keys or set()
        bad: dict[str, str] = {}
        todo = []
        for t in tasks:
            if t.result is None:
                continue
            rec = self.cache.get(t.key) or {}
            if rec.get("checked") == t.prompt:  # уже проверен (продолжение после сбоя)
                if rec.get("bad"):
                    bad[t.key] = rec["bad"]
                continue
            todo.append(t)

        def one(t) -> tuple[object, str | None]:
            verdicts = self.judge.rate(
                [t.result],
                moment=texts.get(t.key, ""),
                query=("infographic: " if t.key in data_keys else "") + t.prompt[:200],
                thematic=not texts.get(t.key),
                label="judge_ai",
            )
            if verdicts is None:
                return t, _local_reject(t.result)
            v = verdicts[0]
            log(f"AI-кадр {t.key}: {v.note()}")
            return t, ("" if v.ok else (v.reject or f"не похоже на мир ролика (контекст {v.context}/10)"))

        if todo:
            with ThreadPoolExecutor(max_workers=min(_CHECK_WORKERS, len(todo))) as ex:
                for t, why in ex.map(one, todo):
                    if why is None:
                        continue
                    rec = self.cache.setdefault(t.key, {})
                    rec.update({"checked": t.prompt, "bad": why})
                    if why:
                        bad[t.key] = why
            self._save()
        return bad

    def fix_prompt(self, prompt: str, why: str, *, place: str, era: str, markers: list[str]) -> str:
        where = ", ".join(x for x in (place, era) if x)
        look = "; ".join(markers[:5])
        return (
            f"{prompt} IMPORTANT: a previous attempt was rejected ({why}). "
            + (f"The shot must clearly look like {where}" if where else "The shot must clearly match the film")
            + (f": {look}." if look else ".")
        )


def _local_reject(path) -> str | None:
    """Зрячая нейросеть недоступна: локально (CLIP) ловим только явный брак — надписи, каракули-буквы,
    коллаж. Брак → причина отказа; чисто → None (кадр ставится, позже его ещё проверит нейросеть)."""
    from pathlib import Path

    from engine.slides.clip_ranker import JUNK_FROM, junk_scores

    try:
        prob = junk_scores([Path(path)]).get(Path(path), 0.0)
    except Exception:
        return None
    if prob >= JUNK_FROM:
        log(f"AI-кадр {Path(path).name}: локальная проверка — надпись или коллаж ({prob:.2f})")
        return "надпись, буквы или коллаж на кадре (локальная проверка)"
    return None
