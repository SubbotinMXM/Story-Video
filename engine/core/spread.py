"""Расстановка без повторов подряд.

Если кадры приходится повторять (мало материала), одинаковые не должны стоять
рядом: каждый следующий выбирается из тех, что дольше всего не показывались,
поэтому повторы расходятся как можно дальше и порядок каждый раз разный.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Callable, Hashable, Sequence
from typing import TypeVar

T = TypeVar("T")


def spread(
    items: Sequence[T],
    key: Callable[[T], Hashable] = lambda x: x,
    rng: random.Random | None = None,
    *,
    avoid_first: Hashable | None = None,
) -> list[T]:
    """Переставить items так, чтобы одинаковые (по key) не шли подряд.

    Повторы — максимально далеко друг от друга. Если без соседства никак
    (один кадр занимает больше половины), соседство остаётся только там, где неизбежно.
    avoid_first — ключ кадра, который стоит прямо перед этим списком.
    """
    rng = rng or random.Random()
    pools: dict[Hashable, list[T]] = {}
    for item in items:
        pools.setdefault(key(item), []).append(item)
    for pool in pools.values():
        rng.shuffle(pool)
    n = len(items)
    if not pools:
        return []
    top_key, top_n = max(((k, len(v)) for k, v in pools.items()), key=lambda kv: kv[1])
    if top_n > (n + 1) // 2:
        return _dominant(pools, top_key, key, rng)

    left = Counter({k: len(v) for k, v in pools.items()})
    distinct = len(pools)
    # повтор не раньше, чем через половину всех разных кадров — и порядок каждый раз новый
    min_gap = max(1, distinct // 2)
    last_seen: dict[Hashable, int] = {}
    if avoid_first is not None:
        last_seen[avoid_first] = -1
    out: list[T] = []
    for step in range(n):
        keys = [k for k, c in left.items() if c > 0]
        prev = key(out[-1]) if out else avoid_first
        allowed = [k for k in keys if k != prev] or keys
        # кадров, которых осталось больше всех, нельзя откладывать — иначе в конце повторы
        most = max(left[k] for k in allowed)
        urgent = [k for k in allowed if left[k] == most and left[k] * 2 > n - step]
        pool_keys = urgent or allowed
        rested = [k for k in pool_keys if step - last_seen.get(k, -(10**9)) > min_gap]
        if rested:
            choice = rng.choice(rested)
        else:  # все показывались недавно — тот, что дольше всех ждёт
            choice = max(pool_keys, key=lambda k: (step - last_seen.get(k, -(10**9)), rng.random()))
        out.append(pools[choice].pop())
        left[choice] -= 1
        last_seen[choice] = step
    return out


def _dominant(pools: dict, top_key: Hashable, key, rng: random.Random) -> list:
    """Один кадр больше половины: делим его на равные серии между остальными (aabaa, а не abaaa)."""
    top = pools[top_key]
    others = spread([x for k, v in pools.items() if k != top_key for x in v], key, rng)
    groups = len(others) + 1
    base, extra = divmod(len(top), groups)
    sizes = [base + (1 if i < extra else 0) for i in range(groups)]
    rng.shuffle(sizes)
    out: list = []
    for i, size in enumerate(sizes):
        out.extend(top[:size])
        top = top[size:]
        if i < len(others):
            out.append(others[i])
    return out


def max_run(items: Sequence[T], key: Callable[[T], Hashable] = lambda x: x) -> int:
    """Самая длинная серия одинаковых подряд (для проверок)."""
    best = cur = 0
    prev: object = object()
    for item in items:
        k = key(item)
        cur = cur + 1 if k == prev else 1
        prev = k
        best = max(best, cur)
    return best
