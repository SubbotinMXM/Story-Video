"""Lumean: озвучка по шаблону (ElevenLabs внутри)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from engine.core.cancel import CancelToken
from engine.core.lumean import (
    DEFAULT_TEMPLATE_ID,
    DEFAULT_VOICE_ID,
    DEFAULT_VOICE_SPEED,
    VOICE_SPEED_MAX,
    VOICE_SPEED_MIN,
    LumeanClient,
    synthesize_resumable,
)
from engine.tts.base import FieldSpec, NamedList, ProgressCb, TtsProvider


class LumeanProvider(TtsProvider):
    id = "lumean"
    label = "Lumean"
    description = "Озвучка ElevenLabs через шаблоны Lumean"
    fields = (
        FieldSpec("api_key", "API key", "secret", placeholder="lum_…", required=True),
        FieldSpec(
            "voice",
            "Голос",
            "named_list",
            quick=True,
            item_label="голос",
            id_label="Voice ID",
            hint="Voice ID из ElevenLabs и понятное название. Читает выбранный голос.",
            fallback_id=DEFAULT_VOICE_ID,
        ),
        # Lumean требует шаблон в каждом заказе: из него берутся модель и тех. настройки,
        # голос и скорость задаём сами. Выбирать его не нужно — если сохранённый шаблон
        # пропал, resolve_template_id возьмёт первый живой шаблон аккаунта.
        FieldSpec(
            "template",
            "Шаблон",
            "named_list",
            hidden=True,
            fallback_id=DEFAULT_TEMPLATE_ID,
        ),
        FieldSpec(
            "speed",
            "Скорость речи",
            "slider",
            quick=True,
            hint="Меняет темп озвучки. Для модели eleven_v3 дотягивается после скачивания.",
            min=VOICE_SPEED_MIN,
            max=VOICE_SPEED_MAX,
            step=0.01,
            default=DEFAULT_VOICE_SPEED,
        ),
    )

    def voice_tag(self, opts: dict[str, Any]) -> str:
        return self.choice(opts, "voice")

    def synthesize(
        self,
        text: str,
        dest: Path,
        opts: dict[str, Any],
        *,
        state_file: Path,
        cancel: CancelToken | None = None,
        on_progress: ProgressCb | None = None,
    ) -> Path:
        self.check(opts)
        voices = NamedList.parse(opts.get("voice"))
        if on_progress and voices.items:
            on_progress(0.02, f"Озвучка Lumean: голос «{voices.selected_name}»")
        return synthesize_resumable(
            api_key=self.text(opts, "api_key"),
            text=text,
            template_id=self.choice(opts, "template"),
            voice_id=self.choice(opts, "voice"),
            speed=self.number(opts, "speed"),
            dest=dest,
            state_file=state_file,
            cancel=cancel,
            on_progress=on_progress,
        )

    def ping(self, opts: dict[str, Any]) -> str:
        key = self.text(opts, "api_key")
        if not key:
            raise ValueError("Вставь API key Lumean")
        client = LumeanClient(key)
        try:
            return client.ping()
        finally:
            client.close()
