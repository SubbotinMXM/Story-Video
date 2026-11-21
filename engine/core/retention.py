"""Автоочистка старого: карточки завершённых задач, снимки ошибок выкладки и старые журналы старше KEEP_DAYS дней
удаляются сами (раз при запуске и потом раз в несколько часов). Идущие и ждущие в очереди задачи не трогаются;
готовые ролики и рабочие папки сборки — тоже."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from engine.core.logutil import log

KEEP_DAYS = 5
EVERY_SEC = 6 * 3600


def _old(path: Path, cutoff: float) -> bool:
    try:
        return path.stat().st_mtime < cutoff
    except OSError:
        return False


def sweep_files(data_dir: Path, *, days: int = KEEP_DAYS, now: float | None = None) -> int:
    """Старые снимки ошибок выкладки (publish/errors) и ротированные журналы (app.log.1). → сколько удалено."""
    cutoff = (now or time.time()) - days * 86400
    gone = 0
    errors = data_dir / "publish" / "errors"
    if errors.is_dir():
        for p in errors.iterdir():
            if _old(p, cutoff):
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
                gone += 1
    for p in data_dir.glob("app.log.*"):
        if _old(p, cutoff):
            p.unlink(missing_ok=True)
            gone += 1
    return gone


def sweep(manager, data_dir: Path, *, days: int = KEEP_DAYS) -> None:
    try:
        cards = manager.purge_old(days)
        files = sweep_files(data_dir, days=days)
        if cards or files:
            log(f"Автоочистка (старше {days} дн.): карточек задач {cards}, снимков и журналов {files}")
    except Exception as exc:  # noqa: BLE001 — очистка не должна мешать работе
        log(f"Автоочистка не вышла: {exc}")


def loop(manager, data_dir: Path) -> None:
    while True:
        sweep(manager, data_dir)
        time.sleep(EVERY_SEC)
