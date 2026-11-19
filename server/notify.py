"""Уведомление macOS, когда очередь доделала работу: не нужно следить за окном программы."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading

from engine.core.logutil import log

# в тестах и вне macOS уведомлений нет
_OFF_ENV = "STORY_VIDEO_NO_NOTIFY"
_SCRIPT = ("on run argv", "display notification (item 1 of argv) with title (item 2 of argv)", "end run")


def notify(title: str, text: str) -> None:
    """Показать уведомление (в фоне; сбой — не ошибка: уведомление не важнее самой работы).
    Текст передаётся аргументами скрипта, а не вставляется в него — кавычки и спецсимволы безопасны."""
    exe = shutil.which("osascript")
    if sys.platform != "darwin" or not exe or os.environ.get(_OFF_ENV):
        return

    def run() -> None:
        try:
            cmd = [exe]
            for line in _SCRIPT:
                cmd += ["-e", line]
            subprocess.run([*cmd, text[:240], title[:80]], capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"Уведомление не показано: {exc}")

    threading.Thread(target=run, name="notify", daemon=True).start()


def queue_message(done: int, failed: int, last_title: str) -> tuple[str, str]:
    """(заголовок, текст) по итогам пачки задач."""
    if failed and done:
        title = "Story Video: есть ошибки"
        text = f"Готово: {done}, с ошибкой: {failed}."
    elif failed:
        title = "Story Video: ошибка"
        text = f"Не получилось: {failed}." if failed > 1 else "Задача не получилась."
    else:
        title = "Story Video: готово"
        text = f"Готово задач: {done}." if done > 1 else "Задача готова."
    if last_title:
        text += f" Последняя: {last_title}"
    return title, text
