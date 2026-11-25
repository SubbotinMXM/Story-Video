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
from engine.slides.hook import NO_AI_SEC
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
    if "перегружен" in e:
        return "сервис RoyalTechno перегружен — генерацию остановил, там живые кадры"
    if "не успело" in e:
        return "не успело сгенерироваться за отведённое время"
    if "policy" in e or "moderat" in e or "safety" in e:
        return "запрос отклонён правилами модели (контент-политика)"
    if "отклонён проверкой" in e:
        return "кадр не прошёл проверку по смыслу ролика (не та страна/эпоха или брак) — даже после повторной генерации"
    if "модель не выбрана" in e:
        return "выбранной модели больше нет в прайсе RoyalTechno — выбери другую"
    return f"ошибка RoyalTechno: {error[:120]}"


# часть бюджета держим на повторные генерации (кадр не прошёл проверку по смыслу)
FIX_RESERVE = 0.15


def _state(run_dir: Path) -> dict:
    try:
        raw = json.loads((run_dir / STATE_NAME).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _paid(rec: dict) -> bool:
    """Задача, за которую списаны деньги: отправлена и не упала (упавшие и отменённые RoyalTechno возвращает)."""
    return bool(rec.get("job_id")) and not rec.get("failed")


def spent_cents(cfg: AiConfig, run_dir: Path) -> int:
    """Сколько этот ролик потратил на AI-кадры: готовые — по реальной цене из ответа RoyalTechno,
    ещё в работе — по прайсу (деньги за них уже списаны)."""
    total = 0
    for rec in _state(run_dir).values():
        if not _paid(rec):
            continue
        cost = rec.get("cost") if rec.get("done") else None
        total += int(cost) if cost is not None else cfg.cost(str(rec.get("kind") or "image"))
    return total


def fit_budget(cfg: AiConfig, tasks: list[GenTask], run_dir: Path, *, reserve: float = 0.0) -> list[GenTask]:
    """Задачи, которые влезают в бюджет ролика (порядок — по важности). Уже оплаченные (продолжение
    после сбоя) — бесплатно. Дорогое не влезло — дешёвое после него ещё может влезть."""
    state = _state(run_dir)
    left = cfg.budget_cents * (1 - reserve) - spent_cents(cfg, run_dir)
    out: list[GenTask] = []
    for t in tasks:
        if _paid(state.get(t.key) or {}):
            out.append(t)
            continue
        cost = cfg.cost(t.kind)
        if cost > left:
            continue
        left -= cost
        out.append(t)
    skipped = len(tasks) - len(out)
    if skipped:
        log(f"AI-кадры: лимит ролика ${cfg.budget_cents / 100:.2f} — {skipped} кадров не генерирую, "
            "дальше только живые кадры (видео, фото, стоки)")
        _bump_report(run_dir, budget_skipped=skipped)
    return out


def _bump_report(run_dir: Path, **add: int) -> None:
    path = run_dir / REPORT_NAME
    try:
        rep = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rep = {"requested": 0, "generated": 0, "reasons": {}}
    for k, v in add.items():
        rep[k] = int(rep.get(k) or 0) + v
    path.write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")


def record_report(run_dir: Path, tasks: list[GenTask], cfg: AiConfig | None = None) -> None:
    """Сколько AI-кадров заказано / получено и почему не получились — копится за весь подбор."""
    path = run_dir / REPORT_NAME
    try:
        rep = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rep = {"requested": 0, "generated": 0, "reasons": {}}
    if cfg is not None:
        rep["spent_cents"] = spent_cents(cfg, run_dir)
        rep["budget_cents"] = cfg.budget_cents
    # по ключу задачи: инфографику, заказанную заранее, повторный вызов не считает второй раз
    done = rep.setdefault("tasks", {})
    for t in tasks:
        done[t.key] = "" if t.result is not None else reason_of(t.error or "неизвестно")
    rep["requested"] = len(done)
    rep["generated"] = sum(1 for why in done.values() if not why)
    rep["reasons"] = {}
    for why in done.values():
        if why:
            rep["reasons"][why] = rep["reasons"].get(why, 0) + 1
    path.write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")


def report_lines(run_dir: Path) -> tuple[list[str], list[str]]:
    """(строки итога, предупреждения) по AI-кадрам этого ролика."""
    try:
        rep = json.loads((run_dir / REPORT_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], []
    req, ok = int(rep.get("requested") or 0), int(rep.get("generated") or 0)
    money = ""
    if rep.get("spent_cents") is not None:
        money = f", потрачено ${int(rep['spent_cents']) / 100:.2f} из ${int(rep.get('budget_cents') or 0) / 100:.2f}"
    budget = int(rep.get("budget_skipped") or 0)
    tail = f" (ещё {budget} мест — живыми кадрами: бюджет ролика)" if budget else ""
    if req == 0:
        return ([f"AI-кадры: 0{money}{tail}"] if budget else []), []
    if ok == req:
        return [f"AI-кадры: {ok}{money}{tail}"], []
    reasons = "; ".join(f"{why} ({n})" for why, n in sorted(rep.get("reasons", {}).items(), key=lambda kv: -kv[1]))
    warn = f"AI-кадры: сгенерировано {ok} из {req}{money}, на остальных местах живые кадры. Причина: {reasons}"
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


def _generate_checked(
    cfg: AiConfig,
    tasks: list[GenTask],
    plan: ThemePlan,
    *,
    quality,
    texts: dict[str, str],
    data_keys: set[str],
    pool: Path,
    cancel: CancelToken | None,
    progress,
) -> None:
    """Сгенерировать; не прошедшие проверку — ещё раз с исправленным описанием; снова мимо — без кадра."""
    state = pool.parent / STATE_NAME
    _safe_generate(cfg, tasks, state_file=state, cancel=cancel, progress=progress)
    if quality is None:
        return
    bad = quality.check(tasks, texts, data_keys=data_keys)
    if not bad:
        return
    ctx = plan.context
    fixes = [
        GenTask(
            key=f"{t.key}:fix", kind=t.kind,
            prompt=quality.fix_prompt(t.prompt, bad[t.key], place=ctx.place, era=ctx.era, markers=ctx.visual_markers),
            dest=t.dest.with_name(f"{t.dest.stem}_fix{t.dest.suffix}"), meta={"orig": t},
        )
        for t in tasks if t.key in bad and t.result is not None
    ]
    fixed_keys = {f.key for f in fit_budget(cfg, fixes, pool.parent)}
    for f in fixes:
        if f.key not in fixed_keys:
            # повтор не влез в бюджет — отклонённый кадр не ставим, остаётся живой
            f.meta["orig"].result.unlink(missing_ok=True)
            f.meta["orig"].result = None
            f.meta["orig"].error = "отклонён проверкой: " + bad[f.meta["orig"].key]
    fixes = [f for f in fixes if f.key in fixed_keys]
    if not fixes:
        return
    log(f"AI-кадры: {len(fixes)} не прошли проверку по смыслу ролика — генерирую заново с исправленным описанием")
    _safe_generate(cfg, fixes, state_file=state, cancel=cancel, progress=progress)
    bad2 = quality.check(fixes, {f.key: texts.get(f.meta["orig"].key, "") for f in fixes},
                         data_keys={f"{k}:fix" for k in data_keys})
    for f in fixes:
        orig: GenTask = f.meta["orig"]
        if orig.result is not None:
            orig.result.unlink(missing_ok=True)
        if f.result is not None and f.key not in bad2:
            orig.result = f.result
            continue
        if f.result is not None:
            f.result.unlink(missing_ok=True)
        orig.result = None
        orig.error = "отклонён проверкой: " + (bad2.get(f.key) or f.error or bad[orig.key])
        log(f"AI-кадр {orig.key}: и вторая попытка мимо — оставляю живой кадр")


def _write_prompts(quality, tasks: list[GenTask], items: list[dict]) -> None:
    """Описания AI-кадров пишет LLM по «паспорту ролика» и моменту рассказа (не вышло — простые)."""
    if quality is None or not items:
        return
    got = quality.prompts(items)
    by_key = {t.key: t for t in tasks}
    n = 0
    for it, prompt in zip(items, got):
        if prompt and it["key"] in by_key:
            by_key[it["key"]].prompt = prompt
            n += 1
    log(f"AI-кадры: описаний по смыслу ролика — {n} из {len(items)}")


def _quality(judge, pool: Path):
    if judge is None or not judge.enabled:
        return None
    from engine.slides.ai_quality import AiQuality

    return AiQuality(judge, pool.parent)


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
    share: float | None = None,
    tag: str = "slot",
    judge=None,
    total: int | None = None,
) -> list[dict]:
    """Строгий ряд: слабые куски и дыры → сгенерированный кадр под текст этого куска.
    judge — описание пишет LLM по смыслу ролика и момента, готовый кадр проверяется.
    total — сколько кадров во всём ролике (для потолка доли), если slots — не весь ряд.
    Задача AI-кадра привязана ко времени куска: инфографику, заказанную заранее (параллельно
    с подбором живых кадров), повторный вызов узнаёт и не оплачивает второй раз."""
    if not cfg.active or not slots:
        return slots
    # первые 2 минуты ролика — только живые кадры (самые красивые, видео в приоритете)
    late = [float(s.get("start") or 0) >= NO_AI_SEC for s in slots]
    data = [i for i, s in enumerate(slots) if s.get("data") and late[i]]
    share = cfg.share if share is None else share
    n_all = total or len(slots)
    cap = int(n_all * share)
    if data:
        # цифры и графики — обязательно: ради них потолок поднимается до share_max
        cap = max(cap, min(len(data), int(n_all * max(share, cfg.share_max))))
    order = data[:cap] + sorted(
        (i for i, s in enumerate(slots) if late[i] and i not in set(data) and (s.get("fallback") or int(s.get("fit") or 0) < WEAK_FIT)),
        key=lambda i: (not slots[i].get("fallback"), int(slots[i].get("fit") or 0)),
    )
    order = order[:cap]
    quality = _quality(judge, pool)
    tasks: list[GenTask] = []
    texts: dict[str, str] = {}
    data_keys: set[str] = set()
    to_write: list[dict] = []
    for i in order:
        s = slots[i]
        # инфографика — всегда одного вида (фото, если выбрана модель фото): заказ заранее и после совпадает
        kind = _kind_for("image" if s.get("data") else str(s.get("kind") or "image"), cfg)
        if kind is None:
            break
        query = str(s.get("query") or s.get("text") or plan.summary)
        if query.startswith(CTA_MARK):
            query = query[len(CTA_MARK):]
        ms = int(round(float(s.get("start") or 0) * 1000))
        key = f"{tag}:{ms}"
        texts[key] = str(s.get("text") or "")
        if s.get("data"):
            ctx = plan.context
            prompt = build_prompt(str(s["data"]), kind=kind, era=ctx.era, place=ctx.place, historical=ctx.historical,
                                  data=True, figures=figures_in(str(s.get("text") or "")))
            data_keys.add(key)
        else:
            prompt = _prompt(plan, query, kind)
            around = " … ".join(str(slots[j].get("text") or "") for j in (i - 1, i + 1) if 0 <= j < len(slots))
            to_write.append({"key": key, "kind": kind, "moment": texts[key], "around": around, "query": query})
        tasks.append(GenTask(key=key, kind=kind, prompt=prompt,
                             dest=pool / f"ai_{tag}_{ms:08d}{_ext(kind)}", meta={"index": i}))
    tasks = fit_budget(cfg, tasks, pool.parent, reserve=FIX_RESERVE)
    if not tasks:
        return slots
    keep = {t.key for t in tasks}
    _write_prompts(quality, tasks, [it for it in to_write if it["key"] in keep])
    n_data = sum(1 for t in tasks if slots[int(t.meta["index"])].get("data"))
    log(f"AI-кадры: {len(tasks)} из {len(slots)} кадров ролика (цифры/графики: {n_data}, остальное — без хорошего живого кадра)")
    _generate_checked(cfg, tasks, plan, quality=quality, texts=texts, data_keys=data_keys, pool=pool,
                      cancel=cancel, progress=progress)
    record_report(pool.parent, tasks, cfg)
    out = list(slots)
    for t in tasks:
        if t.result is not None:
            i = int(t.meta["index"])
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
    share: float | None = None,
    tag: str = "pool",
    judge=None,
    moments: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Пул по теме: нехватка и самые слабые кадры → сгенерированные (≤ share пула).
    moments — фразы рассказа: каждый AI-кадр пула показывает конкретный момент истории,
    а не общую тему; judge — описания пишет LLM, готовые кадры проверяются."""
    if not cfg.active:
        return videos, images
    total = max(len(videos) + len(images), image_target + video_target)
    cap = int(total * (cfg.share if share is None else share))
    # нехватка картинок до нормы закрывается AI целиком — в пределах денежного лимита ролика (fit_budget)
    cap = max(cap, max(0, image_target - len(images)))
    plan_q = {"image": [q for q in plan.image_queries if q] or [plan.summary], "video": [q for q in plan.video_queries if q] or [plan.summary]}
    tasks: list[GenTask] = []
    quality = _quality(judge, pool)
    texts: dict[str, str] = {}
    to_write: list[dict] = []
    # моменты рассказа — вразбивку по всему тексту, чтобы AI-кадры не толпились в одном месте
    picks = list(moments or [])
    if picks:
        step = max(1, len(picks) // max(1, cap))
        picks = picks[::step]

    def add(kind: str, base: dict, replace: tuple[str, int] | None) -> None:
        n = len(tasks)
        query = str(base.get("query") or plan_q[kind][n % len(plan_q[kind])])
        key = f"{tag}:{kind}:{n}"
        moment = picks[n % len(picks)] if picks else ""
        texts[key] = moment
        to_write.append({"key": key, "kind": kind, "moment": moment, "around": "", "query": query})
        tasks.append(GenTask(key=key, kind=kind, prompt=_prompt(plan, query, kind),
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
    tasks = fit_budget(cfg, tasks, pool.parent, reserve=FIX_RESERVE)
    if not tasks:
        return videos, images
    keep = {t.key for t in tasks}
    log(f"AI-кадры: пул — {len(tasks)} сгенерированных (нехватка и слабые кадры)")
    _write_prompts(quality, tasks, [it for it in to_write if it["key"] in keep])
    _generate_checked(cfg, tasks, plan, quality=quality, texts=texts, data_keys=set(), pool=pool,
                      cancel=cancel, progress=progress)
    record_report(pool.parent, tasks, cfg)
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
