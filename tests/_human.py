"""Тестам с поддельной страницей (MagicMock) «человеческие» действия не нужны: клик — обычный click(),
набор — keyboard.type(). Сами human.* проверяются в test_human.py."""

from __future__ import annotations

from unittest import mock


def plain_human():
    from engine.publish import human

    patches = [
        mock.patch.object(human, "click", lambda page, loc, **k: loc.click(**k)),
        mock.patch.object(human, "type_text", lambda page, text, cancel=None: page.keyboard.type(text)),
        mock.patch.object(human, "clear_field", lambda page: None),
        mock.patch.object(human, "press", lambda page, key: page.keyboard.press(key)),
        mock.patch.object(human, "reach", lambda page, loc: None),
    ]
    for p in patches:
        p.start()
    return lambda: [p.stop() for p in patches]


def setUpModule():  # noqa: N802 — подключается в модулях тестов: from _human import setUpModule, tearDownModule
    global _stop
    _stop = plain_human()


def tearDownModule():  # noqa: N802
    _stop()
