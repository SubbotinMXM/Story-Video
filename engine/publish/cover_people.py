"""Известные люди на обложке-образце: найти ДРУГОЕ настоящее фото того же человека.

Обложка про знаменитостей (10 звёзд и надпись) должна остаться про тех же людей — меняются только снимки.
Генератор сам не знает, как выглядит, например, Алла Пугачёва, поэтому получает её настоящее фото:
- поиск картинок по имени (Google Images, без ключа — как у слайдов);
- лицо героя — то, что встречается на большинстве найденных картинок (чужие люди отсекаются);
- снимок не тот же, что на образце: лицо почти совпадает с лицом на образце — это тот же кадр, берём другой;
- крупное фото одного человека лучше группового.
Не нашлось — None: генератор держится лица с образца (человек тот же, но снимок может выйти похожим).
"""

from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

import httpx

from engine.core.cancel import CancelToken
from engine.core.logutil import log

MAX_PEOPLE = 10  # больше — генератор путает людей (и не все модели берут столько картинок)
PER_PERSON = 14  # сколько картинок скачать на человека — чтобы опознать его лицо по нескольким
MIN_SIDE = 400
PHOTO_SIDE = 900  # фото человека для генератора — не больше (data URI небольшой)
SAME_PHOTO = 0.86  # лицо совпадает с лицом на образце сильнее — это тот же снимок, не берём


def slug(name: str) -> str:
    s = re.sub(r"[^\w\- ]+", "", name, flags=re.U).strip().replace(" ", "_")
    return (s or hashlib.sha1(name.encode()).hexdigest()[:8])[:60]


def _queries(name: str) -> list[str]:
    return [f"{name} фото", name]


def _download(url: str, dest: Path) -> bool:
    try:
        with httpx.Client(timeout=20, follow_redirects=True, trust_env=False,
                          headers={"User-Agent": "Mozilla/5.0"}) as http:
            r = http.get(url)
        if r.status_code != 200 or len(r.content) < 8000 or len(r.content) > 15_000_000:
            return False
        from PIL import Image

        with Image.open(io.BytesIO(r.content)) as im:
            if min(im.size) < MIN_SIDE:
                return False
            im.convert("RGB").save(dest, "JPEG", quality=92)
        return True
    except Exception:
        return False


def _shrink(src: Path, dest: Path) -> Path:
    from PIL import Image

    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((PHOTO_SIDE, PHOTO_SIDE))
        im.save(dest, "JPEG", quality=90)
    return dest


def pick_photo(paths: list[Path], reference_faces: list) -> Path | None:
    """Из скачанных картинок — лучшее фото героя: его лицо главное, это не тот же снимок, что на образце."""
    from engine.slides import faces

    per = [faces.face_embeddings(p) for p in paths]
    hero = faces.find_hero(per)
    if hero is None:
        return None
    best, best_key = None, None
    for p, vecs in zip(paths, per):
        if not vecs or not faces.is_hero_shot(vecs, hero):
            continue
        main = max(vecs, key=lambda v: float(v @ hero))
        if any(float(main @ r) >= SAME_PHOTO for r in reference_faces):
            continue  # тот же кадр, что на образце
        try:
            from PIL import Image

            with Image.open(p) as im:
                area = im.size[0] * im.size[1]
        except Exception:
            continue
        key = (len(vecs) == 1, float(main @ hero), area)  # один человек в кадре, узнан уверенно, крупнее
        if best_key is None or key > best_key:
            best, best_key = p, key
    return best


def find_photos(names: list[str], reference: bytes | None, work: Path, *,
                cancel: CancelToken | None = None, search=None) -> dict[str, Path | None]:
    """Имя → путь к другому настоящему фото этого человека (или None). Уже найденные — из папки работы.
    search(query, n) → список URL картинок (по умолчанию — Google Images)."""
    from engine.slides import faces

    folder = work / "люди"
    folder.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path | None] = {}
    todo = []
    for name in names[:MAX_PEOPLE]:
        done = folder / f"{slug(name)}.jpg"
        if done.is_file():
            out[name] = done
        else:
            todo.append(name)
    if not todo:
        return out
    ref_faces: list = []
    if reference:
        tmp = folder / "_образец.jpg"
        tmp.write_bytes(reference)
        try:
            ref_faces = faces.face_embeddings(tmp)
        except Exception as exc:
            log(f"Обложка: лица на образце не прочитались ({exc})")
    client = None
    if search is None:
        from engine.slides.source_google import GoogleImagesClient

        client = GoogleImagesClient(tabs=2)

        def search(query: str, n: int) -> list[str]:
            return [c.download_url for c in client.search(query, per_page=n) if c.download_url]
    try:
        for name in todo:
            if cancel:
                cancel.check()
            urls: list[str] = []
            for q in _queries(name):
                urls += [u for u in search(q, PER_PERSON) if u not in urls]
                if len(urls) >= PER_PERSON:
                    break
            raw = folder / f"_{slug(name)}"
            raw.mkdir(exist_ok=True)
            got = []
            for k, url in enumerate(urls[:PER_PERSON * 2]):
                if cancel:
                    cancel.check()
                dest = raw / f"{k:02d}.jpg"
                if dest.is_file() or _download(url, dest):
                    got.append(dest)
                if len(got) >= PER_PERSON:
                    break
            photo = pick_photo(got, ref_faces) if got else None
            if photo is None:
                log(f"Обложка: другое фото «{name}» не нашлось (скачано {len(got)}) — генератор держится образца")
                out[name] = None
                continue
            out[name] = _shrink(photo, folder / f"{slug(name)}.jpg")
            log(f"Обложка: фото «{name}» — {photo.name} из {len(got)}")
    finally:
        if client is not None:
            client.close()
    return out
