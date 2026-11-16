"""Превью YouTube «портреты героев»: чёрный фон 16:9, фото в два ряда (поровну сверху и снизу),
посередине — широкая пустая чёрная полоса под текст. Текста на превью нет.

На вход — список имён. Для каждого: фото из интернета (Google, Wikimedia), проверка по лицу
(лицо, которое повторяется на большинстве найденных фото, — это он), лучший портрет: лицо
крупное, смотрит в камеру, исходник не мелкий. Кадр ячейки центрируется по лицу, с плечами.
"""

from __future__ import annotations

import math
import re
import shutil
import tempfile
from collections.abc import Callable
from concurrent.futures import as_completed
from datetime import datetime
from pathlib import Path

import httpx

from engine.core.cancel import CancelToken
from engine.core.logutil import log
from engine.core.threads import ThreadPoolExecutor

THUMB_W, THUMB_H = 1280, 720
ROW_FRAC = 0.39  # ряд фото — 39% высоты; посередине ~22% — чёрная полоса под текст
GAP = 8
CELL_MAX_ASPECT = 1.5  # ячейка не шире 1,5 высоты: при 1–2 людях в ряду иначе в кадре одни глаза
CANDIDATES = 24  # фото на человека, из которых выбираем портрет
NAME_WORKERS = 3  # людей ищем одновременно (Google не любит больше)
FETCH_WORKERS = 6

Box = tuple[float, float, float, float]
ProgressCb = Callable[[float, str], None]


def parse_names(text: str) -> list[str]:
    """Имена из текста: по строке или через запятую/точку с запятой; нумерацию и маркеры убираем."""
    out: list[str] = []
    # запятая внутри скобок — часть уточнения («Андрей Миронов (советский актёр, 1941–1987)»)
    for line in (text or "").split("\n"):
        prev = ""
        for raw in re.split(r"[,;](?![^()]*\))", line):
            name = re.sub(r"^\s*(\d+[.)]|[-—•*])\s*", "", raw).strip(" .\t")
            name = re.sub(r"\s+/\s+", " — ", name)  # «Marga Barbu / Марга Барбу»
            if not name:
                continue
            if prev and not name[0].isupper():
                # «Андрей Миронов, советский актёр, 1941–1987»: не с заглавной — уточнение к имени, а не новый человек
                joined = f"{prev}; {name}" if " — " in prev else f"{prev} — {name}"
                out[out.index(prev)] = joined
                prev = joined
                continue
            if name.lower() not in (n.lower() for n in out):
                out.append(name)
                prev = name
    return out


def even_names(names: list[str]) -> tuple[list[str], str | None]:
    """Поровну сверху и снизу: при нечётном числе (больше одного) последний на превью не попадает."""
    if len(names) > 1 and len(names) % 2:
        return names[:-1], names[-1]
    return names, None


def best_portrait(paths: list[Path]) -> tuple[Path, Box] | None:
    """Лучшее фото для превью: лицо этого человека крупное, анфас, кадр не мелкий. → (файл, рамка лица)."""
    ranked = ranked_portraits(paths, top=1)
    return ranked[0] if ranked else None


def ranked_portraits(paths: list[Path], *, top: int = 3, marked: set[Path] | None = None) -> list[tuple[Path, Box]]:
    """Лучшие портреты человека по лицу (крупное, анфас, исходник не мелкий), лучший — первым."""
    from PIL import Image

    from engine.slides import faces

    found = [(p, faces.face_boxes(p)) for p in paths]
    found = [(p, f) for p, f in found if f]
    if not found:
        return []
    hero = faces.find_hero([[vec for _b, vec, _fr in f] for _p, f in found])
    scored: list[tuple[float, Path, Box]] = []
    for p, fs in found:
        for box, vec, frontal in fs:
            if hero is not None and float(vec @ hero) < faces.HERO_MATCH:
                continue
            try:
                with Image.open(p) as img:
                    w, h = img.size
            except Exception:
                break
            face = box[3] / max(1, h)
            # лицо крупное, но с запасом на плечи; смотрит в камеру; исходник не мелкий
            score = (1.0 - abs(face - 0.35)) + frontal + min(1.0, min(w, h) / 900)
            if marked and p in marked:
                score -= 2.0  # сайт ставит свой водяной знак — только если других фото нет
            scored.append((score, p, box))
            break  # на фото — только самое крупное лицо этого человека
    scored.sort(key=lambda x: -x[0])
    return [(p, box) for _s, p, box in scored[:top]]


def _cell_crop(img, box: Box, cw: int, ch: int, *, color: bool):
    """Кадр под ячейку: лицо по центру, на ~40% высоты, вокруг — голова и плечи."""
    from PIL import ImageOps

    W, H = img.size
    x, y, fw, fh = box
    crop_h = min(H, max(fh / 0.42, fh * 1.6))
    crop_w = crop_h * cw / ch
    if crop_w > W:
        crop_w = W
        crop_h = crop_w * ch / cw
    cx, cy = x + fw / 2, y + fh / 2
    left = min(max(0.0, cx - crop_w / 2), W - crop_w)
    top = min(max(0.0, cy - crop_h * 0.42), H - crop_h)
    part = img.crop((int(left), int(top), int(left + crop_w), int(top + crop_h))).resize((cw, ch))
    if color:
        return part
    return ImageOps.autocontrast(ImageOps.grayscale(part), cutoff=1).convert("RGB")


def cell_width(k: int, row_h: int) -> int:
    """Ширина ячейки при k фото в ряду: на всю ширину, но не шире CELL_MAX_ASPECT высоты."""
    return min((THUMB_W - GAP * (k - 1)) // k, int(row_h * CELL_MAX_ASPECT))


def make_thumbnail(choices: list[tuple[Path, Box]], dest: Path, *, color: bool = True) -> Path:
    """Чёрное превью 16:9: портреты в два ряда (сверху и снизу поровну), полоса посередине пустая."""
    from PIL import Image

    canvas = Image.new("RGB", (THUMB_W, THUMB_H), (0, 0, 0))
    n = len(choices)
    row_h = round(THUMB_H * ROW_FRAC)
    rows = [choices[: math.ceil(n / 2)], choices[math.ceil(n / 2):]] if n > 1 else [choices]
    for r, row in enumerate(rows):
        if not row:
            continue
        k = len(row)
        cw = cell_width(k, row_h)
        x0 = (THUMB_W - (cw * k + GAP * (k - 1))) // 2  # узкий ряд — по центру
        y = 0 if r == 0 else THUMB_H - row_h
        for c, (path, box) in enumerate(row):
            try:
                with Image.open(path) as img:
                    tile = _cell_crop(img.convert("RGB"), box, cw, row_h, color=color)
            except Exception as exc:
                log(f"Превью: не смог взять {path.name} ({exc})")
                continue
            canvas.paste(tile, (x0 + c * (cw + GAP), y))
    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dest, "JPEG", quality=92)
    return dest


def find_portrait(name: str, clients: dict, http: httpx.Client, workdir: Path, *,
                  cancel: CancelToken | None = None, person=None) -> list[tuple[Path, Box]]:
    """Фото человека из интернета → до PICK лучших портретов (файлы в workdir), лучший — первым.
    person (identities.Person) — кто именно: поиск с уточнением, чтобы не попал тёзка."""
    from engine.slides.media_pipeline import _download
    from engine.slides.person_media import _search

    who = (person.who if person is not None else "") or (person.hint if person is not None else "")
    if who:
        # тёзки: сначала имя с уточнением («советская актриса»), голое имя — последним
        queries = [f'"{name}" {who}', person.query(), f'"{name}" {who} фото', f'"{name}"']
    else:
        queries = [f'"{name}"', f'"{name}" фото', f'"{name}" portrait']
    cands = _search(clients, "google", queries, video=False, cancel=cancel)
    cands += _search(clients, "wikimedia", [name], video=False, cancel=cancel)
    if not cands:
        # точная фраза ничего не дала (капча, редкое написание) — ищем без кавычек
        loose = [q for q in (person.query() if person is not None else "", f"{name} {who}".strip(), name) if q]
        cands = _search(clients, "google", list(dict.fromkeys(loose)), video=False, cancel=cancel)
    cands.sort(key=lambda c: bool((c.meta or {}).get("watermark")))  # водяной знак — в конец
    cands = cands[:CANDIDATES]
    folder = workdir / re.sub(r"\W+", "_", name)
    folder.mkdir(parents=True, exist_ok=True)

    def fetch(k: int, cand) -> Path | None:
        if cancel:
            cancel.check()
        path = folder / f"{k:02d}.jpg"
        return path if _download(http, cand.download_url, path, retries=2) else None

    paths: list[Path] = []
    marked: set[Path] = set()
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
        for cand, p in zip(cands, ex.map(lambda kc: fetch(*kc), enumerate(cands))):
            if p is not None:
                paths.append(p)
                if (cand.meta or {}).get("watermark"):
                    marked.add(p)
    log(f"Превью: {name} — скачано фото {len(paths)} из {len(cands)}")
    return ranked_portraits(paths, top=PICK, marked=marked)


PICK = 3  # портретов на человека — из них зрячая LLM выбирает без водяных знаков и надписей


def _pick_clean(judge, name: str, options: list[tuple[Path, Box]], cw: int, ch: int, tmp: Path, color: bool,
                person=None) -> tuple[Path, Box]:
    """Из лучших портретов — тот, что без водяного знака, логотипа и надписей и именно тот человек
    (не тёзка другой эпохи: смотрим, совпадает ли с описанием «кто это»)."""
    if len(options) < 2 and person is None:
        return options[0]
    if judge is None or not judge.enabled:
        return _local_pick(name, options)
    from PIL import Image

    tiles = []
    for k, (path, box) in enumerate(options):
        tile = tmp / f"tile_{re.sub(r'\W+', '_', name)}_{k}.jpg"
        with Image.open(path) as img:
            _cell_crop(img.convert("RGB"), box, cw, ch, color=color).save(tile, "JPEG", quality=90)
        tiles.append(tile)
    who = person.describe() if person is not None and person.who else name
    moment = (f"Портрет для обложки YouTube: {who}. Отклони (reject), если на фото явно другой человек или другая "
              "эпоха (например, современный молодой актёр вместо советского артиста), а также водяные знаки и надписи.")
    verdicts = judge.rate(tiles, moment=moment, query=f"{name} portrait", label="judge_thumb")
    if verdicts is None:
        return _local_pick(name, options)
    order = sorted(range(len(options)), key=lambda k: (bool(verdicts[k].reject), -verdicts[k].quality, k))
    best = order[0]
    if best:
        log(f"Превью: {name} — взял портрет №{best + 1} (у лучшего по лицу: {verdicts[0].reject or 'съёмка хуже'})")
    return options[best]


def build_thumbnail(
    names: list[str],
    *,
    keys,
    dest: Path,
    color: bool = True,
    llm: tuple[str, str, str] | None = None,
    cancel: CancelToken | None = None,
    progress: ProgressCb | None = None,
) -> tuple[Path, list[str], list[str]]:
    """Превью по списку имён → (файл, кто на превью, кого не нашёл/не поместился)."""
    from engine.slides.media_pipeline import _build_clients

    def step(pct: float, msg: str) -> None:
        log(msg)
        if progress:
            progress(pct, msg)

    if not names:
        raise ValueError("Впиши имена людей для превью")
    from engine.slides.identities import doubt_message, identify

    # кто есть кто по всей подборке: тёзок и однофамильцев другой эпохи отсеиваем по большинству
    step(0.02, "Превью: уточняю, кто эти люди…")
    cast = identify(names, llm=llm)
    if cast.doubts:
        raise ValueError(doubt_message(cast.doubts, "в списке имён в скобках"))
    people = {p.name: p for p in cast.people}
    names = [p.name for p in cast.people]
    clients = _build_clients(keys)
    http = httpx.Client(timeout=60.0, trust_env=False, follow_redirects=True)
    tmp = Path(tempfile.mkdtemp(prefix="thumb_"))
    found: dict[str, list[tuple[Path, Box]]] = {}
    missing: list[str] = []
    try:
        with ThreadPoolExecutor(max_workers=NAME_WORKERS) as ex:
            futs = {ex.submit(find_portrait, n, clients, http, tmp, cancel=cancel, person=people.get(n)): n for n in names}
            for k, fut in enumerate(as_completed(futs), 1):
                if cancel:
                    cancel.check()
                name = futs[fut]
                choice = fut.result()
                if not choice:
                    missing.append(name)
                else:
                    found[name] = choice
                step(0.05 + 0.85 * k / len(names), f"Превью: {k}/{len(names)} — {name}: {'есть фото' if choice else 'фото не нашёл'}")
        used, dropped = even_names([n for n in names if n in found])
        if dropped:
            missing.append(f"{dropped} (не поместился: нужно чётное число — поровну сверху и снизу)")
        if not used:
            raise ValueError("Не нашёл фото ни одного человека из списка")
        step(0.92, "Превью: выбираю портреты без водяных знаков и надписей…")
        picks = _choose(used, found, llm, tmp, color, cancel, people)
        step(0.97, f"Превью: собираю {len(used)} портретов ({'цветные' if color else 'чёрно-белые'})…")
        make_thumbnail(picks, dest, color=color)
        return dest, used, missing
    finally:
        http.close()
        for c in clients.values():
            try:
                c.close()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def _local_pick(name: str, options: list[tuple[Path, Box]]) -> tuple[Path, Box]:
    """Без зрячей нейросети: лицо героя уже сверено (ranked_portraits); здесь — без надписей, водяных знаков
    и коллажей (CLIP) и без второго крупного лица рядом. Порядок по лицу сохраняется среди равных."""
    if len(options) < 2:
        return options[0]
    from engine.slides import faces
    from engine.slides.clip_ranker import JUNK_FROM, junk_scores

    try:
        junk = junk_scores([p for p, _b in options])
    except Exception as exc:
        log(f"Превью: локальная проверка надписей не вышла ({exc}) — беру лучший по лицу")
        return options[0]

    def crowded(path: Path) -> bool:
        heights = [b[3] for b, _v, _f in faces.face_boxes(path)]
        return len([h for h in heights if h >= 0.5 * max(heights)]) > 1 if heights else False

    order = sorted(range(len(options)), key=lambda k: (junk.get(options[k][0], 0.0) >= JUNK_FROM,
                                                      crowded(options[k][0]), k))
    if order[0] != 0:
        log(f"Превью: {name} — взял портрет №{order[0] + 1} (локально: у лучшего по лицу надпись, "
            "водяной знак, коллаж или второе лицо)")
    return options[order[0]]


def _choose(used: list[str], found: dict[str, list[tuple[Path, Box]]], llm, tmp: Path, color: bool,
            cancel: CancelToken | None, people: dict | None = None) -> list[tuple[Path, Box]]:
    """По портрету на человека; с LLM — без водяных знаков (каждого проверяем параллельно)."""
    judge = None
    if llm and llm[0]:
        from engine.slides.judge import make_judge
        from engine.slides.theme_plan import ThemePlan

        judge = make_judge(llm[0], llm[2], llm[1], ThemePlan(summary="YouTube thumbnail: portraits of " + ", ".join(used)))
        judge.client.busy_wait = 20  # сервис лёг — не ждём, берём лучший по лицу
        if cancel:
            cancel.register(judge.client)
    n = len(used)
    k = math.ceil(n / 2) if n > 1 else 1
    ch = round(THUMB_H * ROW_FRAC)
    cw = cell_width(k, ch)
    try:
        with ThreadPoolExecutor(max_workers=min(6, max(1, n))) as ex:
            return list(ex.map(lambda name: _pick_clean(judge, name, found[name], cw, ch, tmp, color,
                                                        (people or {}).get(name)), used))
    finally:
        if judge is not None:
            judge.close()


def thumbnail_path(desktop: Path, when: datetime | None = None) -> Path:
    """Desktop/Превью ДД.ММ ЧЧ-ММ.jpg (не затирая прежние)."""
    stamp = (when or datetime.now()).strftime("%d.%m %H-%M")
    dest = desktop / f"Превью {stamp}.jpg"
    n = 2
    while dest.exists():
        dest = desktop / f"Превью {stamp} ({n}).jpg"
        n += 1
    return dest
