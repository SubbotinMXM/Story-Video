"""Общий интерфейс сервиса озвучки.

Каждый сервис описывает свои настройки списком полей (FieldSpec). Интерфейс
рисует их сам, поэтому новый сервис = один файл в engine/tts + строка в реестре.

Значения настроек сервиса хранятся в config["tts"][<id сервиса>] как dict:
  secret / text  → str
  slider         → float
  named_list     → {"items": [{"id": "...", "name": "..."}], "selected": "<id>"}
  remote_list    → то же, но список не вводится руками, а подтягивается из сервиса
                   кнопкой «Обновить» (TtsProvider.refresh)
  multi_choice   → list[str]: отмеченные варианты из options (например, языки перевода)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from engine.core.cancel import CancelToken

ProgressCb = Callable[[float, str], None]
FieldKind = Literal["secret", "text", "slider", "named_list", "remote_list", "multi_choice"]


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    kind: FieldKind
    hint: str = ""
    placeholder: str = ""
    # показывать в карточке «Озвучка» на странице ролика (не только в Настройках)
    quick: bool = False
    # служебное поле: хранится и используется, но в интерфейсе не показывается
    hidden: bool = False
    required: bool = False
    # slider
    min: float = 0.0
    max: float = 1.0
    step: float = 0.01
    default: Any = None
    # named_list: подписи для формы добавления
    item_label: str = ""
    id_label: str = "ID"
    # named_list: что подставить, если список пуст
    fallback_id: str = ""
    # multi_choice: варианты (значение, подпись)
    options: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NamedItem:
    id: str
    name: str


@dataclass
class NamedList:
    items: list[NamedItem] = field(default_factory=list)
    selected: str = ""

    @property
    def selected_id(self) -> str:
        if any(i.id == self.selected for i in self.items):
            return self.selected
        return self.items[0].id if self.items else ""

    @property
    def selected_name(self) -> str:
        sid = self.selected_id
        return next((i.name for i in self.items if i.id == sid), sid)

    @classmethod
    def parse(cls, raw: object) -> NamedList:
        items: list[NamedItem] = []
        seen: set[str] = set()
        selected = ""
        if isinstance(raw, dict):
            selected = str(raw.get("selected") or "").strip()
            for item in raw.get("items") or []:
                if not isinstance(item, dict):
                    continue
                iid = str(item.get("id") or "").strip()
                if not iid or iid in seen:
                    continue
                seen.add(iid)
                items.append(NamedItem(iid, str(item.get("name") or "").strip() or iid[:12]))
        return cls(items=items, selected=selected)

    def to_dict(self) -> dict[str, Any]:
        return {"items": [{"id": i.id, "name": i.name} for i in self.items], "selected": self.selected_id}


@dataclass
class DubTrack:
    """Готовый перевод: озвучка, текст и субтитры на одном языке."""

    language: str
    audio: Path
    text: Path | None = None
    srt: Path | None = None
    # слова с таймингами {"words": [{"word", "start", "end"}]} — для субтитров в нашем стиле
    timings: Path | None = None


class TtsProvider:
    """Базовый класс сервиса озвучки. Наследник задаёт id/label/fields и synthesize."""

    id: str = ""
    label: str = ""
    description: str = ""
    fields: tuple[FieldSpec, ...] = ()

    # --- настройки ---

    def normalize(self, raw: object) -> dict[str, Any]:
        """Привести сохранённые значения к виду, который ждут поля."""
        src = raw if isinstance(raw, dict) else {}
        out: dict[str, Any] = {}
        for f in self.fields:
            value = src.get(f.key)
            if f.kind in ("named_list", "remote_list"):
                out[f.key] = NamedList.parse(value).to_dict()
            elif f.kind == "multi_choice":
                picked = {str(v) for v in value} if isinstance(value, (list, tuple)) else set()
                out[f.key] = [v for v, _ in f.options if v in picked]  # порядок — как в options
            elif f.kind == "slider":
                try:
                    num = float(value if value is not None else f.default if f.default is not None else f.min)
                except (TypeError, ValueError):
                    num = float(f.default if f.default is not None else f.min)
                out[f.key] = min(f.max, max(f.min, num))
            else:
                out[f.key] = str(value if value is not None else f.default or "").strip()
        return out

    def check(self, opts: dict[str, Any]) -> None:
        """ValueError с понятным текстом, если озвучить нельзя."""
        for f in self.fields:
            if not f.required:
                continue
            value = opts.get(f.key)
            if f.kind == "remote_list":
                if not NamedList.parse(value).items and not f.fallback_id:
                    raise ValueError(
                        f"{self.label}: нет списка «{f.label}» — нажми «Обновить» (Настройки → Озвучка)"
                    )
                continue
            empty = not NamedList.parse(value).items if f.kind == "named_list" else not str(value or "").strip()
            if empty and not f.fallback_id:
                raise ValueError(f"{self.label}: заполни «{f.label}» (Настройки → Озвучка)")

    def text(self, opts: dict[str, Any], key: str) -> str:
        return str(opts.get(key) or "").strip()

    def choice(self, opts: dict[str, Any], key: str) -> str:
        """id выбранного элемента named_list (или fallback поля)."""
        spec = next(f for f in self.fields if f.key == key)
        return NamedList.parse(opts.get(key)).selected_id or spec.fallback_id

    def number(self, opts: dict[str, Any], key: str) -> float:
        return float(self.normalize({key: opts.get(key)})[key])

    def voice_tag(self, opts: dict[str, Any]) -> str:
        """Короткая метка голоса для логов и checkpoint."""
        return self.id

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "fields": [f.to_dict() for f in self.fields],
        }

    # --- работа ---

    def synthesize(
        self,
        text: str,
        dest: Path,
        opts: dict[str, Any],
        *,
        state_file: Path,
        cancel: CancelToken | None = None,
        on_progress: ProgressCb | None = None,
    ) -> Path:
        """Озвучить text в mp3 dest.

        state_file — куда сервис может записать незавершённый заказ, чтобы при
        повторе после сбоя не платить второй раз.
        """
        raise NotImplementedError

    def ping(self, opts: dict[str, Any]) -> str:
        """Проверка ключа. Вернуть короткое «ok, …» или бросить исключение."""
        raise NotImplementedError

    # --- перевод (дубляж) готовой озвучки на другие языки ---

    def dub_languages(self, opts: dict[str, Any]) -> list[str]:
        """Языки, на которые нужно дополнительно перевести и переозвучить ролик."""
        return []

    def start_dub(self, opts: dict[str, Any], work_dir: Path, *, source_language: str, targets: list[str]) -> list[str]:
        """Запустить перевод озвучки из work_dir. Вернуть языки, которые реально переводятся."""
        raise NotImplementedError

    def iter_dubs(
        self,
        opts: dict[str, Any],
        work_dir: Path,
        *,
        cancel: CancelToken | None = None,
        on_progress: ProgressCb | None = None,
    ) -> Iterator[DubTrack]:
        """Готовые переводы по мере готовности (уже обработанные — пропускаются)."""
        raise NotImplementedError

    def mark_dub_done(self, work_dir: Path, language: str, result: str) -> None:
        """Перевод на language превращён в ролик: при «Продолжить» больше не трогаем."""
        raise NotImplementedError

    def refresh(self, opts: dict[str, Any], key: str) -> list[NamedItem]:
        """Свежий список для поля remote_list (например, шаблоны из кабинета сервиса)."""
        raise NotImplementedError

    def refreshed(self, opts: dict[str, Any], key: str) -> dict[str, Any]:
        """Значение поля после «Обновить»: новый список, выбор сохраняется, если элемент ещё есть."""
        old = NamedList.parse(opts.get(key))
        items = self.refresh(opts, key)
        keep = old.selected_id if any(i.id == old.selected_id for i in items) else ""
        return NamedList(items=items, selected=keep).to_dict()
