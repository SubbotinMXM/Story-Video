/** Фильтры по группам каналов — чистая логика (без React), чтобы её можно было проверить тестом.
 *
 *  Фильтры СУЖАЮТ: выбрано несколько групп — остаются только каналы, которые есть во ВСЕХ выбранных группах
 *  («Максим» и «Рассказы» → каналы, что и там, и там). Больше фильтров — уже выборка.
 *  «none» — каналы без группы; с группами не сочетается (канал не может быть и без группы, и в группе). */
export const NO_GROUP = "none";

export type GroupLike = { id: string; channels: string[] };

/** Канал проходит фильтр: пусто — проходят все; иначе он состоит во всех выбранных группах. */
export function inFilter(filter: string[], channelId: string, groups: GroupLike[]): boolean {
  if (filter.length === 0) return true;
  const mine = groups.filter((g) => g.channels.includes(channelId));
  return filter.every((f) => (f === NO_GROUP ? mine.length === 0 : mine.some((g) => g.id === f)));
}

/** Объединение («любая из групп») — для выбора, ЧТО синхронизировать: «Максим» и «Страны» → каналы обеих групп. */
export function inAnyGroup(selected: string[], channelId: string, groups: GroupLike[]): boolean {
  if (selected.length === 0) return false;
  const mine = groups.filter((g) => g.channels.includes(channelId));
  return selected.some((f) => (f === NO_GROUP ? mine.length === 0 : mine.some((g) => g.id === f)));
}

/** Включить/выключить фильтр. «Без группы» несовместимо с группами: выбор одного снимает другое. */
export function toggleFilter(value: string[], id: string): string[] {
  if (value.includes(id)) return value.filter((x) => x !== id);
  return id === NO_GROUP ? [NO_GROUP] : [...value.filter((x) => x !== NO_GROUP), id];
}
