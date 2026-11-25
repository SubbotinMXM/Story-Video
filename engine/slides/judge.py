"""Проверка кадров «глазами» LLM: подходит ли кадр к ролику целиком, к словам в этот момент
и насколько хороша сама съёмка.

Кандидаты (2–4 штуки) склеиваются в один лист с номерами — на кусок текста один запрос.
У видео в строке два кадра из начала и конца клипа: видно движение, тряску, смену сцены.
Ответ — оценки 0–10 по трём осям и причина отказа (чужая страна, анахронизм, водяной знак…).

Проверка — надстройка: если сервис картинки не понимает или лёг, подбор идёт по CLIP, как раньше.
"""

from __future__ import annotations

import base64
import io
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from engine.core.jsonish import json_object
from engine.core.cancel import CancelledError
from engine.core.logutil import log
from engine.slides.ffprobe_util import grab_frame
from engine.slides.llm_client import LLMClient
from engine.slides.story_context import JUDGE_AMBIGUITY_RULE
from engine.slides.theme_plan import ThemePlan

# смысл ролика важнее всего (чужая страна/эпоха — брак), потом слова момента, потом съёмка
W_CONTEXT, W_BEAT, W_QUALITY = 0.45, 0.35, 0.20
CONTEXT_MIN = 5  # ниже — кадр из другого мира (страна, эпоха): не берём
# 360×203: сюжет, страна/эпоха и брак видны так же, а картинка в запросе почти вдвое дешевле
_TILE_W, _TILE_H = 360, 203
_VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm"}
# подряд столько сбоев проверки — дальше этот ролик подбирается без неё (по CLIP)
_MAX_FAILS = 6
# перегруженный сервис: проверка кадра не стоит долгого ожидания — берём по CLIP
_BUSY_WAIT = 90

_SYSTEM = """\
You are a strict photo editor of a high-end documentary YouTube channel. You choose B-roll for a narrated film.
You see numbered candidates. A row with two frames is ONE video clip (an early and a later frame of it).
Rate every candidate with integers 0-10:
- "context": does it belong to THIS film's world — the right country/region (architecture, script on signs,
  people, nature, climate), the right era, no anachronisms? 0-3 = clearly another country, culture or era.
  For a chart/infographic judge context by its topic.
- "beat": does it show what the narration says at this moment? (For a general theme shot: fits the film's topic.)
- "quality": professional, cinematic, well composed, sharp, good light, steady camera (drone, gimbal, tripod,
  smooth pan) = high. Shaky amateur phone footage, motion blur, bad framing, dull snapshot, low resolution = low.
- "reject": a short reason if the candidate must not be used at all, else "". Reject: wrong country or culture,
  wrong era/anachronism, watermark or logo, burned-in text or subtitles, collage or split screen, meme,
  screenshot of a website/app/game, distorted AI artifacts (extra fingers, melted faces, gibberish text).
Era is judged only by what visibly dates a frame (clothes, cars, screens, devices that did not exist yet). A component,
schematic, tool, material or place that looks the same across decades is NOT an anachronism.
Be strict: a generic village with no sign of the film's country is NOT the film's country.
Return ONLY JSON: {"items": [{"n": 1, "context": 7, "beat": 6, "quality": 8, "reject": ""}]}
"""
_SYSTEM = _SYSTEM.replace("Be strict:", JUDGE_AMBIGUITY_RULE + "\nBe strict:", 1)


# первые минуты ролика: зрелищность важнее буквального смысла фразы
_OPENING = """THIS SHOT OPENS THE FILM (first 1-2 minutes): it must hook the viewer. Rate "quality" harshly:
9-10 only for stunning professional cinematography — aerial/drone flyovers, sweeping landscapes and cityscapes,
golden-hour light, smooth gimbal or dolly moves, rich colour and depth. If the film is about the people (or women)
of a country, a beautiful, attractive young local woman of that country filmed cinematically (natural, photogenic,
smiling, in local or summer clothing, at a striking place) is a top opening shot too (9-10). Give quality 0-4 to:
a flag or emblem filling the frame, charts, maps, documents, posters, screenshots, museum displays, dark or
underexposed shots, flat dull snapshots, stock clichés (hands, drawers, papers), shaky or amateur footage, still
photos that look like snapshots, plain or unattractive people shots.
"""

# первые 30 секунд — главное место ролика: зритель решает, смотреть ли дальше
_PRIME = """THIS SHOT IS IN THE FIRST 30 SECONDS — the most important moment of the whole film. Only a WOW shot
deserves quality 8-10: the most spectacular aerial/drone view of the place, an iconic breathtaking landscape or
cityscape, or (for a film about the people/women of a country) a strikingly beautiful young local woman filmed
cinematically. Anything merely "fine" gets quality 6 at most.
"""


@dataclass
class Verdict:
    context: int
    beat: int
    quality: int
    reject: str = ""

    @property
    def score(self) -> float:
        return W_CONTEXT * self.context + W_BEAT * self.beat + W_QUALITY * self.quality

    @property
    def ok(self) -> bool:
        return not self.reject and self.context >= CONTEXT_MIN

    def note(self) -> str:
        why = f", отказ: {self.reject}" if self.reject else ""
        return f"контекст {self.context}, момент {self.beat}, съёмка {self.quality}{why}"


def brief_of(plan: ThemePlan) -> str:
    """«Паспорт ролика» одним текстом — для проверки кадров и описаний AI-кадров."""
    ctx = plan.context
    lines = [f"Film: {ctx.brief or plan.summary}"]
    if ctx.place:
        lines.append(f"Place: {ctx.place}")
    if ctx.era:
        lines.append(f"Era: {ctx.era}" + (" (historical: nothing modern)" if ctx.historical else ""))
    if ctx.characters:
        lines.append("People: " + ", ".join(ctx.characters[:5]))
    if ctx.visual_markers:
        lines.append("Looks like: " + "; ".join(ctx.visual_markers[:10]))
    if ctx.wrong_look or ctx.avoid:
        lines.append("Wrong for this film: " + "; ".join([*ctx.wrong_look[:8], *ctx.avoid[:6]]))
    return "\n".join(lines)


def _frames_of(path: Path, tmp: Path, *, video: bool) -> list[Path]:
    """Картинка — она сама; видео — 2 кадра (≈20% и ≈75% длины клипа)."""
    if not video:
        return [path]
    from engine.slides.ffprobe_util import probe_duration

    try:
        dur = probe_duration(path)
    except Exception:
        dur = 0.0
    out: list[Path] = []
    for k, frac in enumerate((0.2, 0.75)):
        dest = tmp / f"{path.stem}_{k}.jpg"
        at = max(0.0, dur * frac) if dur > 0 else float(k)
        if grab_frame(path, dest, at=at, quality=4, width=_TILE_W):
            out.append(dest)
    return out


def contact_sheet(rows: list[list[Path]]) -> bytes:
    """Лист: строка на кандидата, слева крупный номер; до двух кадров в строке."""
    from PIL import Image, ImageDraw, ImageFont

    label_w = 70
    # у фото в строке один кадр (у видео — два): пустую половину листа не шлём — картинка в запросе дешевле на ~40%
    cols = max(1, min(2, max((len(r) for r in rows), default=1)))
    width = label_w + cols * _TILE_W + (cols - 1) * 5 + 5
    sheet = Image.new("RGB", (width, len(rows) * (_TILE_H + 10)), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=44)
    except TypeError:  # старый Pillow
        font = ImageFont.load_default()
    for r, frames in enumerate(rows):
        y = r * (_TILE_H + 10) + 5
        draw.text((14, y + _TILE_H // 2 - 26), str(r + 1), fill=(255, 220, 60), font=font)
        for k, frame in enumerate(frames[:2]):
            try:
                with Image.open(frame) as img:
                    tile = img.convert("RGB")
                    tile.thumbnail((_TILE_W, _TILE_H))
            except Exception:
                continue
            sheet.paste(tile, (label_w + k * (_TILE_W + 5), y + (_TILE_H - tile.height) // 2))
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=82)
    return buf.getvalue()


def _parse(raw: str, n: int) -> list[Verdict] | None:
    data = json_object(raw)
    if data is None:
        return None
    got: dict[int, Verdict] = {}
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        try:
            k = int(item.get("n"))
            got[k] = Verdict(
                context=_clamp(item.get("context")),
                beat=_clamp(item.get("beat")),
                quality=_clamp(item.get("quality")),
                reject=str(item.get("reject") or "").strip(),
            )
        except (TypeError, ValueError):
            continue
    if not got:
        return None
    # кандидат без оценки — не рискуем
    return [got.get(k + 1, Verdict(0, 0, 0, "нет оценки")) for k in range(n)]


def _clamp(v: object) -> int:
    return max(0, min(10, int(round(float(v)))))  # type: ignore[arg-type]


class Judge:
    """Одна на ролик. Потокобезопасна: куски проверяются параллельно."""

    def __init__(self, client: LLMClient, plan: ThemePlan) -> None:
        self.client = client
        self.client.busy_wait = _BUSY_WAIT
        self.brief = brief_of(plan)
        self.enabled = True
        self._fails = 0
        self._down_said = False
        self._lock = threading.Lock()
        self.calls = 0
        self._batcher: PoolBatcher | None = None

    def pool(self) -> PoolBatcher:
        """Проверка кадров пула листами по 4 (одна на ролик)."""
        with self._lock:
            if self._batcher is None:
                self._batcher = PoolBatcher(self)
            return self._batcher

    def close(self) -> None:
        self.client.close()

    def rate(
        self,
        paths: list[Path],
        *,
        moment: str = "",
        query: str = "",
        thematic: bool = False,
        label: str = "judge",
        opening: bool = False,
        prime: bool = False,  # первые 30 секунд: только «вау»-кадры
    ) -> list[Verdict] | None:
        """Оценки кандидатов по порядку. None — проверка недоступна (подбирай по CLIP).
        opening — кадр для первых минут ролика: оценка съёмки строже."""
        if not self.enabled or not paths:
            return None
        with tempfile.TemporaryDirectory() as tmp:
            rows = [_frames_of(p, Path(tmp), video=p.suffix.lower() in _VIDEO_EXT) for p in paths]
            if not all(rows):
                return None
            image = base64.b64encode(contact_sheet(rows)).decode()
        what = (
            "This shot is a general theme shot for the film (no specific sentence)."
            if thematic or not moment.strip()
            else f"Narration at this moment (original language): «{moment.strip()[:400]}»\nSearch intent: {query}"
        )
        user = (f"FILM PASSPORT:\n{self.brief}\n\n{what}\n\n" + (_OPENING if opening else "")
                + (_PRIME if prime else "") + f"Rate all {len(paths)} candidates.")
        try:
            raw = self.client.complete_vision(label=label, system=_SYSTEM, user_text=user, image_b64=image)
        except CancelledError:
            raise
        except Exception as exc:
            self._failed(exc)
            return None
        verdicts = _parse(raw, len(paths))
        if verdicts is None:
            self._failed(ValueError(f"непонятный ответ: {raw[:120]!r}"))
            return None
        with self._lock:
            self._fails = 0
            self.calls += 1
        return verdicts

    def _failed(self, exc: Exception) -> None:
        from engine.slides.llm_client import LLMUnavailable

        if isinstance(exc, LLMUnavailable):
            # сервис лёг на время: эти кадры — по CLIP, но проверку не выключаем — оживёт, продолжит
            with self._lock:
                said, self._down_said = self._down_said, True
            if not said:
                log("Проверка кадров: сервис LLM недоступен — пока оцениваю локально (CLIP, лица, надписи)")
            return
        text = str(exc).lower()
        unsupported = any(k in text for k in ("image", "vision", "multimodal", "content type")) and "400" in text
        with self._lock:
            self._fails += 1
            off = unsupported or self._fails >= _MAX_FAILS
            if off and self.enabled:
                self.enabled = False
                log(f"Проверка кадров LLM выключена на этот ролик ({exc}) — подбираю по CLIP, как раньше")
            elif self.enabled:
                log(f"Проверка кадров: сбой ({exc}) — этот кусок по CLIP")


def make_judge(api_key: str, model: str, base_url: str, plan: ThemePlan) -> Judge | None:
    if not (api_key or "").strip():
        return None
    return Judge(LLMClient(api_key, model, base_url), plan)


# Одновременно в LLM — не больше стольких листов на всё приложение: сервис держит ~10–12 запросов (4 фото/с),
# при сотне потоков запросы стоят в очереди по минуте и ролик собирается дольше, а не быстрее.
JUDGE_SLOTS = threading.BoundedSemaphore(10)


class PoolBatcher:
    """Кадры пула (без привязки к фразе) проверяются листами по size: потоки сдают по кадру,
    лист уходит, как только набрался или прошло wait секунд. Запросов к LLM — в size раз меньше."""

    def __init__(self, judge: Judge, *, size: int = 6, wait: float = 4.0) -> None:
        self.judge = judge
        self.size = size
        self.wait = wait
        self._lock = threading.Lock()
        self._pending: list[dict] = []

    def rate(self, path: Path, query: str = "") -> Verdict | None:
        entry = {"path": path, "query": query, "done": threading.Event(), "verdict": None}
        with self._lock:
            self._pending.append(entry)
            full = len(self._pending) >= self.size
        if full:
            self._flush()
        if not entry["done"].wait(self.wait):
            self._flush()  # лист не набрался — отправляем, что есть
        entry["done"].wait()
        return entry["verdict"]

    def _flush(self) -> None:
        with self._lock:
            batch, self._pending = self._pending[: self.size], self._pending[self.size :]
        if not batch:
            return
        try:
            with JUDGE_SLOTS:
                verdicts = self.judge.rate([e["path"] for e in batch], thematic=True, label="judge_pool")
        except BaseException:
            for e in batch:
                e["done"].set()
            raise
        for k, e in enumerate(batch):
            e["verdict"] = verdicts[k] if verdicts is not None else None
            e["done"].set()
