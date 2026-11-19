"""Копии готового ролика со сдвигом скорости: 99, 101, 98, 102, …"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from engine.core.cancel import CancelToken, CancelledError
from engine.core.logutil import log
from engine.slides.ffmpeg_util import find_ffmpeg, low_priority
from engine.slides.ffprobe_util import disk_full_hint, has_audio_stream, probe_duration, probe_video_size
from engine.slides.render_defaults import AUDIO_BITRATE, VIDEO_CRF, rate_cap

BASE_NAME = "ролик.mp4"
ProgressCb = Callable[[float, str], None]

_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def copy_percents(count: int) -> list[int]:
    """1 → [99], 2 → [99, 101], 3 → [99, 101, 98], 4 → [99, 101, 98, 102]."""
    out: list[int] = []
    n = max(0, int(count))
    for i in range(1, n + 1):
        step = (i + 1) // 2
        out.append(100 - step if i % 2 else 100 + step)
    return out


def percent_phrase(pct: int) -> str:
    n = abs(int(pct)) % 100
    tail = n % 10
    if 11 <= n <= 14 or tail == 0 or tail >= 5:
        word = "процентов"
    elif tail == 1:
        word = "процент"
    else:
        word = "процента"
    return f"{int(pct)} {word}"


def copy_filename(pct: int, base: str = BASE_NAME) -> str:
    stem = Path(base).stem
    suffix = Path(base).suffix or ".mp4"
    return f"{percent_phrase(pct)} {stem}{suffix}"


def write_speed_copies(
    source: Path,
    folder: Path,
    count: int,
    *,
    base_name: str | None = None,
    cancel: CancelToken | None = None,
    on_progress: ProgressCb | None = None,
) -> list[Path]:
    """Оригинал не трогает. Пишет копии в ту же папку.

    base_name — имя файла в подписи копии. По умолчанию «ролик.mp4»,
    как в папке экспорта стоков. Для готового файла передавай его имя.
    """
    percents = copy_percents(count)
    if not percents or not source.is_file():
        return []
    folder.mkdir(parents=True, exist_ok=True)
    try:
        duration = probe_duration(source)
    except Exception:
        duration = 0.0
    audio = has_audio_stream(source)
    base = base_name or BASE_NAME
    written: list[Path] = []
    n = len(percents)
    log(
        f"Копии скорости: {n} шт. из {source.name} "
        f"({duration:.0f}с, audio={'да' if audio else 'нет'})"
    )
    if on_progress:
        on_progress(0.0, f"Копии: старт ({n} шт., {duration:.0f}с)…")

    for i, pct in enumerate(percents):
        if cancel:
            cancel.check()
        dest = folder / copy_filename(pct, base)
        phrase = percent_phrase(pct)
        lo = i / n
        hi = (i + 1) / n
        log(f"Копия {phrase}: кодирую → {dest.name}")
        if on_progress:
            on_progress(lo, f"Копия {phrase} ({i + 1}/{n})…")

        def _pct(local: float, msg: str, *, _lo=lo, _hi=hi) -> None:
            if on_progress:
                on_progress(_lo + (_hi - _lo) * max(0.0, min(1.0, local)), msg)

        _write_one(
            source,
            dest,
            pct,
            audio=audio,
            duration=duration,
            cancel=cancel,
            on_progress=_pct,
        )
        written.append(dest)
        log(f"Копия {phrase}: готово ({dest.stat().st_size} bytes)")
    if on_progress:
        on_progress(1.0, f"Копии готовы: {n}")
    return written


def _write_one(
    source: Path,
    dest: Path,
    pct: int,
    *,
    audio: bool,
    duration: float,
    cancel: CancelToken | None,
    on_progress: ProgressCb | None,
) -> None:
    rate = pct / 100.0
    rate_s = f"{rate:.4f}".rstrip("0").rstrip(".")
    # Выходная длительность ≈ duration / rate
    out_dur = duration / rate if duration > 0 else 0.0
    # Без fps=: форс 30fps после setpts на слайдшоу раздувает и ломает PTS.
    # Без VideoToolbox: на длинных роликах давал битый H.264 + CBR 8M (~×3 размер).
    video = f"[0:v]setpts=PTS/{rate_s},format=yuv420p[v]"
    if audio:
        graph = video + f";[0:a]atempo={rate_s}[a]"
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        graph = video
        maps = ["-map", "[v]", "-an"]
    label = f"speed {pct}"
    cmd = [
        find_ffmpeg(),
        "-y",
        "-hide_banner",
        "-i",
        str(source),
        "-filter_complex",
        graph,
        *maps,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(VIDEO_CRF),
        *rate_cap((probe_video_size(source) or (0, 1080))[1]),
        "-pix_fmt",
        "yuv420p",
    ]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", AUDIO_BITRATE]
    cmd += ["-movflags", "+faststart", str(dest)]
    try:
        _run_ffmpeg_progress(
            cmd,
            total=out_dur,
            label=label,
            cancel=cancel,
            on_progress=on_progress,
        )
    except Exception:
        _unlink_quiet(dest)
        raise


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _parse_time(s: str) -> float | None:
    m = _TIME_RE.search(s)
    if not m:
        return None
    h, mi, sec = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(sec)


def _run_ffmpeg_progress(
    cmd: list[str],
    *,
    total: float,
    label: str,
    cancel: CancelToken | None,
    on_progress: ProgressCb | None,
) -> None:
    # ffmpeg крутит прогресс через \r — line-buffered text mode молчит до конца.
    proc = subprocess.Popen(
        low_priority(cmd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    assert proc.stderr is not None
    err_lines: list[str] = []
    buf = b""
    last_log = 0.0
    last_pct = -1.0
    try:
        while True:
            chunk = proc.stderr.read(256)
            if not chunk:
                break
            if cancel and cancel.is_cancelled():
                proc.kill()
                raise CancelledError("Остановлено пользователем")
            buf += chunk
            while True:
                cut = -1
                for sep in (b"\r", b"\n"):
                    i = buf.find(sep)
                    if i >= 0 and (cut < 0 or i < cut):
                        cut = i
                if cut < 0:
                    break
                line = buf[:cut].decode("utf-8", errors="replace")
                buf = buf[cut + 1 :]
                if not line.strip():
                    continue
                err_lines.append(line)
                if len(err_lines) > 80:
                    err_lines = err_lines[-80:]
                t = _parse_time(line)
                if t is None:
                    continue
                pct = max(0.0, min(0.99, t / total)) if total > 0 else 0.0
                now = time.monotonic()
                if on_progress and pct - last_pct >= 0.02:
                    last_pct = pct
                    on_progress(pct, f"{label}: {pct * 100:.0f}%")
                if now - last_log >= 5.0:
                    last_log = now
                    log(f"{label}: {pct * 100:.0f}% ({t:.0f}/{total:.0f}с)")
        code = proc.wait()
    except CancelledError:
        proc.kill()
        proc.wait(timeout=5)
        raise
    if code != 0:
        tail = "\n".join(err_lines[-40:])
        hint = disk_full_hint(cmd, tail)
        if hint:
            raise RuntimeError(hint)
        raise RuntimeError(f"{label} failed ({code}):\n{tail}")
