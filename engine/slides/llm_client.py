"""OpenAI-совместимый chat (тот же подход, что story-rewriter / fishappedu)."""

from __future__ import annotations

import os
import re
import threading
import time

import httpx
from openai import APIConnectionError, APITimeoutError, Omit, OpenAI

from engine.core import usage
from engine.core.cancel import CancelledError
from engine.core.logutil import log

DEFAULT_BASE_URL = "https://dindindon.ru/v1"
# пустая модель — автоматически: самая дешёвая подходящая по прайсу сервиса (engine/slides/llm_models)
DEFAULT_MODEL = ""

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
# сервис перегружен («No upstream accounts…», 503): обычно это минуты — ждём до 20 минут, а не роняем ролик
_BUSY_WAIT_SEC = 20 * 60
_MAX_ATTEMPTS = 80
_PING_TIMEOUT = httpx.Timeout(15.0, connect=8.0)
_CHAT_TIMEOUT = httpx.Timeout(600.0, connect=15.0)  # сервис недоступен — узнаём за секунды, а не за минуту


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


def _overloaded(exc: BaseException) -> bool:
    """У сервиса LLM сейчас нет свободных мощностей — пройдёт само, нужно просто подождать."""
    text = str(exc).lower()
    return getattr(exc, "status_code", None) == 503 or "pool_empty" in text or "no upstream accounts" in text


def _busy_error(waited: float) -> RuntimeError:
    return RuntimeError(
        f"Сервис LLM перегружен: {max(1, round(waited / 60))} мин нет свободных мощностей. "
        "Это на стороне сервиса — попробуй позже кнопкой «Продолжить с места падения» (готовое не пропадёт)."
    )


def _models_unsupported(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if status in (404, 405):
        return True
    return type(exc).__name__ in ("NotFoundError", "NotFound")


def _quota_exceeded(exc: BaseException) -> bool:
    """Кончились деньги/квота токенов у ключа — ждать бесполезно, нужно пополнить."""
    text = _exc_chain(exc).lower()
    return any(k in text for k in ("insufficient_quota", "quota_exceeded", "token quota", "insufficient balance",
                                   "insufficient_balance", "error code: 402"))


class LLMQuotaError(RuntimeError):
    pass


class LLMUnavailable(RuntimeError):
    """Сервис LLM не отвечает: запрос с картинкой не делаем — кадры оцениваются локально (CLIP)."""


# ---------- сервис лёг: запоминаем на всю программу, текстовые запросы — в Claude ----------

OUTAGE_SEC = 10 * 60  # столько не стучимся в лежащий сервис; потом первый же запрос проверяет, ожил ли он
CONNECT_FAILS_DOWN = 2  # подряд не удалось даже подключиться — сервис лежит
CLAUDE_BUSY_WAIT = 3 * 60  # перегружен, а Claude есть — ждём недолго и идём к Claude
CLAUDE_TIMEOUT = 15 * 60
_outage_lock = threading.Lock()
_down_until = 0.0
_down_logged = False
_claude_slots = threading.Semaphore(2)
_rejected: set[tuple[str, str]] = set()  # (сервис, модель), которую сервис не даёт — до перезапуска  # одновременно не больше двух запросов к Claude Code


def service_down() -> bool:
    return time.monotonic() < _down_until


def _mark_down(why: str) -> None:
    global _down_until, _down_logged
    with _outage_lock:
        first = not service_down()
        _down_until = time.monotonic() + OUTAGE_SEC
        _down_logged = False
    if first:
        log(f"Сервис LLM недоступен ({why[:140]}) — {OUTAGE_SEC // 60} мин: тексты — через Claude, "
            "кадры — локальная оценка (CLIP)")


def _mark_up() -> None:
    global _down_until
    if _down_until:
        with _outage_lock:
            if _down_until:
                _down_until = 0.0
                log("Сервис LLM снова отвечает — запросы идут в него")


def _connect_failure(exc: BaseException) -> bool:
    """До сервиса не достучаться вообще (а не он медленно отвечает)."""
    text = _exc_chain(exc).lower()
    return any(k in text for k in ("connecttimeout", "connecterror", "handshake", "unexpected_eof",
                                   "connection refused", "nodename nor servname", "name or service not known"))


# сразу встроенная нейросеть, без подписок:
# - массовые задачи (оценка кадров — сотни запросов с картинкой на ролик, запросы поиска на каждый кусок):
#   через Claude/Codex каждый — отдельный запуск на 10–30 с, подбор растянулся бы на часы и съел лимит;
# - части рерайта встроенной нейросетью (rewrite_*): это её собственная схема — подписки рерайт уже пробовали;
# - главы (chapters*): ~60 тыс. знаков расшифровки ради ответа на 400 токенов — дешёвая встроенная модель делает их
#   не хуже, а у Claude это съедало ~20% лимита на ролик (журнал 14.11). Сервис лёг — главы всё равно уйдут подписке
_BUILTIN_ONLY = re.compile(r"^(judge|beat_queries|rewrite_|chapters)")


def builtin_only(label: str) -> bool:
    return bool(_BUILTIN_ONLY.match(label or ""))


def _report_clip(label: str) -> None:
    from engine.core import switches

    switches.report(f"запрос «{label}»", "llm", "CLIP (локально, без нейросети)", "сервис встроенной нейросети недоступен")


def claude_fallback_ok() -> bool:
    """Claude Code или Codex установлены — текстовые запросы можно отдать подписке (тесты — никогда)."""
    if os.environ.get("STORY_VIDEO_LLM_OFFLINE"):
        return False
    from engine.rewrite.engines import claude_binary, codex_binary

    return claude_binary() is not None or codex_binary() is not None


def _param_unsupported(exc: BaseException, name: str) -> bool:
    text = str(exc).lower()
    return getattr(exc, "status_code", None) == 400 and name in text and any(
        k in text for k in ("unsupported", "not supported", "unrecognized", "unknown", "invalid", "not allowed"))


class _Streamed:
    """Ответ, собранный из потока, — в том же виде, что обычный (choices[0].message.content, usage)."""

    def __init__(self, content: str, usage) -> None:
        from types import SimpleNamespace

        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content))]
        self.usage = usage


def _usage_line(resp, label: str = "") -> str:
    """Реальный расход токенов из ответа сервиса: вход (из них кеш), ответ (из них скрытые размышления).
    Заодно сообщает его в учёт задачи (engine.core.usage)."""
    u = getattr(resp, "usage", None)
    if u is None:
        return ""
    cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", None) or 0
    think = getattr(getattr(u, "completion_tokens_details", None), "reasoning_tokens", None) or 0
    if label:
        usage.record(label, u.prompt_tokens, cached, u.completion_tokens)
    return (f" tokens in={u.prompt_tokens}" + (f" (кеш {cached})" if cached else "")
            + f" out={u.completion_tokens}" + (f" (размышл. {think})" if think else ""))


def _image_unsupported(exc: BaseException) -> bool:
    text = str(exc).lower()
    return getattr(exc, "status_code", None) == 400 and "image" in text and ("not supported" in text or "unsupported" in text)


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
        self.api_key = api_key.strip()
        # своя модель из настроек — она первая; пусто — по цене (см. llm_models)
        self.model = (model or DEFAULT_MODEL).strip()
        # сколько ждать, если сервис перегружен; необязательным шагам (главы) — меньше
        self.busy_wait: float = _BUSY_WAIT_SEC
        self._reasoning_ok = True
        # ответ потоком: длинная генерация (рерайт, разбор — 5+ минут) иначе обрывается прокси сервиса
        # (Cloudflare 524 «нет ответа 120 с») и уходит в повторы; поток идёт кусками — соединение живое
        self._stream_ok = True
        self._claude_jobs: list = []  # запросы к Claude Code сейчас: «Стоп» (close) их обрывает
        self._http = httpx.Client(timeout=_CHAT_TIMEOUT, trust_env=False)
        self.client = OpenAI(
            api_key=api_key.strip(),
            base_url=url,
            http_client=self._http,
            max_retries=1,
            default_headers=_SAFE_HEADERS,
        )
        log(f"LLM клиент: base_url={url} model={self.model or 'авто (по цене)'}")

    def close(self) -> None:
        for token in list(self._claude_jobs):
            token.cancel()
        try:
            self.client.close()
        except Exception:
            pass
        try:
            if not self._http.is_closed:
                self._http.close()
        except Exception:
            pass

    def _sleep(self, sec: float) -> None:
        """Пауза между попытками; «Стоп» (закрытый клиент) прерывает её сразу."""
        end = time.monotonic() + sec
        while time.monotonic() < end:
            if self._http.is_closed:
                raise CancelledError("Остановлено пользователем")
            time.sleep(min(1.0, max(0.0, end - time.monotonic())))

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
            if self.model:
                has = self.model in ids if ids else False
                summary = f"OK ({ms} ms). Моделей: {len(ids)}. {'Есть' if has else 'Нет'} «{self.model}»."
            else:
                from engine.slides import llm_models

                light = llm_models.chain(self.base_url, self.api_key, "light")
                smart = llm_models.chain(self.base_url, self.api_key, "smart")
                summary = (f"OK ({ms} ms). Моделей: {len(ids)}. Авто по цене: простые задачи — «{light[0]}», "
                           f"точные — «{smart[0]}»; запасные — дальше по цене.")
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
            model=self.model or self.chain("check")[0],
            messages=[{"role": "user", "content": "Ответь одним словом: ок"}],
            max_tokens=5,
        )
        content = (resp.choices[0].message.content or "").strip()
        ms = int((time.monotonic() - t0) * 1000)
        log(f"LLM PING OK ({ms} ms): {content[:80]!r}")
        return content or "ok"

    def chain(self, label: str, model: str | None = None, *, vision: bool = False) -> list[str]:
        """Модели для запроса по порядку: явная (аргумент или настройки), потом по цене для этой задачи."""
        from engine.slides import llm_models

        tier = llm_models.vision_tier(label) if vision else llm_models.tier_of(label)
        auto = llm_models.chain(self.base_url, self.api_key, tier)
        first = (model or self.model or "").strip()
        models = list(dict.fromkeys([first, *auto] if first else auto)) or ["gpt-6.1-sol"]
        # сервис уже отказал в модели — следующие запросы сразу идут на рабочую
        alive = [m for m in models if (self.base_url, m) not in _rejected]
        return alive or models

    def complete_vision(
        self,
        *,
        label: str,
        system: str,
        user_text: str,
        image_b64: str,
        mime: str = "image/jpeg",
        model: str | None = None,
        subs: bool = True,
    ) -> str:
        """Multimodal chat: text + одно изображение (base64). Сначала — подписки (Claude → Codex), см. _via_subs."""
        if subs:
            import base64 as _b64

            ext = (mime.split("/")[-1] or "jpg").replace("jpeg", "jpg")
            got = self._via_subs(label, system, user_text, images=[(_b64.b64decode(image_b64), ext)])
            if got is not None:
                return got
        user_content = [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
        ]
        log(f"LLM VISION [{label}] → {self.base_url}/chat/completions img≈{len(image_b64)}b64")
        return self._chat(label, system, user_content, model=model, vision=True)

    def complete(self, *, label: str, system: str, user: str, model: str | None = None, subs: bool = True) -> str:
        """Текстовый запрос. Порядок для всех задач программы: Claude → Codex → встроенная нейросеть
        (массовые и внутренние задачи — сразу встроенная, см. builtin_only). subs=False — только встроенная."""
        if subs:
            got = self._via_subs(label, system, user)
            if got is not None:
                return got
        log(f"LLM REQ [{label}] → {self.base_url}/chat/completions chars≈{len(system) + len(user)}")
        return self._chat(label, system, user, model=model, vision=False)

    def _via_subs(self, label: str, system: str, user: str,
                  images: list[tuple[bytes, str]] | None = None) -> str | None:
        """Подписки по приоритету: Claude, потом Codex (только с выполненным входом и без исчерпанного лимита).
        → ответ; None — пусть отвечает встроенная (о смене — уведомление). Сбой подписки задачу не роняет."""
        if builtin_only(label):
            return None
        from engine.core import switches
        from engine.core.cancel import CancelToken
        from engine.core.config import load_config
        from engine.rewrite.engines import RewriteError, note_limit, run_sub, sub_available

        tools = [e for e in switches.SUB_ORDER if sub_available(e)]
        if not tools:
            return None
        prompt = (f"{system}\n\n=====\n\n{user}\n\n=====\nОтветь строго в том формате, который требуется выше, "
                  "без пояснений и без markdown-обёртки.")
        cfg = load_config()
        task = f"запрос «{label}»"
        prev, why = "", ""
        for engine in tools:
            if self._http.is_closed:
                raise CancelledError("Остановлено пользователем")
            if prev:
                switches.report(task, prev, engine, why)
            token = CancelToken()
            self._claude_jobs.append(token)
            t0 = time.monotonic()
            log(f"{switches.name(engine)} [{label}] (chars≈{len(prompt)}{', картинка' if images else ''})")
            try:
                with _claude_slots:
                    text = run_sub(engine, prompt, model=str(cfg.get("codex_model" if engine == "codex" else "claude_model") or ""),
                                   timeout=CLAUDE_TIMEOUT, cancel=token, images=images)
            except RewriteError as exc:
                if self._http.is_closed:
                    raise CancelledError("Остановлено пользователем") from exc
                note_limit(engine, exc)
                prev, why = engine, str(exc).splitlines()[0][:160]
                log(f"{switches.name(engine)} [{label}] не смог: {why}")
                continue
            finally:
                self._claude_jobs.remove(token)
            if text.strip():
                log(f"{switches.name(engine)} OK [{label}] ({int((time.monotonic() - t0) * 1000)} ms) out_chars={len(text)}")
                return text.strip()
            prev, why = engine, "пустой ответ"
        if prev:
            switches.report(task, prev, "llm", why)
        return None

    def _chat(self, label: str, system: str, user, *, model: str | None, vision: bool) -> str:
        """Запрос с переходом по моделям: модель недоступна / не понимает картинки — следующая по цене;
        сервис перегружен — пробуем другие модели по кругу, между кругами ждём (не дольше busy_wait)."""
        from engine.slides import llm_models

        tag = "LLM VISION" if vision else "LLM"
        if service_down():
            if vision:
                _report_clip(label)
                raise LLMUnavailable("Сервис LLM недоступен — кадры оцениваю локально (CLIP)")
            if claude_fallback_ok():
                return self._claude(label, system, user)
        claude = not vision and claude_fallback_ok()
        busy_wait = min(self.busy_wait, CLAUDE_BUSY_WAIT) if claude else self.busy_wait
        connect_fails = 0
        models = self.chain(label, model, vision=vision)
        # простым задачам глубокие размышления не нужны: ответ (в 5 раз дороже входа) короче, качество то же
        extra: dict = {}
        if llm_models.reasoning_for(label, vision=vision) and self._reasoning_ok:
            extra["reasoning_effort"] = llm_models.reasoning_for(label, vision=vision)
        idx = 0
        use_model = models[0]
        t0 = time.monotonic()
        last_exc: BaseException | None = None
        resp = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            if self._http.is_closed:
                raise CancelledError("Остановлено пользователем")
            try:
                resp = self._create(use_model, [{"role": "system", "content": system},
                                                 {"role": "user", "content": user}], extra)
                break
            except CancelledError:
                raise
            except Exception as exc:
                last_exc = exc
                ms = int((time.monotonic() - t0) * 1000)
                if self._http.is_closed:
                    raise CancelledError("Остановлено пользователем") from exc
                if self._stream_ok and (_param_unsupported(exc, "stream") or _param_unsupported(exc, "stream_options")):
                    log(f"{tag} [{label}]: потоковый ответ не поддерживается — обычный запрос")
                    self._stream_ok = False
                    continue
                if extra and _param_unsupported(exc, "reasoning"):
                    # сервис/модель не знает этот параметр — без него (и дальше в этом клиенте тоже)
                    log(f"{tag} [{label}]: reasoning_effort не поддерживается — запрос без него")
                    extra.clear()
                    self._reasoning_ok = False
                    continue
                if (_model_rejected(exc) or (vision and _image_unsupported(exc))) and len(models) > 1:
                    # модели нет у сервиса / не понимает картинки — дальше её не пробуем
                    why = "не понимает картинки" if vision and _image_unsupported(exc) else "недоступна у сервиса"
                    models.remove(use_model)
                    _rejected.add((self.base_url, use_model))
                    idx = idx % len(models)
                    log(f"{tag} [{label}]: модель {use_model} {why} — беру {models[idx]}")
                    from engine.core import switches

                    switches.report(f"запрос «{label}»", use_model, models[idx], f"модель {why}")
                    use_model = models[idx]
                    continue
                busy = _overloaded(exc)
                connect_fails = connect_fails + 1 if _connect_failure(exc) else 0
                if connect_fails >= CONNECT_FAILS_DOWN:
                    log(f"{tag} ERR [{label}] ({ms} ms): {_exc_chain(exc)}")
                    _mark_down(str(exc))
                    if vision:
                        _report_clip(label)
                        raise LLMUnavailable("Сервис LLM недоступен — кадры оцениваю локально (CLIP)") from exc
                    if claude:
                        return self._claude(label, system, user, original=exc)
                    raise
                if not _is_transient(exc) or (busy and ms / 1000 >= busy_wait) or (not busy and attempt >= _CHAT_ATTEMPTS):
                    log(f"{tag} ERR [{label}] ({ms} ms): {_exc_chain(exc)}")
                    if claude and (_is_transient(exc) or busy or _quota_exceeded(exc)):
                        return self._claude(label, system, user, original=exc)
                    if _quota_exceeded(exc):
                        host = self.base_url.split("//")[-1].split("/")[0]
                        raise LLMQuotaError(
                            f"У ключа LLM закончилась квота или деньги ({host}). Пополни баланс или подними лимит "
                            "у сервиса (или поставь другой ключ в Настройки → LLM), потом нажми «Продолжить» — "
                            "ролик продолжится с того же места."
                        ) from exc
                    if busy:
                        raise _busy_error(busy_wait) from exc
                    raise
                if busy and len(models) > 1:
                    # перегружена — сразу следующая по цене; прошли круг — пауза и снова с самой дешёвой
                    idx += 1
                    if idx < len(models):
                        use_model = models[idx]
                        log(f"{tag} [{label}]: {models[idx - 1]} перегружена — пробую {use_model}")
                        from engine.core import switches

                        switches.report(f"запрос «{label}»", models[idx - 1], use_model, "модель перегружена")
                        continue
                    idx, use_model = 0, models[0]
                if busy:
                    delay = 60 if attempt > 8 else 30
                    log(f"{tag} RETRY [{label}]: сервис LLM перегружен — жду {delay} с (жду не дольше {busy_wait // 60:.0f} мин)")
                else:
                    delay = _retry_delay(exc, attempt)
                    log(f"{tag} RETRY [{label}] {attempt}/{_CHAT_ATTEMPTS} ({ms} ms); sleep {delay}s")
                self._sleep(delay)
        else:
            assert last_exc is not None
            raise last_exc

        assert resp is not None
        content = resp.choices[0].message.content
        ms = int((time.monotonic() - t0) * 1000)
        if not content or not content.strip():
            log(f"{tag} ERR [{label}] ({ms} ms): пустой ответ")
            raise RuntimeError(f"Пустой ответ модели {use_model}")
        log(f"{tag} OK  [{label}] ({ms} ms) model={use_model} out_chars={len(content)}{_usage_line(resp, label)}")
        _mark_up()
        return content.strip()

    def _create(self, model: str, messages: list, extra: dict):
        """Один запрос к сервису. Потоком (если сервис умеет) — собираем ответ из кусков; → объект с
        .choices[0].message.content и .usage, как у обычного ответа."""
        if not self._stream_ok:
            return self.client.chat.completions.create(model=model, messages=messages, **extra)
        from openai import Stream

        # сервис не принимает потоковый режим (400 про stream) — _chat переключит клиент на обычные запросы
        stream = self.client.chat.completions.create(model=model, messages=messages, stream=True,
                                                     stream_options={"include_usage": True}, **extra)
        if not isinstance(stream, Stream):
            return stream  # уже целый ответ
        if "event-stream" not in stream.response.headers.get("content-type", "event-stream"):
            # сервис поток не включил и ответил обычным JSON — разбираем как обычный ответ
            from openai.types.chat import ChatCompletion

            stream.response.read()
            return ChatCompletion.model_validate(stream.response.json())
        parts: list[str] = []
        usage = None
        try:
            for chunk in stream:
                if self._http.is_closed:
                    raise CancelledError("Остановлено пользователем")
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                for choice in chunk.choices or []:
                    piece = getattr(choice.delta, "content", None) if choice.delta else None
                    if piece:
                        parts.append(piece)
        finally:
            try:
                stream.close()
            except Exception:
                pass
        if not parts and usage is None:
            # из потока ничего не пришло — сервис потоковый режим не поддерживает (ответил обычным JSON):
            # дальше в этом клиенте — обычные запросы
            log("LLM: потоковый ответ пуст — сервис его не поддерживает, перехожу на обычные запросы")
            self._stream_ok = False
            return self.client.chat.completions.create(model=model, messages=messages, **extra)
        return _Streamed("".join(parts), usage)

    def _claude(self, label: str, system: str, user: str, *, original: BaseException | None = None) -> str:
        """Тот же текстовый запрос — в подписку: Claude, не вышло — Codex (ChatGPT). Каждая смена — уведомление.
        Ни одна не помогла — исходная ошибка сервиса LLM."""
        from engine.core import switches
        from engine.core.cancel import CancelToken
        from engine.core.config import load_config
        from engine.rewrite.engines import claude_binary, codex_binary, run_sub

        prompt = (f"{system}\n\n=====\n\n{user}\n\n=====\nОтветь строго в том формате, который требуется выше, "
                  "без пояснений и без markdown-обёртки.")
        cfg = load_config()
        task = f"запрос «{label}»"
        why = (f"сервис встроенной нейросети: {str(original).splitlines()[0][:120]}" if original is not None
               else "сервис встроенной нейросети недоступен")
        from engine.rewrite.engines import sub_available

        # с выполненным входом и без исчерпанного лимита; нет таких — хоть установленные (скажут, что не так)
        tools = ([e for e in switches.SUB_ORDER if sub_available(e)]
                 or [e for e in switches.SUB_ORDER
                     if (claude_binary() if e == "claude_code" else codex_binary()) is not None] or ["claude_code"])
        prev, last_exc = "llm", None
        for engine in tools:
            switches.report(task, prev, engine, why)
            token = CancelToken()
            self._claude_jobs.append(token)
            t0 = time.monotonic()
            log(f"LLM → {switches.name(engine)} [{label}] (chars≈{len(prompt)})")
            try:
                with _claude_slots:
                    if self._http.is_closed:
                        raise CancelledError("Остановлено пользователем")
                    model = str(cfg.get("codex_model" if engine == "codex" else "claude_model") or "")
                    text = run_sub(engine, prompt, model=model, timeout=CLAUDE_TIMEOUT, cancel=token)
            except CancelledError:
                raise
            except Exception as exc:
                if self._http.is_closed:
                    raise CancelledError("Остановлено пользователем") from exc
                log(f"LLM → {switches.name(engine)} [{label}] не вышло: {exc}")
                prev, why, last_exc = engine, str(exc).splitlines()[0][:160], exc
                continue
            finally:
                self._claude_jobs.remove(token)
            log(f"LLM → {switches.name(engine)} OK [{label}] ({int((time.monotonic() - t0) * 1000)} ms) out_chars={len(text)}")
            return text.strip()
        exc = last_exc or RuntimeError("нет Claude Code и Codex")
        if original is not None and _quota_exceeded(original):
            raise LLMQuotaError(f"У ключа LLM закончилась квота или деньги, а подписки не помогли ({exc}). Пополни "
                                "баланс и нажми «Продолжить».") from exc
        if original is not None:
            raise original from exc
        raise LLMUnavailable(f"Сервис LLM недоступен, подписки (Claude, Codex) тоже не ответили: {exc}") from exc
