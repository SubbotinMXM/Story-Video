"""FastAPI: настройки, очередь задач, события, статика фронтенда."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine.core.config import PROJECT_ROOT, load_config, save_config
from engine.tts import PROVIDERS, get_provider, provider_options
from engine.slides.llm_client import DEFAULT_BASE_URL, DEFAULT_MODEL, LLMClient
from engine.slides.paths import RUNS_DIR
from engine.slides.resume import detect_stage
from server import dialogs
from server.events import hub
from server.jobs import get_manager
from server.runners import MAX_COPIES

WEB_DIST = PROJECT_ROOT / "web" / "dist"

app = FastAPI(title="Story Video")

STAGE_LABELS = {
    "none": "пусто",
    "script_saved": "текст сохранён",
    "tts_done": "озвучка готова",
    "timings_done": "тайминги готовы",
    "chunks_done": "тема готова",
    "media_done": "кадры готовы",
    "render_done": "ролик готов",
}


# ---------- мета / настройки ----------


def _rewrite_auto_model() -> str:
    """Какую модель встроенная нейросеть возьмёт для рерайта сама (поле модели пустое)."""
    from engine.slides import llm_models

    cfg = load_config()
    if str(cfg.get("llm_model") or "").strip():
        return str(cfg["llm_model"]).strip()
    base = str(cfg.get("llm_base_url") or "").strip() or DEFAULT_BASE_URL
    try:
        return (llm_models.chain(base, str(cfg.get("llm_api_key") or ""), "smart") or [""])[0]
    except Exception:
        return ""


def _llm_models() -> list[str]:
    """Модели сервиса по цене (для подсказки в настройках): с ключом — живой прайс, без — встроенный."""
    from engine.slides import llm_models

    cfg = load_config()
    base = str(cfg.get("llm_base_url") or "").strip() or DEFAULT_BASE_URL
    try:
        return llm_models.chain(base, str(cfg.get("llm_api_key") or ""), "light")
    except Exception:
        return [m for m, _i, _o in llm_models.FALLBACK_PRICES]


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    return {
        "llm_models": _llm_models(),
        "llm_default_model": DEFAULT_MODEL,
        "llm_default_base_url": DEFAULT_BASE_URL,
        "tts_providers": [p.describe() for p in PROVIDERS.values()],
        "max_copies": MAX_COPIES,
        "source_mix_default": _default_mix(),
        **_rewrite_meta(),
        **_cover_meta(),
        "ffmpeg": bool(shutil.which("ffmpeg") or Path("/opt/homebrew/bin/ffmpeg").exists()),
        "desktop": dialogs.window is not None,
    }


def _cover_meta() -> dict[str, Any]:
    from engine.publish.cover_prompts import DEFAULTS, ROLES

    return {
        "cover_default_prompts": DEFAULTS,
        "cover_prompt_roles": [{"id": k, "label": v[0], "hint": v[1], "variables": list(v[2])} for k, v in ROLES.items()],
    }


# модели Claude Code (псевдонимы CLI — всегда последняя версия семейства)
CLAUDE_MODELS = [
    {"id": "", "label": "По умолчанию подписки"},
    {"id": "opus", "label": "Opus — самый сильный, лимит тратится быстрее"},
    {"id": "sonnet", "label": "Sonnet — быстрее, лимита хватает надолго"},
    {"id": "haiku", "label": "Haiku — самый быстрый и простой"},
]


def _rewrite_meta() -> dict[str, Any]:
    from engine.rewrite.prompts import DEFAULT_PROMPTS, LANGUAGES, SECTIONS
    from engine.rewrite.run import PICKABLE

    return {
        "rewrite_default_prompts": DEFAULT_PROMPTS,
        "rewrite_sections": SECTIONS,
        "rewrite_languages": [{"id": k, "label": v[0].capitalize()} for k, v in LANGUAGES.items()],
        "rewrite_engines": PICKABLE,
        "rewrite_auto_model": _rewrite_auto_model(),
        "claude_models": CLAUDE_MODELS,
    }


def _default_mix() -> dict[str, int]:
    from engine.slides.source_mix import DEFAULT_MIX

    return dict(DEFAULT_MIX)


def _settings_out(cfg: dict[str, Any]) -> dict[str, Any]:
    out = dict(cfg)
    # у каждого сервиса — полный набор полей, даже если ещё не настраивали
    out["tts"] = {pid: provider_options(cfg, pid) for pid in PROVIDERS}
    return out


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return _settings_out(load_config())


@app.patch("/api/settings")
def patch_settings(patch: dict[str, Any]) -> dict[str, Any]:
    return _settings_out(save_config(patch))


@app.post("/api/tts/{provider_id}/refresh/{key}")
def refresh_tts_list(provider_id: str, key: str) -> dict[str, Any]:
    """«Обновить»: подтянуть список (шаблоны и т.п.) из кабинета сервиса и сохранить."""
    cfg = load_config()
    try:
        provider = get_provider(provider_id)
        if not any(f.key == key and f.kind == "remote_list" for f in provider.fields):
            raise ValueError(f"У {provider.label} нет списка {key}")
        opts = provider_options(cfg, provider_id)
        value = provider.refreshed(opts, key)
    except Exception as exc:
        raise HTTPException(400, str(exc) or type(exc).__name__) from None
    tts = dict(cfg.get("tts") or {})
    tts[provider_id] = {**opts, key: value}
    return {"count": len(value["items"]), "settings": _settings_out(save_config({"tts": tts}))}


# ---------- выкладка: профили AdsPower и каналы в них ----------


def _registry_out() -> dict[str, Any]:
    from dataclasses import asdict

    from engine.publish import registry

    from engine.publish import analytics

    profiles = sorted(registry.load().values(), key=lambda p: (int(p.number) if p.number.isdigit() else 10**9, p.name))
    stats = analytics.load()  # подписчики из «Аналитики»: различать каналы с одинаковыми названиями
    out = []
    for p in profiles:
        d = asdict(p)
        for ch in d["channels"]:
            st = stats.get(ch["id"])
            ch["subscribers"] = st.subscribers if st else None
            ch["stats_at"] = st.synced_at if st else 0
        out.append(d)
    return {"profiles": out}


@app.get("/api/publish/profiles")
def publish_profiles() -> dict[str, Any]:
    """Запомненные профили и каналы (без обращения к AdsPower)."""
    return _registry_out()


class ChannelLanguageIn(BaseModel):
    channel_id: str
    language: str = ""


@app.post("/api/publish/language")
def publish_language(body: ChannelLanguageIn) -> dict[str, Any]:
    """Язык канала (меняется в любой момент — смена ниши): ролик другого языка на канал не заливается."""
    from engine.publish import registry

    try:
        registry.set_language(body.channel_id, body.language)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return _registry_out()


@app.post("/api/publish/sync")
def publish_sync() -> dict[str, Any]:
    """«Синхронизировать с AdsPower»: актуальный список из AdsPower (имена, новые, удалённые)."""
    from engine.publish import registry
    from engine.publish.adspower import AdsPower, AdsPowerError

    cfg = load_config()
    try:
        with AdsPower(str(cfg.get("adspower_api_key") or ""), str(cfg.get("adspower_url") or "")) as ap:
            registry.sync(ap.profiles())
    except AdsPowerError as exc:
        raise HTTPException(400, str(exc)) from None
    return _registry_out()


@app.post("/api/publish/scan/{profile_id}")
def publish_scan(profile_id: str) -> dict[str, Any]:
    """«Найти каналы» — задача в очереди (ход, «Стоп», «Продолжить»). Уже ищет этот профиль — та же задача."""
    mgr = get_manager()
    for j in mgr.list():
        if j["kind"] == "scan" and j["params"].get("profile_id") == profile_id and j["status"] in ("queued", "running"):
            return {"job": j}
    try:
        job = mgr.create("scan", {"profile_id": profile_id})
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"job": mgr.add(job).public()}


@app.get("/api/analytics")
def analytics_get() -> dict[str, Any]:
    """Последние цифры каналов (из прошлой синхронизации) — и каналы, по которым их ещё нет."""
    from engine.publish import analytics, registry

    stored = analytics.load()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in registry.load().values():
        if p.missing:
            continue
        for ch in p.channels:
            if ch.id in seen:
                continue
            seen.add(ch.id)
            st = stored.get(ch.id)
            # динамика — относительно вчерашнего дня (последний снимок до сегодняшнего по МСК)
            prev = analytics.day_before(st.history, st.synced_at) if st and st.synced_at else None
            rows.append({
                "channel_id": ch.id, "name": ch.name, "handle": ch.handle, "profile_id": p.id,
                "profile": p.label(), "language": ch.language,
                "subscribers": st.subscribers if st else None, "views_48h": st.views_48h if st else None,
                "watch_hours": st.watch_hours if st else None, "views_total": st.views_total if st else None,
                "synced_at": st.synced_at if st else 0, "error": st.error if st else "",
                "prev": prev,
            })
    from engine.core.config import load_config as _cfg
    from engine.publish import analytics_auto

    st = analytics_auto.state()
    auto = {"next_at": analytics_auto.next_run(_cfg().get("analytics_auto")),
            "last_at": float(st.get("last_at") or 0), "last_job": str(st.get("last_job") or "")}
    return {"channels": rows, "auto": auto}


# ---------- проверки ключей ----------


@app.post("/api/ping/{service}")
def ping(service: str) -> dict[str, Any]:
    cfg = load_config()
    try:
        if service.startswith("tts:"):
            pid = service.split(":", 1)[1]
            return {"ok": True, "message": get_provider(pid).ping(provider_options(cfg, pid))}
        if service == "ai":
            from engine.slides.ai_gen import ping as ai_ping

            key = str(cfg.get("royaltechno_api_key") or "").strip()
            if not key:
                raise ValueError("Вставь ключ RoyalTechno")
            return {"ok": True, "message": ai_ping(key)}
        if service == "llm":
            key = str(cfg.get("llm_api_key") or "").strip()
            if not key:
                raise ValueError("Вставь ключ LLM")
            client = LLMClient(
                api_key=key,
                base_url=str(cfg.get("llm_base_url") or "").strip() or DEFAULT_BASE_URL,
                model=str(cfg.get("llm_model") or "").strip() or DEFAULT_MODEL,
            )
            try:
                return {"ok": True, "message": client.ping()}
            finally:
                client.close()
        if service == "adspower":
            from engine.publish.adspower import ping as adspower_ping

            return {"ok": True, "message": adspower_ping(str(cfg.get("adspower_api_key") or ""),
                                                          str(cfg.get("adspower_url") or ""))}
        if service == "stocks":
            from engine.slides.source_ping import ping_stocks

            return {
                "ok": True,
                "message": ping_stocks(
                    pexels=str(cfg.get("pexels_api_keys") or ""),
                    pixabay=str(cfg.get("pixabay_api_keys") or ""),
                    unsplash=str(cfg.get("unsplash_api_keys") or ""),
                    youtube=str(cfg.get("youtube_api_keys") or ""),
                ),
            }
    except Exception as exc:
        return {"ok": False, "message": str(exc) or type(exc).__name__}
    raise HTTPException(404, "Неизвестный сервис")


# ---------- задачи ----------


class JobIn(BaseModel):
    kind: str
    params: dict[str, Any] = {}


class ChannelsIn(BaseModel):
    channels: list[str]
    count: int = 5
    params: dict[str, Any] = {}  # как у рерайта по ссылке: раздел, промпт, язык, объём, чем переписывать…


class PollsIn(BaseModel):
    channel: str
    count: int = 3


@app.post("/api/polls/generate")
def polls_generate(body: PollsIn) -> dict[str, Any]:
    """Черновики опросов по теме канала — показать и поправить до запуска (публикует задача «polls»)."""
    from engine.publish import polls as pl
    from engine.publish.registry import resolve
    from engine.slides.stocks_base import parse_keys
    from server.runners import _llm_opt

    targets = resolve([body.channel]) if body.channel else []
    if not targets:
        raise HTTPException(400, "Выбери канал")
    t = targets[0]
    cfg = load_config()
    keys = parse_keys(cfg.get("youtube_api_keys") or cfg.get("youtube_api_key"))
    try:
        name, titles = pl.channel_titles(t.channel.id, keys)
        asked = [h.get("question", "") for h in pl.history(t.channel.id)]
        polls = pl.generate(name or t.channel.name, titles, max(1, min(pl.COUNT_MAX, body.count)),
                            language=t.channel.language, asked=asked, llm=_llm_opt(cfg))
    except Exception as exc:
        raise HTTPException(400, f"Опросы не получились: {str(exc).splitlines()[0][:300]}") from None
    return {"polls": [p.as_dict() for p in polls], "titles": titles[:12], "asked": len(asked),
            "limits": {"option": pl.OPTION_MAX, "question": pl.QUESTION_MAX, "options_max": pl.OPTIONS_MAX}}


@app.get("/api/polls/history")
def polls_history(channel: str) -> dict[str, Any]:
    from engine.publish import polls as pl
    from engine.publish.registry import split_key

    _pid, cid = split_key(channel)
    return {"items": list(reversed(pl.history(cid)))[:50]}


@app.get("/api/niches/runs")
def niches_runs() -> dict[str, Any]:
    """История поисков + встроенный список «вечнозелёных» ниш для выбора."""
    from engine.research import niches

    return {"runs": niches.runs(), "evergreen": niches.EVERGREEN}


@app.delete("/api/niches/runs/{run_id}")
def niches_run_delete(run_id: str) -> dict[str, Any]:
    """Убрать поиск из истории (только из журнала программы)."""
    from engine.research import niches

    if not niches.remove(run_id):
        raise HTTPException(404, "Такого поиска уже нет")
    return {"runs": niches.runs()}


@app.delete("/api/niches/runs/{run_id}/channels/{channel_id}")
def niches_channel_delete(run_id: str, channel_id: str) -> dict[str, Any]:
    """Убрать один канал из выдачи поиска."""
    from engine.research import niches

    if not niches.remove(run_id, channel_id):
        raise HTTPException(404, "Такого канала в поиске уже нет")
    return {"runs": niches.runs()}


@app.get("/api/jobs")
def list_jobs() -> list[dict[str, Any]]:
    return get_manager().list()


@app.post("/api/rewrite/channels")
def rewrite_channels(body: ChannelsIn) -> dict[str, Any]:
    """«По каналам»: с каждого канала — count следующих залетевших роликов, которых ещё не брали → рерайты в очередь."""
    from engine.rewrite.channels import ChannelError, pick
    from engine.slides.stocks_base import parse_keys

    urls = [u.strip() for u in body.channels if u.strip()]
    if not urls:
        raise HTTPException(400, "Укажи хотя бы один канал")
    count = max(1, min(50, int(body.count or 1)))
    cfg = load_config()
    keys = parse_keys(cfg.get("youtube_api_keys") or cfg.get("youtube_api_key"))
    section = str(body.params.get("section") or "broll")
    states = _job_states()
    items: list[JobIn] = []
    report: list[dict[str, Any]] = []
    chosen: set[str] = set()  # один канал двумя ссылками (@имя и /channel/UC…) — ролики не повторяются
    for url in urls:
        try:
            got = pick(url, count, section=section, keys=keys, jobs=states, exclude=chosen)
        except ChannelError as exc:
            report.append({"url": url, "error": str(exc)})
            continue
        report.append({"url": url, "channel": got["channel"], "taken": len(got["videos"]), "left": got["left"],
                       "titles": [v.title for v in got["videos"]]})
        for v in got["videos"]:
            chosen.add(v.id)
            items.append(JobIn(kind="rewrite", params={**body.params, "url": v.url, "video_title": v.title,
                                                       "channel_title": got["channel"], "force": True}))
    if not items:
        errors = "; ".join(r["error"] for r in report if r.get("error"))
        raise HTTPException(400, errors or "С этих каналов брать больше нечего — все подходящие ролики уже переписаны")
    jobs = _enqueue(items)
    _reserve_links(jobs)
    return {"jobs": jobs, "channels": report}


@app.get("/api/rewrite/channels")
def rewrite_channels_stats() -> dict[str, Any]:
    """Счётчики по каналам конкурентов: сколько взято и сколько подходящих осталось (на момент проверки)."""
    from engine.rewrite.channels import channel_stats

    return {"channels": list(channel_stats().values())}


def _job_states() -> dict[str, str]:
    return {j["id"]: j["status"] for j in get_manager().list()}


@app.post("/api/jobs")
def add_jobs(items: list[JobIn]) -> list[dict[str, Any]]:
    # ролик по ссылке уже переписывался — спросить (409); «всё равно переписать» — params.force
    from engine.rewrite.channels import duplicates, video_id

    urls = [str(i.params.get("url") or "") for i in items if i.kind == "rewrite" and not i.params.get("force")]
    dups = duplicates([u for u in urls if u], _job_states())
    seen: dict[str, str] = {}
    for u in urls:  # одна и та же ссылка дважды в одном запуске
        vid = video_id(u)
        if vid and vid in seen and not any(d["id"] == vid for d in dups):
            dups.append({"url": u, "id": vid, "title": u, "status": "дважды в этом запуске"})
        elif vid:
            seen[vid] = u
    if dups:
        names = "\n".join(f"• {d['title']} — {d['status']}" for d in dups)
        raise HTTPException(409, f"Эти ролики уже переписывались:\n{names}")
    out = _enqueue(items)
    _reserve_links(out)
    return out


def _reserve_links(jobs: list[dict[str, Any]]) -> None:
    """Ролики по ссылкам — в журнал переписанного (с номером задачи): повторно не возьмутся с канала."""
    from engine.rewrite.channels import reserve, video_id

    for j in jobs:
        p = j.get("params") or {}
        vid = video_id(str(p.get("url") or "")) if j.get("kind") == "rewrite" else ""
        if vid:
            reserve(vid, title=str(p.get("video_title") or ""), channel=str(p.get("channel_title") or ""),
                    section=str(p.get("section") or "broll"), job=j["id"])


def _enqueue(items: list[JobIn]) -> list[dict[str, Any]]:
    # сначала проверяем всё, потом ставим — чтобы не было «половины очереди»
    from server.runners import SETTINGS_KEY, snapshot_settings

    mgr = get_manager()
    cfg = load_config()
    snap = snapshot_settings(cfg)
    try:
        # снимок настроек на момент постановки: задача идёт по нему до конца, что бы ни поменяли потом
        jobs = [mgr.create(i.kind, {**i.params, SETTINGS_KEY: i.params.get(SETTINGS_KEY) or snap}) for i in items]
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    _publish_plan(cfg, jobs)
    from server.runners import royal_warning

    notice = royal_warning(cfg, jobs)  # денег на генерацию может не хватить — предупредить сразу
    out = [mgr.add(j).public() for j in jobs]
    if notice and out:
        out[0]["notice"] = notice
    return out


def _publish_plan(cfg: dict[str, Any], jobs: list) -> None:
    """Выкладка раздела включена: даты из календаря — на этот запуск (ролики берут их по порядку
    готовности), после запуска календарь очищается — следующий запуск их не займёт."""
    sections = {"broll": ("broll",), "slides": ("slides_full",)}
    for section, kinds in sections.items():
        targets = [j for j in jobs if j.kind in kinds
                   or (j.kind == "rewrite" and j.params.get("section", "broll") == section)]
        if not targets or not cfg.get(f"{section}_publish"):
            continue
        from engine.publish.schedule import create_plan

        dates_key = f"{section}_publish_dates"
        plan_id = create_plan(list(cfg.get(dates_key) or []), str(cfg.get(f"{section}_publish_time") or ""))
        for j in targets:
            j.params.setdefault("publish_plan", plan_id)
        if cfg.get(dates_key):
            save_config({dates_key: []})


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, bool]:
    return {"ok": get_manager().cancel(job_id)}


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str) -> dict[str, Any]:
    try:
        return get_manager().resume(job_id).public()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


class MoveIn(BaseModel):
    to: str


@app.post("/api/jobs/{job_id}/move")
def move_job(job_id: str, body: MoveIn) -> dict[str, bool]:
    try:
        return {"ok": get_manager().move(job_id, body.to)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/jobs/{job_id}/reset")
def reset_job(job_id: str) -> dict[str, bool]:
    try:
        return {"ok": get_manager().reset(job_id)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/jobs/clear")
def clear_jobs() -> dict[str, bool]:
    get_manager().clear_finished()
    return {"ok": True}


@app.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    queue = hub.subscribe()

    async def stream():
        try:
            snapshot = {
                "type": "snapshot",
                "jobs": get_manager().list(),
                "logs": list(hub.logs),
            }
            yield f"data: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
            while not await request.is_disconnected():
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {payload}\n\n"
                except TimeoutError:
                    yield ": ping\n\n"
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------- запуски слайдов ----------


@app.get("/api/runs")
def list_runs() -> list[dict[str, Any]]:
    if not RUNS_DIR.is_dir():
        return []
    out = []
    dirs = sorted((p for p in RUNS_DIR.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    for run in dirs[:60]:
        stage = detect_stage(run)
        script = run / "script.txt"
        snippet = ""
        if script.is_file():
            snippet = " ".join(script.read_text(encoding="utf-8", errors="replace").split())[:140]
        out.append(
            {
                "name": run.name,
                "path": str(run),
                "stage": stage,
                "stage_label": STAGE_LABELS.get(stage, stage),
                "updated": datetime.fromtimestamp(run.stat().st_mtime).isoformat(timespec="seconds"),
                "snippet": snippet,
                "has_final": stage == "render_done",
            }
        )
    return out


# ---------- файлы ----------


class PickIn(BaseModel):
    kind: str = "any"
    title: str = ""
    multiple: bool = False


@app.post("/api/pick")
def pick(body: PickIn) -> dict[str, Any]:
    paths = dialogs.pick(body.kind, title=body.title, multiple=body.multiple)
    out: dict[str, Any] = {"paths": paths}
    if body.kind == "text":
        out["texts"] = [
            {"name": Path(p).name, "text": Path(p).read_text(encoding="utf-8", errors="replace").strip()}
            for p in paths
            if Path(p).is_file()
        ]
    return out


class PathIn(BaseModel):
    path: str


@app.post("/api/reveal")
def reveal(body: PathIn) -> dict[str, bool]:
    path = Path(body.path).expanduser()
    if not path.exists():
        raise HTTPException(404, "Файл не найден")
    dialogs.reveal(str(path))
    return {"ok": True}


@app.post("/api/open")
def open_file(body: PathIn) -> dict[str, bool]:
    """Открыть текст рерайта в редакторе по умолчанию (только Desktop/Рерайты и data/rewrites)."""
    from engine.core.config import DATA_DIR
    from engine.slides.paths import DESKTOP

    path = Path(body.path).expanduser()
    allowed = [(DESKTOP / "Рерайты").resolve(), (DATA_DIR / "rewrites").resolve()]
    try:
        real = path.resolve()
    except OSError:
        raise HTTPException(404, "Файл не найден") from None
    if not path.is_file() or not any(base in real.parents for base in allowed):
        raise HTTPException(404, "Файл не найден")
    subprocess.run(["open", str(real)], check=False)
    return {"ok": True}


@app.get("/api/claude/status")
def claude_status() -> dict[str, Any]:
    """Claude Code: установлен ли, выполнен ли вход и в какой аккаунт (почта, тариф)."""
    from engine.rewrite.engines import claude_account

    return claude_account()


@app.post("/api/claude/check")
def claude_check() -> dict[str, Any]:
    """Пробный запрос к Claude Code: установлен ли и выполнен ли вход."""
    from engine.core.config import load_config
    from engine.rewrite.engines import RewriteError, run_claude

    try:
        answer = run_claude("Ответь одним словом: готово", model=str(load_config().get("claude_model") or ""), timeout=120)
    except RewriteError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": f"Claude отвечает: {answer.strip()[:60]}"}


class SubLoginIn(BaseModel):
    action: str = "login"  # login — войти; switch — выйти и войти в другой аккаунт; install — поставить и войти


def _terminal(cmd: str) -> None:
    """Открыть Терминал с командой: вход в аккаунт идёт через браузер, Терминал показывает ход и ссылку."""
    safe = cmd.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "Terminal" to do script "{safe}"'
    subprocess.run(["osascript", "-e", script, "-e", 'tell application "Terminal" to activate'], check=False)


_DONE_HINT = "; echo; echo 'Готово — вернись в Story Video и нажми «Обновить»'"


@app.post("/api/claude/terminal")
def claude_terminal(body: SubLoginIn | None = None) -> dict[str, bool]:
    """Claude Code: войти / сменить аккаунт / установить. Вход — подписка Claude (браузер)."""
    from engine.rewrite.engines import claude_binary

    action = (body.action if body else "login") or "login"
    path = claude_binary()
    if not path:
        _terminal("curl -fsSL https://claude.ai/install.sh | bash && ~/.local/bin/claude auth login --claudeai" + _DONE_HINT)
        return {"ok": True}
    q = f"'{path}'"
    if action == "switch":
        _terminal(f"{q} auth logout; {q} auth login --claudeai" + _DONE_HINT)
    elif action == "logout":
        subprocess.run([path, "auth", "logout"], capture_output=True, timeout=30, check=False)
    else:
        _terminal(f"{q} auth login --claudeai" + _DONE_HINT)
    return {"ok": True}


@app.get("/api/codex/status")
def codex_status_api() -> dict[str, Any]:
    """Codex (ChatGPT): установлен ли, выполнен ли вход, аккаунт и модели."""
    from engine.rewrite.engines import codex_models, codex_status

    st = codex_status()
    return {**st, "models": codex_models() if st.get("installed") else []}


@app.post("/api/codex/check")
def codex_check() -> dict[str, Any]:
    from engine.core.config import load_config
    from engine.rewrite.engines import RewriteError, run_codex

    try:
        answer = run_codex("Ответь одним словом: готово", model=str(load_config().get("codex_model") or ""), timeout=180)
    except RewriteError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": f"Codex отвечает: {answer.strip()[:60]}"}


def _brew() -> str:
    for b in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if Path(b).is_file():
            return b
    return ""


@app.post("/api/codex/terminal")
def codex_terminal(body: SubLoginIn | None = None) -> dict[str, bool]:
    """Codex: войти через ChatGPT / сменить аккаунт / установить (Homebrew, иначе npm)."""
    from engine.rewrite.engines import codex_binary

    action = (body.action if body else "login") or "login"
    path = codex_binary()
    if not path:
        brew = _brew()
        install = f"'{brew}' install --cask codex" if brew else "npm install -g @openai/codex"
        _terminal(f"{install} && codex login" + _DONE_HINT)
        return {"ok": True}
    q = f"'{path}'"
    if action == "switch":
        _terminal(f"{q} logout; {q} login" + _DONE_HINT)
    elif action == "logout":
        subprocess.run([path, "logout"], capture_output=True, timeout=30, check=False)
    else:
        _terminal(f"{q} login" + _DONE_HINT)
    return {"ok": True}


@app.post("/api/check-path")
def check_path(body: PathIn) -> dict[str, Any]:
    raw = body.path.strip()
    path = Path(raw).expanduser()
    return {"exists": bool(raw) and path.exists(), "is_dir": path.is_dir(), "is_file": path.is_file()}


# ---------- фронтенд ----------

if (WEB_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")


# ---------- AI-кадры ----------


@app.get("/api/ai/models")
def ai_models() -> dict[str, Any]:
    from engine.slides.ai_gen import AI_SHARE, AI_SHARE_MAX, model_options

    return {"share": AI_SHARE, "share_max": AI_SHARE_MAX, **model_options()}


# ---------- фоновая музыка ----------


@app.get("/api/music")
def music_list() -> dict[str, Any]:
    from engine.slides.music import catalog, claimed

    cat = catalog()
    bad = claimed()
    return {
        "groups": cat["groups"],
        "tracks": [{"id": t["id"], "title": t["title"], "group": t["group"], "duration": t["duration"],
                    "claimed": t["id"] in bad} for t in cat["tracks"]],
    }


@app.get("/api/music/{track_id}")
def music_file(track_id: str) -> FileResponse:
    from engine.slides.music import track_path

    path = track_path(track_id)
    if path is None:
        raise HTTPException(404, "Нет такого трека")
    return FileResponse(path, media_type="audio/mpeg")


@app.get("/api/thumbnail")
def thumbnail_file(path: str) -> FileResponse:
    """Готовое превью для показа на вкладке «Превью» — только файлы «Превью …jpg» на Desktop."""
    from engine.slides.paths import DESKTOP

    p = Path(path).expanduser()
    try:
        inside = p.resolve().parent == DESKTOP.resolve()
    except OSError:
        inside = False
    if not inside or not p.name.startswith("Превью") or p.suffix.lower() != ".jpg" or not p.is_file():
        raise HTTPException(404, "Нет такого превью")
    return FileResponse(p, media_type="image/jpeg")


class CoverUploadIn(BaseModel):
    data: str  # data:image/...;base64,…
    name: str = ""


@app.post("/api/covers/upload")
def cover_upload(body: CoverUploadIn) -> dict[str, str]:
    """Образец для раздела «Обложки» (вставлен из буфера или перетащен): сохраняем — в задачу идёт путь."""
    import base64
    import hashlib

    from engine.publish.cover import output_root

    m = re.match(r"data:image/[\w.+-]+;base64,(.+)$", body.data.strip(), re.S)
    if not m:
        raise HTTPException(400, "Это не картинка")
    try:
        raw = base64.b64decode(m.group(1), validate=False)
    except ValueError:
        raise HTTPException(400, "Картинка повреждена") from None
    if len(raw) > 25 * 1024 * 1024:
        raise HTTPException(400, "Картинка больше 25 МБ")
    from engine.publish.cover import to_jpeg

    try:
        jpeg = to_jpeg(raw, 2560)
    except Exception:
        raise HTTPException(400, "Не получилось прочитать картинку") from None
    folder = output_root() / "образцы"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"образец_{hashlib.sha1(jpeg).hexdigest()[:10]}.jpg"
    path.write_bytes(jpeg)
    return {"path": str(path)}


@app.get("/api/cover-image")
def cover_image(path: str) -> FileResponse:
    """Картинки раздела «Обложки» и обложки выкладки — только из папки Desktop/Обложки и папок обложек роликов."""
    from engine.publish.cover import output_root

    p = Path(path).expanduser()
    try:
        rp = p.resolve()
        ok = rp.is_relative_to(output_root().resolve()) or rp.parent.name == "обложки"
    except OSError:
        ok = False
    if not ok or p.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp") or not p.is_file():
        raise HTTPException(404, "Нет такой картинки")
    media = "image/png" if p.suffix.lower() == ".png" else "image/webp" if p.suffix.lower() == ".webp" else "image/jpeg"
    return FileResponse(p, media_type=media)


@app.get("/{full_path:path}", include_in_schema=False)
def spa(full_path: str):
    if full_path.startswith("api/"):
        raise HTTPException(404)
    index = WEB_DIST / "index.html"
    if not index.is_file():
        raise HTTPException(503, "Фронтенд не собран: cd web && npm install && npm run build")
    candidate = WEB_DIST / full_path
    if full_path and candidate.is_file() and WEB_DIST in candidate.resolve().parents:
        return FileResponse(candidate)
    return FileResponse(index)
