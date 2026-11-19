"""Импортировать первым в каждом тесте: лог тестов не попадает в data/app.log."""

import os
import tempfile

# выбранная в настройках папка роликов не для тестов: пишем только туда, куда подменили Desktop
os.environ["STORY_VIDEO_NO_OUTPUT_DIR"] = "1"
# прайс моделей LLM — встроенный: тесты не ходят в сеть и не пишут data/llm_pricing.json
os.environ["STORY_VIDEO_LLM_OFFLINE"] = "1"
# главы оригиналов (для переводов роликов) — во временную папку, не в data/chapters
os.environ["STORY_VIDEO_CHAPTERS_DIR"] = tempfile.mkdtemp(prefix="story-video-chapters-")
# учёт выкладки (обложки, даты, каналы) — во временную папку, не в data/publish
os.environ["STORY_VIDEO_PUBLISH_DIR"] = tempfile.mkdtemp(prefix="story-video-publish-")
# память проверки фото героев между роликами — во временную папку, не в data/cache
os.environ["STORY_VIDEO_JUDGE_CACHE_DIR"] = tempfile.mkdtemp(prefix="story-video-judgecache-")
# распознавание надписей macOS не собирается в data/bin (тесты, где оно нужно, подменяют чтение)
os.environ["STORY_VIDEO_OCR_OFF"] = "1"
# баланс RoyalTechno при постановке задач — без запроса в сеть
os.environ["STORY_VIDEO_ROYAL_OFFLINE"] = "1"
# уведомления macOS о конце очереди — не в тестах
os.environ["STORY_VIDEO_NO_NOTIFY"] = "1"
os.environ["STORY_VIDEO_NO_BACKGROUND"] = "1"  # фоновые проверки (соавторства) в тестах не стартуют
# Claude Code и Codex: тесты не запускают настоящие CLI (лимиты подписок, минуты ожидания)
os.environ["STORY_VIDEO_SUBS_OFF"] = "1"
# модели Whisper / CLIP — только из локального кэша HuggingFace, без проверки обновлений в сети
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ.setdefault("STORY_VIDEO_LOG", os.path.join(tempfile.gettempdir(), "story-video-tests.log"))


# ── сеть: тесты не ходят на настоящие хосты ─────────────────────────────────
# Любой httpx / requests / openai / urllib в итоге резолвит имя (getaddrinfo) или
# соединяет сокет. Здесь пускаем только локальные адреса (127.*, ::1, localhost,
# unix-сокеты); остальное — сразу NetworkBlocked с именем хоста. Подмены
# (httpx.MockTransport, mock.patch клиентов) сокетов не трогают и работают как раньше.
# Код с «except Exception» может проглотить ошибку, поэтому каждая попытка ещё и
# записывается, а тест, во время которого она была, помечается упавшим.
# Если когда-нибудь понадобится живой тест: STORY_VIDEO_TEST_ALLOW_NET=1.

import ipaddress as _ipaddress
import socket as _socket
import threading as _threading
import unittest as _unittest


class NetworkBlocked(RuntimeError):
    """Тест полез в настоящую сеть. Подмените клиент (mock / httpx.MockTransport)."""


_blocked: list[str] = []
_blocked_lock = _threading.Lock()


def _is_local(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("idna", "replace")
    h = str(host).strip("[]").lower()
    if h in ("", "localhost", "localhost.localdomain") or h.endswith(".localhost"):
        return True
    try:
        ip = _ipaddress.ip_address(h.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


def _deny(host, port) -> None:
    msg = (
        f"Тест полез в сеть: {host}:{port}. Тестам нельзя ходить на настоящие хосты "
        f"(платные сервисы, баланс, минуты ожидания) — подмените клиент через mock "
        f"или httpx.MockTransport."
    )
    with _blocked_lock:
        _blocked.append(f"{host}:{port}")
    raise NetworkBlocked(msg)


if os.environ.get("STORY_VIDEO_TEST_ALLOW_NET") != "1" and not getattr(_socket, "_story_video_guard", False):
    _socket._story_video_guard = True
    _real_getaddrinfo = _socket.getaddrinfo
    _real_connect = _socket.socket.connect
    _real_connect_ex = _socket.socket.connect_ex

    def _guard_getaddrinfo(host, port, *a, **kw):
        if not _is_local(host):
            _deny(host, port)
        return _real_getaddrinfo(host, port, *a, **kw)

    def _check_addr(sock, address) -> None:
        if sock.family in (_socket.AF_INET, _socket.AF_INET6) and isinstance(address, tuple):
            if not _is_local(address[0]):
                _deny(address[0], address[1] if len(address) > 1 else "")

    def _guard_connect(self, address):
        _check_addr(self, address)
        return _real_connect(self, address)

    def _guard_connect_ex(self, address):
        _check_addr(self, address)
        return _real_connect_ex(self, address)

    _socket.getaddrinfo = _guard_getaddrinfo
    _socket.socket.connect = _guard_connect
    _socket.socket.connect_ex = _guard_connect_ex

    _real_run = _unittest.TestCase.run

    def _run_with_net_check(self, result=None):
        with _blocked_lock:
            _blocked.clear()
        before = len(result.errors) + len(result.failures) if result is not None else 0
        out = _real_run(self, result)
        with _blocked_lock:
            hits = list(dict.fromkeys(_blocked))
            _blocked.clear()
        res = result if result is not None else out
        if hits and res is not None and len(res.errors) + len(res.failures) == before:
            try:
                raise NetworkBlocked(
                    "Тест пытался выйти в сеть (ошибку, похоже, проглотил код): " + ", ".join(hits)
                    + ". Подмените клиент через mock или httpx.MockTransport."
                )
            except NetworkBlocked:
                import sys as _sys
                res.addFailure(self, _sys.exc_info())
        return out

    _unittest.TestCase.run = _run_with_net_check
