"""Обход подмены DNS для отдельных доменов.

Часть доменов (TMDB) у провайдера/в сети отдаётся как 127.0.0.1 / ::1 — соединение «отказано», хотя сам сервис
доступен. Для таких доменов, и только для них, адрес берём через DNS-over-HTTPS (dns.google, запасной —
cloudflare-dns.com), а остальное разрешается как всегда. Подмена ставится на `socket.getaddrinfo`, поэтому работает для
любых клиентов в программе (httpx, скачивание картинок). Системный ответ проверяется первым: если он нормальный
(не loopback) — используется он, DoH не нужен.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time

import httpx

from engine.core.logutil import log

HOSTS = ("themoviedb.org", "tmdb.org")  # домены с подменой (и их поддомены)
TTL = 600.0
_DOH = (("https://dns.google/resolve", {}), ("https://cloudflare-dns.com/dns-query", {"accept": "application/dns-json"}))

_orig = socket.getaddrinfo
_lock = threading.Lock()
_cache: dict[str, tuple[float, list[str]]] = {}
_installed = False


def _covered(host: str) -> bool:
    h = (host or "").lower().rstrip(".")
    return any(h == d or h.endswith("." + d) for d in HOSTS)


def _bad(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True
    return ip.is_loopback or ip.is_unspecified or ip.is_private or ip.is_link_local


def doh(host: str) -> list[str]:
    """Реальные IPv4-адреса домена через DNS-over-HTTPS (по адресам провайдеров, а не по имени)."""
    with _lock:
        hit = _cache.get(host)
        if hit and time.monotonic() - hit[0] < TTL:
            return hit[1]
    out: list[str] = []
    for url, headers in _DOH:
        try:
            r = httpx.get(url, params={"name": host, "type": "A"}, headers=headers, timeout=8.0, trust_env=False)
            out = [str(a["data"]) for a in r.json().get("Answer") or [] if a.get("type") == 1 and not _bad(str(a.get("data")))]
        except Exception:
            out = []
        if out:
            break
    if out:
        with _lock:
            _cache[host] = (time.monotonic(), out)
    return out


def _getaddrinfo(host, port, *args, **kwargs):
    if isinstance(host, str) and _covered(host):
        try:
            res = _orig(host, port, *args, **kwargs)
            if res and not all(_bad(r[4][0]) for r in res):
                return res
        except OSError:
            pass
        ips = doh(host)
        if ips:
            family = kwargs.get("family") or (args[0] if args else 0)
            type_ = kwargs.get("type") or (args[1] if len(args) > 1 else socket.SOCK_STREAM)
            return [(socket.AF_INET, type_ or socket.SOCK_STREAM, 6, "", (ip, port or 443)) for ip in ips]
    return _orig(host, port, *args, **kwargs)


def install() -> None:
    """Один раз на процесс."""
    global _installed
    if _installed:
        return
    socket.getaddrinfo = _getaddrinfo  # type: ignore[assignment]
    _installed = True
    log("DNS: для TMDB адрес берётся через DNS-over-HTTPS, если системный DNS его подменяет")
