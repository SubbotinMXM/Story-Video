"""Голосования в сообществе канала: текстовые опросы по теме канала, первый — сразу, остальные — по расписанию.

Тема канала — из названий его последних роликов (YouTube API / yt-dlp). Опросы пишет нейросеть
(Codex → встроенная; подписку Claude не тратим), в стиле «вопрос к жизненному опыту зрителя + 3–4 разных взгляда».
Публикация — на youtube.com, вкладка «Сообщество/Posts» канала: «Add a text poll» → вопрос, варианты →
сразу «Post» или «Action menu» → «Schedule post» → дата, время → «Schedule». Всё проверяется чтением со
страницы: вписанные поля, выставленные дата/время, сам пост во вкладке Published/Scheduled.
Что уже выкладывали — помним (data/publish/polls.json), новые вопросы не повторяют старые.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from engine.core.jsonish import json_dict
from engine.core.cancel import CancelToken
from engine.core.logutil import log
from engine.publish import human
from engine.publish.store import LOCK, read_json, write_json

HISTORY_FILE = "polls.json"
OPTION_MAX = 65  # ограничение YouTube на вариант ответа
QUESTION_MAX = 140
OPTIONS_MIN, OPTIONS_MAX = 2, 5
COUNT_MAX = 30
STEP_MAX_DAYS = 60
SLOT_MIN = 15  # YouTube даёт выбрать время с шагом 15 минут
MIN_AHEAD = timedelta(minutes=30)  # запланировать ближе к «сейчас» YouTube может не дать — сдвигаем
TITLES_FOR_TOPIC = 40

_SYSTEM = """Ты ведёшь YouTube-канал и пишешь текстовые опросы для вкладки «Сообщество».
Цель опроса — вовлечь зрителей: чтобы каждый захотел нажать вариант и написать комментарий.
Как в примерах удачных опросов канала аудиорассказов:
- «❤️ Можно ли встретить настоящую любовь после шестидесяти?» → «Конечно, любовь не спрашивает возраст» /
  «Можно, но довериться человеку непросто» / «Мне ближе дружба и душевная близость» / «Лучше жить спокойно одному»
- «Что тяжелее простить: предательство мужа или предательство детей?» → «Предательство мужа» /
  «Предательство детей» / «Простить можно всё» / «Такое не прощают»
Правила:
- тема — то, о чём канал (смотри названия его роликов), но не пересказ конкретного ролика;
- вопрос — к жизненному опыту, чувствам или мнению зрителя, простой и цепляющий; можно один эмодзи в начале;
- 3–4 варианта, каждый — отдельная понятная позиция (не «да/нет/не знаю»), вариант не длиннее 60 символов;
- без политики, религиозных споров, медицины и денежных советов; без ссылок и призывов подписаться;
- опросы разные по теме и форме, не повторяют уже заданные вопросы;
- язык — язык канала.
Ответ — только JSON: {"polls": [{"question": "…", "options": ["…", "…", "…"]}]}"""


@dataclass
class Poll:
    question: str
    options: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def clean(raw) -> Poll | None:
    """Опрос из ответа нейросети/из интерфейса → проверенный Poll (или None, если он негоден)."""
    if not isinstance(raw, dict):
        return None
    q = " ".join(str(raw.get("question") or "").split())[:QUESTION_MAX].strip()
    opts: list[str] = []
    for o in raw.get("options") or []:
        text = " ".join(str(o or "").split())
        if len(text) > OPTION_MAX:  # длиннее не впишется — режем по слову
            text = text[:OPTION_MAX].rsplit(" ", 1)[0].rstrip(" ,.;:—-")
        if text and text.lower() not in (x.lower() for x in opts):
            opts.append(text)
    opts = opts[:OPTIONS_MAX]
    if not q or len(opts) < OPTIONS_MIN:
        return None
    return Poll(q, opts)


# ---------- история ----------


def history(channel_id: str) -> list[dict]:
    return list(read_json(HISTORY_FILE, {}).get(channel_id) or [])


def remember(channel_id: str, poll: Poll, when: datetime | None, status: str) -> None:
    with LOCK:
        data = read_json(HISTORY_FILE, {})
        items = [*(data.get(channel_id) or []), {**poll.as_dict(), "at": (when or datetime.now(timezone.utc)).isoformat(),
                                                   "status": status, "saved": time.time()}]
        data[channel_id] = items[-300:]
        write_json(HISTORY_FILE, data)


# ---------- генерация ----------


def channel_titles(channel_id: str, keys: list[str]) -> tuple[str, list[str]]:
    """→ (название канала, названия последних роликов) — по ним нейросеть понимает тему канала."""
    from engine.rewrite.channels import list_channel

    try:
        _cid, title, videos = list_channel(f"https://www.youtube.com/channel/{channel_id}", keys)
    except Exception as exc:  # у нового канала ещё нет вкладки «Видео» — тему возьмём по названию
        log(f"Голосования: список роликов канала не получить ({str(exc).splitlines()[0][:120]}) — тема по названию канала")
        return "", []
    videos = sorted(videos, key=lambda v: -v.published)
    return title, [v.title for v in videos if v.title][:TITLES_FOR_TOPIC]


def prompt_for(name: str, titles: list[str], count: int, language: str, asked: list[str]) -> str:
    lines = [f"Канал: {name}", f"Язык канала: {language or 'как у названий роликов'}",
             f"Нужно опросов: {count}", "Названия последних роликов:", *[f"- {t}" for t in titles]]
    if asked:
        lines += ["Уже задавали (не повторяй):", *[f"- {q}" for q in asked[-40:]]]
    return "\n".join(lines)


def generate(name: str, titles: list[str], count: int, *, language: str = "", asked: list[str] | None = None,
             llm: tuple[str, str, str] | None = None, cancel: CancelToken | None = None) -> list[Poll]:
    """count опросов по теме канала. Нейросеть ответила хуже, чем нужно (меньше опросов) — спрашиваем ещё раз."""
    if not titles:
        raise ValueError("у канала не нашлось роликов — не по чему понять его тему")
    count = max(1, min(COUNT_MAX, int(count)))
    asked = list(asked or [])
    got: list[Poll] = []
    for _ in range(3):
        if cancel:
            cancel.check()
        need = count - len(got)
        raw = ask(_SYSTEM, prompt_for(name, titles, need, language, asked + [p.question for p in got]), llm, cancel)
        for poll in parse(raw):
            if poll.question.lower() not in {p.question.lower() for p in got} | {q.lower() for q in asked}:
                got.append(poll)
        if len(got) >= count:
            break
        log(f"Голосования: нейросеть дала {len(got)} из {count} — прошу ещё")
    if not got:
        raise RuntimeError("нейросеть не написала ни одного годного опроса")
    return got[:count]


def parse(raw: str | None) -> list[Poll]:
    items = json_dict(raw).get("polls") or []
    return [p for p in (clean(it) for it in items) if p]


# опросы и поиск ниш — задачи лёгкие: подписку Claude не тратим, порядок Codex → встроенная нейросеть
CHEAP_ORDER = ("codex",)


def ask(system: str, user: str, llm: tuple[str, str, str] | None, cancel: CancelToken | None,
        label: str = "community_polls", engines: tuple[str, ...] = CHEAP_ORDER) -> str:
    """Подписки из engines (по умолчанию только Codex), потом встроенная нейросеть (о смене — уведомление)."""
    from engine.core import switches
    from engine.rewrite.engines import RewriteError, run_sub, sub_available

    prompt = f"{system}\n\n=====\n\n{user}\n\n=====\nОтветь строго JSON, без пояснений и без markdown-обёртки."
    prev, why = "", ""
    for engine in engines:
        if not sub_available(engine):
            prev, why = prev or engine, why or "не выполнен вход или исчерпан лимит"
            continue
        if prev and prev != engine:
            switches.report(f"запрос «{label}»", prev, engine, why)
        try:
            return run_sub(engine, prompt, cancel=cancel, timeout=10 * 60)
        except RewriteError as exc:
            if cancel is not None:
                cancel.check()
            prev, why = engine, str(exc).splitlines()[0][:160]
    if not (llm and llm[0]):
        raise RuntimeError(f"нейросеть недоступна: Codex — {why or 'нет входа'}, ключа встроенной нет (Настройки → LLM)")
    if prev:
        switches.report(f"запрос «{label}»", prev, "llm", why)
    from engine.slides.llm_client import LLMClient

    client = LLMClient(llm[0], llm[2], llm[1])
    if cancel is not None:
        cancel.register(client)
    try:
        return client.complete(label=label, system=system, user=user, subs=False)  # subs=False — без Claude
    finally:
        client.close()


# ---------- расписание ----------


def plan(count: int, step_days: float, start: datetime) -> list[datetime | None]:
    """Когда выходит каждый опрос: первый — сразу (None), дальше — каждые step_days от старта,
    время округляется вверх до 15 минут (так его даёт выбрать YouTube)."""
    out: list[datetime | None] = [None]
    for k in range(1, count):
        out.append(round_up(start + timedelta(days=step_days * k)))
    return out[:count]


def round_up(when: datetime) -> datetime:
    base = when.replace(second=0, microsecond=0)
    extra = (-base.minute) % SLOT_MIN
    if extra == 0 and (when.second or when.microsecond):
        extra = SLOT_MIN
    return base + timedelta(minutes=extra)


# ---------- youtube.com: вкладка «Posts» ----------

DIALOG = "ytd-backstage-post-dialog-renderer"
_TZ = re.compile(r"GMT\s*([+-])(\d{1,2}):?(\d{2})?")
_SCHEDULE_ITEM = re.compile(r"^\s*(schedule post|запланировать( публикацию| пост)?)\s*$", re.I)
_SUBMIT = re.compile(r"^\s*(post|schedule|опубликовать|запланировать)\s*$", re.I)
_TAB_PUBLISHED = re.compile(r"^\s*(published|опубликованные)\s*$", re.I)
_TAB_SCHEDULED = re.compile(r"^\s*(scheduled|запланированные)\s*$", re.I)


class Unverified(RuntimeError):
    """Опрос отправлен, но во вкладке его не видно — повторно не выкладываем."""


def accept_dialogs(page) -> None:
    """Окно браузера «Покинуть страницу?» (несохранённый пост после сбоя) — соглашаемся, иначе переход зависнет."""
    def on(dialog):
        try:
            dialog.accept()
        except Exception:
            pass

    page.on("dialog", on)


def discard(page) -> None:
    """После сбоя на опросе: закрыть недописанный пост («Cancel»), чтобы следующий начался с чистого листа."""
    try:
        button = page.locator(DIALOG).get_by_role("button", name=re.compile(r"^\s*(cancel|отмена)\s*$", re.I)).first
        if button.count() and button.is_visible():
            human.click(page, button)
            page.wait_for_timeout(1500)
    except Exception:
        pass


def posts_url(channel) -> str:
    q = f"?authuser={channel.authuser}" if getattr(channel, "authuser", 0) else ""
    return f"https://www.youtube.com/channel/{channel.id}/posts{q}"


def _norm(text: str) -> str:
    return " ".join(str(text or "").replace(" ", " ").replace(" ", " ").split()).lower()


def _open_composer(page, channel, cancel) -> None:
    from engine.publish import youtube
    from engine.publish.studio_kit import el

    youtube.goto(page, posts_url(channel))
    page.wait_for_timeout(5000)
    youtube._check_signed_in(page)
    try:
        box = el(page, "post_box", timeout=30000)
    except Exception:
        raise youtube.StudioError("на вкладке «Posts» канала нет поля для поста — у канала нет доступа к "
                                  "сообществу или в профиле активен другой канал") from None
    youtube._act(cancel)
    human.click(page, box)
    page.wait_for_timeout(1500)
    human.dismiss_notice(page)  # «Remember that anyone can see your posts» — «Got it»
    youtube._act(cancel)
    human.click(page, el(page, "poll_add"))
    el(page, "poll_question")
    page.wait_for_timeout(1200)


def _fill(page, poll: Poll, cancel) -> None:
    from engine.publish import youtube
    from engine.publish.studio_kit import el

    q = el(page, "poll_question")
    youtube._act(cancel)
    human.click(page, q)
    human.select_all(page)
    human.type_text(page, poll.question, cancel)
    options = page.locator(f"{DIALOG} input[maxlength='{OPTION_MAX}']")
    for _ in range(OPTIONS_MAX):
        if options.count() >= len(poll.options):
            break
        youtube._act(cancel)
        human.click(page, el(page, "poll_add_option"))
        page.wait_for_timeout(800)
    if options.count() < len(poll.options):
        raise youtube.StudioError(f"YouTube дал только {options.count()} поля для вариантов, нужно {len(poll.options)}")
    for k, text in enumerate(poll.options):
        youtube._act(cancel)
        human.click(page, options.nth(k))
        human.select_all(page)
        human.type_text(page, text, cancel)
    # всё вписанное — читаем обратно со страницы
    shown_q = _norm(q.inner_text())
    shown_opts = [_norm(options.nth(k).input_value()) for k in range(len(poll.options))]
    if shown_q != _norm(poll.question) or shown_opts != [_norm(o) for o in poll.options]:
        raise youtube.StudioError(f"поля опроса вписались не так: вопрос «{shown_q}», варианты {shown_opts}")


def _offset(page) -> tuple[int, int] | None:
    from engine.publish.studio_kit import el

    m = _TZ.search(el(page, "post_tz").inner_text())
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    return sign * int(m.group(2)), sign * int(m.group(3) or 0)


_DATE_JS = """(iso) => { const [y, m, d] = iso.split('-').map(Number);
    return new Intl.DateTimeFormat(document.documentElement.lang || 'en', {day: 'numeric', month: 'short', year: 'numeric'})
        .format(new Date(y, m - 1, d)); }"""


def _schedule(page, when: datetime, cancel) -> datetime:
    """«Action menu» → «Schedule post» → дата и время (в поясе, который показывает YouTube). → время в этом поясе."""
    from engine.publish import youtube
    from engine.publish.studio_kit import el

    youtube._act(cancel)
    human.click(page, el(page, "post_menu"))
    item = page.get_by_text(_SCHEDULE_ITEM).first
    item.wait_for(state="visible", timeout=10000)
    youtube._act(cancel)
    human.click(page, item)
    el(page, "post_date")
    page.wait_for_timeout(1500)
    offset = _offset(page)
    if offset is None:
        raise youtube.StudioError(f"не понял часовой пояс расписания («{el(page, 'post_tz').inner_text().strip()}»)")
    local = youtube.studio_time(when, offset, youtube._browser_zone(page))
    _set_date(page, local, cancel)
    _set_time(page, local, cancel)
    return local


def _set_date(page, local: datetime, cancel) -> None:
    from engine.publish import youtube
    from engine.publish.studio_kit import el

    variants = [page.evaluate(_DATE_JS, local.date().isoformat()), f"{local:%b} {local.day}, {local:%Y}", f"{local.day} {local:%b %Y}"]
    for text in dict.fromkeys(variants):
        youtube._act(cancel)
        human.click(page, el(page, "post_date"))
        field_ = el(page, "post_date_input")
        human.click(page, field_)
        human.select_all(page)
        human.type_text(page, text, cancel)
        human.press(page, "Enter")
        page.wait_for_timeout(1200)
        shown = el(page, "post_date").inner_text()
        if _norm(shown) == _norm(text) or youtube.studio_date(page, shown) == local.date().isoformat():
            return  # та же дата — хоть по строке, хоть по смыслу (язык, порядок полей, «г.», узкие пробелы)
        log(f"Голосования: дата «{text}» не принята — пробую другой формат")
    raise youtube.StudioError(f"дата опроса не вписалась (показано «{el(page, 'post_date').inner_text().strip()}», "
                              f"нужно {local:%d.%m.%Y})")


def _set_time(page, local: datetime, cancel) -> None:
    from engine.publish import youtube
    from engine.publish.studio_kit import el

    want = (local.hour, local.minute)
    youtube._act(cancel)
    human.click(page, el(page, "post_time"))
    items = page.locator("tp-yt-paper-listbox tp-yt-paper-item[role=option]")
    items.first.wait_for(state="visible", timeout=10000)
    texts = items.all_inner_texts()
    pick = next((k for k, t in enumerate(texts) if youtube.clock_of(t) == want), None)
    if pick is None:
        raise youtube.StudioError(f"в списке времени нет {want[0]:02d}:{want[1]:02d}")
    human.reach(page, items.nth(pick))
    youtube._act(cancel)
    human.click(page, items.nth(pick))
    page.wait_for_timeout(1200)
    shown = youtube.clock_of(el(page, "post_time").inner_text())
    if shown != want:
        raise youtube.StudioError(f"время опроса не выставилось (показано {shown}, нужно {want[0]:02d}:{want[1]:02d})")


def _submit(page, cancel) -> None:
    from engine.publish import youtube

    human.dismiss_notice(page)
    button = page.locator(DIALOG).get_by_role("button", name=_SUBMIT).last
    button.wait_for(state="visible", timeout=10000)
    youtube._act(cancel)
    human.click(page, button)
    page.wait_for_timeout(6000)


def _find_post(page, poll: Poll, scheduled: bool) -> str:
    """Пост с этим вопросом во вкладке Published/Scheduled. → текст поста ('' — не нашёлся)."""
    tab = page.get_by_role("tab", name=_TAB_SCHEDULED if scheduled else _TAB_PUBLISHED).first
    if not tab.count():
        tab = page.get_by_text(_TAB_SCHEDULED if scheduled else _TAB_PUBLISHED).first
    want = _norm(poll.question)
    for attempt in range(3):
        if attempt:
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
        try:
            human.click(page, tab)
            page.wait_for_timeout(3500)
        except Exception:
            pass
        # запланированные посты могут рисоваться другой обёрткой — запасной путь: весь список вкладки
        found = page.locator("ytd-backstage-post-thread-renderer, ytd-backstage-post-renderer").all_inner_texts()
        for text in found or page.locator("ytd-section-list-renderer #contents").all_inner_texts():
            if want and want in _norm(text):
                return text
    return ""


def post_poll(page, channel, poll: Poll, when: datetime | None, *, cancel: CancelToken | None = None) -> str:
    """Один опрос на канал: when=None — сразу, иначе по расписанию. Страница уже на нужном канале
    (youtube.switch_to). → строка для итогов. Пост не нашёлся после публикации — ошибка (проверь вручную)."""
    from engine.publish import youtube
    from engine.publish.studio_kit import step

    with step(page, "голосование: новый опрос"):
        _open_composer(page, channel, cancel)
    with step(page, "голосование: вопрос и варианты"):
        _fill(page, poll, cancel)
    local = None
    if when is not None:
        with step(page, "голосование: дата и время"):
            local = _schedule(page, when, cancel)
    with step(page, "голосование: публикация"):
        _submit(page, cancel)
        text = _find_post(page, poll, scheduled=local is not None)
    if not text:
        raise Unverified(f"после публикации опроса «{poll.question[:60]}» не вижу его во вкладке "
                                  f"{'Scheduled' if local else 'Published'} — проверь канал вручную, повторно не публикую")
    if local is None:
        log(f"Голосования {channel.handle}: опубликован «{poll.question}»")
        return f"сразу: «{poll.question}»"
    log(f"Голосования {channel.handle}: запланирован на {local:%d.%m %H:%M} «{poll.question}»")
    return f"{local:%d.%m %H:%M}: «{poll.question}»"
