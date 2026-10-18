"""AI-кадры (RoyalTechno AI): генерация фото и видео для слабых кусков стокового ролика.

Документация: https://api.royaltechno.cc/docs (правила для скриптов соблюдаются все):
- POST /v1/jobs → 201 с id; опрос GET /v1/jobs/{id} каждые 5 с (фото) / 10 с (видео)
  из ОДНОГО цикла, с If-None-Match (304 — ничего не поменялось);
- не больше 20 задач «в полёте» на ключ (берём limits.recommended_max_in_flight);
- задачу в queued/processing не переотправляем; повтор POST — только на 429/503,
  с тем же Idempotency-Key и с Retry-After; на 400/401/402/403 — стоп;
- на 429 queue_full POST не повторяем, ждём завершения своих задач.

Кадры берутся «живые» (фото, стоки, YouTube), а сгенерированные — только туда, где
живого хорошего кадра не нашлось, и не больше AI_SHARE_MAX кадров ролика.
"""

from __future__ import annotations

import json
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from engine.core.cancel import CancelToken
from engine.core.logutil import log

BASE_URL = "https://api.royaltechno.cc"
# доля сгенерированных кадров в ролике: «примерно 10–15%» — не больше 15, цель 12
AI_SHARE = 0.12
AI_SHARE_MAX = 0.15
# сколько ждём генерацию на весь ролик; не успело — остаются живые кадры (задачи отменяем)
WAIT_SEC = 25 * 60
MAX_IN_FLIGHT = 20
POLL_SEC = {"image": 5.0, "video": 10.0}
STATE_NAME = "ai_jobs.json"
_RES_SUFFIX = re.compile(r"-(480p|720p|1080p|2k|4k)$")
# тесты подменяют сеть (httpx.MockTransport)
_TRANSPORT: httpx.BaseTransport | None = None

# Каталог из документации — на случай, если живой прайс (GET /v1/public/pricing) недоступен.
FALLBACK_MODELS: list[dict] = [
    {"model_id": "veo-3.1-lite", "api_name": "veo-3.1", "display_name": "Veo 3.1", "maker": "Google", "media_type": "video", "unit": "per_clip", "price_cents": 2, "example_duration_sec": 8, "example_cents": 2, "variable_duration": False},
    {"model_id": "veo-3.1-1080p", "api_name": "veo-3.1", "display_name": "Veo 3.1 1080p", "maker": "Google", "media_type": "video", "unit": "per_clip", "price_cents": 5, "example_duration_sec": 8, "example_cents": 5, "variable_duration": False},
    {"model_id": "omni-flash", "api_name": "omni-flash", "display_name": "Omni 1.1 Flash", "maker": "Google", "media_type": "video", "unit": "per_second", "price_cents": 1, "example_duration_sec": 4, "example_cents": 4, "variable_duration": True},
    {"model_id": "seedance-mini", "api_name": "seedance-2.0-mini", "display_name": "Seedance 2.0 Mini", "maker": "ByteDance", "media_type": "video", "unit": "per_second", "price_cents": 8, "example_duration_sec": 5, "example_cents": 40, "variable_duration": True},
    {"model_id": "seedance-fast", "api_name": "seedance-2.0-fast", "display_name": "Seedance 2.0 Fast", "maker": "ByteDance", "media_type": "video", "unit": "per_second", "price_cents": 9, "example_duration_sec": 5, "example_cents": 45, "variable_duration": True},
    {"model_id": "seedance-2.0-720p", "api_name": "seedance-2.0", "display_name": "Seedance 2.0", "maker": "ByteDance", "media_type": "video", "unit": "per_second", "price_cents": 11, "example_duration_sec": 5, "example_cents": 55, "variable_duration": True},
    {"model_id": "seedance-2.0-1080p", "api_name": "seedance-2.0", "display_name": "Seedance 2.0 1080p", "maker": "ByteDance", "media_type": "video", "unit": "per_second", "price_cents": 25, "example_duration_sec": 5, "example_cents": 125, "variable_duration": True},
    {"model_id": "seedance-2.5-480p", "api_name": "seedance-2.5", "display_name": "Seedance 2.5 480p", "maker": "ByteDance", "media_type": "video", "unit": "per_second", "price_cents": 10, "example_duration_sec": 5, "example_cents": 50, "variable_duration": True},
    {"model_id": "seedance-2.5-720p", "api_name": "seedance-2.5", "display_name": "Seedance 2.5", "maker": "ByteDance", "media_type": "video", "unit": "per_second", "price_cents": 21, "example_duration_sec": 5, "example_cents": 105, "variable_duration": True},
    {"model_id": "seedance-2.5-1080p", "api_name": "seedance-2.5", "display_name": "Seedance 2.5 1080p", "maker": "ByteDance", "media_type": "video", "unit": "per_second", "price_cents": 52, "example_duration_sec": 5, "example_cents": 260, "variable_duration": True},
    {"model_id": "grok-imagine-1.5", "api_name": "grok-imagine-1.5", "display_name": "Grok Imagine", "maker": "xAI", "media_type": "video", "unit": "per_second", "price_cents": 1, "example_duration_sec": 6, "example_cents": 7, "variable_duration": True},
    {"model_id": "grok-imagine-1.5-720p", "api_name": "grok-imagine-1.5-720p", "display_name": "Grok Imagine 720p", "maker": "xAI", "media_type": "video", "unit": "per_second", "price_cents": 2, "example_duration_sec": 6, "example_cents": 14, "variable_duration": True},
    {"model_id": "nano-banana-pro", "api_name": "nano-banana-pro", "display_name": "Nano Banana Pro", "maker": "Google", "media_type": "image", "unit": "per_image", "price_cents": 2, "variable_duration": False},
    {"model_id": "nano-banana-pro-2k", "api_name": "nano-banana-pro", "display_name": "Nano Banana Pro 2K", "maker": "Google", "media_type": "image", "unit": "per_image", "price_cents": 4, "variable_duration": False},
    {"model_id": "nano-banana-pro-4k", "api_name": "nano-banana-pro", "display_name": "Nano Banana Pro 4K", "maker": "Google", "media_type": "image", "unit": "per_image", "price_cents": 6, "variable_duration": False},
    {"model_id": "nano-banana-2", "api_name": "nano-banana-2", "display_name": "Nano Banana 2.1", "maker": "Google", "media_type": "image", "unit": "per_image", "price_cents": 2, "variable_duration": False},
    {"model_id": "nano-banana-2-2k", "api_name": "nano-banana-2", "display_name": "Nano Banana 2.1 2K", "maker": "Google", "media_type": "image", "unit": "per_image", "price_cents": 4, "variable_duration": False},
    {"model_id": "nano-banana-2-4k", "api_name": "nano-banana-2", "display_name": "Nano Banana 2.1 4K", "maker": "Google", "media_type": "image", "unit": "per_image", "price_cents": 6, "variable_duration": False},
    {"model_id": "seedream-4.6", "api_name": "seedream-4.6", "display_name": "Seedream 4.6", "maker": "ByteDance", "media_type": "image", "unit": "per_image", "price_cents": 3, "variable_duration": False},
]
# длина клипа: наши видеокуски ≤ 6 с — берём самый короткий разрешённый вариант (дешевле)
_CLIP_SEC = {"omni-flash": 6, "grok-imagine-1.5": 6, "grok-imagine-1.5-720p": 6}
_DEFAULT_CLIP_SEC = 5
_catalog_cache: tuple[float, list[dict]] | None = None


class AiGenError(RuntimeError):
    pass


# ---------- каталог моделей и цены ----------


def catalog(*, refresh: bool = False) -> list[dict]:
    """Модели фото и видео с ценами: живой прайс API (кеш 1 ч), иначе — из документации."""
    global _catalog_cache
    if not refresh and _catalog_cache and time.monotonic() - _catalog_cache[0] < 3600:
        return _catalog_cache[1]
    models = FALLBACK_MODELS
    try:
        with httpx.Client(timeout=20.0, trust_env=False, transport=_TRANSPORT) as http:
            resp = http.get(f"{BASE_URL}/v1/public/pricing")
            if resp.status_code == 200:
                live = [m for m in resp.json().get("models") or [] if m.get("media_type") in ("image", "video")]
                if live:
                    models = live
    except Exception as exc:
        log(f"AI-кадры: прайс недоступен ({exc}) — цены из документации")
    _catalog_cache = (time.monotonic(), models)
    return models


def _usd(cents: float) -> str:
    return f"${cents / 100:.2f}"


def resolution_of(entry: dict) -> str:
    """Вариант модели → поле resolution запроса («veo-3.1-1080p» → «1080p»; база — auto)."""
    m = _RES_SUFFIX.search(str(entry.get("model_id") or ""))
    if not m or str(entry.get("api_name") or "").endswith(m.group(1)):
        return ""
    return m.group(1)


def clip_seconds(entry: dict) -> int | None:
    """duration_sec для видео: фиксированная длина (Veo) — None, иначе короткий клип."""
    if entry.get("media_type") != "video" or not entry.get("variable_duration"):
        return None
    return _CLIP_SEC.get(str(entry.get("api_name")), _DEFAULT_CLIP_SEC)


def price_of(entry: dict) -> int:
    """Цена одного кадра в центах: фото — за штуку; видео — за наш короткий клип."""
    cents = int(entry.get("price_cents") or 0)
    if entry.get("unit") == "per_second":
        return cents * (clip_seconds(entry) or int(entry.get("example_duration_sec") or 5))
    return cents


def model_options() -> dict[str, list[dict]]:
    """Для выпадающих списков: {"image": [...], "video": [...]} с ценой и разрешением."""
    out: dict[str, list[dict]] = {"image": [], "video": []}
    for e in catalog():
        kind = e.get("media_type")
        if kind not in out:
            continue
        cents = int(e.get("price_cents") or 0)
        if kind == "image":
            price = f"{_usd(cents)} за фото"
        elif e.get("unit") == "per_second":
            price = f"{_usd(cents)}/с · клип {clip_seconds(e)} с = {_usd(price_of(e))}"
        else:
            price = f"{_usd(cents)} за клип {e.get('example_duration_sec') or 8} с"
        res = resolution_of(e) or ("480p" if e.get("api_name") == "grok-imagine-1.5" else "")
        out[kind].append({
            "id": e["model_id"],
            "label": f"{e.get('display_name') or e['model_id']} ({e.get('maker') or ''}) — {price}",
            "price_cents": price_of(e),
            "low_res": res == "480p",
        })
    for kind in out:
        out[kind].sort(key=lambda o: o["price_cents"])
    return out


def entry(model_id: str) -> dict | None:
    return next((e for e in catalog() if e.get("model_id") == model_id), None)


# ---------- клиент API ----------


@dataclass
class AiConfig:
    """Что генерировать: модели (варианты из прайса) и ключ. Пустая модель — этот вид не генерим."""

    api_key: str
    image_model: str = ""
    video_model: str = ""

    @property
    def active(self) -> bool:
        return bool(self.api_key.strip() and (self.image_model or self.video_model))


class RoyalClient:
    def __init__(self, api_key: str, base_url: str = BASE_URL) -> None:
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key.strip()}", "Accept": "application/json"},
            timeout=httpx.Timeout(60.0, connect=20.0),
            trust_env=False,
            follow_redirects=True,
            transport=_TRANSPORT,
        )
        self._etags: dict[str, str] = {}
        self._last: dict[str, dict] = {}

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, path: str, *, idempotency: str | None = None, body: dict | None = None, retry: bool = True) -> httpx.Response:
        """Запрос по правилам API: 429/503 — Retry-After и экспоненциальная пауза (до 6 раз)."""
        headers = {"Idempotency-Key": idempotency} if idempotency else {}
        for attempt in range(1, 7):
            resp = self._http.request(method, path, json=body, headers=headers)
            if resp.status_code not in (429, 503) or not retry:
                return resp
            code = _error_code(resp)
            if resp.status_code == 429 and code == "queue_full":
                return resp  # POST не повторяем: ждём свои задачи
            if code == "model_unavailable":
                return resp
            wait = max(float(resp.headers.get("Retry-After") or 0), min(60.0, 2.0**attempt))
            wait *= random.uniform(0.8, 1.2)
            log(f"AI-кадры: {resp.status_code} {code or ''} — жду {wait:.0f} с")
            time.sleep(wait)
        return resp

    def account(self) -> dict:
        resp = self._call("GET", "/v1/account")
        if resp.status_code >= 400:
            raise AiGenError(_explain(resp))
        return resp.json()

    def submit(self, model: str, inp: dict, idempotency: str) -> dict:
        resp = self._call("POST", "/v1/jobs", idempotency=idempotency, body={"model": model, "input": inp})
        if resp.status_code == 429:
            raise AiGenError("queue_full")
        if resp.status_code >= 400:
            raise AiGenError(_explain(resp))
        return resp.json()

    def poll(self, job_id: str) -> dict:
        headers = {"If-None-Match": self._etags[job_id]} if job_id in self._etags else {}
        resp = self._http.get(f"/v1/jobs/{job_id}", headers=headers)
        if resp.status_code == 304:
            return self._last[job_id]
        if resp.status_code >= 400:
            raise AiGenError(_explain(resp))
        if resp.headers.get("ETag"):
            self._etags[job_id] = resp.headers["ETag"]
        self._last[job_id] = resp.json()
        return self._last[job_id]

    def cancel(self, job_id: str) -> None:
        try:
            self._http.post(f"/v1/jobs/{job_id}/cancel")
        except Exception:
            pass

    def download(self, url: str, dest: Path) -> bool:
        try:
            # файлы лежат на CDN (публичные ссылки) — без нашего ключа в заголовке
            with httpx.Client(timeout=httpx.Timeout(300.0, connect=30.0), follow_redirects=True,
                              trust_env=False, transport=_TRANSPORT) as http:
                with http.stream("GET", url) as resp:
                    if resp.status_code >= 400:
                        return False
                    with dest.open("wb") as fh:
                        for chunk in resp.iter_bytes(1 << 20):
                            fh.write(chunk)
            return dest.stat().st_size > 1000
        except Exception as exc:
            log(f"AI-кадры: не скачалось ({exc})")
            return False


def _error_code(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return ""
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("code") or err.get("type") or "")
    detail = data.get("detail") if isinstance(data, dict) else None
    return str(detail.get("code") if isinstance(detail, dict) else "")


def _explain(resp: httpx.Response) -> str:
    hint = {401: "ключ RoyalTechno неверный", 402: "на балансе RoyalTechno не хватает денег", 403: "доступ запрещён (ключ/бан)"}.get(resp.status_code, "")
    try:
        data = resp.json()
        err = data.get("error") if isinstance(data, dict) else None
        msg = (err or {}).get("message") if isinstance(err, dict) else data.get("detail")
    except ValueError:
        msg = resp.text[:200]
    text = f"RoyalTechno {resp.status_code}: {msg}"
    return f"{text} — {hint}" if hint else text


def ping(api_key: str) -> str:
    client = RoyalClient(api_key)
    try:
        acc = client.account()
    finally:
        client.close()
    bal = acc.get("balance_usd") or _usd(int(acc.get("balance_usd_cents") or 0))
    plans = ", ".join(p.get("name", "") for p in acc.get("plans") or [] if p.get("name"))
    return f"ok, баланс {bal}" + (f", тарифы: {plans}" if plans else "")


# ---------- генерация пачкой ----------


@dataclass
class GenTask:
    """Один кадр на генерацию: куда положить и чем заменить."""

    key: str  # стабильный ключ (номер куска / место в пуле) — для «Продолжить» без двойной оплаты
    kind: str  # image | video
    prompt: str
    dest: Path
    result: Path | None = None
    error: str = ""
    meta: dict = field(default_factory=dict)


_FIGURE = re.compile(r"\d[\d\s.,]*\s?(?:%|процент\w*|percent|млн|млрд|million|billion|тыс\w*|thousand)?", re.I)


_UNITS = ((re.compile(r"\s?(процент\w*|percent)", re.I), "%"), (re.compile(r"\s?(млрд|billion)", re.I), "B"),
          (re.compile(r"\s?(млн|million)", re.I), "M"), (re.compile(r"\s?(тыс\w*|thousand)", re.I), "K"))


def figures_in(text: str, limit: int = 4) -> list[str]:
    """Числа из текста куска — подписи для инфографики без слов: «15,3 млрд» → «15,3B», «12 процентов» → «12%»."""
    out: list[str] = []
    for m in _FIGURE.finditer(text or ""):
        f = " ".join(m.group(0).split()).strip(" .,")
        for rx, unit in _UNITS:
            f = rx.sub(unit, f)
        if f and f not in out and any(ch.isdigit() for ch in f):
            out.append(f)
    return out[:limit]


def build_prompt(
    query: str,
    *,
    kind: str,
    era: str,
    place: str,
    historical: bool,
    data: bool = False,
    figures: list[str] | None = None,
) -> str:
    """Подсказка для модели.

    Обычный кадр — документальное фото/видео без надписей. Кусок про цифры (data) —
    чистая инфографика: график/диаграмма с числами из текста и без других слов.
    """
    scene = (query or "").strip().rstrip(".")
    where = ", ".join(x for x in (place, era) if x)
    if data:
        look = (
            "Animated infographic, smooth motion graphics, clean modern data visualization, dark elegant background"
            if kind == "video"
            else "Clean modern infographic, data visualization, bold clear chart, elegant dark background, high contrast"
        )
        parts = [scene, look]
        if where:
            parts.append(f"Topic: {where}")
        if figures:
            parts.append("Show these figures as the only labels: " + ", ".join(figures))
        parts.append("Numbers only, no words, no captions, no watermark, no logos")
        return ". ".join(parts) + "."
    look = (
        "Cinematic documentary footage, slow steady camera movement, photorealistic, natural light"
        if kind == "video"
        else "Documentary photograph, photorealistic, natural light, sharp details"
    )
    parts = [scene, look]
    if where:
        parts.append(f"Setting: {where}")
    if historical:
        parts.append("Historically accurate clothing, architecture and objects; nothing modern")
    parts.append("No text, no captions, no subtitles, no watermark, no logos, no split screen")
    return ". ".join(parts) + "."


def generate(
    cfg: AiConfig,
    tasks: list[GenTask],
    *,
    state_file: Path,
    cancel: CancelToken | None = None,
    progress=None,
    wait_sec: float = WAIT_SEC,
    poll_scale: float = 1.0,
) -> list[GenTask]:
    """Сгенерировать кадры: отправка окном ≤ 20 задач, опрос из одного цикла, скачивание.

    Задачи и их id пишутся в state_file сразу после отправки: после сбоя «Продолжить»
    дожидается тех же задач (и Idempotency-Key тот же), второй раз не платим.
    Возвращает задачи с result (файл) или error.
    """
    if not tasks:
        return tasks
    state: dict[str, dict] = {}
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}

    def save() -> None:
        state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    client = RoyalClient(cfg.api_key)
    if cancel is not None:
        cancel.register(client)
    try:
        try:
            limits = client.account().get("limits") or {}
            window = max(1, min(MAX_IN_FLIGHT, int(limits.get("recommended_max_in_flight") or MAX_IN_FLIGHT)))
        except (AiGenError, httpx.HTTPError) as exc:
            log(f"AI-кадры: {exc} — генерацию пропускаю")
            for t in tasks:
                t.error = str(exc)
            return tasks

        todo = list(tasks)
        flying: dict[str, GenTask] = {}  # job_id → task
        next_poll: dict[str, float] = {}
        deadline = time.monotonic() + wait_sec
        stop_submit = False
        done = 0

        def model_for(t: GenTask) -> dict | None:
            return entry(cfg.image_model if t.kind == "image" else cfg.video_model)

        while todo or flying:
            if cancel is not None:
                cancel.check()
            # отправка: окно в полёте ≤ window; уже отправленные (после сбоя) — сразу в опрос
            while todo and len(flying) < window and not stop_submit:
                t = todo.pop(0)
                rec = state.get(t.key)
                if rec and rec.get("prompt") == t.prompt and rec.get("done") and t.dest.is_file():
                    t.result = t.dest  # уже сгенерировано и скачано в прошлый раз
                    t.meta.update(job_id=rec.get("job_id"))
                    done += 1
                    continue
                if rec and rec.get("prompt") == t.prompt and rec.get("job_id"):
                    flying[rec["job_id"]] = t
                    next_poll[rec["job_id"]] = 0.0
                    continue
                e = model_for(t)
                if e is None:
                    t.error = "модель не выбрана или пропала из прайса"
                    continue
                inp: dict[str, Any] = {"prompt": t.prompt, "aspect_ratio": "landscape"}
                res = resolution_of(e)
                if res:
                    inp["resolution"] = res
                secs = clip_seconds(e)
                if secs:
                    inp["duration_sec"] = secs
                idem = (rec or {}).get("idempotency") if rec and rec.get("prompt") == t.prompt else None
                idem = idem or str(uuid.uuid4())
                state[t.key] = {"prompt": t.prompt, "idempotency": idem, "kind": t.kind}
                save()
                try:
                    job = client.submit(str(e["api_name"]), inp, idem)
                except httpx.TransportError as exc:
                    t.error = f"нет связи с RoyalTechno: {exc}"
                    log(f"AI-кадры: {t.error}")
                    continue
                except AiGenError as exc:
                    if str(exc) == "queue_full":
                        todo.insert(0, t)
                        stop_submit = True  # ждём завершения своих задач
                        break
                    t.error = str(exc)
                    log(f"AI-кадры: задача не принята — {exc}")
                    if any(s in str(exc) for s in (" 401", " 402", " 403")):
                        # ключ/баланс/бан — дальше отправлять бессмысленно
                        for rest in todo:
                            rest.error = t.error
                        todo.clear()
                    continue
                state[t.key]["job_id"] = job["id"]
                save()
                flying[job["id"]] = t
                next_poll[job["id"]] = time.monotonic() + POLL_SEC[t.kind] * poll_scale

            if not flying:
                if todo and stop_submit:
                    time.sleep(5 * poll_scale)
                    stop_submit = False
                    continue
                break

            # опрос: одного цикла хватает на всё; у фото шаг 5 с, у видео 10 с
            now = time.monotonic()
            due = [jid for jid in flying if next_poll.get(jid, 0) <= now]
            for jid in due:
                t = flying[jid]
                try:
                    job = client.poll(jid)
                except (AiGenError, httpx.TransportError) as exc:
                    log(f"AI-кадры: опрос {jid}: {exc}")
                    next_poll[jid] = now + POLL_SEC[t.kind] * poll_scale
                    continue
                status = job.get("status")
                if status == "succeeded":
                    url = _output_url(job)
                    t.dest.parent.mkdir(parents=True, exist_ok=True)
                    if url and client.download(url, t.dest):
                        t.result = t.dest
                        t.meta.update(job_id=jid, cost_cents=job.get("cost_usd_cents"))
                        state.setdefault(t.key, {})["done"] = True
                        save()
                    else:
                        t.error = "готовый файл не скачался"
                    del flying[jid]
                    stop_submit = False
                    done += 1
                    if progress:
                        progress(done / len(tasks), f"AI-кадры: готово {done}/{len(tasks)}")
                elif status in ("failed", "canceled", "cancelled"):
                    err = job.get("error") or {}
                    t.error = str(err.get("message") or status)
                    log(f"AI-кадры: не сгенерировалось ({t.error})")
                    del flying[jid]
                    stop_submit = False
                    done += 1
                else:
                    next_poll[jid] = now + POLL_SEC[t.kind] * poll_scale
            if time.monotonic() > deadline:
                log(f"AI-кадры: время вышло, отменяю {len(flying)} незавершённых — остаются живые кадры")
                for jid, t in flying.items():
                    client.cancel(jid)
                    t.error = "не успело сгенерироваться"
                for t in todo:
                    t.error = "не успело сгенерироваться"
                break
            time.sleep(max(0.01, min(1.0, min(POLL_SEC.values()) * poll_scale / 5)))
    finally:
        client.close()
    ok = sum(1 for t in tasks if t.result)
    log(f"AI-кадры: сгенерировано {ok} из {len(tasks)}")
    return tasks


def _output_url(job: dict) -> str:
    out = job.get("output") or {}
    if out.get("url"):
        return str(out["url"])
    for key in ("images", "videos"):
        items = out.get(key) or []
        if items and items[0].get("url"):
            return str(items[0]["url"])
    return ""
