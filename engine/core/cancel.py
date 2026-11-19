"""Отмена длинных операций из UI."""

from __future__ import annotations

import contextlib
import contextvars
import subprocess
import threading
from typing import Protocol


class Closable(Protocol):
    def close(self) -> None: ...


class CancelledError(RuntimeError):
    """Операция остановлена пользователем."""


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._closables: list[Closable] = []

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise CancelledError("Остановлено пользователем")

    def unregister(self, closable: Closable) -> None:
        with self._lock:
            if closable in self._closables:
                self._closables.remove(closable)

    def register(self, closable: Closable) -> None:
        with self._lock:
            if self._event.is_set():
                try:
                    closable.close()
                except Exception:
                    pass
                raise CancelledError("Остановлено пользователем")
            self._closables.append(closable)

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            closables = list(self._closables)
            self._closables.clear()
        for c in closables:
            try:
                c.close()
            except Exception:
                pass


# Токен задачи, которая сейчас работает в этом потоке (его ставит очередь; пул потоков engine.core.threads
# переносит его в рабочие потоки). Нужен, чтобы «Стоп» обрывал внешние программы (ffmpeg) сразу,
# а не после того, как они доработают несколько минут.
current_cancel: contextvars.ContextVar[CancelToken | None] = contextvars.ContextVar("current_cancel", default=None)


class _Kill:
    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc

    def close(self) -> None:
        try:
            self.proc.kill()
        except OSError:
            pass


@contextlib.contextmanager
def watch_process(proc: subprocess.Popen, token: CancelToken | None = None):
    """Пока идёт внешняя программа, «Стоп» задачи её убивает. После выхода из блока: останов был —
    CancelledError (а не «ffmpeg failed» с обрывком вывода)."""
    token = token or current_cancel.get()
    if token is None:
        yield
        return
    kill = _Kill(proc)
    try:
        token.register(kill)  # уже остановлено — убьёт процесс и сразу бросит CancelledError
    except CancelledError:
        proc.wait()
        raise
    try:
        yield
    finally:
        token.unregister(kill)
    token.check()


def run_cancellable(cmd: list[str], *, text: bool = False, capture_output: bool = True,
                    timeout: float | None = None) -> subprocess.CompletedProcess:
    """subprocess.run, который «Стоп» задачи обрывает сразу. Результат и таймаут — как у subprocess.run."""
    kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE} if capture_output else {}
    with subprocess.Popen(cmd, text=text, **kwargs) as proc:
        try:
            with watch_process(proc):
                try:
                    out, err = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                    raise
        except CancelledError:
            proc.wait()
            raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
