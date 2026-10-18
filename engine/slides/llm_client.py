"""OpenAI-совместимый chat (тот же подход, что story-rewriter / fishappedu)."""

from __future__ import annotations

import time

import httpx
from openai import APIConnectionError, APITimeoutError, Omit, OpenAI

from engine.core.cancel import CancelledError
from engine.core.logutil import log

DEFAULT_BASE_URL = "https://fishappedu.online/v1"
DEFAULT_MODEL = "gpt-5.6-terra"
DEFAULT_MODELS = [
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-6-luna",
    "gpt-6-sol",
    "gpt-6.1-sol",
    "gpt-6-astra",
]

_SAFE_HEADERS = {
    "User-Agent": "curl/8.7.1",
    "X-Stainless-Lang": Omit(),
    "X-Stainless-Package-Version": Omit(),
    "X-Stainless-OS": Omit(),
    "X-Stainless-Arch": Omit(),
    "X-Stainless-Runtime": Omit(),
    "X-Stainless-Runtime-Version": Omit(),
    "X-Stainless-Async": Omit(),
    "x-stainless-retry-count": Omit(),
    "x-stainless-read-timeout": Omit(),
}

_CHAT_ATTEMPTS = 6
_PING_TIMEOUT = httpx.Timeout(15.0, connect=8.0)
_CHAT_TIMEOUT = httpx.Timeout(600.0, connect=60.0)


def _exc_chain(exc: BaseException) -> str:
    parts: list[str] = [f"{type(exc).__name__}: {exc}"]
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        nxt = cur.__cause__ or cur.__context__
        if nxt is None or id(nxt) in seen:
            break
        parts.append(f"{type(nxt).__name__}: {nxt}")
        cur = nxt
    return " ← ".join(parts)


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (APIConnectionError, APITimeoutError, httpx.TransportError)):
        return True
    name = type(exc).__name__
    if "Timeout" in name or "Connection" in name:
        return True
    if "RateLimit" in name:
        return True
    status = getattr(exc, "status_code", None)
    if status in (408, 429, 500, 502, 503, 504):
        return True
    msg = str(exc).lower()
    return any(
        s in msg
        for s in (
            "connection",
            "timed out",
            "timeout",
            "reset by peer",
            "broken pipe",
            "temporarily unavailable",
            "ssl",
            "eof",
            "rate limit",
            "429",
            "too many requests",
        )
    )


def _retry_delay(exc: BaseException, attempt: int) -> float:
    """429 — дольше ждём, иначе короткий backoff."""
    msg = str(exc).lower()
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    if status == 429 or "RateLimit" in name or "rate limit" in msg or "429" in msg:
        return min(30 * attempt, 120)
    return min(2**attempt, 16)


def _models_unsupported(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if status in (404, 405):
        return True
    return type(exc).__name__ in ("NotFoundError", "NotFound")


def _model_rejected(exc: BaseException) -> bool:
    """Сервис больше не даёт эту модель (400/404 «model not supported / does not exist»)."""
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    return status in (400, 404) and "model" in text and any(
        k in text for k in ("not supported", "does not exist", "not found", "model_not_found", "unsupported")
    )


class LLMClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        url = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        self.base_url = url
        self.model = (model or DEFAULT_MODEL).strip()
        self._http = httpx.Client(timeout=_CHAT_TIMEOUT, trust_env=False)
        self.client = OpenAI(
            api_key=api_key.strip(),
            base_url=url,
            http_client=self._http,
            max_retries=1,
            default_headers=_SAFE_HEADERS,
        )
        log(f"LLM клиент: base_url={url} model={self.model}")

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass
        try:
            if not self._http.is_closed:
                self._http.close()
        except Exception:
            pass

    def ping(self) -> str:
        client = self.client.with_options(timeout=_PING_TIMEOUT, max_retries=0)
        t0 = time.monotonic()
        log(f"LLM PING → GET {self.base_url}/models")
        try:
            page = client.models.list()
            items = getattr(page, "data", None) or []
            ids = [
                str(getattr(item, "id", "") or "").strip()
                for item in items
                if str(getattr(item, "id", "") or "").strip()
            ]
            ms = int((time.monotonic() - t0) * 1000)
            has = self.model in ids if ids else False
            summary = (
                f"OK ({ms} ms). Моделей: {len(ids)}. "
                f"{'Есть' if has else 'Нет'} «{self.model}»."
            )
            log(f"LLM PING OK ({ms} ms): models={len(ids)}")
            return summary
        except Exception as models_exc:
            ms = int((time.monotonic() - t0) * 1000)
            if self._http.is_closed:
                raise CancelledError("Остановлено пользователем")
            if not _models_unsupported(models_exc):
                log(f"LLM PING FAIL ({ms} ms): {_exc_chain(models_exc)}")
                raise
            log(f"LLM PING /models недоступен → fallback chat ({ms} ms)")

        t0 = time.monotonic()
        resp = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": "Ответь одним словом: ок"}],
            max_tokens=5,
        )
        content = (resp.choices[0].message.content or "").strip()
        ms = int((time.monotonic() - t0) * 1000)
        log(f"LLM PING OK ({ms} ms): {content[:80]!r}")
        return content or "ok"

    def complete_vision(
        self,
        *,
        label: str,
        system: str,
        user_text: str,
        image_b64: str,
        mime: str = "image/jpeg",
        model: str | None = None,
    ) -> str:
        """Multimodal chat: text + одно изображение (base64)."""
        use_model = (model or self.model).strip() or self.model
        log(
            f"LLM VISION [{label}] → {self.base_url}/chat/completions "
            f"model={use_model} img≈{len(image_b64)}b64"
        )
        t0 = time.monotonic()
        last_exc: BaseException | None = None
        resp = None
        user_content = [
            {"type": "text", "text": user_text},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{image_b64}"},
            },
        ]
        for attempt in range(1, _CHAT_ATTEMPTS + 1):
            if self._http.is_closed:
                raise CancelledError("Остановлено пользователем")
            try:
                resp = self.client.chat.completions.create(
                    model=use_model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_content},
                    ],
                    max_tokens=200,
                )
                break
            except CancelledError:
                raise
            except Exception as exc:
                last_exc = exc
                ms = int((time.monotonic() - t0) * 1000)
                if self._http.is_closed:
                    raise CancelledError("Остановлено пользователем") from exc
                if _model_rejected(exc) and use_model != DEFAULT_MODEL:
                    # модель убрали у сервиса — не роняем ролик, берём рабочую по умолчанию
                    log(f"LLM: модель {use_model} больше недоступна у сервиса — переключаюсь на {DEFAULT_MODEL}")
                    use_model = self.model = DEFAULT_MODEL
                    continue
                if not _is_transient(exc) or attempt >= _CHAT_ATTEMPTS:
                    log(f"LLM VISION ERR [{label}] ({ms} ms): {_exc_chain(exc)}")
                    raise
                delay = _retry_delay(exc, attempt)
                log(
                    f"LLM VISION RETRY [{label}] {attempt}/{_CHAT_ATTEMPTS} "
                    f"({ms} ms); sleep {delay}s"
                )
                time.sleep(delay)
        else:
            assert last_exc is not None
            raise last_exc

        assert resp is not None
        content = resp.choices[0].message.content
        ms = int((time.monotonic() - t0) * 1000)
        if not content or not content.strip():
            log(f"LLM VISION ERR [{label}] ({ms} ms): пустой ответ")
            raise RuntimeError(f"Пустой vision-ответ {use_model}")
        log(f"LLM VISION OK [{label}] ({ms} ms) out_chars={len(content)}")
        return content.strip()

    def complete(self, *, label: str, system: str, user: str, model: str | None = None) -> str:
        use_model = (model or self.model).strip() or self.model
        chars_in = len(system) + len(user)
        log(
            f"LLM REQ [{label}] → {self.base_url}/chat/completions "
            f"model={use_model} chars≈{chars_in}"
        )
        t0 = time.monotonic()
        last_exc: BaseException | None = None
        resp = None

        for attempt in range(1, _CHAT_ATTEMPTS + 1):
            if self._http.is_closed:
                raise CancelledError("Остановлено пользователем")
            try:
                resp = self.client.chat.completions.create(
                    model=use_model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
                break
            except CancelledError:
                raise
            except Exception as exc:
                last_exc = exc
                ms = int((time.monotonic() - t0) * 1000)
                if self._http.is_closed:
                    raise CancelledError("Остановлено пользователем") from exc
                if _model_rejected(exc) and use_model != DEFAULT_MODEL:
                    # модель убрали у сервиса — не роняем ролик, берём рабочую по умолчанию
                    log(f"LLM: модель {use_model} больше недоступна у сервиса — переключаюсь на {DEFAULT_MODEL}")
                    use_model = self.model = DEFAULT_MODEL
                    continue
                if not _is_transient(exc) or attempt >= _CHAT_ATTEMPTS:
                    log(f"LLM ERR [{label}] ({ms} ms): {_exc_chain(exc)}")
                    raise
                delay = _retry_delay(exc, attempt)
                log(
                    f"LLM RETRY [{label}] {attempt}/{_CHAT_ATTEMPTS} "
                    f"({ms} ms); sleep {delay}s"
                )
                time.sleep(delay)
        else:
            assert last_exc is not None
            raise last_exc

        assert resp is not None
        content = resp.choices[0].message.content
        ms = int((time.monotonic() - t0) * 1000)
        if not content or not content.strip():
            log(f"LLM ERR [{label}] ({ms} ms): пустой ответ")
            raise RuntimeError(f"Пустой ответ модели {use_model}")
        log(f"LLM OK  [{label}] ({ms} ms) out_chars={len(content)}")
        return content.strip()
