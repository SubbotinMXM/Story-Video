"""Смешивание источников: сколько кандидатов брать с каждого и какое качество ждать.

Для каждого куска текста есть CANDIDATE_CAP мест под кандидатов. Они делятся
между источниками по долям, которые зависят от темы ролика, и подстраиваются
по ходу подбора: источник, который чаще даёт подходящие кадры, получает больше
мест. У каждого доступного источника остаётся минимум одно место — разнообразие.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# Нижняя граница качества: видео и картинки ниже не берём никогда.
MIN_HEIGHT = 720
# Целимся в 1080p: пока есть время, предпочитаем его.
TARGET_HEIGHT = 1080
# Тайминги подбора кадров (ролик целиком ~15 мин, из них на подбор ~7).
SOFT_SEC = 180.0  # дальше хорошее 720p уже не хуже 1080p — экономим время
HARD_SEC = 360.0  # дальше берём лучшее из найденного, без перебора
# Один источник не должен забирать больше этой доли кадров.
DIVERSITY_SHARE = 0.6
DIVERSITY_MIN_PICKS = 5

SUBJECTS = ("person", "place", "nature", "history", "other")

# Базовые доли под 6 мест. Ключи — группы источников.
_VIDEO_WEIGHTS = {
    "person": {"youtube": 4, "stocks": 1, "archive": 1},
    "place": {"stocks": 3, "youtube": 3},
    "nature": {"stocks": 4, "youtube": 2},
    "history": {"youtube": 3, "archive": 2, "stocks": 1},
    "other": {"youtube": 3, "stocks": 2, "archive": 1},
}
_IMAGE_WEIGHTS = {
    # про конкретного человека: его фото есть в основном в Google; стоки — только фон эпохи/места
    "person": {"google": 4, "tmdb": 2, "wikimedia": 1, "stocks": 1},
    "place": {"stocks": 3, "google": 2, "wikimedia": 1, "tmdb": 1},
    "nature": {"stocks": 4, "google": 2},
    "history": {"google": 2, "wikimedia": 2, "archive": 1, "stocks": 1, "tmdb": 1},
    "other": {"google": 2, "stocks": 2, "wikimedia": 1, "archive": 1, "tmdb": 1},
}
# Доля Google в пуле картинок (режим без строгого соответствия).
GOOGLE_SHARE = {"person": 0.8, "place": 0.35, "nature": 0.3, "history": 0.45, "other": 0.45}

_STOCK_PROVIDERS = {"pexels", "pixabay", "unsplash"}

# Доли контента в ролике по умолчанию, % — как подбор работает сейчас для «обычной» темы
# (_VIDEO_WEIGHTS/_IMAGE_WEIGHTS «other», видео и фото примерно поровну, AI — AI_SHARE).
# Пока пользователь их не менял, доли сами сдвигаются под тему ролика (человек, место, природа…);
# свои цифры (настройка source_mix) применяются как есть ко всем роликам.
DEFAULT_MIX = {"ai": 12, "stocks": 30, "youtube": 22, "google": 15, "wikimedia": 6, "archive": 12, "tmdb": 3}
MIX_KEYS = tuple(DEFAULT_MIX)
_VIDEO_GROUPS = ("youtube", "stocks", "archive")
_IMAGE_GROUPS = ("google", "stocks", "wikimedia", "archive", "tmdb")


def clean_mix(raw: object) -> dict[str, int] | None:
    """Свои доли из настроек → {источник: %}; пусто или как по умолчанию — None (автоподбор по теме)."""
    if not isinstance(raw, dict) or not raw:
        return None
    out: dict[str, int] = {}
    for k in MIX_KEYS:
        try:
            out[k] = max(0, min(100, int(round(float(raw.get(k, DEFAULT_MIX[k]))))))
        except (TypeError, ValueError):
            out[k] = DEFAULT_MIX[k]
    if out == DEFAULT_MIX or not any(out[k] for k in MIX_KEYS if k != "ai"):
        return None
    return out


def custom_weights(mix: dict[str, int], *, video: bool) -> dict[str, int]:
    """Свои доли → веса источников для видео или фото (источник с 0% не ищется)."""
    groups = _VIDEO_GROUPS if video else _IMAGE_GROUPS
    weights = {g: mix.get(g, 0) for g in groups if mix.get(g, 0) > 0}
    return weights or {"stocks": 1}


def google_share(mix: dict[str, int]) -> float:
    """Доля Google среди картинок по своим долям."""
    total = sum(mix.get(g, 0) for g in _IMAGE_GROUPS)
    return mix.get("google", 0) / total if total else 0.0


def ordered_sources(mix: dict[str, int], *, video: bool) -> list[str]:
    """Источники пула по убыванию своих долей (0% — не ищем)."""
    groups = _VIDEO_GROUPS if video else _IMAGE_GROUPS
    return [g for g in sorted(groups, key=lambda g: -mix.get(g, 0)) if mix.get(g, 0) > 0]


def source_group(provider: str) -> str:
    p = (provider or "").lower()
    if p in _STOCK_PROVIDERS:
        return "stocks"
    if p in ("google", "bing"):
        return "google"
    return p


def subject_of(subject: str, *, visual_style: str = "", historical: bool = False) -> str:
    """Тема ролика от LLM; для старых запусков без поля — грубая догадка."""
    s = (subject or "").strip().lower()
    if s in SUBJECTS:
        return s
    if visual_style == "nature":
        return "nature"
    if historical:
        return "history"
    return "other"


def base_weights(subject: str, *, video: bool) -> dict[str, int]:
    table = _VIDEO_WEIGHTS if video else _IMAGE_WEIGHTS
    return dict(table.get(subject, table["other"]))


def allot(weights: dict[str, float], available: list[str], slots: int) -> dict[str, int]:
    """Разделить slots мест между доступными источниками: минимум 1 каждому, остальное по весам."""
    names = [n for n in weights if n in available and weights[n] > 0]
    if not names:
        return {}
    if slots <= len(names):
        ranked = sorted(names, key=lambda n: -weights[n])
        return {n: 1 for n in ranked[:slots]}
    total = sum(weights[n] for n in names)
    exact = {n: slots * weights[n] / total for n in names}
    out = {n: max(1, int(exact[n])) for n in names}
    while sum(out.values()) > slots:  # минимум по 1 месту съел лишнее — отнимаем у самых крупных
        big = max((n for n in names if out[n] > 1), key=lambda n: out[n] - exact[n])
        out[big] -= 1
    while sum(out.values()) < slots:  # остаток — тем, кто больше всех недополучил
        out[max(names, key=lambda n: exact[n] - out[n])] += 1
    return out


@dataclass
class SourceStats:
    """Что источники давали по ходу подбора (общий на все потоки)."""

    seen: dict[str, list[int]] = field(default_factory=dict)  # оценки всех проверенных кадров
    picks: dict[str, int] = field(default_factory=dict)  # сколько кадров из источника в ролике
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def observe(self, provider: str, fit: int) -> None:
        with self._lock:
            self.seen.setdefault(source_group(provider), []).append(int(fit))

    def picked(self, provider: str) -> None:
        with self._lock:
            g = source_group(provider)
            self.picks[g] = self.picks.get(g, 0) + 1

    def unpick(self, provider: str) -> None:
        with self._lock:
            g = source_group(provider)
            if self.picks.get(g, 0) > 0:
                self.picks[g] -= 1

    def adjusted(self, weights: dict[str, int]) -> dict[str, float]:
        """Базовые доли × (средняя оценка источника / средняя по всем), в пределах 0.5–2."""
        with self._lock:
            seen = {k: list(v) for k, v in self.seen.items()}
        all_fits = [f for v in seen.values() for f in v]
        if len(all_fits) < 6:
            return {k: float(v) for k, v in weights.items()}
        mean = max(0.5, sum(all_fits) / len(all_fits))
        out: dict[str, float] = {}
        for name, w in weights.items():
            fits = seen.get(name) or []
            if len(fits) < 3:
                out[name] = float(w)
                continue
            ratio = (sum(fits) / len(fits)) / mean
            out[name] = w * min(2.0, max(0.5, ratio))
        return out

    def overused(self, provider: str) -> bool:
        with self._lock:
            total = sum(self.picks.values())
            if total < DIVERSITY_MIN_PICKS:
                return False
            return self.picks.get(source_group(provider), 0) / total > DIVERSITY_SHARE

    def summary(self) -> str:
        with self._lock:
            total = sum(self.picks.values()) or 1
            return ", ".join(f"{k} {v * 100 // total}%" for k, v in sorted(self.picks.items(), key=lambda kv: -kv[1]))


class Budget:
    """Фазы подбора по времени: normal → soft → hard."""

    def __init__(self, soft: float = SOFT_SEC, hard: float = HARD_SEC) -> None:
        self.t0 = time.monotonic()
        self.soft = soft
        self.hard = hard

    @classmethod
    def for_beats(cls, n_beats: int, *, slow: bool = False) -> Budget:
        """Длинный ролик в режиме «весь ролик» получает время пропорционально кускам."""
        # кадров стало вдвое больше (смена каждые 3–5 с), а общее время держим прежним:
        # ~3,5 с на кадр до «хватит качества 720p», ~6 с — до «берём лучшее из найденного».
        # slow — каждый кадр ещё смотрит LLM: времени на четверть больше (качество важнее, но в пределах)
        k = 1.25 if slow else 1.0
        return cls(soft=max(SOFT_SEC, 3.5 * k * n_beats), hard=max(HARD_SEC, 6.0 * k * n_beats))

    @property
    def phase(self) -> str:
        dt = time.monotonic() - self.t0
        if dt >= self.hard:
            return "hard"
        if dt >= self.soft:
            return "soft"
        return "normal"
