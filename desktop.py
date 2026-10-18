#!/usr/bin/env python3
"""Story Video: локальный сервер + окно приложения на весь экран.

  python desktop.py            окно приложения (pywebview)
  python desktop.py --browser  открыть в браузере
  python desktop.py --dev      сервер для `npm run dev` (порт 8765, без окна)
"""

from __future__ import annotations

import argparse
import os
import socket
import threading
import time
import webbrowser

import uvicorn

from engine.core.logutil import log

DEV_PORT = 8765


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(port: int) -> uvicorn.Server:
    from server.app import app
    from server.jobs import get_manager

    get_manager()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    threading.Thread(target=server.run, name="http", daemon=True).start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:
            raise SystemExit("Сервер не стартовал за 20 секунд")
        time.sleep(0.05)
    return server


def _mac_click_fix() -> None:
    """Кнопки срабатывают с первого клика.

    WKWebView по умолчанию не принимает «первый клик» по неактивному окну: macOS
    тратит его на активацию, и кнопка нажимается только со второго. А на macOS 14+
    приложение, запущенное из Терминала, часто вообще не становится активным
    (activateIgnoringOtherApps там игнорируется). Разрешаем первый клик.
    """
    import objc
    from webview.platforms.cocoa import BrowserView

    host = BrowserView.WebKitHost
    if getattr(host, "_story_first_mouse", False):
        return

    def acceptsFirstMouse_(self, event):  # noqa: N802 (имя метода Objective-C)
        return True

    objc.classAddMethods(host, [objc.selector(acceptsFirstMouse_, selector=b"acceptsFirstMouse:", signature=b"Z@:@")])
    host._story_first_mouse = True


def _mac_activate() -> None:
    """Сделать приложение активным (новый API macOS 14+, старый — для прежних версий)."""
    import AppKit
    from PyObjCTools import AppHelper

    def activate() -> None:
        app = AppKit.NSApplication.sharedApplication()
        if hasattr(app, "activate"):
            app.activate()
        else:
            app.activateIgnoringOtherApps_(True)

    AppHelper.callAfter(activate)


def _window(url: str) -> None:
    import webview

    from server import dialogs
    from server.jobs import get_manager

    _mac_click_fix()

    window = webview.create_window(
        "Story Video",
        url,
        width=1440,
        height=900,
        min_size=(1024, 700),
        maximized=True,
        text_select=True,
    )
    dialogs.window = window

    def on_closing() -> bool:
        if not get_manager().busy():
            return True
        return bool(
            window.create_confirmation_dialog(
                "Идёт работа",
                "В очереди есть задачи. Закрыть приложение и прервать их?",
            )
        )

    window.events.closing += on_closing
    window.events.shown += _mac_activate
    webview.start()


def _single_instance() -> object | None:
    """Блокировка data/app.lock: вторая копия приложения не запустится.

    Две копии одновременно перезаписывали бы друг другу настройки и очередь.
    """
    import fcntl

    from engine.core.config import DATA_DIR

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(DATA_DIR / "app.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def main() -> None:
    lock = _single_instance()
    if lock is None:
        print("Story Video уже запущен. Закрой открытое окно приложения и запусти снова.")
        raise SystemExit(1)
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", action="store_true", help="открыть в браузере вместо окна")
    parser.add_argument("--dev", action="store_true", help="только API на порту 8765")
    args = parser.parse_args()

    port = DEV_PORT if args.dev else _free_port()
    server = _serve(port)
    url = f"http://127.0.0.1:{port}/"
    log(f"Story Video: {url}")

    if args.dev:
        print("API для vite dev: npm run dev в папке web/. Ctrl+C — выход.")
        _wait_forever()
        return
    if not args.browser:
        try:
            _window(url)
            server.should_exit = True
            return
        except ImportError:
            log("pywebview не установлен — открываю в браузере")
    webbrowser.open(url)
    print(f"Открыто в браузере: {url}  (Ctrl+C — выход)")
    _wait_forever()


def _wait_forever() -> None:
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
