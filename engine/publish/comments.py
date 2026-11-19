"""Ответы на комментарии зрителей от имени канала: сердечко + ответ.

Где: YouTube Studio → «Комментарии» канала, фильтр «Без ответа» (сразу все ролики канала).
Каждому комментарию, на который канал ещё не отвечал: сердечко от автора и ответ. Ответ — вариант,
который предложил сам YouTube (если предложил: случайный из них), иначе — наша нейросеть в том же
стиле: коротко, тепло, по-человечески, с городом/страной зрителя; негатив или комментарий, располагающий
к разговору, — доброжелательно и со встречным вопросом (чтобы завязался диалог). Язык — язык зрителя,
без подписи. Ссылки, спам, оскорбления — не трогаем. На что уже ответили — помним (data/publish).
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass

from engine.core.jsonish import json_dict
from engine.core.cancel import CancelToken
from engine.core.logutil import log
from engine.publish import human
from engine.publish.store import LOCK, read_json, write_json

ANSWERED_FILE = "answered.json"
MAX_PER_RUN = 300  # «все» — но с предохранителем от бесконечного прохода
BATCH = 25  # комментариев на один запрос к нейросети

_SYSTEM = """Ты ведёшь YouTube-канал с аудиорассказами и отвечаешь зрителям в комментариях от имени канала.
Стиль — как в примерах: коротко (одно-два предложения), тепло, живо, по-человечески, без шаблонов.
Примеры:
- «Спасибо за интересный рассказ. Беларусь» → «Большой привет Беларуси, очень рад, что вам понравилась история!»
- «Спасибо.мурманск.» → «Большой привет Мурманску, рад видеть вас среди слушателей!»
- «Приднестровье Рыбница Рассказ не для засыпания» → «Приветствую Приднестровье и город Рыбницу, спасибо, что
  поделились своим впечатлением от истории! А какие рассказы вам больше нравятся перед сном?»
Правила:
- отвечай на языке комментария;
- назвал город/страну — поприветствуй их; похвалил голос/историю — поблагодари по сути;
- комментарий негативный, с критикой, сомнением или располагает к разговору — ответь доброжелательно, без
  оправданий и споров, и задай зрителю один простой встречный вопрос, чтобы он захотел ответить;
- не повторяй одну и ту же формулировку в разных ответах; без подписи, без ссылок, без хэштегов,
  без призывов подписаться; эмодзи — не больше одного и не в каждом ответе;
- ссылки, реклама, спам, бессмыслица, оскорбления — skip: true (на такое не отвечаем и сердечко не ставим).
Ответ — только JSON: {"items": [{"id": "…", "reply": "…", "skip": false}]}"""


@dataclass
class Comment:
    id: str
    video: str
    author: str
    text: str


def answered(channel_id: str) -> set[str]:
    return set(read_json(ANSWERED_FILE, {}).get(channel_id) or [])


def remember(channel_id: str, comment_id: str) -> None:
    with LOCK:
        data = read_json(ANSWERED_FILE, {})
        ids = [*(data.get(channel_id) or []), comment_id]
        data[channel_id] = ids[-3000:]
        write_json(ANSWERED_FILE, data)


def generate_replies(comments: list[Comment], llm: tuple[str, str, str] | None) -> dict[str, str | None]:
    """Ответы нейросети пачками → {id комментария: ответ или None (не отвечать)}. Без нейросети — {}."""
    out: dict[str, str | None] = {}
    for k in range(0, len(comments), BATCH):
        chunk = comments[k:k + BATCH]
        user = "Комментарии зрителей:\n" + json.dumps(
            [{"id": c.id, "author": c.author, "text": c.text[:600]} for c in chunk], ensure_ascii=False)
        raw = _ask(user, llm)
        items = json_dict(raw).get("items") or []
        for it in items:
            if not isinstance(it, dict) or not it.get("id"):
                continue
            reply = " ".join(str(it.get("reply") or "").split())
            out[str(it["id"])] = None if it.get("skip") or not reply else reply
    return out


def _ask(user: str, llm: tuple[str, str, str] | None) -> str:
    """Встроенная нейросеть (сама уйдёт в Claude, если сервис лежит); без ключа — сразу Claude."""
    if llm and llm[0]:
        from engine.slides.llm_client import LLMClient

        client = LLMClient(llm[0], llm[2], llm[1])
        try:
            return client.complete(label="comment_replies", system=_SYSTEM, user=user)
        finally:
            client.close()
    from engine.slides.llm_client import claude_fallback_ok

    if not claude_fallback_ok():
        raise RuntimeError("для ответов на комментарии нужна нейросеть: ключ LLM или Claude Code")
    from engine.rewrite.engines import run_claude

    return run_claude(f"{_SYSTEM}\n\n{user}", timeout=10 * 60)


def answer_comments(cdp_url: str, channel, *, llm: tuple[str, str, str] | None,
                    cancel: CancelToken | None = None, progress=None) -> list[str]:
    """Пройти «Без ответа» в Studio канала: сердечко + ответ каждому. → строки для итогов."""
    from engine.publish import youtube
    from engine.publish.studio_kit import set_channel, step

    set_channel(channel.handle or channel.id)
    hearts = replies = skipped = from_youtube = 0
    with youtube.studio_page(cdp_url) as page:
        with step(page, "комментарии: переключение на канал"):
            youtube.switch_to(page, channel, cancel=cancel)
        rounds = 0
        # порциями: ответили на видимые — открываем «Без ответа» заново, пока там есть новые
        while hearts + replies + skipped < MAX_PER_RUN and rounds < 30:
            rounds += 1
            with step(page, "комментарии: раздел «Без ответа»"):
                page.goto(f"{youtube.STUDIO}/channel/{channel.id}/comments/inbox", wait_until="domcontentloaded")
                page.wait_for_timeout(6000)
                done = answered(channel.id)
                todo = [c for c in _read_inbox(page) if c.id not in done]
            if not todo:
                break
            log(f"Studio {channel.handle}: комментариев без ответа — {len(todo)} (порция {rounds})")
            try:
                ai = generate_replies(todo, llm)
            except Exception as exc:
                log(f"Комментарии: нейросеть не ответила ({exc}) — пишу только варианты YouTube")
                ai = {}
            progressed = False
            for n, c in enumerate(todo, 1):
                if cancel:
                    cancel.check()
                if progress:
                    progress(min(0.99, n / len(todo)), f"{n} из {len(todo)}, порция {rounds}, всего отвечено {replies}")
                if c.id in ai and ai[c.id] is None:
                    skipped += 1
                    progressed = True
                    remember(channel.id, c.id)
                    log(f"Studio {channel.handle}: пропуск (спам/ссылка/оскорбление): «{c.text[:60]}»")
                    continue
                with step(page, f"комментарий {c.author}: «{c.text[:40]}»"):
                    thread = _thread(page, c.id)
                    if thread is None:
                        continue  # уже ушёл из «Без ответа» (ответили вручную)
                    if _heart(page, thread, cancel):
                        hearts += 1
                    text, suggested = _reply_text(page, thread, ai.get(c.id), cancel)
                    remember(channel.id, c.id)  # даже без ответа — не крутиться на нём бесконечно
                    progressed = True
                    if not text:
                        log(f"Studio {channel.handle}: нет ни варианта YouTube, ни ответа нейросети — «{c.text[:60]}»")
                        continue
                    _send_reply(page, thread, text, suggested, cancel)
                    replies += 1
                    from_youtube += int(suggested)
                    log(f"Studio {channel.handle}: ответ{' (вариант YouTube)' if suggested else ''} на «{c.text[:50]}» → «{text}»")
                if hearts + replies + skipped >= MAX_PER_RUN:
                    break
            if not progressed:
                break  # ничего не вышло в этой порции — не крутимся
    if not (hearts or replies or skipped):
        log(f"Studio {channel.handle}: новых комментариев без ответа нет")
        return ["комментарии: новых без ответа нет"]
    line = f"комментарии: ответов {replies} (вариантов YouTube {from_youtube}), сердечек {hearts}"
    return [line + (f", пропущено (спам/ссылки/оскорбления) {skipped}" if skipped else "")]


# ---------- Studio: «Комментарии» ----------

_DATA_JS = """e => { const seen = new Set(); const out = {};
    const walk = (o, d) => { if (!o || typeof o !== 'object' || d > 6 || seen.has(o)) return; seen.add(o);
      for (const [k, v] of Object.entries(o)) {
        if ((k === 'commentId' || k === 'videoId') && typeof v === 'string' && !out[k]) out[k] = v; else walk(v, d + 1); } };
    for (const el of [e, ...e.querySelectorAll('*')]) { walk(el.data, 0); walk(el.__data, 0); if (out.commentId && out.videoId) break; }
    return out; }"""


def _read_inbox(page) -> list[Comment]:
    """Все комментарии «Без ответа» (догружаем прокруткой), с ID комментария и ролика."""
    seen: dict[str, Comment] = {}
    for scroll in range(40):
        threads = page.locator("ytcp-comment-thread")
        before = len(seen)
        for k in range(threads.count()):
            t = threads.nth(k)
            try:
                ids = t.evaluate(_DATA_JS)
                cid = ids.get("commentId") or ""
                if not cid or cid in seen:
                    continue
                seen[cid] = Comment(cid, ids.get("videoId") or "", t.locator("#name").first.inner_text().strip(),
                                    t.locator("#content-text").first.inner_text().strip())
            except Exception:
                continue
        if len(seen) >= MAX_PER_RUN or (len(seen) == before and scroll > 0):
            break
        # список прокручивается внутри своей области (не окно страницы): мышь над последним комментарием,
        # колесом вниз — как человек листает; Studio догружает следующую порцию
        _scroll_list(page, threads)
    return list(seen.values())


def _scroll_list(page, threads) -> None:
    import random

    n = threads.count()
    target = threads.nth(n - 1) if n else page.locator("ytcp-activity-section, ytcp-comments-section").first
    try:
        box = target.bounding_box(timeout=5000)
    except Exception:
        box = None
    if box:
        page.mouse.move(box["x"] + box["width"] * random.uniform(0.3, 0.7),
                        min(box["y"] + box["height"] / 2, (page.viewport_size or {"height": 800})["height"] - 60),
                        steps=random.randint(5, 12))
    for _ in range(random.randint(3, 5)):
        page.mouse.wheel(0, random.randint(500, 900))
        page.wait_for_timeout(random.randint(300, 700))
    page.wait_for_timeout(random.randint(1800, 3000))


def _thread(page, comment_id: str):
    threads = page.locator("ytcp-comment-thread")
    for k in range(threads.count()):
        t = threads.nth(k)
        try:
            if t.evaluate(_DATA_JS).get("commentId") == comment_id:
                human.reach(page, t)  # колесом до комментария
                return t
        except Exception:
            continue
    return None


def _heart(page, thread, cancel) -> bool:
    """Сердечко от автора, если ещё не стоит."""
    from engine.publish import youtube

    button = thread.locator("#creator-heart-button").first
    if not button.count():
        return False
    if thread.locator("ytcp-comment-creator-heart").first.evaluate(
            "e => !!(e.querySelector('[aria-pressed=true]') || e.hasAttribute('is-hearted') || "
            "(e.__data && e.__data.isHearted) || (e.data && e.data.isHearted))"):
        return False
    youtube._act(cancel)
    human.click(page, button)
    page.wait_for_timeout(1200)
    return True


# варианты ответа от YouTube: появляются не всегда; узнаём по виду — «чипы» с текстом рядом с полем ответа
_SUGGEST_SEL = ("ytcp-comment-thread [class*=suggest] ytcp-chip, ytcp-comment-thread ytcp-smart-reply, "
                "ytcp-comment-thread [id*=suggest] ytcp-chip, ytcp-comment-thread ytcp-chip-bar ytcp-chip, "
                "ytcp-comment-thread [class*=smart-repl], ytcp-comment-thread [id*=smart-repl]")


def _reply_text(page, thread, ai_reply: str | None, cancel) -> tuple[str, bool]:
    """Открыть ответ; есть варианты YouTube — случайный из них, иначе ответ нейросети. → (текст, вариант YouTube?)."""
    from engine.publish import youtube

    youtube._act(cancel)
    human.click(page, thread.locator("#reply-button").first)
    thread.locator("#reply-dialog-container textarea").first.wait_for(timeout=15000)
    page.wait_for_timeout(1500)
    chips = [t.strip() for t in thread.locator(_SUGGEST_SEL).all_inner_texts() if len(t.strip()) > 8]
    if chips:
        return random.choice(chips), True
    return ai_reply or "", False


def _send_reply(page, thread, text: str, suggested: bool, cancel) -> None:
    from engine.publish import youtube

    box = thread.locator("#reply-dialog-container textarea").first
    youtube._act(cancel)
    human.click(page, box)
    human.clear_field(page)
    human.type_text(page, text, cancel)
    youtube._act(cancel)
    human.click(page, thread.locator("#reply-dialog-container #submit-button").first)
    page.wait_for_timeout(3000)
