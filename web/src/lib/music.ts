import type { MusicCatalog } from "./api";

/** В настройке музыки: id трека, «group:<категория>» — случайный трек категории на каждый ролик, пусто — без музыки. */
export const GROUP_PREFIX = "group:";
export const isGroup = (v: string) => v.startsWith(GROUP_PREFIX);
export const groupOf = (v: string) => v.slice(GROUP_PREFIX.length);

/** Как музыка называется для человека (карточки, сводка, «было → стало»). */
export function musicName(value: string, cat: MusicCatalog | null): string {
  if (!value) return "без музыки";
  if (isGroup(value)) {
    const gid = groupOf(value);
    const n = cat?.tracks.filter((t) => t.group === gid && !t.claimed).length;
    return `случайный из «${cat?.groups[gid] ?? gid}»${n ? ` (${n} треков)` : ""}`;
  }
  return cat?.tracks.find((t) => t.id === value)?.title ?? value;
}
