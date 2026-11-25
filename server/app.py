"""FastAPI: настройки, очередь задач, события, статика фронтенда."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine.core import netguard
from engine.core.config import PROJECT_ROOT, load_config, save_config
from engine.tts import PROVIDERS, get_provider, provider_options
from engine.slides.llm_client import DEFAULT_BASE_URL, DEFAULT_MODEL, LLMClient
from engine.slides.paths import RUNS_DIR
from engine.slides.resume import detect_stage, drop_stock, stock_bytes
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


@app.get("/api/whats-new")
def whats_new() -> dict[str, Any]:
    """Окно «Что нового»: выпуски, которых человек ещё не видел, и вся история версий."""
    from engine.core import changelog

    all_releases = changelog.releases()
    return {
        "version": all_releases[0]["version"] if all_releases else "",
        "unseen": changelog.unseen(str(load_config().get("last_seen_version") or "")),
        "history": all_releases,
    }


@app.post("/api/whats-new/seen")
def whats_new_seen() -> dict[str, str]:
    """Человек закрыл окно: текущая версия — просмотренная, до следующего обновления не показываем."""
    from engine.core import changelog

    version = changelog.current()
    if version:
        save_config({"last_seen_version": version})
    return {"version": version}


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


class ChannelPlaylistIn(BaseModel):
    channel_id: str
    playlist: str = ""


@app.post("/api/publish/playlist")
def publish_playlist(body: ChannelPlaylistIn) -> dict[str, Any]:
    """Плейлист для выкладки, когда плейлистов на канале несколько (пусто — не выбирать)."""
    from engine.publish import registry

    try:
        registry.set_playlist(body.channel_id, body.playlist)
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
                "scheduled": st.scheduled if st else None,
                "public": st.public if st else None, "unlisted": st.unlisted if st else None,
                "private": st.private if st else None,
                "synced_at": st.synced_at if st else 0, "error": st.error if st else "",
                "prev": prev,
            })
    from engine.core.config import load_config as _cfg
    from engine.publish import analytics_auto

    st = analytics_auto.state()
    auto = {"next_at": analytics_auto.next_run(_cfg().get("analytics_auto")),
            "last_at": float(st.get("last_at") or 0), "last_job": str(st.get("last_job") or "")}
    return {"channels": rows, "auto": auto}


# ---------- обновление программы ----------


class UpdateIn(BaseModel):
    when: str = "now"  # now — сразу (идущая работа прервётся), idle — когда очередь освободится


def _update_out(st: dict[str, Any]) -> dict[str, Any]:
    from engine import updater
    from engine.core.config import load_config as _cfg

    s = updater.state()
    return {**st, "busy": get_manager().busy(), "pending": updater.pending(),
            "next_at": updater.next_run(_cfg().get("update_auto")), "last_applied": float(s.get("last_applied") or 0),
            "last_error": str(s.get("last_error") or ""), "last_error_at": float(s.get("last_error_at") or 0)}


@app.get("/api/update")
def update_status() -> dict[str, Any]:
    """Что известно об обновлениях (без обращения к серверу)."""
    from engine import updater

    return _update_out(updater.status())


@app.post("/api/update/check")
def update_check() -> dict[str, Any]:
    from engine import updater

    st = updater.status(fetch=True)
    updater._update_state(checked_at=st.get("checked_at") or 0)
    return _update_out(st)


@app.post("/api/update/apply")
def update_apply(body: UpdateIn) -> dict[str, Any]:
    """Подтянуть обновление и перезапустить программу. when=idle — отложить, пока очередь не освободится."""
    from engine import updater

    if body.when == "idle":
        updater.set_pending("update", "user")
        return {**_update_out(updater.status()), "started": False}
    try:
        st = updater.apply_and_restart(get_manager())
    except updater.UpdateError as exc:
        raise HTTPException(400, str(exc)) from None
    return {**_update_out(st), "started": bool(st.get("behind"))}


@app.post("/api/update/cancel")
def update_cancel() -> dict[str, Any]:
    from engine import updater

    updater.clear_pending()
    return _update_out(updater.status())


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


class FavoriteIn(BaseModel):
    run_id: str
    channel_id: str


@app.get("/api/niches/favorites")
def niches_favorites() -> dict[str, Any]:
    from engine.research import niches

    return {"favorites": niches.favorites()}


@app.post("/api/niches/favorites")
def niches_favorite_add(body: FavoriteIn) -> dict[str, Any]:
    """Канал из результатов поиска — в избранное (остаётся там, пока человек сам не удалит)."""
    from engine.research import niches

    if not niches.add_favorite(body.run_id, body.channel_id):
        raise HTTPException(404, "Такого канала в этом поиске уже нет")
    return {"favorites": niches.favorites()}


@app.delete("/api/niches/favorites/{channel_id}")
def niches_favorite_delete(channel_id: str) -> dict[str, Any]:
    from engine.research import niches

    if not niches.remove_favorite(channel_id):
        raise HTTPException(404, "Этого канала нет в избранном")
    return {"favorites": niches.favorites()}


@app.delete("/api/niches/runs/{run_id}")
def niches_run_delete(run_id: str) -> dict[str, Any]:
    """Убрать поиск из истории (только из журнала программы)."""
    from engine.research import niches

    if not niches.remove(run_id):
        raise HTTPException(404, "Такого поиска уже нет")
    return {"runs": niches.runs()}


@app.get("/api/competition/runs")
def competition_runs() -> dict[str, Any]:
    """История проверок «Конкуренция по языкам» (новые сверху)."""
    from engine.research import competition

    return {"runs": competition.runs()}


@app.delete("/api/competition/runs/{run_id}")
def competition_run_delete(run_id: str) -> dict[str, Any]:
    from engine.research import competition

    if not competition.remove(run_id):
        raise HTTPException(404, "Такой проверки уже нет")
    return {"runs": competition.runs()}


@app.delete("/api/niches/runs/{run_id}/channels/{channel_id}")
def niches_channel_delete(run_id: str, channel_id: str) -> dict[str, Any]:
    """Убрать один канал из выдачи поиска."""
    from engine.research import niches

    if not niches.remove(run_id, channel_id):
        raise HTTPException(404, "Такого канала в поиске уже нет")
    return {"runs": niches.runs()}


# ---------- итоговые отчёты по пачкам задач ----------


class FixIn(BaseModel):
    jobs: list[str] = []  # упавшие задачи, которые продолжить с места падения
    collabs: bool = False  # принять ждущие соавторства сейчас


@app.get("/api/reports")
def reports_list() -> list[dict[str, Any]]:
    from server import report

    return report.listing()


@app.get("/api/reports/unseen")
def reports_unseen() -> dict[str, Any] | None:
    """Последний отчёт, который пользователь ещё не открывал (приложение было закрыто, когда очередь закончила)."""
    from server import report

    return report.unseen()


@app.get("/api/reports/{report_id}")
def reports_get(report_id: str) -> dict[str, Any]:
    from server import report

    found = report.get(report_id)
    if found is None:
        raise HTTPException(404, "Отчёт не найден (старше 5 дней удаляются)")
    return found


@app.post("/api/reports/{report_id}/seen")
def reports_seen(report_id: str) -> dict[str, bool]:
    from server import report

    report.mark_seen(None if report_id == "all" else report_id)
    return {"ok": True}


@app.post("/api/reports/fix")
def reports_fix(body: FixIn) -> dict[str, Any]:
    """«Исправить автоматически»: продолжить упавшие задачи с места падения и/или принять соавторства сейчас."""
    manager = get_manager()
    resumed: list[str] = []
    errors: list[str] = []
    for job_id in dict.fromkeys(body.jobs):
        try:
            manager.resume(job_id)
            resumed.append(job_id)
        except ValueError as exc:
            errors.append(str(exc))
    queued_collabs = False
    if body.collabs:
        if any(j.get("kind") == "collabs" and j.get("status") in ("queued", "running") for j in manager.list()):
            queued_collabs = True  # уже идёт
        else:
            try:
                manager.add(manager.create("collabs", {"auto": False}))
                queued_collabs = True
            except Exception as exc:  # noqa: BLE001 — например, не задан ключ AdsPower
                errors.append(f"Принять соавторства не получилось: {exc}")
    return {"resumed": resumed, "collabs": queued_collabs, "errors": errors}


@app.get("/api/jobs")
def list_jobs() -> list[dict[str, Any]]:
    return get_manager().list()


@app.post("/api/rewrite/channels")
def rewrite_channels(body: ChannelsIn) -> dict[str, Any]:
    """«По каналам»: ровно count следующих залетевших роликов из всех каналов-доноров (по очереди с каждого, сколько
    каналов — неважно), которых ещё не брали → рерайты в очередь."""
    from engine.rewrite.channels import ChannelError, pick, spread
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
    entries: list[dict[str, Any]] = []
    chosen: set[str] = set()  # один канал двумя ссылками (@имя и /channel/UC…) — ролики не повторяются
    for url in urls:
        try:
            # с каждого канала — кандидаты «с запасом»; spread сравнит каналы и возьмёт лучшие (всего ровно count)
            got = pick(url, count, section=section, keys=keys, jobs=states, exclude=chosen)
        except ChannelError as exc:
            report.append({"url": url, "error": str(exc)})
            continue
        chosen.update(v.id for v in got["videos"])  # дубли между ссылками — только по кандидатам
        entry = {**got, "url": url}
        entries.append(entry)
    for ent, v in spread(entries, count):
        items.append(JobIn(kind="rewrite", params={**body.params, "url": v.url, "video_title": v.title,
                                                   "channel_title": ent["channel"], "force": True}))
    for ent in entries:
        report.append({"url": ent["url"], "channel": ent["channel"], "taken": ent["taken"], "left": ent["left"],
                       "titles": [v.title for v in ent["videos"][:ent["taken"]]]})
    if not items:
        errors = "; ".join(r["error"] for r in report if r.get("error"))
        raise HTTPException(400, errors or "С этих каналов брать больше нечего — все подходящие ролики уже переписаны")
    notes: list[str] = []
    if len(items) < count:
        notes.append(f"Подходящих роликов нашлось {len(items)} из {count} — столько и поставлено: на каналах-донорах "
                     "больше нет ещё не переписанных подходящих (от 10 минут для рассказов, от 8 для слайдов, не Shorts и не трансляции). "
                     "Добавь ещё канал-донор — недостающее возьмётся с него.")
    jobs = _enqueue(items)
    _reserve_links(jobs)
    return {"jobs": jobs, "channels": report, "notes": notes}


class TemplateIn(BaseModel):
    name: str
    section: str = "broll"


class TemplateRename(BaseModel):
    name: str


class TemplateLaunchIn(BaseModel):
    count: int = 5
    start: str = ""  # пусто — после последнего запланированного на канале; иначе дата первого ролика (YYYY-MM-DD)
    step: int = 0  # дней между роликами; 0 — как в шаблоне
    channels: list[str] | None = None  # каналы выкладки только для этого запуска (None — как в шаблоне)
    save_channels: bool = False  # и сохранить их в шаблон


def _template_view(tpl: dict[str, Any]) -> dict[str, Any]:
    """Шаблон для интерфейса: настройки + короткая сводка для карточки."""
    s = tpl.get("settings") or {}
    sec = tpl["section"]
    links = _competitor_links(str((s.get("rewrite_channels") or {}).get(sec) or ""))
    return {**tpl, "summary": {
        "publish": bool(s.get(f"{sec}_publish")),
        "channels": list(s.get(f"{sec}_publish_channels") or []),
        "step": max(1, int(s.get(f"{sec}_publish_step") or 1)),
        "time": str(s.get(f"{sec}_publish_time") or ""),
        "competitors": links,
        "ai": bool(s.get("ai_gen_enabled")) if sec == "slides" else False,
    }}


def _competitor_links(text: str) -> list[str]:
    """Каналы конкурентов из поля «Ссылки на каналы» (как в интерфейсе: через пробел/запятую/строку)."""
    return [u for u in (x.strip() for x in re.split(r"[\s,]+", text or "")) if u and re.search(r"youtube\.com|^@", u)]


@app.get("/api/templates")
def templates_list() -> list[dict[str, Any]]:
    from server import templates as tp

    return [_template_view(t) for t in tp.listing()]


@app.post("/api/templates")
def templates_create(body: TemplateIn) -> dict[str, Any]:
    """«Сохранить как шаблон»: текущие настройки раздела → новый шаблон."""
    from server import templates as tp

    try:
        return _template_view(tp.create(body.name, body.section, load_config()))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


def _template_or_404(tid: str) -> dict[str, Any]:
    from server import templates as tp

    tpl = tp.get(tid)
    if tpl is None:
        raise HTTPException(404, "Такого шаблона уже нет")
    return tpl


@app.patch("/api/templates/{tid}")
def templates_rename(tid: str, body: TemplateRename) -> dict[str, Any]:
    from server import templates as tp

    _template_or_404(tid)
    try:
        return _template_view(tp.rename(tid, body.name))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/templates/{tid}/save")
def templates_save(tid: str) -> dict[str, Any]:
    """«Сохранить в шаблон»: текущие настройки раздела становятся содержимым шаблона."""
    from server import templates as tp

    _template_or_404(tid)
    return _template_view(tp.recapture(tid, load_config()))


@app.get("/api/templates/{tid}/base")
def templates_base(tid: str) -> dict[str, Any]:
    """Содержимое шаблона как оно сохранено — чтобы при правке показать «было → стало»."""
    tpl = _template_or_404(tid)
    return {"id": tpl["id"], "name": tpl["name"], "section": tpl["section"], "settings": tpl.get("settings") or {}}


@app.post("/api/templates/{tid}/apply")
def templates_apply(tid: str) -> dict[str, Any]:
    """«Изменить»: загрузить шаблон в настройки (поправить на привычных экранах, потом «Сохранить в шаблон»)."""
    from server import templates as tp

    tpl = _template_or_404(tid)
    return _settings_out(save_config(tp.patch_for(load_config(), tpl)))


@app.post("/api/templates/{tid}/copy")
def templates_copy(tid: str) -> dict[str, Any]:
    from server import templates as tp

    _template_or_404(tid)
    return _template_view(tp.copy_of(tid))


@app.delete("/api/templates/{tid}")
def templates_delete(tid: str) -> dict[str, bool]:
    from server import templates as tp

    if not tp.delete(tid):
        raise HTTPException(404, "Такого шаблона уже нет")
    return {"ok": True}


@app.post("/api/templates/{tid}/launch")
def templates_launch(tid: str, body: TemplateLaunchIn) -> dict[str, Any]:
    """«Сделать N роликов по шаблону»: с каналов-конкурентов шаблона берутся N следующих ещё не переписанных роликов
    (каналы — по очереди), для каждого — рерайт → ролик → выкладка по настройкам шаблона."""
    from engine.publish.schedule import clamp_step
    from engine.rewrite.channels import ChannelError, pick, spread
    from engine.rewrite.prompts import DEFAULT_PROMPTS
    from engine.slides.stocks_base import parse_keys
    from server import templates as tp

    tpl = _template_or_404(tid)
    sec = tpl["section"]
    cfg = tp.merged(load_config(), tpl)
    count = max(1, min(50, int(body.count or 1)))
    step = clamp_step(body.step or cfg.get(f"{sec}_publish_step") or 1)
    cfg[f"{sec}_publish_step"] = step
    notes: list[str] = []
    if body.channels is not None:
        cfg[f"{sec}_publish_channels"] = [c for c in body.channels if c]
        if body.save_channels:
            tp.set_setting(tid, {f"{sec}_publish_channels": cfg[f"{sec}_publish_channels"]})
    if cfg.get(f"{sec}_publish"):
        if not cfg.get(f"{sec}_publish_channels"):
            raise HTTPException(400, "В шаблоне не выбраны каналы выкладки — отметь их или выключи выкладку в шаблоне")
    else:
        notes.append("Выкладка в шаблоне выключена — ролики только соберутся")
    if body.start.strip():
        try:
            first = date.fromisoformat(body.start.strip())
        except ValueError:
            raise HTTPException(400, "Дата начала — в виде ГГГГ-ММ-ДД") from None
        if first < datetime.now().date():
            raise HTTPException(400, "Дата начала уже прошла")
        cfg[f"{sec}_publish_dates"] = [(first + timedelta(days=step * i)).isoformat() for i in range(count)]
    else:
        cfg[f"{sec}_publish_dates"] = []  # после последнего запланированного на канале, с шагом
    urls = _competitor_links(str((cfg.get("rewrite_channels") or {}).get(sec) or ""))
    if not urls:
        raise HTTPException(400, "В шаблоне нет каналов-конкурентов — впиши их в «Источник → Ссылки на каналы» и сохрани шаблон")
    prompts = [p for p in (cfg.get("rewrite_prompts") or DEFAULT_PROMPTS) if p.get("section") == sec]
    chosen_prompt = (cfg.get("rewrite_prompt") or {}).get(sec)
    common = {"section": sec, "prompt_id": chosen_prompt if any(p.get("id") == chosen_prompt for p in prompts)
              else (prompts[0]["id"] if prompts else ""),
              "language": cfg.get("rewrite_language"), "words": cfg.get("rewrite_words"),
              "engine": cfg.get("rewrite_engine"), "review": cfg.get("rewrite_review"), "template": tid}
    keys = parse_keys(cfg.get("youtube_api_keys") or cfg.get("youtube_api_key"))
    states = _job_states()
    lists: list[dict[str, Any]] = []
    report: list[dict[str, Any]] = []
    seen: set[str] = set()  # один канал двумя ссылками — ролики не повторяются
    for url in urls:
        try:
            got = pick(url, count, section=sec, keys=keys, jobs=states, exclude=seen)
        except ChannelError as exc:
            report.append({"url": url, "error": str(exc)})
            continue
        seen.update(v.id for v in got["videos"])
        entry = {**got, "url": url}
        lists.append(entry)
        report.append(entry)
    items: list[JobIn] = []
    for ent, v in spread(lists, count):  # самые залетевшие со всех каналов вместе, ровно N
        items.append(JobIn(kind="rewrite", params={**common, "url": v.url, "video_title": v.title,
                                                   "channel_title": ent["channel"], "force": True}))
    if not items:
        errors = "; ".join(r["error"] for r in report if r.get("error"))
        raise HTTPException(400, errors or "С каналов шаблона брать больше нечего — все подходящие ролики уже переписаны")
    if len(items) < count:
        notes.append(f"Подходящих роликов нашлось {len(items)} из {count} — столько и поставлено")
    jobs = _enqueue(items, cfg, keep_dates=True)
    _reserve_links(jobs)
    return {"jobs": jobs, "notes": notes, "channels": [
        {k: r.get(k) for k in ("url", "channel", "taken", "left", "error") if k in r} for r in report]}


@app.get("/api/collabs")
def collabs_status() -> dict[str, Any]:
    """Приглашения соавторов, которые ещё ждут подтверждения (пары «канал → соавтор»)."""
    from engine.publish import collabs

    items = collabs.pending()
    return {"pending": len(items), "items": [
        {"owner": r.get("owner_label") or r["owner"], "co": r["co"], "videos": r.get("videos") or [],
         "tries": int(r.get("tries") or 0), "error": r.get("error") or "", "since": r.get("since") or 0}
        for r in items]}


@app.post("/api/collabs/reconcile")
def collabs_reconcile() -> dict[str, Any]:
    """Подтвердить ждущие соавторства сейчас (обычно они подтверждаются сами в фоне)."""
    from engine.publish import collabs

    if not collabs.pending():
        return {"queued": False, "pending": 0}
    return {"queued": True, "pending": len(collabs.pending()),
            "jobs": _enqueue([JobIn(kind="collabs", params={})])}


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


def _enqueue(items: list[JobIn], cfg: dict[str, Any] | None = None, *, keep_dates: bool = False) -> list[dict[str, Any]]:
    # сначала проверяем всё, потом ставим — чтобы не было «половины очереди»
    # cfg — настройки запуска (шаблон поверх текущих); keep_dates — календарь в настройках не очищать (даты — из шаблона)
    from server.runners import SETTINGS_KEY, snapshot_settings

    mgr = get_manager()
    cfg = cfg if cfg is not None else load_config()
    snap = snapshot_settings(cfg)
    try:
        # снимок настроек на момент постановки: задача идёт по нему до конца, что бы ни поменяли потом
        jobs = [mgr.create(i.kind, {**i.params, SETTINGS_KEY: i.params.get(SETTINGS_KEY) or snap}) for i in items]
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    _publish_plan(cfg, jobs, keep_dates=keep_dates)
    from server.runners import royal_warning

    notice = royal_warning(cfg, jobs)  # денег на генерацию может не хватить — предупредить сразу
    out = [mgr.add(j).public() for j in jobs]
    if notice and out:
        out[0]["notice"] = notice
    return out


def _publish_plan(cfg: dict[str, Any], jobs: list, *, keep_dates: bool = False) -> None:
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
        plan_id = create_plan(list(cfg.get(dates_key) or []), str(cfg.get(f"{section}_publish_time") or ""),
                              int(cfg.get(f"{section}_publish_step") or 1))
        for j in targets:
            j.params.setdefault("publish_plan", plan_id)
        if cfg.get(dates_key) and not keep_dates:
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


@app.get("/api/net")
def net_status() -> dict[str, Any]:
    """Сторож связи: доступен ли YouTube, что делаем с VPN."""
    return netguard.snapshot()


@app.post("/api/net/check")
def net_check() -> dict[str, Any]:
    """Проверить связь сейчас (кнопка в плашке «нет доступа к YouTube»)."""
    netguard.check_now()
    netguard.kick()
    return netguard.snapshot()


@app.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    queue = hub.subscribe()

    async def stream():
        try:
            snapshot = {
                "type": "snapshot",
                "jobs": get_manager().list(),
                "logs": list(hub.logs),
                "net": netguard.snapshot(),
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
                "stock_bytes": stock_bytes(run),
            }
        )
    return out


def _busy_run_names() -> set[str]:
    return {str(j.get("run") or "") for j in get_manager().list() if j.get("status") in ("queued", "running")}


@app.post("/api/runs/free-space")
def free_run_space() -> dict[str, Any]:
    """Убирает скачанные кадры у уже собранных роликов. Сам ролик, озвучка, тексты и субтитры остаются.
    Запуски, с которыми работает очередь, не трогаем."""
    if not RUNS_DIR.is_dir():
        return {"freed": 0, "runs": 0}
    busy = _busy_run_names()
    freed = runs = 0
    for run in RUNS_DIR.iterdir():
        if not run.is_dir() or run.name in busy or detect_stage(run) != "render_done":
            continue
        got = drop_stock(run)
        if got:
            freed += got
            runs += 1
    return {"freed": freed, "runs": runs}


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
    """Открыть Терминал с командой: вход в аккаунт идёт через браузер, Терминал показывает ход и ссылку.
    Команда начинается с пробела: oh-my-zsh в новом окне может спросить «Would you like to update? [Y/n]» и съесть
    первый символ набранного как ответ — так терялась открывающая кавычка, и zsh зависал на «quote>» (24.11, вход
    в Claude). Пробел он съест как «нет» (обновлять не станет), а без вопроса пробел перед командой ничего не меняет."""
    safe = (" " + cmd).replace("\\", "\\\\").replace('"', '\\"')
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
        "hints": cat.get("hints") or {},
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
