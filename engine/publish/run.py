"""Задача «Выкладка»: готовый ролик и его копии → выбранные каналы YouTube (через профили AdsPower).

Одна копия — один канал (скорости разные: одинаковый файл дважды не уходит), какая копия куда —
случайно. Обложка, название, описание — одни на все каналы этого ролика. Всё выбранное (материалы,
распределение копий, даты) запоминается в задаче: повтор после сбоя не тратит новые обложки и даты.
По каждому каналу — своя отметка: упал один — остальные выкладываются, упавший повторяется кнопкой.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from engine.core.cancel import CancelToken
from engine.core.logutil import log
from engine.publish import collabs, materials, registry, schedule, youtube
from engine.publish.adspower import AdsPower, AdsPowerDown

ProgressCb = Callable[[float, str], None]
DONE, FAILED, LIMIT, SKIPPED = "done", "failed", "limit", "skipped"
# AdsPower закрыт/завис/перезапускается: канал не провален — ждём программу и продолжаем с того же места
WAITING = "waiting"
ADSPOWER_POLL_SEC = 60  # как часто проверять, вернулся ли AdsPower (попытки при этом не тратятся)
ADSPOWER_DOWN = ("AdsPower не отвечает (закрыт, завис или перезапускается) — жду его и продолжу сам с того же места, "
                 "проверяю раз в минуту. Если AdsPower закрыт — просто открой его")


@dataclass
class Version:
    """Языковая версия ролика: основной ролик или перевод (своя папка, свои файлы — основной и копии)."""

    language: str  # ru, es… («» — неизвестен)
    folder: Path
    files: list[tuple[str, int]]  # (путь, скорость %)


@dataclass
class PublishRequest:
    folder: Path  # папка готового ролика
    files: list[tuple[str, int]]  # (путь, скорость %) — основной и копии
    channels: list[str]  # «профиль/канал»
    coauthors: list[str] = field(default_factory=list)
    thumbs_dir: str = ""
    titles_file: str = ""
    descs_file: str = ""
    plan_id: str = ""  # свои даты/время запуска (schedule.create_plan)
    visible: bool = True
    keep_files: bool = True
    run_dir: str = ""  # рабочая папка запуска (слайды): при keep_files=False удаляем и её копию ролика
    comment: str = ""  # комментарий-призыв под вышедшими роликами канала (закрепляется)
    reply_comments: bool = True  # ответить зрителям (сердечко + ответ) на канале выкладки
    llm: tuple[str, str, str] | None = None  # нейросеть для ответов зрителям
    adspower: tuple[str, str] = ("", "")  # (ключ, адрес)
    # материалы версии: prepare(version, base, cancel=, progress=) → {"title", "thumbnail", "description",
    # "descriptions": {файл: описание}, "lines", "cover_text"}. base=None — основная версия (подбор/генерация),
    # иначе — материалы основной версии, их переводим на язык этой. None — подбор из папок, одни на все версии
    prepare: Callable[..., dict[str, Any]] | None = None

    def all_versions(self) -> list[Version]:
        return self.versions or [Version(self.language, self.folder, list(self.files))]
    publish_now: bool = False  # выложить сразу после загрузки и проверок («Public»), без расписания
    language: str = ""  # язык ролика (ru, es…): на канал другого языка не заливаем
    # версии на разных языках (основной + переводы): каждый канал получает версию своего языка.
    # Пусто — одна версия (folder, files, language)
    versions: list[Version] = field(default_factory=list)


class PublishLimit(RuntimeError):
    """Остались невыложенные каналы — очередь сама продолжит в retry_at (лимит YouTube или повтор после сбоя)."""

    def __init__(self, message: str, retry_at: float) -> None:
        super().__init__(message)
        self.retry_at = retry_at


# ролик должен выйти в итоге: сбой на канале — повтор сам, без предела попыток (мин между попытками)
RETRY_MIN = (10, 30, 60)


def validate(channels: list[str], coauthors: list[str], thumbs_dir: str, titles_file: str, descs_file: str, *,
             own_materials: bool = False) -> None:
    """При запуске ролика (до рерайта и озвучки): каналы выбраны, соавторы не среди них, материалы на месте."""
    if not channels:
        raise ValueError("Выкладка на YouTube включена, но не выбрано ни одного канала")
    targets = registry.resolve(channels)
    co = registry.resolve(coauthors, what="соавтора")
    clash = {t.channel.id for t in targets} & {t.channel.id for t in co}
    if clash:
        names = ", ".join(t.channel.name for t in targets if t.channel.id in clash)
        raise ValueError(f"Канал и цель выкладки, и соавтор одновременно: {names} — убери его из одного списка")
    if not own_materials:
        materials.check(thumbs_dir, titles_file, descs_file)


def _note_music_claims(session, rec: dict) -> None:
    """Проверки YouTube нашли претензию — какой трек фоновой музыки виноват: он уходит в чёрный список,
    следующие ролики пойдут с другим. Сбой проверки выкладку не портит."""
    text = " ".join([*rec.get("notes", []), str(rec.get("error") or "")])
    if session is None or not ("проверки YouTube нашли" in text or "предупреждение" in text):
        return
    url = rec.get("url") or rec.get("draft_url") or ""
    try:
        found = youtube.music_claims(session.cdp_url, url)
    except Exception as exc:
        log(f"Выкладка: страницу претензий прочитать не вышло ({str(exc).splitlines()[0][:150]})")
        return
    if found:
        msg = (f"претензия на музыку «{', '.join(found)}» — трек исключён из библиотеки, следующие ролики пойдут "
               "с другим (выбери трек в карточке «Музыка»)")
        rec.setdefault("notes", []).append(msg)
        log(f"Выкладка: {msg}")


def route(t: registry.Target, versions: list[Version], by_lang: dict[str, Version]) -> tuple[Version | None, str]:
    """Какая версия ролика идёт на канал. Версия одна — она (несовпадение языка поймает предохранитель).
    Версий несколько — по языку канала; язык не указан → ждём тебя; версии на языке канала нет → пропуск."""
    if len(versions) == 1:
        return versions[0], ""
    lang = registry.normalize(t.channel.language)
    if not lang:
        return None, (f"у канала не указан язык, а ролик на нескольких языках "
                      f"({', '.join(k.upper() for k in by_lang if k)}) — укажи язык канала в списке каналов "
                      "и нажми «Продолжить»")
    if lang not in by_lang:
        return None, f"ролика на {lang.upper()} нет (есть: {', '.join(k.upper() for k in by_lang if k)}) — канал пропущен"
    return by_lang[lang], ""


def language_mismatch(video_language: str, channel) -> str:
    """Язык ролика и язык канала разные → текст для «ждёт тебя»; совпадают / неизвестно → «»."""
    v, c = registry.normalize(video_language), registry.normalize(getattr(channel, "language", ""))
    if not v or not c or v == c:
        return ""
    return (f"язык ролика {v.upper()}, а у канала указан {c.upper()} — на этот канал НЕ заливаю. "
            "Если язык канала сменился — поменяй его в списке каналов («Выложить на YouTube») и нажми «Продолжить»; "
            "если канал выбран по ошибке — ролик лежит в папке, его можно залить вручную куда нужно")


def copies_for(channels: list[str]) -> int:
    """Сколько копий скорости к основному ролику: по одной на каждый канал, кроме первого."""
    return max(0, len(registry.resolve(channels)) - 1)


def run_publish(req: PublishRequest, state: dict[str, Any], save: Callable[..., None], *,
                cancel: CancelToken | None = None, progress: ProgressCb | None = None) -> dict[str, Any]:
    """state — состояние задачи (job.state), save(**values) — записать его на диск."""
    def step(p: float, msg: str) -> None:
        log(msg)
        if progress:
            progress(p, msg)

    youtube.require_ready()  # Studio не подключена — не тратим обложки и даты зря
    targets = registry.resolve(req.channels)
    coauthors = registry.resolve(req.coauthors, what="соавтора")
    rng = random.Random()

    versions = req.all_versions()
    by_lang = {registry.normalize(v.language): v for v in versions}

    # всё, что выбирается один раз на ролик, — в состояние задачи (повтор берёт то же самое)
    def prep(v: Version, base: dict | None) -> dict:
        if req.prepare is not None:
            return req.prepare(v, base, cancel=cancel, progress=lambda p, m: step(0.05 * p, m))
        return dict(base) if base else materials.pick(req.thumbs_dir, req.titles_file, req.descs_file, rng=rng).as_dict()

    if "materials" not in state:
        save(materials=prep(versions[0], None))
    mat = state["materials"]
    by_version: dict[str, dict] = dict(state.get("materials_by_lang") or {})

    def mat_for(v: Version) -> dict:
        lang = registry.normalize(v.language)
        if v is versions[0]:
            return mat
        if lang not in by_version:
            step(0.05, f"Выкладка: материалы на {lang.upper()} (название, описание, обложка)…")
            by_version[lang] = prep(v, mat)
            save(materials_by_lang=by_version)
        return by_version[lang]

    # какая версия и какая копия — на какой канал. Каналы без решения (новые или ждавшие языка) — заново
    results: dict[str, dict] = dict(state.get("channels") or {})
    assign: dict[str, dict] = dict(state.get("assign") or {})
    used = {a.get("file") for a in assign.values()}
    for t in targets:
        if t.key in assign:
            continue
        rec = dict(results.get(t.key) or {})
        v, why = route(t, versions, by_lang)
        free = [(f, pct) for f, pct in v.files if f not in used] if v is not None else []
        if v is not None and not free:
            v, why = None, f"копий ролика ({v.language.upper() or 'основной'}) меньше, чем каналов"
        if v is None:
            skip = why.startswith("ролика на")
            rec.update(status=SKIPPED if skip else FAILED, error=why, needs_user=not skip, routing=True)
            results[t.key] = rec
            continue
        rng.shuffle(free)  # какая копия на какой канал — случайно
        f, pct = free[0]
        used.add(f)
        assign[t.key] = {"file": f, "pct": pct, "lang": registry.normalize(v.language)}
        if rec.pop("routing", None):  # раньше не было решения (язык канала не был указан) — теперь есть
            for k in ("status", "error", "needs_user"):
                rec.pop(k, None)
        results[t.key] = rec
    save(assign=assign, channels=results)
    if "plan_date" not in state:
        wanted, at = schedule.take_plan_date(req.plan_id) if req.plan_id else (None, "")
        save(plan_date=wanted.isoformat() if wanted else "", plan_time=at,
             plan_step=schedule.plan_step(req.plan_id))
    wanted = date.fromisoformat(state["plan_date"]) if state.get("plan_date") else None
    live = _Live(targets, results, save, progress)
    todo = [t for t in targets if t.key in assign and (results.get(t.key) or {}).get("status") != DONE]
    by_profile: dict[str, list[registry.Target]] = {}
    for t in todo:
        by_profile.setdefault(t.profile_id, []).append(t)
    done_n = len(targets) - len(todo)
    key, url = req.adspower
    with AdsPower(key, url) as ap:
        if todo and not ap.alive():
            # AdsPower ещё не поднялся — не начинаем (попытки не тратим), проверим через минуту
            raise PublishLimit(ADSPOWER_DOWN, time.time() + ADSPOWER_POLL_SEC)
        for pid, group in by_profile.items():
            session = None
            try:
                for t in group:
                    if cancel:
                        cancel.check()
                    rec = dict(results.get(t.key) or {})
                    a = assign.get(t.key) or {}
                    version = by_lang.get(a.get("lang", ""), versions[0])
                    wrong = language_mismatch(version.language, t.channel)
                    if wrong:
                        # предохранитель: ролик не того языка на канал не заливаем — решает человек
                        rec.update(status=FAILED, error=wrong, needs_user=True)
                        log(f"Выкладка: {t.label()} — ✗ {wrong}")
                        results[t.key] = rec
                        save(channels=results)
                        live.finish(t, rec)
                        continue
                    if req.publish_now:
                        rec["publish_at"] = datetime.now(schedule.MSK).isoformat()  # сразу: день в расписании не занимаем
                    elif "publish_at" in rec and datetime.fromisoformat(rec["publish_at"]) < datetime.now(schedule.MSK) + schedule.MIN_AHEAD:
                        # пока шли повторы, время выхода прошло — ближайший свободный день канала
                        old = datetime.fromisoformat(rec.pop("publish_at"))
                        schedule.release(t.channel.id, old)
                        rec.setdefault("notes", []).append(f"дата {_human(old.isoformat())} прошла во время повторов — перенёс")
                    if "publish_at" not in rec:
                        studio_last = date.fromisoformat(t.channel.last_scheduled) if t.channel.last_scheduled else None
                        when = schedule.slot(t.channel.id, wanted=wanted, at=state.get("plan_time") or "",
                                             studio_last=studio_last, step=int(state.get("plan_step") or 1))
                        rec["publish_at"] = when.isoformat()
                    step(0.05 + 0.9 * done_n / len(targets),
                         f"Выкладка {done_n + 1}/{len(targets)}: {t.label()} — {a.get('pct')}%, "
                         f"выход {_human(rec['publish_at'])}")
                    live.start(t, rec["publish_at"], a.get("pct"), done_n)
                    try:
                        file = Path(str(a.get("file") or ""))
                        if not file.is_file():
                            raise youtube.StudioError(f"нет файла ролика: {file}")
                        vm = mat_for(version)
                        job = youtube.UploadJob(
                            file=file, title=vm["title"],
                            description=(vm.get("descriptions") or {}).get(str(file)) or vm["description"],
                            thumbnail=Path(vm["thumbnail"]) if vm.get("thumbnail") else None,
                            publish_at=datetime.fromisoformat(rec["publish_at"]),
                            playlist_hint=t.channel.playlists,
                            # соавторы — только того же языка, что ролик (язык не указан — как раньше)
                            coauthors=[c for c in coauthors if not language_mismatch(version.language, c.channel)],
                            publish_now=req.publish_now,
                        )
                        for attempt in range(1, PROFILE_RESTARTS + 2):
                            if session is None:
                                session = ap.start(pid, headless=not req.visible)
                            try:
                                def keep_draft(link: str, rec=rec, key=t.key) -> None:
                                    # ссылка на видео — сразу в состояние задачи: после падения продолжим его, а не дублем
                                    rec["draft_url"] = link
                                    results[key] = rec
                                    save(channels=results)

                                up = youtube.upload(session.cdp_url, t.channel, job, cancel=cancel,
                                                    progress=live.file_progress(t, done_n),
                                                    draft_url=rec.get("draft_url", ""), on_draft=keep_draft)
                                break
                            except Exception as exc:
                                if cancel is not None and cancel.is_cancelled():
                                    raise
                                if not _profile_lost(ap, pid, exc) or attempt > PROFILE_RESTARTS:
                                    raise
                                # профиль закрылся посреди загрузки — перезапустить и пройти канал заново
                                session = _restart_profile(ap, pid, t.label(), attempt)
                                rec.setdefault("notes", []).append(
                                    "профиль закрывался во время загрузки — в Studio мог остаться лишний черновик, проверь")
                        rec.update(status=DONE, url=up.url, notes=[*rec.get("notes", []), *up.notes], error="",
                                   invites=up.invites or {}, title=job.title, file_ok=bool(up.file_ok))
                        # приглашение создано — оно обязано быть принято, даже если дальше всё упадёт (см. collabs.py)
                        collabs.record_invites(t.key, t.label(), rec["invites"], title=job.title, url=up.url)
                        for co in job.coauthors:  # приглашение не создалось — соавтора доделывать руками
                            if co.key not in rec["invites"]:
                                _coauthor_failed(rec, co, next((n for n in up.notes if "соавтор" in n),
                                                               "приглашение не создалось"))
                        live.set(t, pct=100)
                        if req.comment.strip():
                            live.after_upload(t, "комментарий-призыв под вышедшими роликами")
                            # этот ролик ещё в отложке — призыв под теми, что уже вышли (обычно один)
                            try:
                                rec["notes"] += youtube.backfill_comments(session.cdp_url, t.channel, req.comment,
                                                                          cancel=cancel)
                            except Exception as exc:
                                if cancel is not None and cancel.is_cancelled():
                                    raise
                                rec["notes"].append(f"комментарий-призыв не оставлен: {str(exc).splitlines()[0][:300]}")
                        if req.reply_comments:
                            from engine.publish import comments

                            live.after_upload(t, "ответы на комментарии зрителей")
                            try:
                                rec["notes"] += comments.answer_comments(
                                    session.cdp_url, t.channel, llm=req.llm, cancel=cancel,
                                    progress=lambda _f, msg, t=t: live.after_upload(t, "ответы на комментарии зрителей", msg))
                            except Exception as exc:
                                if cancel is not None and cancel.is_cancelled():
                                    raise
                                rec["notes"].append(f"ответы на комментарии не получились: {str(exc).splitlines()[0][:300]}")
                        done_n += 1
                    except AdsPowerDown:
                        # канал не провален: ссылка на черновик уже сохранена — продолжим его, когда AdsPower вернётся
                        rec.update(status=WAITING, error=ADSPOWER_DOWN)
                        results[t.key] = rec
                        save(channels=results)
                        live.finish(t, rec)
                        log(f"Выкладка: {t.label()} — {ADSPOWER_DOWN}")
                        raise PublishLimit(ADSPOWER_DOWN, time.time() + ADSPOWER_POLL_SEC) from None
                    except youtube.UploadLimit as exc:
                        rec.update(status=LIMIT, error=str(exc), retry_at=exc.until)
                    except Exception as exc:
                        if cancel is not None and cancel.is_cancelled():
                            raise
                        rec.update(status=FAILED, error=str(exc) or type(exc).__name__,
                                   needs_user=isinstance(exc, youtube.NeedsUser))
                        draft = getattr(exc, "draft_url", "")
                        if draft and draft not in rec.get("drafts", []):
                            rec["drafts"] = [*rec.get("drafts", []), draft]
                        log(f"Выкладка: {t.label()} — ✗ {rec['error']}")
                    _note_music_claims(session, rec)
                    results[t.key] = rec
                    save(channels=results)
                    live.finish(t, rec)
            finally:
                if session is not None:
                    try:
                        ap.stop(pid)  # браузер после выкладки не нужен
                    except Exception as exc:
                        log(f"Выкладка: не закрыл профиль {pid}: {exc}")

    if coauthors:
        _accept_coauthors(req, targets, coauthors, results, save, cancel)
    lines = [_line(t, results.get(t.key) or {}) for t in targets]
    failed = [t for t in targets if (results.get(t.key) or {}).get("status") == FAILED]
    limited = [t for t in targets if (results.get(t.key) or {}).get("status") == LIMIT]
    drafts = [d for t in targets for d in (results.get(t.key) or {}).get("drafts", [])]
    if failed or limited:
        summary = "; ".join(_line(t, results[t.key]) for t in failed + limited)
        if drafts:
            summary += ". Недогруженные черновики в Studio — удали: " + ", ".join(drafts)
        waiting_user = [t for t in failed if results[t.key].get("needs_user")]
        if waiting_user:
            raise RuntimeError(f"Выкладка ждёт тебя: {summary}. Сделай, что просит YouTube, и нажми «Продолжить»")
        # остальное повторяется само, пока не выйдет: лимит — когда снимется, сбой — через 10/30/60 мин, дальше раз в час
        n = int(state.get("retries") or 0)
        save(retries=n + 1)
        wait = RETRY_MIN[min(n, len(RETRY_MIN) - 1)] * 60
        until = time.time() + wait
        if limited:
            until = max(until if failed else 0, min(float(results[t.key].get("retry_at") or 0) for t in limited))
        raise PublishLimit(f"Выложено не везде: {summary}. Повторю сам {datetime.fromtimestamp(until).strftime('%d.%m в %H:%M')} "
                           f"(попытка {n + 2})", until)
    removed_lines = []
    unproven = [t.label() for t in targets if (results.get(t.key) or {}).get("status") == DONE
                and not (results.get(t.key) or {}).get("file_ok")]
    if not req.keep_files and unproven:
        # файл с компьютера — единственная копия: удаляем, только когда Studio показала видео на КАЖДОМ канале
        removed_lines.append("Файл ролика на компьютере НЕ удалён: не подтверждено, что Studio приняла видеофайл на "
                             f"{', '.join(unproven)} — проверь ролик в Studio и удали файл сам")
    elif not req.keep_files:
        for v in versions:
            r = remove_videos(v.folder, [Path(f) for f, _ in v.files])
            if r:
                removed_lines.append(r)
        r = remove_run_copy(Path(req.run_dir)) if req.run_dir else ""
        if r:
            removed_lines.append(r)
    removed = " ".join(removed_lines)
    done = sum(1 for t in targets if (results.get(t.key) or {}).get("status") == DONE)
    step(1.0, f"Выкладка готова: {done} канал(а/ов)")
    mat_lines = []
    warnings: list[str] = []
    for v in versions:
        vm = mat if v is versions[0] else by_version.get(registry.normalize(v.language))
        if vm is None:
            continue
        tag = f" [{v.language.upper()}]" if len(versions) > 1 else ""
        mat_lines += [(f"Обложка{tag}: {Path(vm['thumbnail']).name}" if vm.get("thumbnail")
                       else f"Обложка{tag}: без обложки — поставишь сам"), f"Название{tag}: {vm['title']}",
                      *[str(x) for x in vm.get("lines") or []]]
        # версия-перевод берёт материалы основной — то же предупреждение второй раз не повторяем
        warnings += [f"{tag.strip()} {w}".strip() for w in vm.get("warnings") or []
                     if not any(x.endswith(str(w)) for x in warnings)]
    return {
        "summary": f"Выложено на {done} канал(а/ов)",
        "lines": [*mat_lines, *lines,
                  *([removed] if removed else []),
                  *([f"Недогруженные черновики прошлых попыток — удали в Studio: {', '.join(drafts)}"] if drafts else [])],
        "folder": str(req.folder) if req.folder.is_dir() else "",
        "warnings": [*coauthor_warnings(targets, results), *cover_warnings(targets, results), *([removed] if removed.startswith("Файл ролика на компьютере НЕ") else []),
                     *warnings],  # всплывут отдельно: соавтор, файл не удалён, без обложки…
    }


PROFILE_RESTARTS = 2  # профиль AdsPower упал посреди канала — столько раз перезапускаем сразу (дальше — повтор позже)


def _profile_lost(ap, pid: str, exc: BaseException) -> bool:
    """Шаг упал, потому что пропал браузер профиля (ошибка шага вроде «нет элемента» — лишь следствие)?
    AdsPower сам не отвечает — AdsPowerDown: ждём программу, а не перезапускаем профиль впустую."""
    if isinstance(exc, AdsPowerDown) or not ap.alive():
        raise AdsPowerDown(ADSPOWER_DOWN) from exc
    if youtube.browser_lost(exc):
        return True
    try:
        return not ap.is_open(pid)
    except AdsPowerDown:
        raise
    except Exception:
        return False  # не узнать — считаем ошибкой шага, как раньше


def _restart_profile(ap, pid: str, label: str, attempt: int):
    log(f"Выкладка: {label} — профиль AdsPower закрылся, перезапускаю (попытка {attempt + 1})")
    try:
        ap.stop(pid)
    except Exception:
        pass
    time.sleep(10)
    return None  # новая сессия откроется перед следующей попыткой


class _Live:
    """Живой статус выкладки по каналам — в состоянии задачи (state["live"]), очередь показывает его таблицей
    и обновляет сама: шаг Studio, процент загрузки файла, время выхода, итог."""

    def __init__(self, targets, results, save, progress) -> None:
        from engine.publish.studio_kit import set_reporter

        self.save, self.progress, self.total = save, progress, len(targets)
        self.rows: dict[str, dict] = {}
        for t in targets:
            rec = results.get(t.key) or {}
            if rec.get("status") == DONE:
                status, stage = "done", "выложено ✓"
            elif rec.get("status") == SKIPPED:
                status, stage = "done", f"пропущен — {rec.get('error')}"
            elif rec.get("status") in (FAILED, LIMIT):
                # прошлая попытка не удалась — в этой попытке канал ждёт своей очереди (грузим по одному)
                status, stage = "wait", "ждёт очереди — прошлая попытка не удалась, повторяю"
            else:
                status, stage = "wait", "ждёт очереди"
            self.rows[t.key] = {"channel": t.label(), "status": status, "pct": 100 if status == "done" else 0,
                                "stage": stage, "when": _human(rec["publish_at"]) if rec.get("publish_at") else "",
                                "updated": time.time()}
        self.current: str = ""
        set_reporter(self._stage)
        self._push()

    def _push(self) -> None:
        self.save(live=list(self.rows.values()))

    def set(self, t, **values) -> None:
        self.rows[t.key].update(values, updated=time.time())
        self._push()

    def start(self, t, when_iso: str, pct_speed, done_n: int = 0) -> None:
        self.current = t.key
        self.done_n = done_n
        self.phase = ""
        self.set(t, status="active", stage="начинаю", pct=0, when=_human(when_iso), speed=pct_speed)

    # шаги одной выкладки по порядку и какая доля работы пройдена к началу шага: бар канала и общий бар задачи
    # идут вперёд на каждом шаге (раньше стояли на месте, пока шёл не файл, а Studio — монетизация, заставка…)
    _STAGES = (("переключение на канал", 3), ("открытие окна загрузки", 6), ("продолжение", 6), ("выбор файла", 10),
               ("название", 14), ("описание", 18), ("обложка", 22), ("плейлист", 26), ("не для детей", 29),
               ("Show more", 31), ("Далее", 34), ("монетизация", 40), ("пригодность", 50), ("шаг Visibility", 56),
               ("дата и время", 59), ("ожидание полной загрузки", 62), ("сохранение", 90),
               ("проверка, что видео", 92), ("конечная заставка", 94), ("приглашение соавторов", 96),
               ("комментарии", 98), ("комментарий", 98))
    UPLOAD_SPAN = (62, 88)  # на этом отрезке бара — реальный процент загрузки файла

    def after_upload(self, t, what: str, count: str = "") -> None:
        """Ролик уже загружен, идёт долгий шаг после него (призыв под роликами, ответы зрителям) — подписать
        в очереди, чтобы было видно: выкладка не зависла, а занята этим шагом. count — сколько уже сделано."""
        self.current = t.key
        if what != getattr(self, "phase", ""):
            self.count = ""
        self.phase, self.count = what, count or getattr(self, "count", "")
        self._advance(t.key, 100, "")

    def _advance(self, key: str, pct: int, stage: str) -> None:
        row = self.rows[key]
        phase = getattr(self, "phase", "")
        if phase and key == self.current:  # шаг Studio внутри долгого шага — к нему, а не вместо него
            head = f"загружено ✓ — {phase}" + (f" ({self.count})" if self.count else "")
            stage = f"{head}: {stage}" if stage else head
        row.update(pct=max(int(row.get("pct") or 0), min(99, pct)), stage=stage, updated=time.time())
        self._push()
        if self.progress and row.get("status") == "active":
            frac = row["pct"] / 100
            self.progress(0.05 + 0.9 * (getattr(self, "done_n", 0) + frac) / max(1, self.total),
                          f"Выкладка {getattr(self, 'done_n', 0) + 1}/{self.total}: {row['channel']} — {stage}")

    def _stage(self, stage: str) -> None:
        key = self.current
        if key not in self.rows:
            return
        if getattr(self, "phase", ""):  # ролик уже загружен — шаги Studio идут внутри долгого шага
            self._advance(key, 100, stage)
            return
        low = stage.lower()
        mapped = next((pct for word, pct in self._STAGES if word.lower() in low), None)
        if mapped is None:
            self.rows[key].update(stage=stage, updated=time.time())
            self._push()
        else:
            self._advance(key, mapped, stage)

    def file_progress(self, t, done_n: int):
        def cb(p: float, msg: str) -> None:
            lo, hi = self.UPLOAD_SPAN
            self.done_n = done_n
            self._advance(t.key, lo + round((hi - lo) * max(0.0, min(1.0, p))), msg)
        return cb

    def finish(self, t, rec: dict) -> None:
        status = rec.get("status")
        stage = {"done": "выложено ✓", "limit": "лимит YouTube — продолжу позже",
                 WAITING: "жду AdsPower — продолжу сам"}.get(status) or \
            f"ошибка: {str(rec.get('error') or '')[:160]}"
        self.set(t, status=status or "failed", stage=stage, pct=100 if status == DONE else self.rows[t.key]["pct"])
        self.current = ""
        self.phase = ""


def _file_progress(progress: ProgressCb | None, done_n: int, total: int, label: str):
    """Процент загрузки файла → общий прогресс задачи и строка в очереди."""
    if progress is None:
        return None

    def cb(p: float, msg: str) -> None:
        progress(0.05 + 0.9 * (done_n + p) / total, f"Выкладка {done_n + 1}/{total}: {label} — {msg}")

    return cb


def _accept_coauthors(req: PublishRequest, targets: list, coauthors: list, results: dict, save, cancel) -> None:
    """В профилях соавторов принять приглашения к выложенным видео. Не вышло — пометка у канала
    (ролик уже выложен, его не трогаем)."""
    key, url = req.adspower
    with AdsPower(key, url) as ap:
        if not ap.alive():
            raise PublishLimit(ADSPOWER_DOWN, time.time() + ADSPOWER_POLL_SEC)
        for co in coauthors:
            pending = [t for t in targets if (results.get(t.key) or {}).get("status") == DONE
                       and co.key not in (results[t.key].get("coauthors_ok") or [])]
            if not pending:
                continue
            try:
                session = ap.start(co.profile_id, headless=not req.visible)
            except AdsPowerDown:
                raise PublishLimit(ADSPOWER_DOWN, time.time() + ADSPOWER_POLL_SEC) from None
            except Exception as exc:
                for t in pending:
                    _coauthor_failed(results[t.key], co, f"профиль не запустился: {exc}")
                save(channels=results)
                continue
            accepted: list[str] = []  # принятые в этом профиле ролики (за раз принимаем все ждущие от канала)
            try:
                for t in pending:
                    if cancel:
                        cancel.check()
                    rec = results[t.key]
                    try:
                        link = (rec.get("invites") or {}).get(co.key, "")
                        for attempt in range(1, PROFILE_RESTARTS + 2):
                            # Ссылка — на пару «владелец → соавтор», и приниматься должна КАЖДАЯ пара: у каналов одного
                            # человека названия роликов совпадают, поэтому «этот ролик уже принят» по названию —
                            # ошибка (так принималось одно приглашение из нескольких, остальные висели «Pending»)
                            try:
                                got = youtube.accept_coauthor(session.cdp_url, co.channel, link,
                                                              title=rec.get("title", ""), cancel=cancel)
                                accepted += got
                                if rec.get("title") and all(g != "(без названия)" for g in got) \
                                        and not _title_in(rec["title"], got):
                                    raise youtube.StudioError(
                                        "приняты другие приглашения от канала (" + "; ".join(g[:50] for g in got)
                                        + "), а этого ролика среди ждущих нет — проверь соавторство в Studio")
                                break
                            except Exception as exc:
                                if cancel is not None and cancel.is_cancelled():
                                    raise
                                if not _profile_lost(ap, co.profile_id, exc) or attempt > PROFILE_RESTARTS:
                                    raise
                                _restart_profile(ap, co.profile_id, f"соавтор {co.channel.name}", attempt)
                                session = ap.start(co.profile_id, headless=not req.visible)
                        rec.setdefault("coauthors_ok", []).append(co.key)
                        rec["coauthors_failed"] = [f for f in rec.get("coauthors_failed") or []
                                                   if f.get("key") != co.key]
                        collabs.mark_accepted(t.key, co.key)
                    except AdsPowerDown:
                        save(channels=results)  # принятое уже записано; остальное — когда AdsPower вернётся
                        raise PublishLimit(ADSPOWER_DOWN, time.time() + ADSPOWER_POLL_SEC) from None
                    except Exception as exc:
                        if cancel is not None and cancel.is_cancelled():
                            raise
                        _coauthor_failed(rec, co, str(exc))
                        collabs.mark_failed(t.key, co.key, str(exc), link=(rec.get("invites") or {}).get(co.key, ""),
                                            owner_label=t.label())
                        if isinstance(exc, youtube.NeedsUser):  # Google просит пароль — остальные ролики так же
                            for other in pending[pending.index(t) + 1:]:
                                _coauthor_failed(results[other.key], co, str(exc))
                                collabs.mark_failed(other.key, co.key, str(exc),
                                                    link=(results[other.key].get("invites") or {}).get(co.key, ""),
                                                    owner_label=other.label())
                            save(channels=results)
                            break
                    save(channels=results)
            finally:
                try:
                    ap.stop(co.profile_id)
                except Exception:
                    pass


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", t or "")).strip().lower()


def _title_in(title: str, accepted: list[str]) -> bool:
    """Ролик среди принятых (Studio может обрезать или по-другому показать значки — сравниваем по словам)."""
    t = _norm_title(title)
    return bool(t) and any(a and (a == t or (len(a) >= 20 and (t.startswith(a) or a.startswith(t))))
                           for a in map(_norm_title, accepted))


def _coauthor_failed(rec: dict, co, reason: str) -> None:
    """Соавтор не присоединился (не приглашён, не принял, профиль не открылся) — запомнить для итогов:
    это всплывёт предупреждением со ссылкой, чтобы доделать руками."""
    reason = re.sub(r"\s+", " ", str(reason)).strip()[:400]
    fails = [f for f in rec.get("coauthors_failed") or [] if f.get("key") != co.key]
    fails.append({"key": co.key, "name": co.channel.name, "handle": co.channel.handle,
                  "profile": co.profile, "link": (rec.get("invites") or {}).get(co.key, ""),
                  "reason": reason})
    rec["coauthors_failed"] = fails
    rec.setdefault("notes", []).append(f"соавтор {co.channel.name}: {reason}")


def coauthor_warnings(targets: list, results: dict) -> list[str]:
    """Соавторы, которые так и не присоединились, — отдельными предупреждениями: что и где доделать руками."""
    out = []
    for t in targets:
        rec = results.get(t.key) or {}
        if rec.get("status") != DONE:
            continue
        for f in rec.get("coauthors_failed") or []:
            if f.get("key") in (rec.get("coauthors_ok") or []):
                continue
            where = f"«{rec.get('title') or t.channel.name}» на {t.channel.name}" + (f" ({rec['url']})" if rec.get("url") else "")
            prof = f" ({f['profile']})" if f.get("profile") else ""
            how = (f"открой профиль соавтора{prof}, перейди по ссылке {f['link']} и нажми Accept"
                   if f.get("link") else "пригласи соавтора на странице видео (Show more → Collaboration) и прими приглашение в его профиле")
            out.append(f"Соавтор {f.get('name')} {f.get('handle') or ''} НЕ присоединился к {where}: {f.get('reason')}. "
                       f"Доделай руками: {how}")
    return out


def cover_warnings(targets: list, results: dict) -> list[str]:
    """Каналы, где ролик вышел, а обложка в Studio не встала, — отдельными предупреждениями: куда поставить руками."""
    out = []
    for t in targets:
        rec = results.get(t.key) or {}
        if rec.get("status") != DONE:
            continue
        for n in rec.get("notes") or []:
            if str(n).startswith(youtube.COVER_NOT_SET):
                where = f"«{rec.get('title') or t.channel.name}» на {t.channel.name}" + (f" ({rec['url']})" if rec.get("url") else "")
                out.append(f"{where}: {n}")
    return out


def _human(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(schedule.MSK).strftime("%d.%m %H:%M МСК")


def _line(t: registry.Target, rec: dict) -> str:
    status = rec.get("status")
    when = f", выход {_human(rec['publish_at'])}" if rec.get("publish_at") else ""
    if status == DONE:
        notes = "; ".join(rec.get("notes") or [])
        return f"✓ {t.label()}{when}" + (f" — {notes}" if notes else "")
    if status == LIMIT:
        return f"⏸ {t.label()}: {rec.get('error')}"
    if status == FAILED:
        return f"✗ {t.label()}: {rec.get('error')}"
    if status == SKIPPED:
        return f"— {t.label()}: {rec.get('error')}"
    if status == WAITING:
        return f"⏸ {t.label()}: жду AdsPower"
    return f"… {t.label()}: не выкладывался"


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
# файлы с безликими именами — при уборке получают тему и дату ролика («Тема 07.11.2026 — главы.txt»)
_GENERIC = re.compile(r"^(?:\d+ процент\w* )?(озвучка|сценарий|главы|описание|текст|credits|субтитры)$", re.I)
_STAMP = re.compile(r"_(\d{4})(\d{2})(\d{2})-\d{6}$")


def remove_videos(folder: Path, uploaded: list[Path]) -> str:
    """«Не оставлять ролики»: после выкладки на все каналы удаляем только видео, которое выложили (основной
    ролик и копии скорости) — оно весит гигабайты. Текст, озвучка, главы, описание и обложки остаются: по ним
    ролик можно пересобрать. Безликие имена файлов получают тему и дату, чтобы в них было легко ориентироваться."""
    if not folder.is_dir():
        return ""
    gone, size = 0, 0
    for f in uploaded:
        try:
            if f.is_file() and f.suffix.lower() in VIDEO_EXTS and f.resolve().parent == folder.resolve():
                size += f.stat().st_size
                f.unlink()
                gone += 1
        except OSError as exc:
            log(f"Выкладка: не удалил {f.name} ({exc})")
    renamed = _name_leftovers(folder)
    if not gone:
        return ""
    log(f"Выкладка: удалено видео ({gone} файл(а/ов), {size / 1e9:.1f} ГБ) из «{folder.name}», остальное оставлено")
    return (f"Видео удалено с компьютера: {gone} файл(а/ов), {size / 1e9:.1f} ГБ. Текст, озвучка и главы остались "
            f"в папке «{folder.name}»" + (f" (переименованы понятно: {renamed})" if renamed else ""))


def remove_run_copy(run_dir: Path) -> str:
    """«Не оставлять ролики» для слайдов: в рабочей папке запуска лежит своя копия ролика (final.mp4, переводы)
    и, у старых запусков, скачанные стоки — после выкладки они только занимают место. Озвучка и тексты остаются."""
    from engine.slides.paths import RUNS_DIR
    from engine.slides.resume import drop_stock

    try:
        run_dir.resolve().relative_to(RUNS_DIR.resolve())
    except (OSError, ValueError):
        return ""  # не рабочая папка программы — не трогаем
    if not run_dir.is_dir() or run_dir.resolve() == RUNS_DIR.resolve():
        return ""
    size = drop_stock(run_dir)
    for f in run_dir.glob("**/final.mp4"):
        try:
            if f.is_file():
                size += f.stat().st_size
                f.unlink()
        except OSError as exc:
            log(f"Выкладка: не удалил {f} ({exc})")
    if not size:
        return ""
    log(f"Выкладка: рабочая копия ролика удалена ({size / 1e9:.1f} ГБ, {run_dir.name})")
    return f"Рабочая копия ролика в программе тоже удалена: {size / 1e9:.1f} ГБ."


def topic_and_date(folder: Path) -> str:
    """«Тема_20261107-111324» → «Тема 07.11.2026»; «Тема 30.09» — как есть."""
    m = _STAMP.search(folder.name)
    if m:
        return f"{folder.name[:m.start()].strip(' _')} {m.group(3)}.{m.group(2)}.{m.group(1)}"
    return folder.name.strip()


def _name_leftovers(folder: Path) -> int:
    label = topic_and_date(folder)
    n = 0
    for f in sorted(folder.iterdir()):
        if not f.is_file() or f.name.startswith(".") or not _GENERIC.match(f.stem):
            continue
        dest = folder / f"{label} — {f.name}"
        if dest.exists():
            continue
        try:
            f.rename(dest)
            n += 1
        except OSError:
            pass
    return n


def run_comments(channels: list[str], adspower: tuple[str, str], llm: tuple[str, str, str] | None, *,
                 cancel: CancelToken | None = None, progress: ProgressCb | None = None) -> dict[str, Any]:
    """Кнопка «Ответить на комментарии»: по выбранным каналам — сердечко + ответ каждому без ответа.
    Сбой на канале — строка в итогах, остальные каналы проходят."""
    from engine.publish import comments

    targets = registry.resolve(channels)
    if not targets:
        raise ValueError("Выбери хотя бы один канал")
    lines: list[str] = []
    failed: list[str] = []
    key, url = adspower
    with AdsPower(key, url) as ap:
        by_profile: dict[str, list[registry.Target]] = {}
        for t in targets:
            by_profile.setdefault(t.profile_id, []).append(t)
        n = 0
        for pid, group in by_profile.items():
            was_open = ap.is_open(pid)
            session = ap.start(pid)
            try:
                for t in group:
                    if cancel:
                        cancel.check()
                    n += 1
                    if progress:
                        progress((n - 1) / len(targets), f"Комментарии {n}/{len(targets)}: {t.label()}")
                    try:
                        def step_cb(_f, msg, n=n, t=t):
                            if progress:
                                progress((n - 1) / len(targets), f"Комментарии {n}/{len(targets)}: {t.label()} — {msg}")

                        res = comments.answer_comments(session.cdp_url, t.channel, llm=llm, cancel=cancel,
                                                       progress=step_cb)
                        lines.append(f"✓ {t.label()}: " + "; ".join(res))
                    except Exception as exc:
                        if cancel is not None and cancel.is_cancelled():
                            raise
                        if youtube.browser_lost(exc):
                            session = _restart_profile(ap, pid, t.label(), 1) or ap.start(pid)
                        failed.append(t.label())
                        lines.append(f"✗ {t.label()}: {str(exc).splitlines()[0][:300]}")
            finally:
                if not was_open:
                    try:
                        ap.stop(pid)
                    except Exception:
                        pass
    if failed and len(failed) == len(targets):
        raise RuntimeError("Комментарии: не получилось ни на одном канале — " + "; ".join(lines))
    return {"summary": f"Комментарии: {len(targets) - len(failed)} из {len(targets)} каналов", "lines": lines}



def run_polls(channel: str, polls: list[dict], step_days: float, count: int, adspower: tuple[str, str],
              llm: tuple[str, str, str] | None, keys: list[str], *, memo: dict | None = None, save=None,
              cancel: CancelToken | None = None, progress: ProgressCb | None = None) -> dict[str, Any]:
    """«Голосования»: опросы на канал — первый сразу, остальные каждые step_days. Опросов не дали (или меньше
    count) — дописывает нейросеть по теме канала. Сбой одного опроса — строка в итогах, остальные идут дальше.
    memo — память задачи (job.state): старт расписания, написанные опросы и уже выложенные. Повтор/«Продолжить»
    не выкладывает опрос второй раз, не переписывает опросы заново и не сдвигает расписание."""
    from datetime import timezone

    from engine.publish import polls as pl

    memo = memo if memo is not None else {}
    save = save or (lambda: None)
    targets = registry.resolve([channel])
    if not targets:
        raise ValueError("Выбери канал")
    t = targets[0]
    items = [p for p in (pl.clean(x) for x in (memo.get("polls") or polls or [])) if p]
    count = max(len(items), min(pl.COUNT_MAX, int(count or len(items) or 1)))
    if len(items) < count:
        if progress:
            progress(0.02, f"Голосования {t.label()}: нейросеть пишет опросы")
        name, titles = pl.channel_titles(t.channel.id, keys)
        asked = [h.get("question", "") for h in pl.history(t.channel.id)] + [p.question for p in items]
        items += pl.generate(name or t.channel.name, titles, count - len(items), language=t.channel.language,
                             asked=asked, llm=llm, cancel=cancel)
    memo["polls"] = [p.as_dict() for p in items]
    if not memo.get("start"):
        memo["start"] = datetime.now(timezone.utc).isoformat()
    save()
    times = pl.plan(len(items), step_days, datetime.fromisoformat(memo["start"]))
    done = set(memo.get("done") or [])  # номера опросов, которые уже выложены (или выложены без проверки)
    todo = [(n, p, w) for n, (p, w) in enumerate(zip(items, times)) if n not in done]
    # повтор спустя время: часть дат уже прошла — сдвигаем всё оставшееся расписание вперёд, шаг между опросами тот же
    soonest = min((w for _n, _p, w in todo if w is not None), default=None)
    now = datetime.now(timezone.utc)
    if soonest is not None and soonest <= now + pl.MIN_AHEAD:
        shift = pl.round_up(now + pl.MIN_AHEAD) - soonest
        todo = [(n, p, w + shift if w is not None else None) for n, p, w in todo]
    lines: list[str] = [f"• уже выложен раньше: «{items[n].question}»" for n in sorted(done) if n < len(items)]
    failed = 0
    key, url = adspower
    if todo:
        with AdsPower(key, url) as ap:
            was_open = ap.is_open(t.profile_id)
            session = ap.start(t.profile_id)
            try:
                from engine.publish.studio_kit import set_channel, step

                set_channel(t.channel.handle or t.channel.id)
                with youtube.studio_page(session.cdp_url) as page:
                    pl.accept_dialogs(page)
                    with step(page, "голосования: переключение на канал"):
                        youtube.switch_to(page, t.channel, cancel=cancel)
                    for k, (n, poll, when) in enumerate(todo, 1):
                        if cancel:
                            cancel.check()
                        if progress:
                            progress((k - 1) / len(todo), f"Голосования {t.label()}: {k}/{len(todo)}")
                        try:
                            lines.append("✓ " + pl.post_poll(page, t.channel, poll, when, cancel=cancel))
                            pl.remember(t.channel.id, poll, when, "scheduled" if when else "published")
                        except pl.Unverified as exc:
                            # кнопку нажали, но поста не видно — второй раз не выкладываем (будет дубль)
                            pl.remember(t.channel.id, poll, when, "unverified")
                            failed += 1
                            lines.append(f"✗ «{poll.question[:60]}»: {exc}")
                        except Exception as exc:
                            if cancel is not None and cancel.is_cancelled():
                                raise
                            if youtube.browser_lost(exc):
                                raise
                            failed += 1
                            lines.append(f"✗ «{poll.question[:60]}»: {str(exc).splitlines()[0][:300]}")
                            pl.discard(page)
                            continue
                        done.add(n)
                        memo["done"] = sorted(done)
                        save()
            finally:
                if not was_open:
                    try:
                        ap.stop(t.profile_id)
                    except Exception:
                        pass
    if todo and failed == len(todo):
        raise RuntimeError("Голосования: ни один опрос не вышел — " + "; ".join(lines))
    summary = f"Голосования {t.label()}: {len(items) - failed} из {len(items)} (шаг {step_days:g} дн.)"
    return {"summary": summary, "lines": lines, "warnings": [ln for ln in lines if ln.startswith("✗")]}
