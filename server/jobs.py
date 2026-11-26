"""Очередь задач: до MAX_PARALLEL роликов одновременно, остальные ждут по порядку.

Очередь хранится в data/jobs.json: упавшие и прерванные задачи переживают
перезапуск приложения и продолжаются с места падения.
"""

from __future__ import annotations

import json
import shutil
import os
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from engine.core.cancel import CancelledError, CancelToken, current_cancel
from engine.core.config import DATA_DIR
from engine.core import memguard, netguard, usage
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

# тесты (tests/_isolate.py) задают свой файл очереди: настоящий jobs.json пользователя они не трогают
JOBS_FILE = Path(os.environ["STORY_VIDEO_JOBS_FILE"]) if os.environ.get("STORY_VIDEO_JOBS_FILE") else DATA_DIR / "jobs.json"
HISTORY_LIMIT = 100
# плановый перезапуск (обновление, перезапуск по просьбе): какие задачи шли и ждали — после запуска они продолжаются
# сами, а не «Прервано закрытием». Метка живёт RESUME_TTL секунд — старую (программа упала) не используем
RESUME_FILE = JOBS_FILE.with_name("resume_after_restart.json")
RESUME_TTL = 15 * 60


def mark_resume(ids: list[str]) -> None:
    """Перед плановым перезапуском: эти задачи после запуска продолжить сами."""
    RESUME_FILE.write_text(json.dumps({"at": time.time(), "ids": list(ids)}), encoding="utf-8")


def _take_resume() -> set[str]:
    try:
        raw = json.loads(RESUME_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    try:
        RESUME_FILE.unlink()
    except OSError:
        pass
    if not isinstance(raw, dict) or time.time() - float(raw.get("at") or 0) > RESUME_TTL:
        return set()
    return {str(i) for i in raw.get("ids") or []}

# (job, progress_cb, cancel) -> result dict
Runner = Callable[["Job", Callable[[float, str], None], CancelToken], dict[str, Any]]

_SAVE_LOCK = threading.Lock()
_PROGRESS_EVERY = 0.25  # сек между событиями прогресса одной задачи
# Сколько роликов идёт одновременно. Тяжёлые этапы (рендер, Whisper) всё равно
# по одному (engine.core.throttle), параллельно идут озвучка, LLM и подбор кадров.
MAX_PARALLEL = 2
# выкладка на YouTube — своя полоса: идёт параллельно с монтажом, но по одной (окно браузера в фокусе одно)
UPLOAD_KINDS = frozenset({"publish", "comments", "polls", "analytics", "scan", "collabs", "fixvideo"})  # профили AdsPower — по одной задаче
# лёгкие задачи (генерация обложек — только сеть) — своя полоса: не ждут ни монтажа, ни выкладки
LIGHT_KINDS = frozenset({"cover", "niches", "competition"})


# сборка роликов: CLIP, Whisper, ffmpeg — гигабайты памяти. Программа всегда оставляет Mac запас памяти
# (engine.core.memguard): иначе Mac уходит в подкачку и замирает всё — окно VPN (Happ выключает туннель через 10 с),
# Bluetooth (клавиатура и мышь отваливаются)
HEAVY_KINDS = frozenset({"broll", "slides_full", "slides_resume", "slides_voice", "slides_media", "slides_render"})
MEM_WAIT_MAX = 10 * 60.0  # память занята не нами (браузер, другие программы) — ролик ждёт не дольше, дальше бережно
MEM_WAIT_UPLOAD = 3 * 60.0  # выкладка и прочие окна Studio — ждут меньше: у них сроки и расписание


def mem_need(kind: str) -> int:
    """Сколько памяти займёт задача сверх запаса Mac (0 — лёгкая, не ждёт)."""
    if kind in HEAVY_KINDS:
        return memguard.HEAVY_NEED
    if kind in UPLOAD_KINDS:
        return memguard.BROWSER_NEED
    return 0


# без доступа к YouTube (отключился VPN) эти задачи не начинаем: им нужен сам YouTube (Studio, ролик-источник,
# YouTube API). Сборка роликов из готового текста, озвучка и стоки идут дальше — VPN им не нужен
NET_KINDS = UPLOAD_KINDS | frozenset({"rewrite", "niches", "competition"})


def lane_of(kind: str) -> str:
    return "upload" if kind in UPLOAD_KINDS else "light" if kind in LIGHT_KINDS else "main"
# краткий сбой сети или сервиса (обрыв, 5xx/522, «LLM перегружен») — очередь сама повторит задачу
# с места падения через столько минут; дальше — ждёт «Продолжить»
# (ночью YouTube бывает недоступен часами: прокси профиля лагает, на адресе VPN «подтвердите, что вы не робот» —
# коротких трёх повторов хватало на 20 минут и задача оставалась упавшей до утра)
AUTO_RETRY_MIN = (3, 10, 20, 40, 60, 90)
WATCHDOG_SEC = 300  # как часто сторож смотрит на идущие задачи
STALL_SEC = 90 * 60  # идущая задача без прогресса и строк журнала столько — зависла
_TRANSIENT = (
    "connecttimeout", "connecterror", "readtimeout", "handshake operation timed out", "timed out",
    "connection reset", "connection refused", "remoteprotocolerror", "server disconnected",
    "nodename nor servname", "temporary failure in name resolution", "network is unreachable",
    "adspower не отвечает",  # Local API AdsPower под нагрузкой на минуту замолкает — потом отвечает
    "сервис llm перегружен", "no upstream accounts", "нет связи", "временно не работает", "временно не скачивается",
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
    stop_at: float | None = None  # когда нажали «Стоп» на идущей задаче (для счётчика в карточке)
    on_state: Callable[[], None] | None = None
    beat: float = field(default_factory=time.time)  # когда задача последний раз подала признак жизни (прогресс, журнал)

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

    def _targets(self) -> tuple[list[str], bool]:
        """На какие каналы идёт работа: (подписи каналов, после ли сборки). Выкладка, голосования, комментарии — прямо
        сейчас; ролик в сборке с включённой выкладкой раздела — «после сборки» (по снимку настроек задачи)."""
        try:
            p = self.params
            if self.kind in ("publish", "comments", "polls", "collabs"):
                keys = p.get("channels") or ([p["channel"]] if p.get("channel") else [])
                return _channel_names(keys), False
            section = {"broll": "broll", "slides_full": "slides", "slides": "slides"}.get(self.kind)
            if self.kind == "rewrite":
                section = str(p.get("section") or "broll")
            if not section:
                return [], False
            snap = p.get("_settings")
            if not isinstance(snap, dict):
                from engine.core.config import load_config

                snap = load_config()
            if snap.get(f"{section}_publish") and self.status in (QUEUED, RUNNING, FAILED, CANCELLED):
                return _channel_names(snap.get(f"{section}_publish_channels") or []), True
        except Exception:  # noqa: BLE001 — подпись каналов не должна ломать список задач
            pass
        return [], False

    def public(self) -> dict[str, Any]:
        # текст и снимок настроек задачи — не для интерфейса (большие)
        safe = {k: v for k, v in self.params.items() if k not in ("text", "_settings")}
        if self.kind == "cover":  # какой моделью делалась обложка (из снимка настроек задачи)
            from engine.publish.cover import model_label

            safe["model"] = model_label(str((self.params.get("_settings") or {}).get("cover_model") or ""))
        if self.params.get("text"):
            safe["chars"] = len(str(self.params["text"]))
        targets, after = self._targets()
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
            # ждёт повтора по расписанию (лимит YouTube, сбой сети) — когда продолжит сама
            "retry_at": (float(self.state.get("retry_at") or 0) or None) if self.status == QUEUED else None,
            "resumable": self.status in RESUMABLE,
            "stopping": self.status == RUNNING and self.cancel.is_cancelled(),
            "stop_at": self.stop_at if self.status == RUNNING and self.cancel.is_cancelled() else None,
            "run": self.run_name,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            # выкладка: живой статус по каналам (шаг, процент загрузки, время выхода)
            "live": self._live_view(),
            # автосмены инструмента/модели: Claude → Codex → встроенная, модель недоступна → другая
            "switches": self.state.get("switches") or [],
            # расход токенов встроенной нейросети по этапам (Claude Code и Codex — по подписке, не считаются)
            "tokens": usage.summary(self.state.get("tokens")),
            "targets": targets,
            "targets_after": after,
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
        # итоги пачки задач с прошлого простоя очереди — для уведомления «всё готово»
        self._batch = self._new_batch()
        self._uncounted: set[str] = set()  # идут или только что закончились, ещё не учтены в пачке
        self.awake = KeepAwake()
        self._load()
        set_log_callback(self._on_log)
        usage.set_usage_callback(self._on_usage)
        from engine.core import switches

        switches.set_callback(self._on_switch)
        netguard.on_change(self._on_net)
        for n in range(MAX_PARALLEL):
            threading.Thread(target=self._loop, args=("main",), name=f"job-worker-{n + 1}", daemon=True).start()
        threading.Thread(target=self._loop, args=("upload",), name="upload-worker", daemon=True).start()
        threading.Thread(target=self._loop, args=("light",), name="light-worker", daemon=True).start()
        threading.Thread(target=self._watchdog, name="job-watchdog", daemon=True).start()

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
                job.stop_at = time.time()
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

    def purge_old(self, days: float) -> int:
        """Убрать карточки завершённых задач (готово, ошибка, остановлено), к которым не прикасались дольше days дней.
        Идущие и ждущие в очереди не трогаем. → сколько убрано."""
        cutoff = time.time() - days * 86400
        with self._lock:
            gone = [j.id for j in self._jobs if j.status not in (QUEUED, RUNNING)
                    and (j.finished or j.started or j.created) < cutoff]
            if gone:
                self._jobs = [j for j in self._jobs if j.id not in gone]
        if gone:
            self._save()
            for jid in gone:
                hub.publish("job_removed", {"id": jid})
        return len(gone)

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
        resume = _take_resume()
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
            scheduled = job.status == QUEUED and float(job.state.get("retry_at") or 0) > 0
            # приложение закрылось/упало посреди очереди — после запуска работа продолжается сама
            # (остановленные руками — CANCELLED, их не трогаем; тихие задачи идут по своему расписанию)
            from server import retry_policy

            if job.status in (QUEUED, RUNNING) and (
                    job.id in resume or scheduled or job.kind not in retry_policy.QUIET_KINDS):
                # ждущая повтора по расписанию (лимит YouTube и т. п.) не «прервана» закрытием —
                # она и так стояла, поэтому после любого перезапуска продолжает ждать своего времени
                # плановый перезапуск: задача продолжится сама (идущая — с места остановки, по своему state)
                try:
                    job.title, job.runner = self._build(job.kind, job.params, job.state)
                    job.status = QUEUED
                    if not scheduled:  # у ждущей повтора остаётся причина ожидания
                        job.message = "Продолжаю после перезапуска программы" if job.started else "В очереди"
                except Exception as exc:  # noqa: BLE001 — настройки сломались: ждём человека, как раньше
                    job.status, job.error = FAILED, f"после перезапуска не продолжилась: {exc}"
                    job.message = "Прервано закрытием приложения"
                    job.finished = job.finished or time.time()
            elif job.status in (QUEUED, RUNNING):
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
                # нет доступа к YouTube (отключился VPN) — задачи, которым он нужен, не начинаем (не тратят попытки),
                # сторож переподключает VPN и разбудит очередь, когда связь вернётся; остальные идут
                offline = netguard.is_down()
                job = next((j for j in self._jobs if j.status == QUEUED and j.runner is not None
                            and lane_of(j.kind) == lane
                            and float(j.state.get("retry_at") or 0) <= now
                            and not (offline and j.kind in NET_KINDS)
                            and self._mem_ok(j, now)), None)
                if job is None and offline and any(j.status == QUEUED and j.kind in NET_KINDS and lane_of(j.kind) == lane
                                                   for j in self._jobs):
                    self._work.wait(timeout=10.0)  # ждут связь — сторож разбудит раньше, как только она вернётся
                    continue
                if job is None and any(j.status == QUEUED and j.state.get("mem_wait_since") for j in self._jobs
                                       if lane_of(j.kind) == lane):
                    # задача ждёт свободную память — проверяем каждые 15 с
                    self._work.wait(timeout=15.0)
                    continue
                if job is not None:
                    job.state.pop("retry_at", None)
                    job.state.pop("mem_wait_since", None)
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

    def _mem_ok(self, job: Job, now: float) -> bool:
        """Хватит ли памяти начать задачу, не трогая запас Mac. Второй тяжёлый ролик ждёт, пока первый
        доделается (память занимаем мы сами). Иначе (память занята другими программами) — ждём не дольше
        MEM_WAIT_MAX: работа не встаёт навсегда, а внутри задачи рендер сам идёт бережнее."""
        need = mem_need(job.kind)
        ours = any(j.status == RUNNING and j.kind in HEAVY_KINDS for j in self._jobs)
        # первый ролик ждёт только при критической нехватке: память тогда занята другими программами,
        # ожиданием её не вернуть, а рендер сам ужмётся; второй — пока наш первый не освободит память
        short = memguard.critical() if job.kind in HEAVY_KINDS and not ours else bool(need) and memguard.tight(need)
        if not need or not short:
            job.state.pop("mem_wait_since", None)
            return True
        since = float(job.state.get("mem_wait_since") or 0)
        if not since:
            job.state["mem_wait_since"] = since = now
            job.message = f"Жду свободную память — {memguard.describe()}"
            log(f"⏸ {job.title}: {job.message}")
            self._emit(job)
        # второй ролик ждёт первый без предела; выкладка и прочее — не дольше MEM_WAIT_MAX (ролики сами ужмутся)
        limit = MEM_WAIT_MAX if job.kind in HEAVY_KINDS else MEM_WAIT_UPLOAD
        if (not ours or job.kind not in HEAVY_KINDS) and now - since >= limit:
            log(f"{job.title}: памяти мало уже {limit / 60:.0f} мин ({memguard.describe()}) — начинаю бережно")
            return True
        return False

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
        with self._lock:
            self._uncounted.add(job.id)
        self._save()
        self._emit(job)
        log(f"▶ {job.title}" + (f" (попытка {job.attempts})" if job.attempts > 1 else ""))

        job.beat = time.time()

        def progress(pct: float, msg: str) -> None:
            job.beat = time.time()
            job.progress = max(0.0, min(1.0, float(pct)))
            if job.cancel.is_cancelled():
                # «Стоп» нажат, шаг ещё доделывается: не затираем «Останавливаю…» обычным прогрессом —
                # иначе выглядит, будто кнопка не сработала
                job.message = f"Останавливаю — дожидаюсь конца шага ({msg})" if msg else "Останавливаю…"
            elif msg:
                job.message = msg
            self._emit(job, throttle=True)

        try:
            job.cancel.check()
            current_cancel.set(job.cancel)  # внешние программы (ffmpeg) убиваются по «Стоп» сразу
            job.result = job.runner(job, progress, job.cancel)
            job.status = DONE
            job.progress = 1.0
            job.message = (job.result or {}).get("summary") or "Готово"
            log(f"✓ {job.title}: {job.message}")
        except CancelledError as exc:
            if job.state.pop("stalled", None):
                # остановила не рука человека, а сторож: задача зависла — это сбой, а не «Стоп»
                self._failed(job, RuntimeError(f"Зависла: {STALL_SEC / 60:g} мин без движения — перезапускаю с места остановки"))
            else:
                job.status = CANCELLED
                job.message = "Остановлено — можно продолжить"
                log(f"■ {job.title}: остановлено")
        except Exception as exc:
            self._failed(job, exc)
        finally:
            job.finished = time.time()
            self._save()
            self._emit(job)
            self._count_for_notice(job)
            self._notify()  # исполнитель свободен — проверить сон и очередь

    WAIT_BLOCK_SEC = 20 * 60.0  # ждущая автоповтора задача дольше — очередь «закончила», итог не ждёт
    NOTICE_MIN_SEC = 45.0  # быстрые задачи (секунды) не стоят уведомления — пользователь ещё у окна

    @staticmethod
    def _new_batch() -> dict[str, Any]:
        return {"done": 0, "failed": 0, "sec": 0.0, "last": "", "snaps": {}, "started": 0.0}

    def _count_for_notice(self, job: Job) -> None:
        """Пачка задач закончилась (очередь опустела) — итоговый отчёт (окно в приложении + уведомление macOS).
        Остановленные самим пользователем не считаем; задача, ждущая автоповтор, очередь не опустошает."""
        with self._lock:
            self._uncounted.discard(job.id)
            if job.status in (DONE, FAILED):
                self._batch["done" if job.status == DONE else "failed"] += 1
                self._batch["sec"] += max(0.0, (job.finished or 0) - (job.started or job.finished or 0))
                self._batch["last"] = job.title
                self._batch["snaps"][job.id] = job.stored()  # итог задачи (повторный проход затирает прежний)
                begun = job.started or job.finished or 0.0
                if begun and (not self._batch["started"] or begun < self._batch["started"]):
                    self._batch["started"] = begun
            # соседняя задача уже поставила статус «готово», но ещё не учтена — уведомит она, одним на всех
            soon = time.time() + self.WAIT_BLOCK_SEC
            if self._uncounted or any(
                    j.status == RUNNING or (j.status == QUEUED and float(j.state.get("retry_at") or 0) <= soon)
                    for j in self._jobs):
                return  # идёт работа или вот-вот повтор; задача, ждущая до завтра (лимит YouTube), отчёт не задерживает
            batch, self._batch = self._batch, self._new_batch()
            batch["waiting"] = [{"title": j.title, "message": j.message, "retry_at": float(j.state.get("retry_at") or 0)}
                                for j in self._jobs if j.status == QUEUED and float(j.state.get("retry_at") or 0) > soon]
        # итог — по последнему состоянию каждой задачи (упала, потом продолжена и дошла — это «готово»)
        batch["done"] = sum(1 for sn in batch["snaps"].values() if sn["status"] == DONE)
        batch["failed"] = sum(1 for sn in batch["snaps"].values() if sn["status"] == FAILED)
        if not (batch["done"] or batch["failed"]):
            return
        if self._report(batch):
            return
        if batch["sec"] >= self.NOTICE_MIN_SEC:
            from server.notify import notify, queue_message

            notify(*queue_message(batch["done"], batch["failed"], batch["last"]))

    def _report(self, batch: dict[str, Any]) -> bool:
        """Итоговый отчёт по пачке: что сделано, что нет, почему и как исправить. → показан ли (иначе — обычное
        уведомление). Тихие задачи (аналитика раз в сутки) без ошибок отчёта не стоят."""
        from server import report as rep

        snaps = list(batch["snaps"].values())
        failed = batch["failed"] > 0
        if all(sn.get("kind") in rep.QUIET_KINDS for sn in snaps) and not failed:
            return False
        if not (failed or batch["sec"] >= self.NOTICE_MIN_SEC):  # секундные задачи — пользователь ещё у окна
            return False
        try:
            from engine.publish import collabs

            # Очередь за ночь пустеет много раз, а окно открывают утром: итог — ОДИН за всё время, пока прошлый не
            # открыли (иначе в нём «1 из 4», а то, что было раньше, потерялось). Плюс всё упавшее в журнале.
            carried, carried_start = rep.carry()
            merged = {**carried, **batch["snaps"]}
            jobs = list(getattr(self, "_jobs", None) or [])
            if jobs:
                by_id = {j.id: j for j in jobs}
                for jid in list(merged):
                    j = by_id.get(jid)
                    if j is None:
                        if merged[jid].get("status") != DONE:
                            del merged[jid]  # задачу убрали из журнала — упавшей она уже не числится
                    elif jid not in batch["snaps"]:
                        if j.status in (DONE, FAILED):
                            merged[jid] = j.stored()  # свежее состояние (упала → продолжили → дошла)
                        else:
                            del merged[jid]  # снова в работе или ждёт повтора — в «ждут повтора» ниже
                day_ago = time.time() - 24 * 3600
                for j in jobs:
                    if (j.status == FAILED and j.id not in merged and float(j.finished or 0) >= day_ago
                            and j.kind not in rep.QUIET_KINDS):
                        merged[j.id] = j.stored()
            snaps = list(merged.values())
            started = min((x for x in (batch["started"], carried_start) if x), default=0.0)
            report = rep.build(snaps, names=_channel_names, pending_pairs=collabs.pending(),
                               started=started, finished=time.time(), waiting=batch.get("waiting") or [])
            rep.add(report, merged)
        except Exception as exc:  # noqa: BLE001 — отчёт не важнее самой работы
            log(f"Итоговый отчёт не собрался: {exc}")
            return False
        hub.publish("report", {"report": report})
        from server.notify import notify

        notify(f"Story Video: {report['title']}", report["text"])
        log(f"Итог: {report['title']} — {report['text']}")
        return True

    def _failed(self, job: Job, exc: BaseException) -> None:
        job.status = FAILED
        job.error = f"{exc}" if _is_user_error(exc) else f"{type(exc).__name__}: {exc}"
        job.message = "Ошибка — можно продолжить с места падения"
        if not getattr(exc, "retry_at", None):  # ожидание по расписанию (лимит, AdsPower) — без трассировки
            log("".join(traceback.format_exception(exc)))
        log(f"✗ {job.title}: {job.error}")
        self._maybe_retry(job, exc)

    def _watchdog(self) -> None:
        """Сторож зависаний: идущая задача, от которой нет ни прогресса, ни строки в журнале STALL_SEC, — зависла
        (браузер, сеть без таймаута). Останавливаем и перезапускаем с места остановки по общим правилам повторов."""
        while True:
            time.sleep(WATCHDOG_SEC)
            now = time.time()
            with self._lock:
                stuck = [j for j in self._jobs if j.status == RUNNING and now - j.beat > STALL_SEC
                         and not j.cancel.is_cancelled() and j.kind not in ("analytics", "scan")]
            for j in stuck:
                log(f"⚠ {j.title}: {STALL_SEC / 60:g} мин без движения — считаю зависшей, останавливаю и перезапускаю")
                j.state["stalled"] = True
                j.cancel.cancel()

    def _maybe_retry(self, job: Job, exc: BaseException) -> None:
        """Краткий сбой сети/сервиса — поставить задачу обратно в очередь с паузой (с места падения)."""
        if not netguard.check_now():
            # упало из-за обрыва связи (VPN) — ждём связь без счёта попыток, продолжим сразу, как вернётся
            try:
                title, runner = self._build(job.kind, job.params, job.state)
            except Exception as build_exc:
                log(f"Ожидание связи не получилось: {build_exc}")
                return
            job.state["wait_net"] = True
            job.state.pop("retry_at", None)
            job.runner, job.title = runner, title
            job.status = QUEUED
            job.cancel = CancelToken()
            job.message = "Нет связи с YouTube (отключился VPN) — переподключаю, продолжу с места падения сам"
            log(f"↻ {job.title}: {job.message}")
            return
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
        if isinstance(exc, (ValueError, FileNotFoundError)) and not is_transient(exc):
            return  # ошибка настроек/данных — повтор её не исправит
        from server import retry_policy

        first = float(job.state.setdefault("first_failure", time.time()))
        wait, why = retry_policy.plan(f"{type(exc).__name__}: {exc}", n=n, kind=job.kind, first_failure=first,
                                      now=time.time(), early=AUTO_RETRY_MIN)
        if wait is None:
            if why:
                log(f"✋ {job.title}: сама не повторю ({why})")
                if why == "нужен человек лично":
                    self._ping_user(job)
            return
        try:
            title, runner = self._build(job.kind, job.params, job.state)
        except Exception as build_exc:  # настройки сломались — ждём человека
            log(f"Автоповтор не получился: {build_exc}")
            return
        job.state["auto_retries"] = n + 1
        job.state["retry_at"] = time.time() + wait * 60
        job.runner, job.title = runner, title
        job.status = QUEUED
        job.cancel = CancelToken()
        mins = f"{wait:g}" if wait < 1 else f"{wait:.0f}"
        job.message = f"Сбой — сам повторю через {mins} мин (повтор {n + 1})"
        log(f"↻ {job.title}: {job.message}")

    @staticmethod
    def _ping_user(job: Job) -> None:
        """Задача упёрлась в то, что может сделать только человек (вход в Google): уведомление сразу, не по концу очереди."""
        try:
            from server.notify import notify

            notify("Story Video: нужен ваш шаг", f"{job.title[:80]} — требуется подтвердить вход в аккаунт Google; остальные задачи идут дальше")
        except Exception as exc:  # noqa: BLE001
            log(f"Уведомление не отправилось: {exc}")

    def _on_net(self, snap: dict[str, Any]) -> None:
        """Сторож связи: состояние — в интерфейс; связь вернулась — ждавшие её задачи идут сразу."""
        hub.publish("net", {"net": snap})
        if not snap.get("up"):
            return
        with self._lock:
            for j in self._jobs:
                if j.status == QUEUED and j.state.pop("wait_net", None):
                    j.state.pop("retry_at", None)
                    j.message = "Связь вернулась — продолжаю…"
                    self._emit(j)
            self._work.notify_all()

    def _emit(self, job: Job, *, throttle: bool = False) -> None:
        now = time.monotonic()
        if throttle and now - self._last_emit.get(job.id, 0.0) < _PROGRESS_EVERY:
            return
        self._last_emit[job.id] = now
        hub.publish("job", {"job": job.public()})

    def _on_log(self, line: str) -> None:
        jid = current_job.get()
        if jid:
            job = self._find(jid)
            if job is not None:
                job.beat = time.time()
        hub.publish("log", {"line": line, "job_id": jid})

    def _on_usage(self, job_id: str | None, label: str, tokens_in: int, cached: int, tokens_out: int) -> None:
        """Токены встроенной нейросети — в карточку задачи, которая их тратит. На диск уйдут со следующим
        сохранением очереди (конец задачи, смена этапа), на каждый ответ файл не переписываем."""
        with self._lock:
            job = self._find(job_id) if job_id else None
            if job is None:
                return
            usage.add_to(job.state.setdefault("tokens", {}), label, tokens_in, cached, tokens_out)
        self._emit(job, throttle=True)

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


_NAMES: dict[str, Any] = {"mtime": None, "map": {}}


def _channel_names(keys: list[str]) -> list[str]:
    """«профиль/канал» → «@адрес» (или название) по учёту каналов; учёт перечитывается, только когда файл изменился."""
    from engine.publish import registry
    from engine.publish.store import publish_dir

    path = publish_dir() / registry.FILE
    try:
        stamp = path.stat().st_mtime
    except OSError:
        stamp = None
    if _NAMES["mtime"] != stamp or stamp is None:
        names: dict[str, str] = {}
        for entry in registry.load().values():
            for c in entry.channels:
                names[registry.key(entry.id, c.id)] = c.handle or c.name
        _NAMES.update(mtime=stamp, map=names)
    return [str(_NAMES["map"].get(str(k)) or str(k).rpartition("/")[2]) for k in keys if str(k).strip()]


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
        # приглашения соавторов обязаны быть приняты: ждущие пары (в т. ч. от прошлых выкладок) подтверждает фоновая проверка
        from engine.publish import collabs

        if os.environ.get("STORY_VIDEO_NO_BACKGROUND"):
            return manager  # тесты: фоновых проверок нет
        try:
            with manager._lock:
                states = [dict(j.state) for j in manager._jobs if j.kind == "publish"]
            n = collabs.backfill(states)
            if n:
                log(f"Соавторства: в списке на проверку {n} приглашений из прошлых выкладок — подтвержу в фоне")
        except Exception:  # noqa: BLE001 — список не собрался: новые приглашения всё равно записываются
            pass
        try:  # выложенные без обложки/плейлиста раньше — доделать (разово при запуске)
            from server.runners import backfill_fixes

            n = backfill_fixes(manager)
            if n:
                log(f"Доделка роликов: поставлено задач «Доделать ролик» — {n} (выкладки последних дней)")
        except Exception as exc:  # noqa: BLE001
            log(f"Доделка роликов: проверка прошлых выкладок не удалась ({exc})")
        threading.Thread(target=collabs.loop, args=(manager,), name="collabs-auto", daemon=True).start()
        # сторож связи: YouTube доступен только через VPN — отвалился, переподключаем и держим очередь на паузе
        threading.Thread(target=netguard.loop, args=(load_config,), name="netguard", daemon=True).start()
        from engine import updater

        threading.Thread(target=updater.loop, args=(manager, load_config), name="updater", daemon=True).start()
        from engine.core import retention

        threading.Thread(target=retention.loop, args=(manager, DATA_DIR), name="retention", daemon=True).start()
    return manager
