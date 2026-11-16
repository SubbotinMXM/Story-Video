"""VoiceGen: озвучка по шаблонам, созданным в Telegram-боте VoiceGen.

Голос, модель, скорость и громкость задаются в шаблоне в боте; приложение только
подтягивает список шаблонов (кнопка «Обновить») и отправляет текст с template_id.
Документация API: https://api.qw1voicegencore.pro/docs
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from collections.abc import Iterator
from typing import Any

import httpx

from engine.core.cancel import CancelToken
from engine.core.logutil import log
from engine.core.lumean import tts_wait_timeout_sec
from engine.tts.base import DubTrack, FieldSpec, NamedItem, NamedList, ProgressCb, TtsProvider

BASE_URL = "https://api.qw1voicegencore.pro"
API = "/api/v1/client"

POLL_SEC = 3.0
# временные сбои сервиса во время ожидания — не повод ронять ролик
_POLL_ERRORS_MAX = 20
_DOWNLOAD_ATTEMPTS = 5
_RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})
# тесты подменяют сеть (httpx.MockTransport)
_TRANSPORT: httpx.BaseTransport | None = None


class VoiceGenError(RuntimeError):
    pass


class VoiceGenQueuePaused(VoiceGenError):
    """Очередь озвучки VoiceGen на паузе — ждать бесполезно, нужен запуск очереди."""


class VoiceGenTaskFailed(VoiceGenError):
    """Задача на стороне VoiceGen завершилась ошибкой/отменой — можно создать новую."""


def _detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(data, dict):
        d = data.get("detail") or data.get("error") or data.get("message") or data
        if isinstance(d, list):  # ошибки валидации FastAPI
            d = "; ".join(str(x.get("msg") if isinstance(x, dict) else x) for x in d)
        return str(d)[:300]
    return str(data)[:300]


_STATUS_HINT = {
    401: "ключ VoiceGen неверный или пустой",
    403: "нет доступа: закончился пакет символов или нужна подписка",
    404: "не найдено (шаблон удалён в боте? нажми «Обновить»)",
}


class VoiceGenClient:
    def __init__(self, api_key: str, base_url: str = BASE_URL) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key.strip()}", "Accept": "application/json"},
            timeout=httpx.Timeout(60.0, connect=20.0),
            trust_env=False,
            follow_redirects=True,
            transport=_TRANSPORT,
        )

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        resp = self._http.request(method, API + path, **kw)
        if resp.status_code >= 400:
            hint = _STATUS_HINT.get(resp.status_code, "")
            msg = f"VoiceGen {resp.status_code}: {_detail(resp)}"
            raise VoiceGenError(f"{msg} — {hint}" if hint else msg)
        return resp.json()

    def me(self) -> dict:
        data = self._request("GET", "/me")
        return data if isinstance(data, dict) else {}

    def templates(self) -> list[dict]:
        data = self._request("GET", "/templates")
        return [t for t in data if isinstance(t, dict) and t.get("id")] if isinstance(data, list) else []

    def create_task(self, *, text: str, template_id: str, client_task_id: str) -> dict:
        template = self.template(template_id)
        if not template.get("voice_id"):
            raise VoiceGenError(f"Шаблон {template_id[:8]} не найден в боте — нажми «Обновить» и выбери шаблон заново")
        payload = task_payload(text, template, client_task_id)
        return _task(self._request("POST", "/tasks", json=payload))

    def queue_paused(self) -> bool:
        """Очередь озвучки аккаунта на паузе (её ставят на паузу в боте) — задачи в ней не начнутся.
        Не смогли узнать — считаем, что не на паузе."""
        try:
            data = self._request("GET", "/queue")
        except (httpx.TransportError, VoiceGenError):
            return False
        queue = (data or {}).get("queue") if isinstance(data, dict) else None
        return isinstance(queue, dict) and bool(queue.get("paused")) and not queue.get("active")

    def get_task(self, task_id: str) -> dict:
        return _task(self._request("GET", f"/tasks/{task_id}"))

    def download(self, task_id: str, dest: Path) -> None:
        self.fetch(f"/tasks/{task_id}/download", dest)

    def fetch(self, path: str, dest: Path, *, min_size: int = 1024) -> None:
        """Скачать файл API с повторами: соединение рвётся — пробуем ещё."""
        last: Exception | None = None
        for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
            try:
                with self._http.stream("GET", API + path, timeout=httpx.Timeout(300.0, connect=30.0)) as resp:
                    if resp.status_code >= 400:
                        resp.read()
                        err = VoiceGenError(f"VoiceGen {resp.status_code} при скачивании: {_detail(resp)}")
                        if resp.status_code not in _RETRY_STATUS:
                            raise err
                        raise httpx.TransportError(str(err))
                    with dest.open("wb") as fh:
                        for chunk in resp.iter_bytes(256 * 1024):
                            fh.write(chunk)
                if dest.stat().st_size < min_size:
                    raise httpx.TransportError("пустой или битый файл")
                return
            except httpx.TransportError as exc:
                last = exc
                log(f"VoiceGen: скачивание оборвалось ({exc}), попытка {attempt}/{_DOWNLOAD_ATTEMPTS}")
                time.sleep(min(2 * attempt, 10))
        raise VoiceGenError(f"Не удалось скачать файл VoiceGen: {last}")

    # --- дубляж ---

    def template(self, template_id: str) -> dict:
        data = self._request("GET", f"/templates/{template_id}")
        return data if isinstance(data, dict) else {}

    def create_dub(self, payload: dict) -> dict:
        return _job(self._request("POST", "/dubbing", json=payload))

    def get_dub(self, job_id: str) -> dict:
        return _job(self._request("GET", f"/dubbing/{job_id}"))

    def resume_dub(self, job_id: str) -> dict:
        return _job(self._request("POST", f"/dubbing/{job_id}/resume"))


def task_payload(text: str, template: dict, client_task_id: str) -> dict:
    """Задача озвучки с настройками шаблона, переданными явно.

    Сам template_id не передаём: с ним VoiceGen берёт всё из шаблона и молча
    выключает тайминги, а без таймингов готовую озвучку нельзя перевести на другие языки.
    """
    settings = template.get("voice_engine_settings") if isinstance(template.get("voice_engine_settings"), dict) else {}
    # множителями (1.1, 2.0): проценты из шаблона VoiceGen принимает не везде одинаково
    engine_settings: dict[str, float] = {}
    for key, lo, hi in (("speed", 0.6, 1.5), ("volume", 0.5, 2.0)):
        try:
            value = float(settings[key])
        except (KeyError, TypeError, ValueError):
            continue
        if value > 3:  # проценты
            value /= 100
        engine_settings[key] = round(min(hi, max(lo, value)), 3)
    payload: dict[str, Any] = {
        "text": text,
        "client_task_id": client_task_id,
        "voice_id": template["voice_id"],
        "voice_engine": template.get("voice_engine") or "cartesia",
        "model_id": template.get("model_id") or settings.get("model_id") or "sonic-3.6",
        "voice_engine_settings": engine_settings,
        "filename": "voice.mp3",
        # ролику нужен один mp3, даже если в шаблоне бота стоит архив по частям
        "result_format": "single",
        "split_into_chunk_files": False,
        "split_into_paragraph_files": False,
        # полные тайминги нужны VoiceGen, чтобы потом перевести эту озвучку (дубляж)
        "include_timestamps": True,
        "timestamps_granularity": "word",
    }
    # settings_preset не передаём: пресет сбрасывает скорость и громкость шаблона, а они уже выше
    for key in ("chunk_size", "delay_between_chunks"):
        if template.get(key) not in (None, ""):
            payload[key] = template[key]
    return payload


def _job(data: Any) -> dict:
    job = data.get("job") if isinstance(data, dict) else None
    if not isinstance(job, dict) or not job.get("job_id"):
        raise VoiceGenError(f"Неожиданный ответ VoiceGen (дубляж): {str(data)[:300]}")
    return job


def _task(data: Any) -> dict:
    task = data.get("task") if isinstance(data, dict) else None
    if not isinstance(task, dict) or not task.get("task_id"):
        raise VoiceGenError(f"Неожиданный ответ VoiceGen: {str(data)[:300]}")
    return task


def _speed_label(settings: object) -> str:
    if not isinstance(settings, dict):
        return ""
    try:
        speed = float(settings.get("speed"))
    except (TypeError, ValueError):
        return ""
    if speed > 3:  # проценты 60–150
        speed /= 100
    return "" if abs(speed - 1) < 0.005 else f"{speed:.2f}×"


def template_label(t: dict) -> str:
    """«Название · голос · 1.10×» — чтобы в списке было видно, что внутри шаблона."""
    parts = [str(t.get("name") or t.get("id"))]
    voice = str(t.get("voice_display_name") or "").strip()
    if voice and voice not in parts[0]:
        parts.append(voice)
    speed = _speed_label(t.get("voice_engine_settings"))
    if speed:
        parts.append(speed)
    return " · ".join(parts)


def _dub_languages() -> tuple[tuple[str, str], ...]:
    from engine.tts.voicegen_dub import LANGUAGES

    return LANGUAGES


class VoiceGenProvider(TtsProvider):
    id = "voicegen"
    label = "VoiceGen"
    description = "Озвучка Cartesia по шаблонам из Telegram-бота VoiceGen"
    fields = (
        FieldSpec("api_key", "API key", "secret", placeholder="ключ из бота VoiceGen", required=True),
        FieldSpec(
            "template",
            "Шаблон",
            "remote_list",
            quick=True,
            required=True,
            item_label="шаблон",
            hint="Голос, скорость и модель настраиваются в шаблоне в боте. "
            "Создал или поменял шаблон в боте — нажми «Обновить».",
        ),
        FieldSpec(
            "translate",
            "Ещё ролики на языках",
            "multi_choice",
            quick=True,
            hint="Ролик на языке текста делается всегда, отмечать его не нужно — язык определяется сам, "
            "лишняя галочка просто пропустится. Каждый отмеченный язык — отдельный ролик с тем же видеорядом. "
            "Стоимость языка в VoiceGen — 1,5 символа баланса за символ перевода.",
            options=_dub_languages(),
        ),
    )

    def voice_tag(self, opts: dict[str, Any]) -> str:
        return self.choice(opts, "template")

    def dub_languages(self, opts: dict[str, Any]) -> list[str]:
        return list(self.normalize({"translate": opts.get("translate")})["translate"])

    def start_dub(self, opts: dict[str, Any], work_dir: Path, *, source_language: str, targets: list[str]) -> list[str]:
        from engine.tts import voicegen_dub

        return voicegen_dub.start(self.text(opts, "api_key"), work_dir, source_language=source_language, targets=targets)

    def iter_dubs(
        self,
        opts: dict[str, Any],
        work_dir: Path,
        *,
        cancel: CancelToken | None = None,
        on_progress: ProgressCb | None = None,
    ) -> Iterator[DubTrack]:
        from engine.tts import voicegen_dub

        return voicegen_dub.iter_ready(self.text(opts, "api_key"), work_dir, cancel=cancel, on_progress=on_progress)

    def mark_dub_done(self, work_dir: Path, language: str, result: str) -> None:
        from engine.tts import voicegen_dub

        voicegen_dub.mark_done(work_dir, language, result)

    def refresh(self, opts: dict[str, Any], key: str) -> list[NamedItem]:
        if key != "template":
            raise ValueError(f"Нечего обновлять: {key}")
        api_key = self.text(opts, "api_key")
        if not api_key:
            raise ValueError("Сначала вставь API key VoiceGen")
        client = VoiceGenClient(api_key)
        try:
            items = [NamedItem(str(t["id"]), template_label(t)) for t in client.templates()]
        finally:
            client.close()
        log(f"VoiceGen: шаблонов {len(items)}")
        return items

    def ping(self, opts: dict[str, Any]) -> str:
        api_key = self.text(opts, "api_key")
        if not api_key:
            raise ValueError("Вставь API key VoiceGen")
        client = VoiceGenClient(api_key)
        try:
            me = client.me()
            n = len(client.templates())
        finally:
            client.close()
        parts = [f"ok, шаблонов: {n}"]
        free = me.get("free_generation_remaining_chars")
        if isinstance(free, (int, float)) and free > 0:
            parts.append(f"бесплатный остаток: {int(free)} симв.")
        if me.get("has_active_subscription"):
            parts.append("пакет активен")
        return ", ".join(parts)

    def synthesize(
        self,
        text: str,
        dest: Path,
        opts: dict[str, Any],
        *,
        state_file: Path,
        cancel: CancelToken | None = None,
        on_progress: ProgressCb | None = None,
    ) -> Path:
        self.check(opts)
        templates = NamedList.parse(opts.get("template"))
        return synthesize_resumable(
            api_key=self.text(opts, "api_key"),
            text=text,
            template_id=templates.selected_id,
            template_name=templates.selected_name,
            dest=dest,
            state_file=state_file,
            cancel=cancel,
            on_progress=on_progress,
        )


def _load_state(state_file: Path, text_sha: str, template_id: str) -> dict:
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    ok = (
        isinstance(state, dict)
        and state.get("provider") == "voicegen"
        and state.get("text_sha") == text_sha
        and state.get("template_id") == template_id
    )
    return state if ok else {}


def _save_state(state_file: Path, state: dict) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state), encoding="utf-8")


def synthesize_resumable(
    *,
    api_key: str,
    text: str,
    template_id: str,
    template_name: str = "",
    dest: Path,
    state_file: Path,
    cancel: CancelToken | None = None,
    on_progress: ProgressCb | None = None,
    poll_sec: float = POLL_SEC,
    base_url: str = BASE_URL,
) -> Path:
    """Озвучка без двойной оплаты.

    client_task_id пишется в state_file ДО создания задачи: VoiceGen по нему
    не создаёт дубликат, даже если ответ на создание потерялся. После сбоя или
    «Продолжить» дожидаемся и скачиваем ту же задачу.
    """

    def progress(pct: float, msg: str) -> None:
        log(msg)
        if on_progress:
            on_progress(pct, msg)

    def check() -> None:
        if cancel is not None:
            cancel.check()

    text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    state = _load_state(state_file, text_sha, template_id)
    client = VoiceGenClient(api_key, base_url)
    if cancel is not None:
        cancel.register(client)
    try:
        task: dict | None = None
        for _ in range(2):  # вторая попытка — только если прошлая задача упала на стороне VoiceGen
            check()
            task_id = str(state.get("task_id") or "")
            if task_id:
                progress(0.3, f"Озвучка VoiceGen: продолжаю задачу {task_id[:8]}… (без повторной оплаты)")
                try:
                    task = client.get_task(task_id)
                except VoiceGenError as exc:
                    if "404" not in str(exc):
                        raise
                    log(f"VoiceGen: задача {task_id} не найдена (результаты хранятся 3 дня) — создаю новую")
                    state = {}
                    continue
            else:
                if not state.get("client_task_id"):
                    state = {
                        "provider": "voicegen",
                        "client_task_id": str(uuid.uuid4()),
                        "text_sha": text_sha,
                        "template_id": template_id,
                    }
                    _save_state(state_file, state)
                label = f"«{template_name}»" if template_name else template_id[:8]
                progress(0.3, f"Озвучка VoiceGen: шаблон {label}, {len(text)} символов")
                task = client.create_task(text=text, template_id=template_id, client_task_id=state["client_task_id"])
                state["task_id"] = str(task["task_id"])
                _save_state(state_file, state)
            try:
                task = _wait(client, task, timeout=tts_wait_timeout_sec(len(text)), poll_sec=poll_sec, progress=progress, check=check)
                break
            except VoiceGenTaskFailed as exc:
                log(f"VoiceGen: задача {task['task_id']} не удалась ({exc}) — создаю новую")
                state = {}
                task = None
        if task is None:
            raise VoiceGenError("VoiceGen дважды не смог озвучить текст — проверь баланс и шаблон в боте")
        check()
        progress(0.6, "Озвучка VoiceGen: скачиваю аудио…")
        part = dest.with_name(dest.stem + ".part" + dest.suffix)
        client.download(str(task["task_id"]), part)
        part.replace(dest)
        from engine.tts.voicegen_dub import write_source

        write_source(state_file.parent, task_id=str(task["task_id"]), template_id=template_id, text_chars=len(text))
    finally:
        client.close()
    state_file.unlink(missing_ok=True)
    return dest


# задача долго «в очереди» — раз в столько секунд смотрим, не стоит ли очередь на паузе
_QUEUE_CHECK_SEC = 60.0


def _wait(client: VoiceGenClient, task: dict, *, timeout: float, poll_sec: float, progress, check) -> dict:
    task_id = str(task["task_id"])
    deadline = time.monotonic() + timeout
    errors = 0
    last = ""
    queue_checked = 0.0
    while True:
        status = str(task.get("status") or "")
        if status == "done":
            return task
        if status == "queued" and time.monotonic() - queue_checked >= _QUEUE_CHECK_SEC:
            queue_checked = time.monotonic()
            if client.queue_paused():
                raise VoiceGenQueuePaused(
                    "Очередь озвучки VoiceGen стоит на паузе — задача ждёт и сама не начнётся. "
                    f"Запусти очередь в боте VoiceGen и нажми «Продолжить»: задача {task_id[:8]} не потеряется "
                    "и второй раз не оплачивается"
                )
        if status in ("error", "cancelled"):
            raise VoiceGenTaskFailed(str(task.get("error_message") or status))
        try:
            pct = float(task.get("progress") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        note = f"Озвучка VoiceGen: {status or '…'} {pct:.0f}%"
        if note != last:
            progress(0.3 + 0.3 * min(max(pct, 0), 100) / 100, note)
            last = note
        if time.monotonic() > deadline:
            raise VoiceGenError(
                f"VoiceGen не закончил озвучку за {timeout / 60:.0f} мин — нажми «Продолжить», задача {task_id[:8]} не потеряется"
            )
        end = time.monotonic() + poll_sec
        while time.monotonic() < end:
            check()
            time.sleep(min(0.5, poll_sec))
        try:
            task = client.get_task(task_id)
            errors = 0
        except (httpx.TransportError, VoiceGenError) as exc:
            check()
            transient = isinstance(exc, httpx.TransportError) or any(f" {c}:" in f" {exc}" for c in ("502", "503", "504", "429"))
            errors += 1
            if not transient or errors > _POLL_ERRORS_MAX:
                raise
            log(f"VoiceGen: временная ошибка при проверке статуса ({exc}), жду")
