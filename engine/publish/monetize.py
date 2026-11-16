"""Канал на монетизации: в окне загрузки Studio есть шаги «Monetization» и «Ad suitability».

Программа определяет это сама, по самому окну загрузки (шаг MONETIZATION в его шкале). Канал без
монетизации — шагов нет, выкладка идёт как раньше. С монетизацией:
- Monetization: реклама «On», реклама внутри ролика, места рекламы ставит расширение NexLev
  («Every 20 Seconds» → «Add»), автоматические места YouTube выключаются — как на уже вышедших роликах;
- Ad suitability: «None of the above» (нарушений нет) → «Submit rating».

Всё уже сделано (повтор, черновик прошлой попытки) — шаг пропускается. Не получилось (нет расширения NexLev
в профиле, Studio показала незнакомое) — NeedsUser со снимком: ролик без рекламы не выкладывается.
Проверено вживую на канале «Трогательные рассказы» 14.11.2026.
"""

from __future__ import annotations

import re

from engine.core.cancel import CancelToken
from engine.core.logutil import log
from engine.publish import human

AD_EVERY = "Every 20 Seconds"  # пункт меню NexLev: реклама каждые 20 секунд
DIALOG = "ytcp-uploads-dialog"
STEP_TAB = f"{DIALOG} ytcp-stepper button[role=tab]"
PLACER = "#nexlev-ad-placer"  # панель NexLev в редакторе мест рекламы (открытый shadow DOM — локаторы проходят)
NEXLEV_WAIT_MS = 30000
PLACING_MAX_S = 240  # NexLev ставит места по одному (266 на полтора часа — ~10 с)


def steps(page) -> list[str]:
    """Шаги окна загрузки по порядку (служебные метки: DETAILS, MONETIZATION, CONTENT_RATINGS, …)."""
    try:
        return [str(x) for x in page.locator(STEP_TAB).evaluate_all("els => els.map(e => e.getAttribute('test-id'))")]
    except Exception:
        return []


def active_step(page) -> str:
    try:
        return str(page.locator(f"{STEP_TAB}[aria-selected=true]").first.get_attribute("test-id", timeout=5000) or "")
    except Exception:
        return ""


def _act(cancel: CancelToken | None) -> None:
    if cancel is not None:
        cancel.check()


def _text(page, selector: str) -> str:
    try:
        return " ".join(page.locator(selector).first.inner_text(timeout=5000).split())
    except Exception:
        return ""


def _needs_user(message: str):
    from engine.publish.youtube import NeedsUser

    return NeedsUser(message)


M10N = f"{DIALOG} ytcp-video-monetization"
M10N_POPUP = "ytcp-video-monetization-edit-dialog"
RADIO_ON = f"{M10N_POPUP} tp-yt-paper-radio-button[name=ON]"
RADIO_OFF = f"{M10N_POPUP} tp-yt-paper-radio-button[name=OFF]"
POPUP_DONE = f"{M10N_POPUP} #save-button"


def _is_on(page) -> bool:
    """Монетизация видео уже «On»: в поле не «Select» и не «Off» (всплывающий список в проверку не входит)."""
    try:
        shown = " ".join(page.locator(f"{M10N} .m10n-text").first.inner_text(timeout=5000).split())
    except Exception:
        return False
    return bool(re.search(r"\bOn\b", shown)) and "Select" not in shown


def _popup_open(page) -> bool:
    try:
        return page.locator(RADIO_ON).first.is_visible()
    except Exception:
        return False


def _done_enabled(page) -> bool:
    try:
        return page.locator(POPUP_DONE).first.get_attribute("aria-disabled") != "true"
    except Exception:
        return False


def monetization(page, cancel: CancelToken | None = None) -> list[str]:
    """Шаг Monetization: «On» + реклама внутри ролика по местам NexLev. → пометки для итогов."""
    from engine.publish.studio_kit import diagnose

    notes: list[str] = []
    if _popup_open(page):  # список «On/Off» остался открытым с прошлого раза — закрыть и начать чисто
        human.press(page, "Escape")
        page.wait_for_timeout(1000)
    if not _is_on(page):
        _turn_on(page, cancel)
    _midroll_checkbox(page, cancel)
    status = _text(page, DIALOG)
    if "manual mid-roll ad slots" in status:
        log("Монетизация: реклама уже включена, места рекламы уже расставлены — пропускаю")
        return ["монетизация: реклама уже была включена и расставлена"]
    _act(cancel)
    human.click(page, page.locator(f"{DIALOG} #place-manually-button").first)
    placer = page.locator(PLACER)
    try:
        placer.locator("button").first.wait_for(state="visible", timeout=NEXLEV_WAIT_MS)
    except Exception:
        folder = diagnose(page, "монетизация: нет NexLev", RuntimeError("нет панели NexLev"))
        raise _needs_user("в профиле нет расширения NexLev (или оно не загрузилось) — поставь его в профиль, "
                          f"потом «Продолжить». Снимок: {folder}") from None
    count = _place_ads(page, cancel)
    _automatic_off(page, cancel)  # на случай, если галочка вернулась
    _act(cancel)
    human.click(page, page.get_by_role("button", name="Continue").first)
    page.wait_for_timeout(4000)
    if "manual mid-roll ad slots" not in _text(page, DIALOG):
        folder = diagnose(page, "монетизация: места не сохранились", RuntimeError("места рекламы не сохранились"))
        raise _needs_user(f"места рекламы не сохранились после «Continue» — проверь шаг Monetization. Снимок: {folder}")
    log(f"Монетизация: реклама каждые 20 с — мест {count}, автоматические места выключены")
    notes.append(f"монетизация: реклама каждые 20 с ({count} мест, NexLev)")
    return notes


def _turn_on(page, cancel) -> None:
    """Список монетизации → «On» → «Done». «Done» неактивна, пока выбор не изменился: если «On» уже стоит
    в списке сам, переключаем «Off» → «On» (изменение засчитывается) и только потом «Done»."""
    from engine.publish.studio_kit import diagnose

    _act(cancel)
    human.click(page, page.locator(f"{M10N} ytcp-icon-button.edit-button, {M10N} .m10n-text").first)
    try:
        page.locator(RADIO_ON).first.wait_for(state="visible", timeout=10000)
        for attempt in range(3):
            _act(cancel)
            if attempt:
                human.click(page, page.locator(RADIO_OFF).first)
                page.wait_for_timeout(600)
            human.click(page, page.locator(RADIO_ON).first)
            page.wait_for_timeout(900)
            if _done_enabled(page):
                break
        else:
            raise RuntimeError("«Done» не активна")
        _act(cancel)
        human.click(page, page.locator(POPUP_DONE).first)
        page.wait_for_timeout(2000)
    except Exception:
        folder = diagnose(page, "монетизация: «On»", RuntimeError("не выбрался «On»"))
        raise _needs_user(f"не получилось включить монетизацию («On») — включи руками и «Продолжить». Снимок: {folder}") from None
    if _popup_open(page) or not _is_on(page):
        folder = diagnose(page, "монетизация: «On» не встал", RuntimeError("«On» не встал"))
        raise _needs_user(f"монетизация не включилась — проверь шаг Monetization. Снимок: {folder}")
    log("Монетизация: включена («On»)")


def _midroll_checkbox(page, cancel) -> None:
    box = page.locator(f"{DIALOG} ytcp-checkbox-lit").filter(has_text="Show mid-roll ads").first
    try:
        if box.locator("div#checkbox").first.get_attribute("aria-checked", timeout=5000) == "true":
            return
    except Exception:
        return  # галочки нет (вёрстка другая) — дальше проверка по тексту шага
    _act(cancel)
    human.click(page, box.locator("#checkbox-container").first)
    page.wait_for_timeout(1000)


def _placer_state(page) -> str:
    """Что показывает панель NexLev: «place» — расстановка («Auto-Placed» / «Every …» + «Add»);
    «remove» — «Remove Ads» (на ролике уже есть места рекламы); пусто — панели нет."""
    try:
        texts = page.locator(PLACER).locator("button").all_inner_texts()
    except Exception:
        return ""
    joined = " ".join(" ".join(t.split()) for t in texts)
    if "Remove Ads" in joined:
        return "remove"
    if re.search(r"Auto-Placed|Every |Custom|Smart", joined):
        return "place"
    return ""


def _clear_slots(page, cancel) -> None:
    """«Remove Ads» → «Ad Slot Cleanup» → «All slots» → «Remove N ad slots»: убрать все места рекламы ролика,
    чтобы панель NexLev снова позволила расставить их."""
    from engine.publish.studio_kit import diagnose

    placer = page.locator(PLACER)
    _act(cancel)
    human.click(page, placer.locator("button", has_text="Remove Ads").first)
    page.wait_for_timeout(1500)
    remove = page.get_by_role("button", name=re.compile(r"Remove \d+ ad slots?")).first
    try:
        remove.wait_for(state="visible", timeout=10000)
        _act(cancel)
        human.click(page, remove)
    except Exception:
        folder = diagnose(page, "монетизация: очистка мест", RuntimeError("нет кнопки «Remove N ad slots»"))
        raise _needs_user(f"не получилось убрать прежние места рекламы в NexLev — очисти их руками и «Продолжить». "
                          f"Снимок: {folder}") from None
    for _ in range(60):
        page.wait_for_timeout(1000)
        _act(cancel)
        if _placer_state(page) == "place":
            return
    folder = diagnose(page, "монетизация: очистка мест", RuntimeError("места не убрались"))
    raise _needs_user(f"прежние места рекламы не убрались — очисти их руками и «Продолжить». Снимок: {folder}")


def _place_ads(page, cancel) -> int:
    """Автоматические места YouTube — выключить (иначе NexLev показывает «Remove Ads»), старые места убрать,
    потом NexLev: меню режима → «Every 20 Seconds» → «Add»; ждать, пока расставит. → число мест."""
    from engine.publish.studio_kit import diagnose

    _automatic_off(page, cancel)
    page.wait_for_timeout(2000)
    placer = page.locator(PLACER)
    if _placer_state(page) == "remove":
        _clear_slots(page, cancel)
    item = page.get_by_text(AD_EVERY, exact=True).first  # меню NexLev — отдельным слоем, не внутри панели
    if not item.is_visible():
        _act(cancel)
        human.click(page, placer.locator("button").filter(has_text=re.compile(r"Auto-Placed|Every |Custom|Smart")).first)
        page.wait_for_timeout(1500)
    try:
        _act(cancel)
        human.click(page, item)
    except Exception:
        folder = diagnose(page, "монетизация: меню NexLev", RuntimeError(f"нет пункта «{AD_EVERY}»"))
        raise _needs_user(f"в меню NexLev нет пункта «{AD_EVERY}» — расставь рекламу руками и «Продолжить». "
                          f"Снимок: {folder}") from None
    page.wait_for_timeout(1200)
    _act(cancel)
    human.click(page, placer.locator("button", has_text=re.compile(r"^\s*Add\s*$")).first)
    stop = placer.locator("button", has_text="Stop")
    for _ in range(PLACING_MAX_S):
        page.wait_for_timeout(1000)
        _act(cancel)
        if not stop.count() or not stop.first.is_visible():
            break
    page.wait_for_timeout(2000)
    count = page.evaluate("() => (document.body.innerText.match(/Ad slot \\(Manual\\)/g) || []).length")
    if not count:
        folder = diagnose(page, "монетизация: NexLev не расставил", RuntimeError("мест рекламы 0"))
        raise _needs_user(f"NexLev не расставил места рекламы — проверь расширение. Снимок: {folder}")
    return int(count)


def _automatic_off(page, cancel) -> None:
    """Автоматические места YouTube выключаем: реклама только в местах NexLev (как на вышедших роликах)."""
    box = page.locator("ytcp-checkbox-lit").filter(has_text="Automatic ad slots").first
    try:
        if box.locator("div#checkbox").first.get_attribute("aria-checked", timeout=5000) != "true":
            return
    except Exception:
        return
    _act(cancel)
    human.click(page, box.locator("#checkbox-container").first)  # саму галочку перекрывает её контейнер
    page.wait_for_timeout(1000)


QUESTIONNAIRE = f"{DIALOG} ytpp-self-certification-questionnaire"


def _rated(page) -> bool:
    """Анкета уже отправлена: Studio блокирует её (атрибут disabled), кнопка «Submit rating» неактивна."""
    try:
        return page.locator(QUESTIONNAIRE).first.get_attribute("disabled", timeout=3000) is not None
    except Exception:
        return False


def ad_suitability(page, cancel: CancelToken | None = None) -> list[str]:
    """Шаг Ad suitability: «None of the above» (нарушений нет) → «Submit rating». Уже отправлено — пропуск."""
    from engine.publish.studio_kit import diagnose

    if _rated(page):
        log("Пригодность для рекламы: анкета уже отправлена — пропускаю")
        return []
    none = page.locator(f"{DIALOG} ytcp-checkbox-lit").filter(has_text="None of the above").first
    try:
        none.wait_for(state="visible", timeout=15000)
    except Exception:
        folder = diagnose(page, "пригодность: нет анкеты", RuntimeError("нет «None of the above»"))
        raise _needs_user(f"в шаге Ad suitability нет вопроса «None of the above» — отметь руками и «Продолжить». "
                          f"Снимок: {folder}") from None
    if none.locator("div#checkbox").first.get_attribute("aria-checked") != "true":
        _act(cancel)
        human.click(page, none.locator("#checkbox-container").first)
        page.wait_for_timeout(1000)
    if none.locator("div#checkbox").first.get_attribute("aria-checked") != "true":
        folder = diagnose(page, "пригодность: галочка", RuntimeError("«None of the above» не отметилась"))
        raise _needs_user(f"не отметилось «None of the above» в Ad suitability. Снимок: {folder}")
    _act(cancel)
    human.click(page, page.locator(f"{DIALOG} #submit-questionnaire-button").first)
    for _ in range(15):
        page.wait_for_timeout(1000)
        if _rated(page):
            log("Пригодность для рекламы: «нарушений нет» отправлено")
            return ["пригодность для рекламы: нарушений нет"]
    folder = diagnose(page, "пригодность: не отправилось", RuntimeError("анкета не отправилась"))
    raise _needs_user(f"анкета Ad suitability не отправилась — отправь руками и «Продолжить». Снимок: {folder}")
