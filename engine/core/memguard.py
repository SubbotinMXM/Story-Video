"""Запас оперативной памяти для самого Mac: программа никогда не занимает её целиком.

На любом Mac (8, 16, 32 ГБ…) часть памяти остаётся свободной — RESERVE_SHARE от объёма, но не меньше
RESERVE_MIN. Если свободной памяти меньше этого запаса (или macOS сама сообщает о нехватке):
  • второй тяжёлый ролик (сборка, подбор кадров) не начинается, пока не освободится память;
  • рендер запускает меньше ffmpeg одновременно (вплоть до одного) и ждёт между кусками;
  • модели (Whisper, CLIP) выгружаются из памяти, как только этап закончился.

Почему: 22.11.2026 подкачка забилась (13,7 из 14 ГБ), окно VPN Happ замерло на 10 с и Happ выключил
туннель, а следом перестал держать соединение Bluetooth (клавиатура и мышь).

Свободная память — по оценке самой macOS (kern.memorystatus_level, «System-wide memory free percentage»
из `memory_pressure`): она учитывает и сжатую память, и кэш файлов, который система отдаст сама.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

from engine.core.logutil import log

GB = 1 << 30
RESERVE_SHARE = 0.25  # четверть памяти всегда остаётся системе и другим программам
RESERVE_MIN = int(2.5 * GB)  # на 8 ГБ это почти треть — меньше macOS начинает тормозить
HEAVY_NEED = 3 * GB  # столько занимает один ролик в сборке (CLIP + ffmpeg + Python)
BROWSER_NEED = int(1.5 * GB)  # профиль AdsPower (окно браузера со Studio)
FFMPEG_NEED = 700 << 20  # один ffmpeg рендера (кадр 1080p с наездом камеры)
PRESSURE_WARN = 2  # kern.memorystatus_vm_pressure_level: 1 — норма, 2 — предупреждение, 4 — критично
PRESSURE_CRITICAL = 4

_cache: dict[str, tuple[float, int]] = {}
_lock = threading.Lock()


def enabled() -> bool:
    return not os.environ.get("STORY_VIDEO_NO_MEMGUARD")


def _sysctl(name: str, ttl: float = 2.0) -> int:
    """Число из sysctl (с кэшем на ttl секунд: проверяем часто, а вызов — отдельный процесс)."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(name)
        if hit and now - hit[0] < ttl:
            return hit[1]
    try:
        out = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=3).stdout.strip()
        val = int(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        val = -1
    with _lock:
        _cache[name] = (now, val)
    return val


def total() -> int:
    return max(0, _sysctl("hw.memsize", ttl=3600.0))


def reserve() -> int:
    """Сколько памяти программа не трогает никогда."""
    return max(RESERVE_MIN, int(total() * RESERVE_SHARE))


def available() -> int:
    """Сколько памяти macOS может отдать без подкачки (её собственная оценка). -1 — неизвестно."""
    level = _sysctl("kern.memorystatus_level")
    mem = total()
    if level < 0 or mem <= 0:
        return -1
    return int(mem * level / 100)


def pressure() -> int:
    return _sysctl("kern.memorystatus_vm_pressure_level")


def tight(need: int = 0) -> bool:
    """Начать работу, которой нужно ещё `need` байт, — значит залезть в запас Mac (или macOS уже просит память)."""
    if not enabled():
        return False
    p = pressure()
    if p >= PRESSURE_WARN:
        return True
    free = available()
    if free < 0:
        return False  # не смогли узнать — не мешаем работе
    return free - need < reserve()


def critical() -> bool:
    """Памяти совсем нет: macOS на грани — даже начатую работу притормаживаем."""
    if not enabled():
        return False
    free = available()
    return pressure() >= PRESSURE_CRITICAL or (0 <= free < reserve() // 2)


def workers(max_n: int, per_worker: int) -> int:
    """Сколько одинаковых процессов (по per_worker байт) можно запустить, не трогая запас: от 1 до max_n."""
    if not enabled() or max_n <= 1:
        return max(1, max_n)
    if pressure() >= PRESSURE_WARN:
        return 1
    free = available()
    if free < 0:
        return max_n
    return max(1, min(max_n, (free - reserve()) // max(1, per_worker)))


def describe() -> str:
    free = available()
    mem = total()
    if free < 0 or mem <= 0:
        return "память: нет данных"
    return f"свободно {free / GB:.1f} из {mem / GB:.0f} ГБ, запас для Mac {reserve() / GB:.1f} ГБ"


def wait_room(label: str, need: int = 0, *, cancel=None, max_wait: float = 600.0) -> None:
    """Подождать, пока памяти хватит на `need` сверх запаса. Не дольше max_wait: память может занимать
    не наша программа (браузер, другие приложения) — тогда работаем дальше, но по одному процессу."""
    if not tight(need):
        return
    log(f"{label}: жду, пока освободится память ({describe()})")
    t0 = time.monotonic()
    while tight(need) and time.monotonic() - t0 < max_wait:
        if cancel is not None:
            cancel.check()
        time.sleep(2.0)
    if tight(need):
        log(f"{label}: память так и не освободилась за {max_wait / 60:.0f} мин — продолжаю бережно ({describe()})")
