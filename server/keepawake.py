"""Не давать Mac уснуть, пока в очереди есть работа.

macOS `caffeinate -i` держит систему бодрствующей (экран при этом может гаснуть).
`-w <pid>` — caffeinate сам завершится, если приложение упадёт или закроется.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading

from engine.core.logutil import log


class KeepAwake:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._bin = shutil.which("caffeinate") if sys.platform == "darwin" else None

    @property
    def active(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def hold(self) -> None:
        with self._lock:
            if self._bin is None or (self._proc is not None and self._proc.poll() is None):
                return
            try:
                self._proc = subprocess.Popen(
                    [self._bin, "-i", "-w", str(os.getpid())],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as exc:
                log(f"Не удалось запретить сон: {exc}")
                return
        log("Сон Mac отключён на время генерации")

    def release(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        log("Очередь пуста — Mac снова может засыпать")
