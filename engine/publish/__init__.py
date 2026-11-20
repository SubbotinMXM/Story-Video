"""Публикация готовых роликов (YouTube через браузерные профили AdsPower)."""

from __future__ import annotations

import random
import time

# пауза перед каждым действием в браузере (клик, ввод, выбор файла, «Далее»): не мгновенно и не долго
PAUSE_MIN, PAUSE_MAX = 1.5, 5.5


def _beat() -> None:
    from engine.publish.scan import beat

    beat()


def pause(cancel=None) -> float:
    """Случайная пауза 1.5–5.5 с перед действием; «Стоп» прерывает её сразу. → сколько ждали."""
    sec = random.uniform(PAUSE_MIN, PAUSE_MAX)
    end = time.monotonic() + sec
    while time.monotonic() < end:
        _beat()
        if cancel is not None:
            cancel.check()
        time.sleep(min(0.25, max(0.0, end - time.monotonic())))
    return sec
