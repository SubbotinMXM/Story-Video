"""Поиск ниш и каналов-конкурентов: на кого равняться.

Пользователь выбирает нишу из встроенного списка (40 «вечнозелёных») или пишет свои ключевые слова.
1. Запросы: ключевые слова (+ нейросеть добавит похожие, если их мало или язык аудитории не русский).
2. YouTube API: средние и длинные ролики по запросам за последние полгода (Shorts отсекает сам YouTube).
3. Каналы этих роликов → у самых перспективных — последние длинные ролики.
4. Подходит небольшой канал, у которого просмотры стабильно и сильно перевешивают подписчиков:
   • подписчиков — не больше потолка из интерфейса (по умолчанию 10 тыс.; чуть выше — до +15% — ещё берём:
     10,5 или 11 тыс. при хороших просмотрах подходят, 20 тыс. — нет);
   • и ЛИБО стабильно: медиана просмотров последних длинных роликов — от 30 тыс. и минимум в 3 раза больше
     подписчиков, ЛИБО «стреляет»: среди длинных роликов за ~полгода минимум 2 залёта — от 50 тыс. просмотров
     и в 10+ раз больше подписчиков (канал на 2,7 тыс. с роликами по 280 и 270 тыс. при медиане 26 тыс.).
   Возраст канала и сложность формата не учитываются — формат пользователь оценивает сам.
5. Порядок: больше просмотров на подписчика, стабильнее и больше просмотры, меньше роликов на канале — выше.
→ до 15 каналов.

«Похожие на канал» (find_similar): вместо ключевых слов — ссылка на канал-образец. Программа читает его (описание,
ключевые слова, самые просматриваемые и свежие ролики), нейросеть выводит тему и язык аудитории и пишет запросы,
плюс по названиям лучших роликов образца ищутся похожие ролики. Дальше — тот же конвейер и те же критерии (пункты 2–5);
сам образец из выдачи исключается. Пороги — в коде (константы ниже), в интерфейсе — язык и потолок подписчиков.
"""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from engine.core.jsonish import json_dict
from engine.core.cancel import CancelToken
from engine.core.logutil import log
from engine.publish.store import LOCK, read_json, write_json

RESULTS_FILE = "niches.json"
FAVORITES_FILE = "niche_favorites.json"  # избранные каналы: лежат, пока человек сам не удалит
SEARCH_PAGE = 50
# проходы поиска на каждый запрос (по 100 единиц квоты): Shorts отсекает сам YouTube (medium — 4–20 мин,
# long — от 20 мин); order=viewCount на многих запросах отдаёт 0 роликов — поэтому основа — релевантность
SEARCH_PASSES = (("relevance", "medium"), ("relevance", "long"), ("viewCount", "long"))
MAX_QUERIES = 8
WINDOW_DAYS = 180  # ролики по запросам — за последние полгода
MIN_MINUTES = 5.0  # короче — Shorts, не наш формат
PRE_MIN_VIEWS = 15_000  # найденный ролик канала набрал хотя бы столько — иначе канал не разбираем
DEEP_MAX = 100  # у скольких каналов смотреть последние ролики (2–3 единицы квоты на канал)
UPLOADS_PAGE = 50  # последние загрузки канала — из них берутся длинные ролики
RECENT = 15  # медиана — по стольким последним длинным роликам
STABLE_VIEWS = 30_000  # медиана просмотров последних длинных роликов — не меньше (десятки тысяч)
HIT_VIEWS = 50_000  # «залёт»: длинный ролик набрал хотя бы столько…
HIT_RATIO = 10.0  # …и в столько раз больше подписчиков
MIN_HITS = 2  # столько залётов за ~полгода — канал «стреляет», даже если медиана скромная
MAX_SUBS = 10_000  # потолок подписчиков по умолчанию (настраивается в интерфейсе)
SUBS_SLACK = 0.15  # сверх потолка ещё берём столько (10 тыс. → до 11,5 тыс.)
MIN_RATIO = 3.0  # медиана просмотров / подписчики — не меньше (просмотры сильно перевешивают)
MIN_LONG = 3  # хотя бы столько длинных роликов — иначе «стабильно» не про него
RESULTS = 15

LANG_REGION = {"ru": "RU", "en": "US", "es": "ES", "pt": "BR", "de": "DE", "fr": "FR", "it": "IT", "pl": "PL",
               "tr": "TR", "uk": "UA", "ro": "RO", "nl": "NL"}


@dataclass
class Filters:
    """Что настраивается из интерфейса: язык аудитории (регион — по языку) и потолок подписчиков
    (0 — без потолка). Остальное — константы выше."""
    language: str = "ru"
    region: str = ""
    results: int = RESULTS
    max_subs: int = MAX_SUBS

    @property
    def subs_limit(self) -> int:
        """Сколько подписчиков ещё берём: потолок + небольшой запас (0 — без ограничения)."""
        return int(self.max_subs * (1 + SUBS_SLACK)) if self.max_subs > 0 else 0

    @classmethod
    def of(cls, raw: dict | None) -> Filters:
        f = cls()
        for k, v in (raw or {}).items():
            if hasattr(f, k) and v not in (None, ""):
                cur = getattr(f, k)
                try:
                    if isinstance(cur, bool):
                        v = v if isinstance(v, bool) else str(v).strip().lower() not in ("0", "false", "no", "off", "нет")
                    setattr(f, k, type(cur)(v))
                except (TypeError, ValueError):
                    pass
        f.results = max(1, min(30, f.results))
        f.max_subs = max(0, f.max_subs)
        return f


@dataclass
class Found:
    id: str
    title: str
    handle: str = ""
    subs: int = 0
    videos: int = 0
    views: int = 0
    created: float = 0.0
    country: str = ""
    best_id: str = ""
    best_title: str = ""
    best_views: int = 0
    hits: int = 0  # сколько его роликов нашлось по запросам ниши
    queries: list[str] = field(default_factory=list)
    recent_avg: int = 0
    recent_median: int = 0
    recent_count: int = 0  # по скольким длинным роликам медиана
    hit_share: float = 0.0  # доля залётов среди последних длинных роликов
    hit_count: int = 0  # залётов среди длинных роликов из последних загрузок (~полгода)
    top_views: int = 0  # самый просматриваемый из них
    per_week: float = 0.0
    avg_minutes: float = 0.0
    recent_titles: list[str] = field(default_factory=list)
    score: float = 0.0
    anomaly: bool = False
    uploads: str = ""  # плейлист «все ролики» канала (служебное)
    hidden: bool = False  # канал скрыл число подписчиков — соотношение не посчитать, в выдачу не берём

    @property
    def age_days(self) -> int:
        return int((time.time() - self.created) / 86400) if self.created else 0

    @property
    def ratio(self) -> float:
        return (self.recent_median or self.best_views) / max(1, self.subs)

    def as_dict(self) -> dict:
        return {**{k: v for k, v in asdict(self).items() if k not in ("uploads", "hidden")}, "age_days": self.age_days,
                "ratio": round(self.ratio, 1),
                "url": f"https://www.youtube.com/channel/{self.id}",
                "best_url": f"https://www.youtube.com/watch?v={self.best_id}" if self.best_id else ""}


# ---------- 1. ниши ----------

def _n(name: str, why: str, *keywords: str) -> dict:
    return {"name": name, "why": why, "keywords": list(keywords)}


# 40 «вечнозелёных» ниш: спрос не зависит от новостей и сезона, ролики годами набирают просмотры из поиска
# и рекомендаций, а видеоряд собирается тем, что умеет программа — фото по кругу, стоковые и YouTube-кадры,
# немного AI-генерации. Ключевые слова — короткие запросы, которыми зрители ищут такие ролики.
EVERGREEN = [
    _n("Судьбы знаменитостей", "Спрос на имена не кончается; видеоряд — фото звезды по кругу",
       "судьба актрисы", "трагическая судьба звезды", "как сложилась жизнь актёра", "последние годы жизни актёра"),
    _n("Звёзды тогда и сейчас", "Ностальгия + любопытство, огромный поиск; только фото",
       "звёзды тогда и сейчас", "как изменились актёры", "актёры фильма спустя годы"),
    _n("Личная жизнь и тайны звёзд", "Сплетни о звёздах прошлого смотрят всегда; фото и хроника",
       "личная жизнь актрисы", "тайны звёзд", "жёны известных актёров", "романы звёзд"),
    _n("Дети и наследники знаменитостей", "Вечное «а как сейчас живут дети звезды»; фото",
       "дети знаменитостей", "как выглядят дети звёзд", "наследство звезды"),
    _n("Королевские семьи", "Монархии — сериал без конца; фото и хроника",
       "королевская семья", "принцесса Диана", "тайны королевской семьи"),
    _n("Жизненные истории", "Аудитория 50+ слушает часами; видеоряд — сток и AI-кадры",
       "жизненные истории", "истории из жизни", "рассказы о жизни"),
    _n("Свекровь и невестка", "Самая «кликабельная» семейная тема; длинные ролики",
       "истории о свекрови", "свекровь и невестка", "наглая родня истории"),
    _n("Истории измены и предательства", "Сильные эмоции — высокое удержание",
       "измена мужа истории", "предательство мужа рассказ", "жена узнала правду"),
    _n("Деревенские рассказы", "Ностальгия по деревне у 55+, спокойный формат",
       "деревенские рассказы", "рассказ о деревне", "жизнь в деревне истории"),
    _n("Рассказы на ночь для взрослых", "Слушают каждый вечер, ролики по часу и больше",
       "рассказы на ночь", "истории перед сном", "добрые рассказы для взрослых"),
    _n("Мистические истории", "Вечный интерес, атмосферный видеоряд из стока и AI",
       "мистические истории", "страшные истории из жизни", "необъяснимые случаи"),
    _n("Аудиорассказы классиков", "Классика бесплатна и не устаревает; голос + картины",
       "рассказы Чехова", "аудиорассказы классиков", "рассказ Шукшина"),
    _n("Притчи и мудрость", "Пересматривают и пересылают; голос + красивые кадры",
       "притча со смыслом", "мудрые притчи", "мудрость жизни"),
    _n("Цитаты и мысли великих", "Короткая подача, огромный поиск",
       "цитаты великих людей", "мудрые слова", "мысли великих людей"),
    _n("Стоицизм и философия", "Растущий интерес к «мудрости для жизни»; статуи, картины, AI",
       "стоицизм", "Марк Аврелий", "философия жизни"),
    _n("Психология отношений", "Каждому близко, много поисковых запросов; голос + слайды",
       "психология отношений", "нарцисс в отношениях", "почему мужчины уходят"),
    _n("Саморазвитие и привычки", "Поиск «как изменить жизнь» вечен; сток",
       "как изменить жизнь", "привычки успешных людей", "сила привычки"),
    _n("Загадки истории", "Не устаревает, картины и архивные фото",
       "загадки истории", "тайны истории", "необъяснимые исторические факты"),
    _n("Древние цивилизации", "Египет, Рим, майя — вечная классика; AI-кадры и сток",
       "древний египет", "древний рим", "тайны древних цивилизаций"),
    _n("Археология и находки", "Находки «переписывают историю» — высокая кликабельность",
       "археологические находки", "необъяснимые находки", "древние артефакты"),
    _n("Жизнь в СССР", "Ностальгия 45+, архивные фото и хроника",
       "жизнь в СССР", "как жили в СССР", "советский быт"),
    _n("Правители и исторические личности", "Биографии царей и императриц смотрят всегда",
       "история императрицы", "жизнь царя", "правители России"),
    _n("Быт в прошлом", "«Как жили раньше» — простой и любимый формат; картины",
       "как жили крестьяне", "быт в средневековье", "как жили люди 100 лет назад"),
    _n("Катастрофы и крушения", "Сильный интерес и долгий поиск; хроника и реконструкции",
       "авиакатастрофа история", "крушение корабля", "крупнейшие катастрофы"),
    _n("Громкие расследования", "True crime — один из самых смотрибельных жанров; фото и сток",
       "громкие уголовные дела", "загадочное исчезновение", "нераскрытые дела"),
    _n("Космос и Вселенная", "Вечное «что там»; NASA-кадры и AI",
       "тайны космоса", "чёрная дыра", "что за пределами вселенной"),
    _n("Океан и его тайны", "Глубины пугают и манят; сток подводных съёмок",
       "тайны океана", "глубоководные существа", "что на дне океана"),
    _n("Удивительные животные", "Безопасно для рекламы, много стока",
       "удивительные животные", "самые опасные животные", "дикая природа"),
    _n("Истории о собаках и кошках", "Трогательные истории спасения — высокий отклик",
       "история собаки", "верная собака история", "спасение животных"),
    _n("Мегапроекты и инженерия", "«Как это построили» — вечный поиск; сток и хроника",
       "мегапроекты", "как построили", "самые большие сооружения"),
    _n("Заброшенные места", "Города-призраки и руины: атмосферно и кликабельно",
       "заброшенные места", "город призрак", "заброшенные города мира"),
    _n("Страны и их странности", "«Как живут в…» — постоянный поиск; сток",
       "как живут люди в", "самые странные страны", "интересные факты о странах"),
    _n("История брендов и компаний", "Истории успеха и провалов; логотипы и фото",
       "история бренда", "как появилась компания", "почему обанкротилась компания"),
    _n("Истории успеха и миллиардеры", "Мотивация + любопытство к деньгам; фото",
       "история успеха", "как стал миллиардером", "биография миллиардера"),
    _n("Изобретения и история вещей", "«Кто придумал» — вечный вопрос; архив и AI",
       "история изобретения", "кто изобрёл", "как появились привычные вещи"),
    _n("Интересные факты", "Подборки фактов смотрят все возрасты; сток",
       "интересные факты", "факты о которых вы не знали", "удивительные факты"),
    _n("Наука простыми словами", "Объяснения «почему так» не устаревают; сток и AI",
       "наука простыми словами", "как устроен мир", "почему так происходит"),
    _n("Тайны съёмок фильмов", "Любимые фильмы и их закулисье; кадры и фото",
       "тайны съёмок фильма", "как снимали фильм", "актёры советского кино"),
    _n("Деньги и финансовая грамотность", "Вечный вопрос «как сберечь»; сток и слайды",
       "финансовая грамотность", "как экономить деньги", "ошибки с деньгами"),
    _n("Народные приметы и традиции", "Аудитория 55+ ищет постоянно; картины и сток",
       "народные приметы", "старинные традиции", "приметы и суеверия"),
]

_QUERIES = """Ты аналитик YouTube. По ключевым словам ниши составь поисковые запросы, которыми зрители ищут такие
ролики на YouTube: короткие (2–5 слов), разные по смыслу, строго на языке аудитории (если ключевые слова на другом
языке — переведи по смыслу). Ответ — только JSON: {"queries": ["…", "…"]}"""

def _json(raw: str | None, key: str) -> list:
    val = json_dict(raw).get(key)
    return val if isinstance(val, list) else []


def queries_for(keywords: list[str], language: str, llm, cancel, notes: list[str] | None = None) -> list[str]:
    """Поисковые запросы: ключевые слова пользователя + похожие от нейросети, если слов мало.
    Язык аудитории не русский — запросы целиком от нейросети на этом языке (русские слова там ничего не найдут).
    notes — сюда пишется предупреждение, если перевести слова не удалось (искали по словам как есть)."""
    words = list(dict.fromkeys(w.strip() for w in keywords if w and w.strip()))
    foreign = (language or "ru") not in ("ru", "uk")
    out = [] if foreign else list(words)
    if words and (foreign or len(out) < 5):
        from engine.publish.polls import ask

        try:
            raw = ask(_QUERIES, f"Ключевые слова: {'; '.join(words)}\nЯзык аудитории: {language or 'ru'}\n"
                                f"Нужно запросов: {max(6, len(words))}", llm, cancel, "niche_queries")
            out += [str(q).strip() for q in _json(raw, "queries") if str(q).strip()]
        except Exception as exc:
            if cancel is not None:
                cancel.check()
            log(f"Ниши: запросы от нейросети не получены ({str(exc)[:120]}) — ищу по ключевым словам")
    if foreign and words and not out:
        if notes is not None:
            notes.append(f"Нейросеть не перевела слова на язык поиска ({language}) — искал по твоим словам как есть. "
                         "Впиши слова на этом языке или подключи нейросеть (Настройки → LLM).")
    if not out:
        out = words
    return list(dict.fromkeys(out))[:MAX_QUERIES]


# ---------- 2–5. YouTube API ----------


def _chunks(items: list[str], n: int = 50):
    for k in range(0, len(items), n):
        yield items[k:k + n]


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def search(api, queries: list[str], f: Filters, *, now: datetime | None = None,
           cancel: CancelToken | None = None, progress=None,
           passes: tuple[tuple[str, str], ...] = SEARCH_PASSES,
           window_days: int = WINDOW_DAYS) -> tuple[dict[str, Found], dict[str, dict]]:
    """Ролики по запросам (последние window_days) → (каналы, ролики {id: {...}}).
    passes — проходы поиска на каждый запрос (порядок, длительность): каждый стоит 100 единиц квоты."""
    from engine.rewrite.channels import KeysExhausted, _iso_duration, _iso_time

    now = now or datetime.now(timezone.utc)
    # до суток: одинаковый запрос в течение дня берётся из кэша поиска, а не тратит квоту заново
    after = (now - timedelta(days=window_days)).strftime("%Y-%m-%dT00:00:00Z")
    region = f.region or LANG_REGION.get(f.language, "")
    video_q: dict[str, set[str]] = {}
    free_ids: set[str] = set()  # найденные без квоты: дату публикации проверяем сами (ниже)
    free = False  # квота поиска кончилась — дальше ищем бесплатно
    api_error: Exception | None = None
    for n, q in enumerate(queries, 1):
        if cancel:
            cancel.check()
        if progress:
            progress(0.05 + 0.4 * (n - 1) / max(1, len(queries)), f"Поиск: «{q}» ({n}/{len(queries)})")
        params = {"part": "snippet", "type": "video", "q": q, "maxResults": SEARCH_PAGE,
                  "publishedAfter": after, **({"relevanceLanguage": f.language} if f.language else {}),
                  **({"regionCode": region} if region else {})}
        for order, duration in passes:
            ids: list[str] = []
            if not free:
                try:
                    data = api.get("search", {**params, "order": order, "videoDuration": duration})
                    ids = [vid for it in data.get("items") or [] if (vid := (it.get("id") or {}).get("videoId"))]
                except KeysExhausted as exc:
                    api_error, free = exc, True
                    log(f"Ниши: поиск через API недоступен ({exc}) — дальше ищу бесплатно, без квоты")
            if free:
                from engine.research.free_search import search_ids

                ids = search_ids(q, order, duration, region=region)
                free_ids.update(ids)
            for vid in ids:
                video_q.setdefault(vid, set()).add(q)
    if free and not video_q and api_error is not None:
        raise api_error  # и бесплатный поиск ничего не дал — честно говорим про ключи
    videos: dict[str, dict] = {}
    for batch in _chunks(list(video_q)):
        data = api.get("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(batch)})
        for v in data.get("items") or []:
            sn = v.get("snippet") or {}
            if v["id"] in free_ids and (_iso_time(sn.get("publishedAt") or "") or now.timestamp()) < now.timestamp() - window_days * 86400:
                continue  # бесплатный поиск отдаёт «за год» — окно в полгода применяем здесь
            videos[v["id"]] = {"id": v["id"], "title": sn.get("title") or "", "channel": sn.get("channelId") or "",
                               "views": _int((v.get("statistics") or {}).get("viewCount")),
                               "minutes": _iso_duration((v.get("contentDetails") or {}).get("duration") or "") / 60,
                               "ts": _iso_time(sn.get("publishedAt") or ""),
                               # язык звука/ролика, как его указал автор (часто пусто) — «ru», «en-US»…
                               "audio": (sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "").lower(),
                               "queries": sorted(video_q.get(v["id"], ()))}
    channels: dict[str, Found] = {}
    for v in videos.values():
        if not v["channel"] or v["minutes"] < MIN_MINUTES:
            continue
        c = channels.setdefault(v["channel"], Found(id=v["channel"], title=""))
        c.hits += 1
        c.queries = sorted(set(c.queries) | set(v["queries"]))
        if v["views"] > c.best_views:
            c.best_id, c.best_title, c.best_views = v["id"], v["title"], v["views"]
    from engine.rewrite.channels import _iso_time

    for batch in _chunks(list(channels)):
        data = api.get("channels", {"part": "snippet,statistics,contentDetails", "id": ",".join(batch)})
        for ch in data.get("items") or []:
            c = channels.get(ch["id"])
            if not c:
                continue
            sn, st = ch.get("snippet") or {}, ch.get("statistics") or {}
            c.title, c.handle, c.country = sn.get("title") or "", sn.get("customUrl") or "", sn.get("country") or ""
            c.created = _iso_time(sn.get("publishedAt") or "")
            c.hidden = bool(st.get("hiddenSubscriberCount"))
            c.subs = 0 if c.hidden else _int(st.get("subscriberCount"))
            c.videos, c.views = _int(st.get("videoCount")), _int(st.get("viewCount"))
            c.uploads = ((ch.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads") or ""
    return channels, videos


def prefilter(c: Found, f: Filters | None = None) -> list[str]:
    """Дешёвый отсев по найденным роликам — до разбора последних роликов канала."""
    if c.title == "":
        return ["missing"]
    if c.hidden:
        return ["hidden"]
    limit = (f or Filters()).subs_limit
    if limit and c.subs > limit:
        return ["big"]  # уже крупный канал — конкурентов на нём не найти
    if c.best_views < PRE_MIN_VIEWS:
        return ["small_views"]
    if c.best_views < c.subs:
        return ["skew"]  # даже лучший найденный ролик набрал меньше, чем у канала подписчиков
    return []


def rejects(c: Found, f: Filters | None = None) -> list[str]:
    """Каким условиям канал не прошёл после разбора последних роликов (пусто — подходит)."""
    out = prefilter(c, f)
    if out:
        return out
    if c.recent_count < MIN_LONG:
        out.append("few_long")
    if steady(c) or c.hit_count >= MIN_HITS:
        return out
    if c.recent_median < STABLE_VIEWS:
        out.append("unstable")
    if c.recent_median < MIN_RATIO * max(1, c.subs):
        out.append("low_ratio")
    return out


def steady(c: Found) -> bool:
    """Стабильно сильные просмотры: медиана от 30 тыс. и в 3+ раза больше подписчиков."""
    return c.recent_median >= STABLE_VIEWS and c.recent_median >= MIN_RATIO * max(1, c.subs)


def is_hit(views: int, subs: int) -> bool:
    return views >= HIT_VIEWS and views >= HIT_RATIO * max(1, subs)


def passes(c: Found, f: Filters | None = None) -> bool:
    return not rejects(c, f)


def deepen(api, c: Found, f: Filters | None = None) -> None:
    """Последние длинные ролики канала: медиана просмотров, доля 100 тыс.+, частота, длительность."""
    from engine.rewrite.channels import _iso_duration, _iso_time

    if not c.uploads:
        return
    data = api.get("playlistItems", {"part": "contentDetails", "playlistId": c.uploads, "maxResults": UPLOADS_PAGE})
    ids = [i["contentDetails"]["videoId"] for i in data.get("items") or [] if (i.get("contentDetails") or {}).get("videoId")]
    rows = []
    for batch in _chunks(ids):
        data = api.get("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(batch)})
        for v in data.get("items") or []:
            rows.append((_int((v.get("statistics") or {}).get("viewCount")),
                         _iso_time((v.get("snippet") or {}).get("publishedAt") or ""),
                         _iso_duration((v.get("contentDetails") or {}).get("duration") or "") / 60,
                         (v.get("snippet") or {}).get("title") or ""))
    # Shorts не считаем; свежие (меньше 3 дней) ещё не набрали — в медиану не берём
    fresh = time.time() - 3 * 86400
    all_long = sorted((r for r in rows if r[2] >= MIN_MINUTES and (not r[1] or r[1] < fresh)), key=lambda r: -r[1])
    long_ = all_long[:RECENT]
    if not long_:
        return
    hits = [r[0] for r in all_long if is_hit(r[0], c.subs)]
    c.hit_count, c.top_views = len(hits), max(r[0] for r in all_long)
    views = [r[0] for r in long_]
    c.recent_count = len(views)
    c.recent_avg, c.recent_median = int(statistics.mean(views)), int(statistics.median(views))
    c.hit_share = round(sum(1 for v in views if is_hit(v, c.subs)) / len(views), 2)
    stamps = sorted(r[1] for r in long_ if r[1])
    if len(stamps) >= 2:
        weeks = max(1 / 7, (stamps[-1] - stamps[0]) / (7 * 86400))
        c.per_week = round((len(stamps) - 1) / weeks, 1)
    c.avg_minutes = round(statistics.mean(r[2] for r in long_), 1)
    c.recent_titles = [r[3] for r in long_][:10]


def score(c: Found, f: Filters | None = None) -> float:
    """Выше — у кого больше просмотров на подписчика, стабильнее и больше просмотры и меньше роликов на канале."""
    reach = max(c.recent_median, c.top_views // 3)  # «стреляющий» канал — по его залётам, не только по медиане
    return round(math.log10(1 + reach / max(1, c.subs)) * 2 + math.log10(1 + reach) + c.hit_share
                 + 0.15 * min(c.hit_count, 6)
                 - 0.5 * math.log10(1 + c.videos / 50) + 0.1 * min(c.hits, 5), 3)


def find(niche: str, keywords: list[str], f: Filters, keys: list[str], llm: tuple[str, str, str] | None, *,
         cancel: CancelToken | None = None, progress=None) -> dict[str, Any]:
    """Весь поиск → {"channels": [...], "stats": {...}}; итог сохраняется в историю поисков.
    niche — подпись поиска (название выбранной ниши), keywords — по чему искать."""
    if not keys:
        raise ValueError("нужен ключ YouTube Data API (Настройки → YouTube)")
    notes: list[str] = []
    queries = queries_for(keywords or [niche], f.language, llm, cancel, notes)
    if not queries:
        raise ValueError("впиши ключевые слова или выбери нишу")
    label = niche.strip() or queries[0]
    return _search_and_rank(label, list(keywords), queries, f, keys, extra={"notes": notes} if notes else None,
                            cancel=cancel, progress=progress)


def _search_and_rank(label: str, keywords: list[str], queries: list[str], f: Filters, keys: list[str], *,
                     exclude: frozenset[str] = frozenset(), extra: dict | None = None,
                     cancel: CancelToken | None = None, progress=None) -> dict[str, Any]:
    """Общий конвейер: запросы → ролики → каналы → отсев по критериям → рейтинг → история.
    exclude — каналы, которых в выдаче быть не должно (образец в «похожих»)."""
    from engine.rewrite.channels import _Api

    log(f"Ниши: «{label}» — запросы: {', '.join(queries)}")
    api = _Api(keys)
    dropped: dict[str, int] = {}
    try:
        channels, videos = search(api, queries, f, cancel=cancel, progress=progress)
        for cid in exclude:
            channels.pop(cid, None)
        dropped["short"] = sum(1 for v in videos.values() if v["minutes"] < MIN_MINUTES)
        cands = []
        for c in channels.values():
            why = prefilter(c, f)
            if why:
                dropped[why[0]] = dropped.get(why[0], 0) + 1
            else:
                cands.append(c)
        # самые перспективные — первыми: много просмотров найденного ролика на подписчика
        cands.sort(key=lambda c: -(math.log10(1 + c.best_views) + math.log10(1 + c.best_views / max(1, c.subs))))
        dropped["not_checked"] = max(0, len(cands) - DEEP_MAX)
        deep = cands[:DEEP_MAX]
        for n, c in enumerate(deep, 1):
            if cancel:
                cancel.check()
            if progress:
                progress(0.5 + 0.3 * (n - 1) / max(1, len(deep)), f"Каналы: {n}/{len(deep)} — {c.title}")
            try:
                deepen(api, c, f)
            except Exception as exc:
                log(f"Ниши: последние ролики «{c.title}» не получены ({str(exc)[:120]})")
    finally:
        api.close()
    good = []
    for c in deep:
        why = rejects(c, f)
        for k in why:
            dropped[k] = dropped.get(k, 0) + 1
        if not why:
            good.append(c)
    dropped = {k: n for k, n in dropped.items() if n}
    log(f"Ниши: роликов {len(videos)}, каналов {len(channels)}, разобрано {len(deep)}, подошли {len(good)}, "
        f"отсеяно {dropped}")
    for c in good:
        # в 10+ раз больше подписчиков стабильно, или ролик-миллионник
        c.anomaly = c.recent_median >= 10 * max(1, c.subs) or c.top_views >= 1_000_000
        c.score = score(c, f)
    good.sort(key=lambda c: -c.score)
    top = good[:f.results]
    result = {"id": f"{time.time():.6f}", "niche": label, "keywords": list(keywords), "queries": queries,
              "filters": asdict(f), "at": time.time(), **(extra or {}),
              "stats": {"videos": len(videos), "channels": len(channels), "checked": len(deep), "passed": len(good),
                        "dropped": dropped},
              "channels": [c.as_dict() for c in top]}
    save(result)
    return result


# ---------- похожие на канал ----------

_VIDEO_ID = r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|live/|embed/))([\w-]{11})"
SAMPLE_TITLES = 12  # сколько названий роликов образца отдаём нейросети
TITLE_QUERIES = 3  # по стольким названиям лучших роликов образца ищем похожие ролики

_SIMILAR = """Ты аналитик YouTube. Тебе дан канал-образец: описание, ключевые слова и названия его роликов
(самые просматриваемые и свежие). Нужно найти ПОХОЖИЕ каналы: та же тема и подача, та же аудитория.
Определи:
- topic — о чём канал, одной короткой фразой на русском (тема и формат: «судьбы советских актрис, документальные рассказы»);
- language — язык аудитории канала, код (ru, en, es, pt, de, fr, it, pl, tr, uk);
- queries — {n} поисковых запросов, которыми зритель такого канала ищет ролики на YouTube: короткие (2–5 слов),
  разные по смыслу (тема, герои, формат, типичные формулировки названий), СТРОГО на языке {target}.
  Не называй сам канал и не ищи его имя — нужны другие каналы.
- titles — только если язык поиска ЗАДАН и отличается от языка канала: названия роликов, помеченные [лучшие],
  переведи на язык поиска (естественно, как их назвал бы местный автор), по порядку; иначе пустой список.
Ответ — только JSON: {"topic": "…", "language": "ru", "queries": ["…", "…"], "titles": []}"""


class SimilarError(ValueError):
    """Образец не найден или не понят — сообщение показывается пользователю."""


def resolve_channel(api, url: str) -> dict:
    """Ссылка на канал или на ролик → данные канала-образца (snippet, statistics, contentDetails, brandingSettings)."""
    import re

    from engine.rewrite.channels import ChannelError, _channel_query

    text = (url or "").strip()
    if not text:
        raise SimilarError("Вставь ссылку на канал: youtube.com/@имя или youtube.com/channel/UC…")
    part = "snippet,statistics,contentDetails,brandingSettings"
    m = re.search(_VIDEO_ID, text)
    if m and not re.search(r"(UC[\w-]{22})|youtube\.com/@", text):
        data = api.get("videos", {"part": "snippet", "id": m.group(1)})
        items = data.get("items") or []
        if not items:
            raise SimilarError(f"Ролик не найден: {text}")
        query = {"id": items[0]["snippet"]["channelId"]}
    else:
        try:
            query = _channel_query(text)
        except ChannelError as exc:
            custom = re.search(r"youtube\.com/c/([^/?#\s]+)", text)
            if not custom:
                raise SimilarError(str(exc)) from None
            found = api.get("search", {"part": "snippet", "type": "channel", "q": custom.group(1), "maxResults": 1})
            hit = (found.get("items") or [{}])[0].get("snippet", {}).get("channelId")
            if not hit:
                raise SimilarError(f"Канал не найден: {text}") from None
            query = {"id": hit}
    data = api.get("channels", {"part": part, **query})
    items = data.get("items") or []
    if not items and "forUsername" in query:  # у /c/имя часто нет «username» — ищем по названию
        found = api.get("search", {"part": "snippet", "type": "channel", "q": query["forUsername"], "maxResults": 1})
        hit = (found.get("items") or [{}])[0].get("snippet", {}).get("channelId")
        items = (api.get("channels", {"part": part, "id": hit}).get("items") or []) if hit else []
    if not items:
        raise SimilarError(f"Канал не найден: {text}")
    return items[0]


def sample_videos(api, channel: dict) -> list[dict]:
    """Ролики образца: [{title, views, minutes, ts}] — последние загрузки; Shorts в выборку не идут."""
    from engine.rewrite.channels import _iso_duration, _iso_time

    uploads = ((channel.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads") or ""
    if not uploads:
        return []
    data = api.get("playlistItems", {"part": "contentDetails", "playlistId": uploads, "maxResults": UPLOADS_PAGE})
    ids = [i["contentDetails"]["videoId"] for i in data.get("items") or [] if (i.get("contentDetails") or {}).get("videoId")]
    out = []
    for batch in _chunks(ids):
        for v in api.get("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(batch)}).get("items") or []:
            minutes = _iso_duration((v.get("contentDetails") or {}).get("duration") or "") / 60
            if minutes >= MIN_MINUTES:
                out.append({"title": (v.get("snippet") or {}).get("title") or "", "minutes": minutes,
                            "views": _int((v.get("statistics") or {}).get("viewCount")),
                            "ts": _iso_time((v.get("snippet") or {}).get("publishedAt") or "")})
    return out


class SimilarLanguageError(SimilarError):
    """Язык поиска задан, а запросы на нём составить нечем (нет нейросети) — искать по чужим словам бессмысленно."""


def similar_queries(channel: dict, videos: list[dict], language: str, llm, cancel,
                    forced: str = "") -> tuple[list[str], str, str]:
    """→ (запросы, тема, язык аудитории образца). Нейросеть выводит тему/язык и пишет запросы; плюс названия лучших
    роликов образца — по ним YouTube сам находит ролики на то же. Нейросети нет — только названия и ключевые слова.
    forced — язык поиска, который выбрал человек: запросы и названия-образцы только на нём (а не на языке образца)."""
    sn = channel.get("snippet") or {}
    keywords = ((channel.get("brandingSettings") or {}).get("channel") or {}).get("keywords") or ""
    top = sorted(videos, key=lambda v: -v["views"])
    fresh = sorted(videos, key=lambda v: -v["ts"])
    titles = list(dict.fromkeys(v["title"] for v in [*top[:SAMPLE_TITLES // 2], *fresh] if v["title"]))[:SAMPLE_TITLES]
    topic, lang, queries, translated = "", "", [], []
    by_title = [v["title"] for v in top if v["title"]][:TITLE_QUERIES]
    if titles or sn.get("description"):
        try:
            from engine.publish.polls import ask

            target = f"поиска «{forced}» (задан человеком)" if forced else "аудитории"
            marked = [f"- {t}" + (" [лучшие]" if t in by_title else "") for t in titles]
            raw = ask(_SIMILAR.replace("{n}", "6").replace("{target}", target),
                      f"Канал: {sn.get('title') or ''}\nОписание: {(sn.get('description') or '')[:600]}\n"
                      f"Ключевые слова канала: {keywords[:300]}\nЗаписанный язык канала: {sn.get('defaultLanguage') or '—'}\n"
                      + (f"Язык поиска (ЗАДАН): {forced}\n" if forced else "")
                      + "Названия роликов:\n" + "\n".join(marked), llm, cancel, "niche_similar")
            data = json_dict(raw)
            topic = str(data.get("topic") or "").strip()[:160]
            lang = str(data.get("language") or "").strip().lower()[:2]
            queries = [str(q).strip() for q in (data.get("queries") or []) if str(q).strip()]
            translated = [str(t).strip() for t in (data.get("titles") or []) if str(t).strip()][:TITLE_QUERIES]
        except Exception as exc:
            if cancel is not None:
                cancel.check()
            log(f"Ниши: нейросеть не описала образец ({str(exc)[:120]}) — ищу по названиям его роликов")
    own = (sn.get("defaultLanguage") or "").lower()[:2]
    foreign = bool(forced) and lang != forced and own != forced  # язык поиска не совпадает с языком образца
    if foreign and not queries:
        # чужими словами образца искать нечего: английские запросы вернут английские каналы
        raise SimilarLanguageError(
            f"Образец на другом языке (не «{forced}»), а перевести запросы на язык поиска нечем — "
            "подключи нейросеть (Настройки → LLM) или выбери «Как у канала».")
    if not queries:
        import re

        # ключевые слова канала: «слово "несколько слов" ещё» — берём фразы в кавычках и отдельные слова от 4 букв
        words = [a or b for a, b in re.findall(r'"([^"]+)"|(\S+)', keywords)]
        queries = [w for w in words if len(w) >= 4][:3]
    # названия лучших роликов образца: ролик с таким названием найдёт соседей по теме;
    # язык поиска другой — берём их перевод (оригиналы дали бы выдачу на языке образца)
    if foreign:
        by_title = translated
    room = max(0, MAX_QUERIES - len(by_title))
    return list(dict.fromkeys([*queries[:room], *by_title]))[:MAX_QUERIES], topic, lang or language


def find_similar(url: str, f: Filters, keys: list[str], llm: tuple[str, str, str] | None, *,
                 auto_language: bool = True, cancel: CancelToken | None = None, progress=None) -> dict[str, Any]:
    """Каналы, похожие на образец по ссылке, с теми же критериями, что у поиска по нише.
    auto_language — язык аудитории берём у образца (нейросеть выводит его по названиям), иначе — из Filters."""
    from engine.rewrite.channels import _Api

    if not keys:
        raise ValueError("нужен ключ YouTube Data API (Настройки → YouTube)")
    api = _Api(keys)
    try:
        if progress:
            progress(0.02, "Читаю канал-образец…")
        channel = resolve_channel(api, url)
        videos = sample_videos(api, channel)
    finally:
        api.close()
    sn, st = channel.get("snippet") or {}, channel.get("statistics") or {}
    if not videos:
        raise SimilarError("У канала нет длинных роликов (от 5 минут) — по чему искать похожие, непонятно")
    if progress:
        progress(0.04, "Определяю тему и язык образца…")
    queries, topic, lang = similar_queries(channel, videos, f.language, llm, cancel,
                                           forced="" if auto_language else f.language)
    if auto_language and lang in LANG_REGION:
        f = Filters.of({**asdict(f), "language": lang, "region": ""})
    if not queries:
        raise SimilarError("Не удалось составить запросы по каналу — нужен ключ нейросети (Настройки → LLM)")
    title = sn.get("title") or channel["id"]
    source = {"id": channel["id"], "title": title, "url": f"https://www.youtube.com/channel/{channel['id']}",
              "subs": 0 if st.get("hiddenSubscriberCount") else _int(st.get("subscriberCount")), "topic": topic,
              "language": f.language}
    log(f"Ниши: образец «{title}» — {topic or 'тема не определена'}, язык {f.language}")
    return _search_and_rank(f"Похожие на «{title}»", [], queries, f, keys, exclude=frozenset({channel["id"]}),
                            extra={"similar": source}, cancel=cancel, progress=progress)


# ---------- история ----------


def save(result: dict) -> None:
    with LOCK:
        runs = read_json(RESULTS_FILE, [])
        runs = [result, *(runs if isinstance(runs, list) else [])][:20]
        write_json(RESULTS_FILE, runs)


def run_id(run: dict) -> str:
    """У старых поисков id не было — берём время запуска."""
    return str(run.get("id") or f"{float(run.get('at') or 0):.6f}")


def runs() -> list[dict]:
    data = read_json(RESULTS_FILE, [])
    return [{**r, "id": run_id(r)} for r in data if isinstance(r, dict)] if isinstance(data, list) else []


# ---------- избранное ----------


def favorites() -> list[dict]:
    """Избранные каналы: новые сверху. Хранятся отдельно от истории поисков (она обрезается до 20) — не пропадают."""
    data = read_json(FAVORITES_FILE, [])
    items = [f for f in data if isinstance(f, dict) and f.get("id")] if isinstance(data, list) else []
    return sorted(items, key=lambda f: -float(f.get("saved_at") or 0))


def add_favorite(run: str, channel_id: str) -> bool:
    """Канал из выдачи поиска → в избранное (снимок его цифр на момент сохранения). Уже там — обновляем цифры."""
    with LOCK:
        hit = next((r for r in runs() if r["id"] == run), None)
        ch = next((c for c in (hit or {}).get("channels") or [] if c.get("id") == channel_id), None)
        if hit is None or ch is None:
            return False
        label = (hit.get("similar") or {}).get("title") and f"похожие на «{hit['similar']['title']}»" \
            or hit.get("niche") or (hit.get("subniches") or [""])[0]
        lang = (hit.get("similar") or {}).get("language") or (hit.get("filters") or {}).get("language") or ""
        items = favorites()
        old = next((f for f in items if f["id"] == channel_id), None)
        rec = {**ch, "saved_at": (old or {}).get("saved_at") or time.time(), "found_in": (old or {}).get("found_in") or label,
               "language": (old or {}).get("language") or lang, "found_at": hit.get("at") or 0}
        write_json(FAVORITES_FILE, [rec, *[f for f in items if f["id"] != channel_id]])
    return True


def remove_favorite(channel_id: str) -> bool:
    with LOCK:
        items = favorites()
        left = [f for f in items if f["id"] != channel_id]
        if len(left) == len(items):
            return False
        write_json(FAVORITES_FILE, left)
    return True


def remove(run: str, channel: str = "") -> bool:
    """Убрать из истории поиск целиком или один канал из его выдачи (только из журнала программы)."""
    with LOCK:
        data = read_json(RESULTS_FILE, [])
        data = [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
        hit = next((r for r in data if run_id(r) == run), None)
        if hit is None:
            return False
        if channel:
            before = len(hit.get("channels") or [])
            hit["channels"] = [c for c in hit.get("channels") or [] if c.get("id") != channel]
            if len(hit["channels"]) == before:
                return False
        else:
            data.remove(hit)
        write_json(RESULTS_FILE, data)
    return True
