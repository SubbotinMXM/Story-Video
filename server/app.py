"""FastAPI: настройки, очередь задач, события, статика фронтенда."""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine.core.config import PROJECT_ROOT, TEMPLATE_KEYS, load_config, save_config
from engine.tts import PROVIDERS, get_provider, provider_options
from engine.slides.llm_client import DEFAULT_BASE_URL, DEFAULT_MODEL, DEFAULT_MODELS, LLMClient
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


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    return {
        "llm_models": DEFAULT_MODELS,
        "llm_default_model": DEFAULT_MODEL,
        "llm_default_base_url": DEFAULT_BASE_URL,
        "tts_providers": [p.describe() for p in PROVIDERS.values()],
        "template_keys": {kind: list(keys) for kind, keys in TEMPLATE_KEYS.items()},
        "max_copies": MAX_COPIES,
        "ffmpeg": bool(shutil.which("ffmpeg") or Path("/opt/homebrew/bin/ffmpeg").exists()),
        "desktop": dialogs.window is not None,
    }


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


@app.get("/api/jobs")
def list_jobs() -> list[dict[str, Any]]:
    return get_manager().list()


@app.post("/api/jobs")
def add_jobs(items: list[JobIn]) -> list[dict[str, Any]]:
    # сначала проверяем всё, потом ставим — чтобы не было «половины очереди»
    mgr = get_manager()
    try:
        jobs = [mgr.create(i.kind, i.params) for i in items]
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return [mgr.add(j).public() for j in jobs]


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, bool]:
    return {"ok": get_manager().cancel(job_id)}


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str) -> dict[str, Any]:
    try:
        return get_manager().resume(job_id).public()
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
    from engine.slides.music import catalog

    cat = catalog()
    return {
        "groups": cat["groups"],
        "tracks": [{"id": t["id"], "title": t["title"], "group": t["group"], "duration": t["duration"]} for t in cat["tracks"]],
    }


@app.get("/api/music/{track_id}")
def music_file(track_id: str) -> FileResponse:
    from engine.slides.music import track_path

    path = track_path(track_id)
    if path is None:
        raise HTTPException(404, "Нет такого трека")
    return FileResponse(path, media_type="audio/mpeg")


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
