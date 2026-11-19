"""Какую модель LLM брать: самая дешёвая по живому прайсу сервиса, недоступна — следующая по цене.

Два уровня задач:
- light — простая работа (поисковые запросы кадров, главы, переводы): самая дешёвая модель вообще;
- smart — где важна точность (судья по картинкам, проверка рерайта, сам рерайт, факты темы):
  самая дешёвая из «полных» моделей (не облегчённых luna/mini/nano). Проверено на кадрах и тексте:
  облегчённая путает страны на картинках и не видит путаницу имён в рассказе.

Прайс — GET <сервис>/api/pricing по ключу (у dindindon свой список для ключа), иначе список моделей
/v1/models в порядке известных цен, иначе встроенный прайс. Кеш — 6 часов (память + data/llm_pricing.json).
"""

from __future__ import annotations

import json
import os
import threading
import time
from urllib.parse import urlsplit

import httpx

from engine.core.config import DATA_DIR
from engine.core.logutil import log

# прайс dindindon на 25.10.2026, кредиты за 1M токенов: (модель, вход, выход)
FALLBACK_PRICES: list[tuple[str, float, float]] = [
    ("gpt-6-luna", 80, 400),
    ("gpt-5.6-luna", 160, 960),
    ("gpt-6.1-sol", 400, 2000),
    ("gpt-6-sol", 400, 2000),
    ("gpt-5.6-terra", 400, 2400),
    ("gpt-5.6-sol", 1000, 6000),
    ("gpt-5.5", 1000, 6000),
    ("gpt-6-astra", 2000, 10000),
]
LIGHT_MARKERS = ("luna", "mini", "nano", "lite", "flash")
# не текстовые модели в общем списке сервиса
_NOT_TEXT = ("image", "tts", "whisper", "embedding", "dall-e", "sora", "veo")
# задачи, где нужна точность; остальные — light
SMART_LABELS = ("judge", "rewrite_", "theme_context", "story_context", "stars_cast", "ai_prompts", "niche_similar")
# простые задачи: меньше скрытых размышлений (−20…40% токенов ответа). Проверено на запросах кадров
# и главах — ответы те же; «none» не берём: у части моделей с ним ответ странно долгий
LIGHT_REASONING = "low"
# точная модель, но ответ короткий и по готовому тексту — длинные скрытые размышления не нужны: у проверки рерайта
# ~90% ответа уходило в них (журнал 14.11: 2,1 тыс. из 2,4 тыс. токенов), сам ответ — список правок и хук
LOW_REASONING_LABELS = ("rewrite_review",)


# оценка кадров листом (коллаж, надпись, водяной знак, не тот человек): ответ — короткий JSON, глубокие размышления не нужны
LOW_REASONING_VISION = ("judge_pool",)


def reasoning_for(label: str, vision: bool = False) -> str:
    """Какое усилие размышлений просить у встроенной нейросети: «low» — простым задачам и коротким ответам."""
    if vision:
        return LIGHT_REASONING if label.startswith(LOW_REASONING_VISION) else ""
    return LIGHT_REASONING if tier_of(label) == "light" or label.startswith(LOW_REASONING_LABELS) else ""
# у нас в запросе текста в десятки раз больше, чем в ответе — цена входа важнее
OUTPUT_WEIGHT = 0.1
CACHE_SEC = 6 * 3600
CACHE_FILE = DATA_DIR / "llm_pricing.json"

_lock = threading.Lock()
_memory: dict[str, tuple[float, list[dict]]] = {}


def tier_of(label: str) -> str:
    return "smart" if label.startswith(SMART_LABELS) else "light"


def is_light(model: str) -> bool:
    return any(m in model.lower() for m in LIGHT_MARKERS)


def _cost(item: dict) -> tuple[float, float]:
    # при равной цене — у кого дешевле кеш (повторяющиеся инструкции в запросах)
    return float(item["input"]) + float(item["output"]) * OUTPUT_WEIGHT, float(item.get("cache") or 0)


def _origin(base_url: str) -> str:
    parts = urlsplit(base_url)
    return f"{parts.scheme}://{parts.netloc}"


def _fetch(base_url: str, api_key: str) -> list[dict]:
    """[{model, input, output}] — только текстовые модели. Пусто — сервис ничего не сказал."""
    headers = {"Authorization": f"Bearer {api_key.strip()}"}
    known = {m: (i, o) for m, i, o in FALLBACK_PRICES}
    with httpx.Client(timeout=httpx.Timeout(15.0, connect=8.0), trust_env=False, follow_redirects=True) as http:
        try:
            resp = http.get(f"{_origin(base_url)}/api/pricing", headers=headers)
            if resp.status_code == 200:
                items = [
                    {"model": str(it["model"]), "input": float(it["input_credits"]), "output": float(it["output_credits"]),
                     "cache": float(it.get("cache_credits") or 0)}
                    for it in (resp.json().get("items") or [])
                    if it.get("input_credits") is not None and it.get("output_credits") is not None
                    and not any(x in str(it.get("model")).lower() for x in _NOT_TEXT)
                ]
                if items:
                    return items
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            pass
        try:
            resp = http.get(f"{base_url.rstrip('/')}/models", headers=headers)
            ids = [str(m.get("id") or "") for m in (resp.json().get("data") or [])] if resp.status_code == 200 else []
        except (httpx.HTTPError, ValueError, AttributeError):
            ids = []
    ids = [i for i in ids if i and not any(x in i.lower() for x in _NOT_TEXT)]
    # цен нет — известные по встроенному прайсу, незнакомые после них
    return [{"model": i, "input": known.get(i, (1e6, 1e6))[0], "output": known.get(i, (1e6, 1e6))[1]} for i in ids]


def _read_file(key: str) -> tuple[float, list[dict]] | None:
    try:
        raw = json.loads(CACHE_FILE.read_text(encoding="utf-8")).get(key)
        return float(raw["at"]), list(raw["items"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _write_file(key: str, items: list[dict]) -> None:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.is_file() else {}
    except (OSError, ValueError):
        data = {}
    data[key] = {"at": time.time(), "items": items}
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def pricing(base_url: str, api_key: str, *, refresh: bool = False) -> list[dict]:
    """Текстовые модели сервиса с ценами, от дешёвых к дорогим."""
    if os.environ.get("STORY_VIDEO_LLM_OFFLINE"):  # тесты: без сети и без записи в data/
        return sorted(({"model": m, "input": i, "output": o} for m, i, o in FALLBACK_PRICES), key=_cost)
    key = f"{_origin(base_url)}|{api_key.strip()[-6:]}"
    with _lock:
        now = time.time()
        cached = _memory.get(key) or _read_file(key)
        if cached and not refresh and now - cached[0] < CACHE_SEC and cached[1]:
            _memory[key] = cached
            return cached[1]
        items = _fetch(base_url, api_key) if api_key.strip() else []
        if items:
            items.sort(key=_cost)
            _memory[key] = (now, items)
            _write_file(key, items)
            log("LLM: модели по цене — " + ", ".join(i["model"] for i in items))
            return items
        if cached and cached[1]:
            return cached[1]  # сервис не ответил — прошлый прайс лучше встроенного
    return sorted(({"model": m, "input": i, "output": o} for m, i, o in FALLBACK_PRICES), key=_cost)


def chain(base_url: str, api_key: str, tier: str = "light") -> list[str]:
    """Порядок моделей для задачи: первая — основная, дальше — запасные по цене."""
    models = [i["model"] for i in pricing(base_url, api_key)]
    if tier == "smart":
        full = [m for m in models if not is_light(m)]
        return full + [m for m in models if is_light(m)] if full else models
    return models
