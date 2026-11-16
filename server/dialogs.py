"""Нативные диалоги выбора файла/папки.

В окне приложения — через pywebview (sheet поверх окна), в браузере — через osascript.
"""

from __future__ import annotations

import subprocess
from typing import Any

AUDIO_EXTS = ["mp3", "wav", "m4a", "aac", "flac", "ogg"]
VIDEO_EXTS = ["mp4", "mov", "mkv", "webm", "m4v"]
TEXT_EXTS = ["txt", "md", "srt", "vtt"]

_FILTERS = {
    "audio": ("Аудио", AUDIO_EXTS),
    "video": ("Видео", VIDEO_EXTS),
    "text": ("Текст", TEXT_EXTS),
}

# выставляется desktop.py, когда окно создано
window: Any = None


def pick(kind: str, *, title: str = "", multiple: bool = False) -> list[str]:
    """kind: folder | audio | video | text | any. Пустой список — пользователь отменил."""
    if window is not None:
        return _pick_webview(kind, multiple=multiple)
    return _pick_osascript(kind, title=title, multiple=multiple)


def _pick_webview(kind: str, *, multiple: bool) -> list[str]:
    import webview

    if kind == "folder":
        res = window.create_file_dialog(webview.FileDialog.FOLDER)
    else:
        types: tuple[str, ...] = ()
        if kind in _FILTERS:
            label, exts = _FILTERS[kind]
            types = (f"{label} ({';'.join('*.' + e for e in exts)})", "Все файлы (*.*)")
        res = window.create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=multiple, file_types=types
        )
    if not res:
        return []
    return [str(p) for p in (res if isinstance(res, (list, tuple)) else [res])]


def _pick_osascript(kind: str, *, title: str, multiple: bool) -> list[str]:
    prompt = (title or "Выбери").replace('"', "'")
    if kind == "folder":
        script = f'POSIX path of (choose folder with prompt "{prompt}")'
    else:
        types = ""
        if kind in _FILTERS:
            exts = ", ".join(f'"{e}"' for e in _FILTERS[kind][1])
            types = f" of type {{{exts}}}"
        many = " with multiple selections allowed" if multiple else ""
        script = (
            f'set picked to (choose file with prompt "{prompt}"{types}{many})\n'
            "if class of picked is list then\n"
            '  set out to ""\n'
            "  repeat with f in picked\n"
            "    set out to out & POSIX path of f & linefeed\n"
            "  end repeat\n"
            "  return out\n"
            "end if\n"
            "return POSIX path of picked"
        )
    # внутри System Events диалог всплывает поверх всех окон
    script = 'tell application "System Events"\nactivate\n' + script + "\nend tell"
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def reveal(path: str) -> None:
    subprocess.run(["open", "-R", path], check=False)
