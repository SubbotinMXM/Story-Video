"""Профили AdsPower и каналы YouTube в них — запомненные (data/publish/channels.json).

«Синхронизировать с AdsPower» — актуальные профили из AdsPower (названия, номера, удалённые).
Каналы профиля находит программа, открыв его браузер (scan) — результат хранится тут же.
Канал для выкладки — «профиль/ID канала» (UC…); один канал в двух профилях — это один канал.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from engine.publish.store import LOCK, read_json, write_json

FILE = "channels.json"


@dataclass
class Channel:
    id: str  # UC…
    name: str
    handle: str = ""
    authuser: int = 0  # номер аккаунта Google в профиле (0, 1, 2…)
    account: str = ""  # почта/имя аккаунта Google — для понятных сообщений
    playlists: list[str] = field(default_factory=list)
    last_scheduled: str = ""  # последняя запланированная дата по YouTube Studio (YYYY-MM-DD)
    # язык канала (ru, es…): ролик другого языка на канал не заливается. Меняется в любой момент (смена ниши)
    language: str = ""
    language_auto: bool = False  # язык подставлен программой по названиям роликов — стоит проверить
    titles_language: str = ""  # язык названий последних роликов (по данным поиска каналов)
    # плейлист, в который класть ролики, когда плейлистов на канале несколько (выбирает человек; пусто — если
    # плейлист один, берём его, если несколько — не выбираем никакой)
    playlist: str = ""

    def upload_playlists(self) -> list[str]:
        """Плейлисты для выкладки: выбранный (если он ещё есть на канале) или все плейлисты канала."""
        return [self.playlist] if self.playlist and self.playlist in self.playlists else list(self.playlists)


@dataclass
class ProfileEntry:
    id: str
    number: str
    name: str
    group: str = ""
    channels: list[Channel] = field(default_factory=list)
    scanned_at: float = 0.0  # когда искали каналы (0 — ещё не искали)
    missing: bool = False  # профиля больше нет в AdsPower
    # как выглядит Studio в профиле (читается при поиске каналов): язык, 12/24 ч, часовой пояс, платформа
    studio: dict = field(default_factory=dict)

    def label(self) -> str:
        return f"№{self.number} {self.name}".strip()


def key(profile_id: str, channel_id: str) -> str:
    return f"{profile_id}/{channel_id}"


def split_key(raw: str) -> tuple[str, str]:
    profile_id, _, channel_id = str(raw).partition("/")
    return profile_id, channel_id


def load() -> dict[str, ProfileEntry]:
    raw = read_json(FILE, {}).get("profiles") or {}
    out: dict[str, ProfileEntry] = {}
    for pid, p in raw.items():
        if not isinstance(p, dict):
            continue
        chans = [Channel(**{k: v for k, v in c.items() if k in Channel.__dataclass_fields__})
                 for c in p.get("channels") or [] if isinstance(c, dict) and c.get("id")]
        out[pid] = ProfileEntry(id=pid, number=str(p.get("number") or ""), name=str(p.get("name") or ""),
                                group=str(p.get("group") or ""), channels=chans,
                                scanned_at=float(p.get("scanned_at") or 0), missing=bool(p.get("missing")),
                                studio=dict(p.get("studio") or {}))
    return out


def save(profiles: dict[str, ProfileEntry]) -> None:
    write_json(FILE, {"profiles": {pid: asdict(p) for pid, p in profiles.items()}, "synced_at": time.time()})


def sync(found: list) -> dict[str, ProfileEntry]:
    """Профили из AdsPower (adspower.Profile) → учёт: новые добавлены, имена обновлены, пропавшие помечены.
    Найденные раньше каналы профиля сохраняются."""
    with LOCK:
        profiles = load()
        seen = set()
        for p in found:
            seen.add(p.id)
            entry = profiles.get(p.id) or ProfileEntry(id=p.id, number=p.number, name=p.name)
            entry.number, entry.name, entry.group, entry.missing = p.number, p.name, p.group_name, False
            profiles[p.id] = entry
        for pid, entry in profiles.items():
            if pid not in seen:
                entry.missing = True
        save(profiles)
        return profiles


def set_channels(profile_id: str, channels: list[Channel], studio: dict | None = None) -> ProfileEntry:
    """Каналы после поиска. Язык, который ты указал каналу, сохраняется; не указан — подставляем язык
    названий его роликов (с пометкой «определён автоматически»)."""
    with LOCK:
        profiles = load()
        entry = profiles.get(profile_id)
        if entry is None:
            raise ValueError("Профиль не найден — нажми «Синхронизировать с AdsPower»")
        known = {c.id: c for p in profiles.values() for c in p.channels}
        for ch in channels:
            old = known.get(ch.id)
            if old is not None and old.language:
                ch.language, ch.language_auto = old.language, old.language_auto
            if old is not None and old.playlist and old.playlist in ch.playlists:
                ch.playlist = old.playlist  # выбранный плейлист переживает пересканирование
            if not ch.language and ch.titles_language:
                ch.language, ch.language_auto = ch.titles_language, True
        entry.channels, entry.scanned_at = channels, time.time()
        if studio is not None:
            entry.studio = studio
        save(profiles)
        return entry


def set_playlist(channel_id: str, playlist: str) -> None:
    """Плейлист для выкладки (во всех профилях, где есть канал). Пусто — не выбран."""
    name = (playlist or "").strip()
    with LOCK:
        profiles = load()
        found = False
        for p in profiles.values():
            for c in p.channels:
                if c.id == channel_id:
                    if name and name not in c.playlists:
                        raise ValueError(f"на канале нет плейлиста «{name}» — найди каналы профиля заново")
                    c.playlist, found = name, True
        if not found:
            raise ValueError("канал не найден")
        save(profiles)


def update_identity(channel_id: str, *, name: str, handle: str, authuser: int) -> bool:
    """Канал переименовали или сменили @-адрес в YouTube: обновить название, адрес и аккаунт во всех профилях,
    где он есть (ID канала не меняется). → было ли что менять."""
    with LOCK:
        profiles = load()
        changed = False
        for p in profiles.values():
            for c in p.channels:
                if c.id == channel_id and (c.name, c.handle, c.authuser) != (name or c.name, handle, authuser):
                    c.name, c.handle, c.authuser = name or c.name, handle, authuser
                    changed = True
        if changed:
            save(profiles)
        return changed


def set_language(channel_id: str, language: str) -> None:
    """Язык канала (во всех профилях, где он есть). Пусто — язык не указан."""
    lang = normalize(language)
    with LOCK:
        profiles = load()
        found = False
        for p in profiles.values():
            for c in p.channels:
                if c.id == channel_id:
                    c.language, c.language_auto, found = lang, False, True
        if not found:
            raise ValueError("Канал не найден — обнови каналы профиля")
        save(profiles)


def normalize(language: str | None) -> str:
    return str(language or "").strip().lower().split("-")[0].split("_")[0]


@dataclass(frozen=True)
class Target:
    """Канал, на который льём (или соавтор)."""

    profile_id: str
    profile: str  # «№3 Истории»
    channel: Channel

    @property
    def key(self) -> str:
        return key(self.profile_id, self.channel.id)

    def label(self) -> str:
        # у каналов одного человека названия часто совпадают — @адрес их различает
        handle = f" {self.channel.handle}" if self.channel.handle else ""
        return f"{self.channel.name}{handle} ({self.profile})"


def resolve(keys: list[str], *, what: str = "канал") -> list[Target]:
    """Выбранные «профиль/канал» → каналы из учёта. Один канал в двух профилях — один раз (первый)."""
    profiles = load()
    out: list[Target] = []
    seen: set[str] = set()
    for raw in keys or []:
        pid, cid = split_key(raw)
        entry = profiles.get(pid)
        if entry is None or entry.missing:
            raise ValueError(f"Выкладка: профиля {pid} больше нет в AdsPower — нажми «Синхронизировать с AdsPower» и выбери {what} заново")
        chan = next((c for c in entry.channels if c.id == cid), None)
        if chan is None:
            raise ValueError(f"Выкладка: в профиле {entry.label()} нет канала {cid} — обнови каналы профиля")
        if cid in seen:
            continue
        seen.add(cid)
        out.append(Target(pid, entry.label(), chan))
    return out
