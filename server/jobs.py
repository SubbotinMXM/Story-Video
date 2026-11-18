"""Очередь задач: до MAX_PARALLEL роликов одновременно, остальные ждут по порядку.

Очередь хранится в data/jobs.json: упавшие и прерванные задачи переживают
перезапуск приложения и продолжаются с места падения.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from engine.core.cancel import CancelledError, CancelToken
from engine.core.config import DATA_DIR
from engine.core.logutil import current_job, log, set_log_callback
from server.events import hub
from server.keepawake import KeepAwake

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
FINISHED = {DONE, FAILED, CANCELLED}
RESUMABLE = {FAILED, CANCELLED}

JOBS_FILE = DATA_DIR / "jobs.json"
HISTORY_LIMIT = 100

# (job, progress_cb, cancel) -> result dict
Runner = Callable[["Job", Callable[[float, str], None], CancelToken], dict[str, Any]]

_SAVE_LOCK = threading.Lock()
_PROGRESS_EVERY = 0.25  # сек между событиями прогресса одной задачи
# Сколько роликов идёт одновременно. Тяжёлые этапы (рендер, Whisper) всё равно
# по одному (engine.core.throttle), параллельно идут озвучка, LLM и подбор кадров.
MAX_PARALLEL = 2
# выкладка на YouTube — своя полоса: идёт параллельно с монтажом, но по одной (окно браузера в фокусе одно)
UPLOAD_KINDS = frozenset({"publish", "comments", "polls", "analytics", "scan"})  # профили AdsPower — по одной задаче
# лёгкие задачи (генерация обложек — только сеть) — своя полоса: не ждут ни монтажа, ни выкладки
LIGHT_KINDS = frozenset({"cover", "niches"})


def lane_of(kind: str) -> str:
    return "upload" if kind in UPLOAD_KINDS else "light" if kind in LIGHT_KINDS else "main"
# краткий сбой сети или сервиса (обрыв, 5xx/522, «LLM перегружен») — очередь сама повторит задачу
# с места падения через столько минут; дальше — ждёт «Продолжить»
AUTO_RETRY_MIN = (5, 15, 30)
_TRANSIENT = (
    "connecttimeout", "connecterror", "readtimeout", "handshake operation timed out", "timed out",
    "connection reset", "connection refused", "remoteprotocolerror", "server disconnected",
    "nodename nor servname", "temporary failure in name resolution", "network is unreachable",
    "сервис llm перегружен", "no upstream accounts", "нет связи", "временно не работает",
    " 500", " 502", " 503", " 504", " 520", " 521", " 522", " 523", " 524", "cloudflare",
    # YouTube временно ограничил доступ с этого адреса — обычно проходит само через какое-то время
    "подтвердить, что это не робот",
    # браузер не открыл страницу: сеть/VPN ещё не поднялись после сна или обрыв — проходит само
    "page.goto: timeout", "net::err_internet_disconnected", "net::err_name_not_resolved", "net::err_connection",
    "net::err_timed_out", "net::err_network_changed", "net::err_proxy", "net::err_tunnel",
)


def is_transient(exc: BaseException) -> bool:
    """Сбой, который проходит сам (сеть, перегрузка сервиса), а не ошибка настроек или данных."""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(k in text for k in _TRANSIENT)


@dataclass
class Job:
    kind: str
    title: str
    # полные параметры формы (с текстом) — нужны, чтобы продолжить задачу
    params: dict[str, Any]
    runner: Runner | None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    # что задача успела создать: рабочая папка и т.п.
    state: dict[str, Any] = field(default_factory=dict)
    status: str = QUEUED
    progress: float = 0.0
    message: str = "В очереди"
    result: dict[str, Any] | None = None
    error: str | None = None
    attempts: int = 0
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    cancel: CancelToken = field(default_factory=CancelToken)
    on_state: Callable[[], None] | None = None

    @property
    def run_name(self) -> str:
        """Папка запуска слайдов, с которой работает задача (пусто — не слайды)."""
        if self.params.get("run"):
            return Path(str(self.params["run"])).name
        raw = self.state.get("run_dir")
        return Path(raw).name if raw else ""

    def save_state(self, **values: Any) -> None:
        """Записать состояние и сразу сохранить очередь на диск."""
        self.state.update(values)
        if self.on_state:
            self.on_state()

    def _live_view(self):
        """Живой статус каналов. Задача ждёт очереди — прошлые «ошибка»/«идёт» устарели: канал просто ждёт."""
        live = self.state.get("live")
        if self.status != QUEUED or not live:
            return live
        return [r if r.get("status") == "done" else
                {**r, "status": "wait", "pct": 0, "stage": "ждёт очереди — продолжу с места падения"
                 if r.get("status") in ("failed", "limit", "active") else r.get("stage", "ждёт очереди")}
                for r in live]

    def public(self) -> dict[str, Any]:
        # текст и снимок настроек задачи — не для интерфейса (большие)
        safe = {k: v for k, v in self.params.items() if k not in ("text", "_settings")}
        if self.kind == "cover":  # какой моделью делалась обложка (из снимка настроек задачи)
            from engine.publish.cover import model_label

            safe["model"] = model_label(str((self.params.get("_settings") or {}).get("cover_model") or ""))
        if self.params.get("text"):
            safe["chars"] = len(str(self.params["text"]))
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "params": safe,
            "status": self.status,
            "progress": round(self.progress, 4),
            "message": self.message,
            "result": self.result,
            "error": self.error,
            "attempts": self.attempts,
            "resumable": self.status in RESUMABLE,
            "run": self.run_name,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            # выкладка: живой статус по каналам (шаг, процент загрузки, время выхода)
            "live": self._live_view(),
            # автосмены инструмента/модели: Claude → Codex → встроенная, модель недоступна → другая
            "switches": self.state.get("switches") or [],
            "notice": "",  # предупреждение при постановке (заполняет /api/jobs, например «мало денег RoyalTechno»)
        }

    def stored(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "params": self.params,
            "state": self.state,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "result": self.result,
            "error": self.error,
            "attempts": self.attempts,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
        }


# kind, params, state -> (title, runner); задаётся из server.runners
Builder = Callable[[str, dict[str, Any], dict[str, Any]], tuple[str, Runner]]


class JobManager:
    def __init__(self, builder: Builder) -> None:
        self._build = builder
        self._lock = threading.RLock()
        self._jobs: list[Job] = []
        self._work = threading.Condition(self._lock)
        self._last_emit: dict[str, float] = {}
        self.awake = KeepAwake()
        self._load()
        set_log_callback(self._on_log)
        from engine.core import switches

        switches.set_callback(self._on_switch)
        for n in range(MAX_PARALLEL):
            threading.Thread(target=self._loop, args=("main",), name=f"job-worker-{n + 1}", daemon=True).start()
        threading.Thread(target=self._loop, args=("upload",), name="upload-worker", daemon=True).start()
        threading.Thread(target=self._loop, args=("light",), name="light-worker", daemon=True).start()

    # --- API ---

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [j.public() for j in self._jobs]

    def create(self, kind: str, params: dict[str, Any]) -> Job:
        """Проверить вход и собрать задачу (ValueError — показать пользователю)."""
        title, runner = self._build(kind, params, {})
        job = Job(kind=kind, title=title, params=params, runner=runner)
        self._check_run_free(job)
        return job

    def _check_run_free(self, job: Job) -> None:
        """Одну папку запуска не обрабатывают две задачи сразу (теперь их идёт две)."""
        name = job.run_name
        if not name:
            return
        with self._lock:
            busy = any(
                j is not job and j.status in (QUEUED, RUNNING) and j.run_name == name
                for j in self._jobs
            )
        if busy:
            raise ValueError(f"Запуск {name} уже в работе — дождись окончания или останови его")

    def add(self, job: Job, *, after: str | None = None) -> Job:
        """В очередь. after — id задачи, сразу за которой встать (продолжение цепочки: рерайт → его ролик
        собирается следующим, а не после всех остальных ссылок)."""
        job.on_state = self._save
        with self._lock:
            pos = next((i for i, j in enumerate(self._jobs) if j.id == after), None) if after else None
            if pos is None:
                self._jobs.append(job)
            else:
                self._jobs.insert(pos + 1, job)
        self._save()
        self._emit(job)
        self._emit_order()
        log(f"Задача в очереди: {job.title}")
        self._notify()
        return job

    def move(self, job_id: str, to: str) -> bool:
        """Приоритет в очереди: up / down — на одну ждущую задачу, top / bottom — в начало / конец ожидающих.
        Работающие и готовые задачи не двигаются; следующей берётся верхняя ждущая."""
        if to not in ("up", "down", "top", "bottom"):
            raise ValueError(f"Неизвестное направление: {to}")
        with self._lock:
            job = self._find(job_id)
            if job is None:
                raise ValueError("Задача не найдена")
            if job.status != QUEUED:
                raise ValueError("Двигать можно только задачи, которые ждут в очереди")
            queued = [j for j in self._jobs if j.status == QUEUED]
            k = queued.index(job)
            target = {"up": k - 1, "down": k + 1, "top": 0, "bottom": len(queued) - 1}[to]
            target = max(0, min(len(queued) - 1, target))
            if target == k:
                return False
            # переставляем среди ждущих, остальные задачи остаются на своих местах в списке
            slots = [i for i, j in enumerate(self._jobs) if j.status == QUEUED]
            queued.insert(target, queued.pop(k))
            for i, j in zip(slots, queued):
                self._jobs[i] = j
        self._save()
        self._emit_order()
        log(f"Очередь: «{job.title}» — {'выше' if to in ('up', 'top') else 'ниже'}")
        return True

    def _emit_order(self) -> None:
        with self._lock:
            ids = [j.id for j in self._jobs]
        hub.publish("jobs_order", {"ids": ids})

    def resume(self, job_id: str) -> Job:
        """Продолжить упавшую/остановленную задачу с места падения.

        Задача пересобирается из свежих настроек — новые ключи подхватятся.
        """
        with self._lock:
            job = self._find(job_id)
            if job is None:
                raise ValueError("Задача не найдена")
            if job.status not in RESUMABLE:
                raise ValueError("Продолжить можно только упавшую или остановленную задачу")
            self._check_run_free(job)
            # продолжение руками — счётчик автоповторов с нуля
            job.state.pop("auto_retries", None)
            job.state.pop("retry_at", None)
            title, runner = self._build(job.kind, job.params, job.state)
            job.runner = runner
            job.title = title
            job.status = QUEUED
            job.progress = 0.0
            job.message = "В очереди: продолжение"
            job.error = None
            job.result = None
            job.finished = None
            job.cancel = CancelToken()
        self._save()
        self._emit(job)
        log(f"Продолжение: {job.title}")
        self._notify()
        return job

    def reset(self, job_id: str) -> bool:
        """Полный сброс: убрать задачу и её незаконченную рабочую папку."""
        with self._lock:
            job = self._find(job_id)
            if job is None:
                return False
            if job.status == RUNNING:
                raise ValueError("Сначала останови задачу")
            job.cancel.cancel()
            self._jobs.remove(job)
        if job.status != DONE:
            _remove_owned_dir(job)
        self._save()
        hub.publish("job_removed", {"id": job_id})
        log(f"Сброс: {job.title}")
        return True

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._find(job_id)
            if job is None or job.status in FINISHED:
                return False
            if job.status == QUEUED:
                job.status = CANCELLED
                job.message = "Остановлено до старта"
                job.finished = time.time()
            else:
                job.message = "Останавливаю…"
            job.cancel.cancel()
        self._save()
        self._emit(job)
        return True

    def clear_finished(self) -> None:
        with self._lock:
            gone = [j.id for j in self._jobs if j.status == DONE]
            self._jobs = [j for j in self._jobs if j.status != DONE]
        self._save()
        for jid in gone:
            hub.publish("job_removed", {"id": jid})

    def busy(self) -> bool:
        with self._lock:
            return any(j.status in (QUEUED, RUNNING) for j in self._jobs)

    # --- хранение ---

    def _save(self) -> None:
        with self._lock:
            done = [j for j in self._jobs if j.status == DONE]
            keep_done = {j.id for j in done[-HISTORY_LIMIT:]}
            data = [j.stored() for j in self._jobs if j.status != DONE or j.id in keep_done]
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with _SAVE_LOCK:  # два потока писали один временный файл — второй падал на переименовании (14.11)
                tmp = JOBS_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                tmp.replace(JOBS_FILE)
        except OSError as exc:
            log(f"Не удалось сохранить очередь: {exc}")

    def _load(self) -> None:
        if not JOBS_FILE.is_file():
            return
        try:
            raw = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for item in raw if isinstance(raw, list) else []:
            try:
                job = Job(
                    kind=item["kind"],
                    title=item["title"],
                    params=item.get("params") or {},
                    runner=None,
                    id=item["id"],
                    state=item.get("state") or {},
                    status=item.get("status") or FAILED,
                    progress=float(item.get("progress") or 0),
                    message=item.get("message") or "",
                    result=item.get("result"),
                    error=item.get("error"),
                    attempts=int(item.get("attempts") or 0),
                    created=float(item.get("created") or time.time()),
                    started=item.get("started"),
                    finished=item.get("finished"),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if job.status in (QUEUED, RUNNING):
                # приложение закрыли посреди работы — ждём решения пользователя
                job.status = CANCELLED if job.status == QUEUED else FAILED
                job.error = None if job.status == CANCELLED else "Приложение было закрыто во время работы"
                job.message = "Прервано закрытием приложения"
                job.finished = job.finished or time.time()
            job.on_state = self._save
            self._jobs.append(job)
        self._save()

    # --- worker ---

    def _find(self, job_id: str) -> Job | None:
        return next((j for j in self._jobs if j.id == job_id), None)

    def _notify(self) -> None:
        with self._lock:
            self._work.notify_all()

    def _claim(self, lane: str = "main") -> Job:
        """Ждать задачу в очереди и забрать её (атомарно — два исполнителя не возьмут одну).
        lane — полоса исполнителя (main / upload / light): берёт только задачи своей полосы."""
        with self._lock:
            while True:
                now = time.time()
                job = next((j for j in self._jobs if j.status == QUEUED and j.runner is not None
                            and lane_of(j.kind) == lane
                            and float(j.state.get("retry_at") or 0) <= now), None)
                if job is not None:
                    job.state.pop("retry_at", None)
                    job.status = RUNNING
                    job.started = time.time()
                    job.attempts += 1
                    job.message = "Продолжаю…" if job.attempts > 1 else "Старт…"
                    return job
                waiting = [float(j.state.get("retry_at") or 0) for j in self._jobs if j.status == QUEUED]
                if not any(j.status == RUNNING for j in self._jobs) and not waiting:
                    # ничего не идёт и не ждёт — Mac снова может засыпать
                    self.awake.release()
                # задачи, ждущие автоповтора, — проснуться к ближайшему повтору
                due = [t for t in waiting if t > now]
                self._work.wait(timeout=max(1.0, min(due) - now) if due else None)

    def _loop(self, lane: str = "main") -> None:
        while True:
            job = self._claim(lane)
            self.awake.hold()
            current_job.set(job.id)  # журнал этой задачи — отдельно
            try:
                self._run(job)
            finally:
                current_job.set(None)

    def _run(self, job: Job) -> None:
        self._save()
        self._emit(job)
        log(f"▶ {job.title}" + (f" (попытка {job.attempts})" if job.attempts > 1 else ""))

        def progress(pct: float, msg: str) -> None:
            job.progress = max(0.0, min(1.0, float(pct)))
            if msg:
                job.message = msg
            self._emit(job, throttle=True)

        try:
            job.cancel.check()
            job.result = job.runner(job, progress, job.cancel)
            job.status = DONE
            job.progress = 1.0
            job.message = (job.result or {}).get("summary") or "Готово"
            log(f"✓ {job.title}: {job.message}")
        except CancelledError:
            job.status = CANCELLED
            job.message = "Остановлено — можно продолжить"
            log(f"■ {job.title}: остановлено")
        except Exception as exc:
            job.status = FAILED
            job.error = f"{exc}" if _is_user_error(exc) else f"{type(exc).__name__}: {exc}"
            job.message = "Ошибка — можно продолжить с места падения"
            if not getattr(exc, "retry_at", None):  # ожидание по расписанию (лимит, AdsPower) — без трассировки
                log(traceback.format_exc())
            log(f"✗ {job.title}: {job.error}")
            self._maybe_retry(job, exc)
        finally:
            job.finished = time.time()
            self._save()
            self._emit(job)
            self._notify()  # исполнитель свободен — проверить сон и очередь

    def _maybe_retry(self, job: Job, exc: BaseException) -> None:
        """Краткий сбой сети/сервиса — поставить задачу обратно в очередь с паузой (с места падения)."""
        retry_at = getattr(exc, "retry_at", None)
        if retry_at:
            # известно, когда пробовать снова (лимит загрузок YouTube) — ждём до этого времени, без счёта попыток
            try:
                title, runner = self._build(job.kind, job.params, job.state)
            except Exception as build_exc:
                log(f"Повтор по расписанию не получился: {build_exc}")
                return
            job.state["retry_at"] = float(retry_at)
            job.runner, job.title = runner, title
            job.status = QUEUED
            job.cancel = CancelToken()
            job.message = str(exc)
            log(f"↻ {job.title}: {job.message}")
            return
        n = int(job.state.get("auto_retries") or 0)
        if not is_transient(exc) or n >= len(AUTO_RETRY_MIN):
            return
        try:
            title, runner = self._build(job.kind, job.params, job.state)
        except Exception as build_exc:  # настройки сломались — ждём человека
            log(f"Автоповтор не получился: {build_exc}")
            return
        wait = AUTO_RETRY_MIN[n]
        job.state["auto_retries"] = n + 1
        job.state["retry_at"] = time.time() + wait * 60
        job.runner, job.title = runner, title
        job.status = QUEUED
        job.cancel = CancelToken()
        job.message = f"Сбой сети или сервиса — сам повторю через {wait} мин (повтор {n + 1} из {len(AUTO_RETRY_MIN)})"
        log(f"↻ {job.title}: {job.message}")

    def _emit(self, job: Job, *, throttle: bool = False) -> None:
        now = time.monotonic()
        if throttle and now - self._last_emit.get(job.id, 0.0) < _PROGRESS_EVERY:
            return
        self._last_emit[job.id] = now
        hub.publish("job", {"job": job.public()})

    def _on_log(self, line: str) -> None:
        hub.publish("log", {"line": line, "job_id": current_job.get()})

    SWITCHES_MAX = 30  # столько смен инструмента храним в карточке задачи

    def _on_switch(self, job_id: str | None, item: dict[str, Any]) -> None:
        """Автосмена инструмента/модели: в карточку задачи (с повторами одной и той же смены — счётчиком)
        и всплывающим уведомлением, чтобы было видно сразу."""
        with self._lock:
            job = self._find(job_id) if job_id else None
            fresh = True
            if job is not None:
                items: list = job.state.setdefault("switches", [])
                key = (item["task"], item["from"], item["to"])
                same = next((x for x in items if (x["task"], x["from"], x["to"]) == key), None)
                if same is not None:  # та же смена ещё раз (модель перегружена снова) — без нового уведомления
                    same.update(reason=item["reason"], at=item["at"], times=int(same.get("times") or 1) + 1)
                    fresh = False
                else:
                    items.append(item)
                    del items[:-self.SWITCHES_MAX]
        if job is not None:
            self._save()
            self._emit(job)
        if fresh:
            hub.publish("switch", {"job_id": job_id, "title": job.title if job else "", **item})


def _remove_owned_dir(job: Job) -> None:
    """Удаляем только папку, которую задача создала сама (не чужой старый запуск)."""
    raw = job.state.get("owned_dir")
    if not raw:
        return
    path = Path(raw)
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
        log(f"Удалена незаконченная папка: {path}")


def _is_user_error(exc: Exception) -> bool:
    # ошибки пайплайнов уже написаны по-человечески
    return isinstance(exc, (ValueError, FileNotFoundError)) or type(exc).__module__.startswith(
        "engine."
    )


manager: JobManager | None = None


def get_manager() -> JobManager:
    global manager
    if manager is None:
        from server.runners import build

        manager = JobManager(build)
        from engine.core.config import load_config
        from engine.publish import analytics_auto

        # «Аналитика» → автообновление по расписанию: проверка раз в 30 с, пока работает приложение
        threading.Thread(target=analytics_auto.loop, args=(manager, load_config), name="analytics-auto",
                         daemon=True).start()
    return manager
