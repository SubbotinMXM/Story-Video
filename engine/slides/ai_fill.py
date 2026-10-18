"""Куда в ролике ставить AI-кадры: только туда, где живой кадр не нашёлся или слабый.

Приоритет у живых кадров (фото, стоки, YouTube). Сгенерированные заменяют:
0) куски про цифры, графики, статистику — обязательно (живой кадр к ним не найти);
1) дыры — кусок без своего кадра (там стоит повтор другого кадра);
2) самые слабые по смыслу кадры (оценка ниже порога);
3) нехватку в пуле (нашлось меньше фото/видео, чем нужно).
Обычно не больше AI_SHARE кадров ролика (≈12%); ради кусков с цифрами — до AI_SHARE_MAX (15%).
"""

from __future__ import annotations

import json
from pathlib import Path

from engine.core.cancel import CancelledError, CancelToken
from engine.core.logutil import log
from engine.slides.ai_gen import AI_SHARE, AI_SHARE_MAX, STATE_NAME, AiConfig, GenTask, build_prompt, figures_in, generate
from engine.slides.ffprobe_util import probe_duration
from engine.slides.theme_plan import ThemePlan

WEAK_FIT = 6  # ниже — кадр «не идеально подходит» (как IMAGE_FIT_MIN подбора)
REPORT_NAME = "ai_report.json"
CTA_MARK = "CTA|"


def reason_of(error: str) -> str:
    """Причина неудачи по-человечески — для оповещения в карточке ролика."""
    e = (error or "").lower()
    if " 402" in e or "balance" in e or "денег" in e:
        return "на балансе RoyalTechno не хватает денег"
    if " 401" in e or "ключ royaltechno неверный" in e:
        return "ключ RoyalTechno неверный или отозван"
    if " 403" in e or "banned" in e:
        return "RoyalTechno запретил доступ (ключ заблокирован или временный бан)"
    if "нет связи" in e or "connect" in e or "timeout" in e or "timed out" in e or "name resolution" in e:
        return "сервис RoyalTechno недоступен (нет связи)"
    if " 503" in e or " 502" in e or " 500" in e or "unavailable" in e or "queue_full" in e:
        return "сервис RoyalTechno временно не работает или перегружен"
    if "не успело" in e:
        return "не успело сгенерироваться за отведённое время"
    if "policy" in e or "moderat" in e or "safety" in e:
        return "запрос отклонён правилами модели (контент-политика)"
    if "модель не выбрана" in e:
        return "выбранной модели больше нет в прайсе RoyalTechno — выбери другую"
    return f"ошибка RoyalTechno: {error[:120]}"


def record_report(run_dir: Path, tasks: list[GenTask]) -> None:
    """Сколько AI-кадров заказано / получено и почему не получились — копится за весь подбор."""
    path = run_dir / REPORT_NAME
    try:
        rep = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rep = {"requested": 0, "generated": 0, "reasons": {}}
    rep["requested"] += len(tasks)
    rep["generated"] += sum(1 for t in tasks if t.result is not None)
    for t in tasks:
        if t.result is None:
            why = reason_of(t.error or "неизвестно")
            rep["reasons"][why] = rep["reasons"].get(why, 0) + 1
    path.write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")


def report_lines(run_dir: Path) -> tuple[list[str], list[str]]:
    """(строки итога, предупреждения) по AI-кадрам этого ролика."""
    try:
        rep = json.loads((run_dir / REPORT_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], []
    req, ok = int(rep.get("requested") or 0), int(rep.get("generated") or 0)
    if req == 0:
        return [], []
    if ok == req:
        return [f"AI-кадры: {ok}"], []
    reasons = "; ".join(f"{why} ({n})" for why, n in sorted(rep.get("reasons", {}).items(), key=lambda kv: -kv[1]))
    warn = f"AI-кадры: сгенерировано {ok} из {req}, на остальных местах живые кадры. Причина: {reasons}"
    return [], [warn]


def _safe_generate(cfg: AiConfig, tasks: list[GenTask], **kw) -> None:
    """AI-кадры — только дополнение: любой их сбой (ноль на балансе, нет сети, сервис лёг)
    не роняет ролик — остаются живые кадры. «Стоп» пользователя проходит дальше."""
    try:
        generate(cfg, tasks, **kw)
    except CancelledError:
        raise
    except Exception as exc:
        log(f"AI-кадры недоступны ({type(exc).__name__}: {exc}) — ролик собирается из живых кадров")
        for t in tasks:
            if t.result is None and not t.error:
                t.error = str(exc)


def _kind_for(want: str, cfg: AiConfig) -> str | None:
    """Вид кадра: как у заменяемого, если эта модель выбрана; иначе — тот, что выбран."""
    if want == "video" and cfg.video_model:
        return "video"
    if want == "image" and cfg.image_model:
        return "image"
    return "image" if cfg.image_model else ("video" if cfg.video_model else None)


def _prompt(plan: ThemePlan, query: str, kind: str) -> str:
    ctx = plan.context
    return build_prompt(query, kind=kind, era=ctx.era, place=ctx.place, historical=ctx.historical)


def _ext(kind: str) -> str:
    return ".mp4" if kind == "video" else ".png"


def _item(task: GenTask, cfg: AiConfig, pool: Path, base: dict) -> dict:
    out = dict(base)
    out.update({
        "kind": task.kind,
        "path": str(task.result.relative_to(pool.parent)),
        "provider": "ai",
        "id": str(task.meta.get("job_id") or task.key),
        "ai_model": cfg.video_model if task.kind == "video" else cfg.image_model,
        "fit": max(int(base.get("fit") or 0), WEAK_FIT),
        "score": max(float(base.get("score") or 0), WEAK_FIT / 10),
        "weak": False,
        "fallback": False,
    })
    return out


def fill_slots(
    slots: list[dict],
    plan: ThemePlan,
    cfg: AiConfig,
    *,
    pool: Path,
    cancel: CancelToken | None = None,
    progress=None,
    share: float = AI_SHARE,
    tag: str = "slot",
) -> list[dict]:
    """Строгий ряд: слабые куски и дыры → сгенерированный кадр под текст этого куска."""
    if not cfg.active or not slots:
        return slots
    data = [i for i, s in enumerate(slots) if s.get("data")]
    cap = int(len(slots) * share)
    if data:
        # цифры и графики — обязательно: ради них потолок поднимается до AI_SHARE_MAX
        cap = max(cap, min(len(data), int(len(slots) * max(share, AI_SHARE_MAX))))
    order = data[:cap] + sorted(
        (i for i, s in enumerate(slots) if i not in set(data) and (s.get("fallback") or int(s.get("fit") or 0) < WEAK_FIT)),
        key=lambda i: (not slots[i].get("fallback"), int(slots[i].get("fit") or 0)),
    )
    order = order[:cap]
    tasks: list[GenTask] = []
    for i in order:
        s = slots[i]
        kind = _kind_for(str(s.get("kind") or "image"), cfg)
        if kind is None:
            break
        query = str(s.get("query") or s.get("text") or plan.summary)
        if query.startswith(CTA_MARK):
            query = query[len(CTA_MARK):]
        if s.get("data"):
            ctx = plan.context
            prompt = build_prompt(str(s["data"]), kind=kind, era=ctx.era, place=ctx.place, historical=ctx.historical,
                                  data=True, figures=figures_in(str(s.get("text") or "")))
        else:
            prompt = _prompt(plan, query, kind)
        tasks.append(GenTask(key=f"{tag}:{i}", kind=kind, prompt=prompt,
                             dest=pool / f"ai_{tag}_{i:04d}{_ext(kind)}", meta={"index": i}))
    if not tasks:
        return slots
    n_data = sum(1 for t in tasks if slots[int(t.meta["index"])].get("data"))
    log(f"AI-кадры: {len(tasks)} из {len(slots)} кадров ролика (цифры/графики: {n_data}, остальное — без хорошего живого кадра)")
    _safe_generate(cfg, tasks, state_file=pool.parent / STATE_NAME, cancel=cancel, progress=progress)
    record_report(pool.parent, tasks)
    out = list(slots)
    for t in tasks:
        if t.result is not None:
            i = int(t.meta.get("index", t.key.split(":")[1]))
            out[i] = _item(t, cfg, pool, slots[i])
    return out


def fill_pool(
    videos: list[dict],
    images: list[dict],
    plan: ThemePlan,
    cfg: AiConfig,
    *,
    pool: Path,
    image_target: int,
    video_target: int,
    cancel: CancelToken | None = None,
    progress=None,
    share: float = AI_SHARE,
    tag: str = "pool",
) -> tuple[list[dict], list[dict]]:
    """Пул по теме: нехватка и самые слабые кадры → сгенерированные (≤ share пула)."""
    if not cfg.active:
        return videos, images
    total = max(len(videos) + len(images), image_target + video_target)
    cap = int(total * share)
    plan_q = {"image": [q for q in plan.image_queries if q] or [plan.summary], "video": [q for q in plan.video_queries if q] or [plan.summary]}
    tasks: list[GenTask] = []

    def add(kind: str, base: dict, replace: tuple[str, int] | None) -> None:
        n = len(tasks)
        query = str(base.get("query") or plan_q[kind][n % len(plan_q[kind])])
        tasks.append(GenTask(key=f"{tag}:{kind}:{n}", kind=kind, prompt=_prompt(plan, query, kind),
                             dest=pool / f"ai_{tag}_{kind}_{n:03d}{_ext(kind)}", meta={"replace": replace, "base": base}))

    for kind, items, target in (("image", images, image_target), ("video", videos, video_target)):
        gen_kind = _kind_for(kind, cfg)
        if gen_kind != kind:
            continue  # этот вид не генерируем (модель не выбрана)
        # нехватка в пуле — добавить; потом самые слабые — заменить
        for _ in range(max(0, target - len(items))):
            if len(tasks) < cap:
                add(kind, {}, None)
        weak = sorted((i for i, it in enumerate(items) if float(it.get("score") or 0) * 10 < WEAK_FIT), key=lambda i: float(items[i].get("score") or 0))
        for i in weak:
            if len(tasks) < cap:
                add(kind, items[i], (kind, i))
    if not tasks:
        return videos, images
    log(f"AI-кадры: пул — {len(tasks)} сгенерированных (нехватка и слабые кадры)")
    _safe_generate(cfg, tasks, state_file=pool.parent / STATE_NAME, cancel=cancel, progress=progress)
    record_report(pool.parent, tasks)
    new_videos, new_images = list(videos), list(images)
    for t in tasks:
        if t.result is None:
            continue
        item = _item(t, cfg, pool, t.meta.get("base") or {})
        if t.kind == "video":
            try:
                item["duration"] = round(min(probe_duration(t.result), 10.0), 3)
            except Exception:
                item["duration"] = 5.0
        repl = t.meta.get("replace")
        target_list = new_videos if t.kind == "video" else new_images
        if repl:
            target_list[repl[1]] = item
        else:
            target_list.append(item)
    return new_videos, new_images
