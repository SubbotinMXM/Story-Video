"""Сервисы озвучки.

Новый сервис: класс-наследник TtsProvider в отдельном файле + строка в PROVIDERS.
"""

from __future__ import annotations

from typing import Any

from engine.tts.base import FieldSpec, NamedList, TtsProvider
from engine.tts.lumean import LumeanProvider
from engine.tts.voicegen import VoiceGenProvider

PROVIDERS: dict[str, TtsProvider] = {p.id: p for p in (LumeanProvider(), VoiceGenProvider())}
DEFAULT_PROVIDER = "lumean"


def get_provider(provider_id: str) -> TtsProvider:
    provider = PROVIDERS.get((provider_id or "").strip())
    if provider is None:
        raise ValueError(f"Неизвестный сервис озвучки: {provider_id}")
    return provider


def provider_options(cfg: dict[str, Any], provider_id: str) -> dict[str, Any]:
    """Сохранённые настройки сервиса, приведённые к его полям."""
    tts = cfg.get("tts") if isinstance(cfg.get("tts"), dict) else {}
    return get_provider(provider_id).normalize(tts.get(provider_id))


__all__ = [
    "DEFAULT_PROVIDER",
    "FieldSpec",
    "NamedList",
    "PROVIDERS",
    "TtsProvider",
    "get_provider",
    "provider_options",
]
