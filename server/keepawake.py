"""Не давать Mac уснуть и не давать macOS притормаживать приложение, пока в очереди есть работа.

macOS `caffeinate -i` держит систему бодрствующей (экран при этом может гаснуть).
`-w <pid>` — caffeinate сам завершится, если приложение упадёт или закроется.

Но одного caffeinate мало: с погасшим экраном macOS включает приложению App Nap — процессору
даются крохи. По журналу: распознавание 59 минут аудио днём 6–8 мин, ночью с погасшим экраном —
7,5 часа, и вся ночная очередь вставала. Поэтому на время работы приложение объявляет системе
«занят работой, начатой пользователем» (NSProcessInfo.beginActivity) — App Nap не включается.
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
        self._activity = None  # токен NSProcessInfo: пока есть — App Nap выключен

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
            self._begin_activity()
        log("Сон Mac отключён на время генерации (и притормаживание с погасшим экраном — тоже)")

    def _begin_activity(self) -> None:
        if self._activity is not None or sys.platform != "darwin":
            return
        try:
            from Foundation import NSProcessInfo

            # NSActivityUserInitiated (0x00FFFFFF): без App Nap и без сна по простою
            self._activity = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
                0x00FFFFFF, "Story Video: генерация роликов"
            )
        except Exception as exc:  # без pyobjc — остаётся caffeinate
            log(f"Не удалось выключить App Nap: {exc}")

    def _end_activity(self) -> None:
        token, self._activity = self._activity, None
        if token is None:
            return
        try:
            from Foundation import NSProcessInfo

            NSProcessInfo.processInfo().endActivity_(token)
        except Exception:
            pass

    def release(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            self._end_activity()
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        log("Очередь пуста — Mac снова может засыпать")
