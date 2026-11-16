"""Анимация подписки: 3 раза за ролик, внизу кадра, без блюра.

Когда: ~20–30 с от начала, в середине ролика и примерно за 30 с до конца.
Анимация (хромакей) обрезается по самой плашке — иначе её прозрачные поля
поднимали бы кнопку к центру кадра.
"""

from __future__ import annotations

import random

FIRST_MIN, FIRST_MAX = 20.0, 30.0
BEFORE_END = 30.0
# между показами — хотя бы столько секунд пустого кадра
MIN_GAP = 10.0


def subscribe_starts(total: float, sub_dur: float, rng: random.Random | None = None) -> list[float]:
    """Моменты начала показов (секунды от начала готового ролика). Короткий ролик — меньше показов."""
    rng = rng or random.Random()
    last = total - sub_dur - 0.2
    if last <= 0.3:
        return [0.3] if total > 1.0 else []
    wanted = [rng.uniform(FIRST_MIN, FIRST_MAX), total / 2 - sub_dur / 2, total - BEFORE_END]
    out: list[float] = []
    for t in wanted:
        t = min(max(0.3, t), last)
        if out and t < out[-1] + sub_dur + MIN_GAP:
            continue
        out.append(t)
    return out


def subscribe_graph(
    sub_idx: int,
    sub_audio: bool,
    *,
    starts: list[float],
    sub_dur: float,
    crop: tuple[int, int, int, int] | None,
    width: int,
    margin: int,
    fps: int,
    chroma: str,
    similarity: float,
    blend: float,
    volume: float,
    v_in: str = "cat_v",
    a_in: str = "cat_a",
) -> list[str]:
    """Плашка подписки поверх [v_in]/[a_in] → [vout]/[aout] в моменты starts (без блюра фона)."""
    n = len(starts)
    if n == 0:
        return [f"[{v_in}]null[vout]", f"[{a_in}]anull[aout]"]
    crop_f = ""
    if crop is not None:
        cx, cy, cw, ch = crop
        crop_f = f"crop={cw}:{ch}:{cx}:{cy},"
    outs = "".join(f"[sv{k}]" for k in range(n))
    filters = [
        f"[{sub_idx}:v]fps={fps},chromakey={chroma}:{similarity}:{blend},format=yuva420p,"
        + crop_f
        + f"scale={width}:-2:flags=lanczos,split={n}{outs}"
    ]
    prev = v_in
    for k, start in enumerate(starts):
        end = start + sub_dur
        out = "vout" if k == n - 1 else f"vs{k}"
        filters.append(f"[sv{k}]setpts=PTS+{start:.3f}/TB[sp{k}]")
        filters.append(
            f"[{prev}][sp{k}]overlay=(W-w)/2:H-h-{margin}:format=auto:"
            f"eof_action=pass:enable='between(t\\,{start:.3f}\\,{end:.3f})'[{out}]"
        )
        prev = out
    if not sub_audio:
        filters.append(f"[{a_in}]anull[aout]")
        return filters
    aouts = "".join(f"[sa{k}]" for k in range(n))
    filters.append(f"[{sub_idx}:a]volume={volume},aformat=sample_fmts=fltp:channel_layouts=stereo,asplit={n}{aouts}")
    mix = f"[{a_in}]aformat=sample_fmts=fltp:channel_layouts=stereo[sbase]"
    filters.append(mix)
    for k, start in enumerate(starts):
        ms = int(start * 1000)
        filters.append(f"[sa{k}]adelay={ms}|{ms}[sd{k}]")
    filters.append(
        "[sbase]" + "".join(f"[sd{k}]" for k in range(n))
        + f"amix=inputs={n + 1}:duration=first:dropout_transition=0:normalize=0[aout]"
    )
    return filters
