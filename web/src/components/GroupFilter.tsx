import { useEffect, useState } from "react";
import type { AnalyticsGroup } from "../lib/api";
import { useLoadedSettings } from "../lib/store";

import { inAnyGroup, inFilter, NO_GROUP, toggleFilter } from "../lib/groups";

/** Фильтр каналов по группам из «Аналитики». Фильтры сужают: выбрано несколько групп — остаются каналы, которые есть
 *  во всех выбранных. Пусто — без фильтра. «none» — каналы без группы (с группами не сочетается). */
export { inAnyGroup, inFilter, NO_GROUP };

/** Выбор фильтра запоминается в браузере отдельно для каждого места (scope). Удалённые группы отбрасываются. */
export function useGroupFilter(scope: string): [string[], (v: string[]) => void, AnalyticsGroup[]] {
  const { settings } = useLoadedSettings();
  const groups: AnalyticsGroup[] = settings.analytics_groups ?? [];
  const storeKey = `group-filter:${scope}`;
  const [raw, setRaw] = useState<string[]>(() => {
    try {
      const v = JSON.parse(localStorage.getItem(storeKey) || "[]");
      return Array.isArray(v) ? v.map(String) : [];
    } catch {
      return [];
    }
  });
  const set = (v: string[]) => {
    setRaw(v);
    try {
      localStorage.setItem(storeKey, JSON.stringify(v));
    } catch {
      /* storage недоступен */
    }
  };
  const known = new Set([NO_GROUP, ...groups.map((g) => g.id)]);
  const value = raw.filter((f) => known.has(f));
  // группу удалили — выкидываем её из запомненного, иначе она вернётся, если создать группу с тем же id
  useEffect(() => {
    if (groups.length && value.length !== raw.length) set(value);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groups.length, raw.length, value.length]);
  return [value, set, groups];
}

/** Чипы групп: «Все», каждая группа с числом каналов, «Без группы». Клик — включить/выключить группу. */
export function GroupFilter({
  value,
  onChange,
  groups,
  channelIds,
  right,
}: {
  value: string[];
  onChange: (v: string[]) => void;
  groups: AnalyticsGroup[];
  /** Все каналы, среди которых выбираем (для чисел на чипах). */
  channelIds: string[];
  /** Кнопки справа («Отметить показанные»). */
  right?: React.ReactNode;
}) {
  if (groups.length === 0) return null;
  const count = (g: AnalyticsGroup) => channelIds.filter((id) => g.channels.includes(id)).length;
  const loose = channelIds.filter((id) => !groups.some((g) => g.channels.includes(id))).length;
  const toggle = (id: string) => onChange(toggleFilter(value, id));
  return (
    <div className="gf">
      <div className="chips" role="group" aria-label="Фильтр по группам">
        <button type="button" className={`chip ${value.length === 0 ? "active" : ""}`} onClick={() => onChange([])}>
          Все · {channelIds.length}
        </button>
        {groups.map((g) => (
          <button
            type="button"
            key={g.id}
            className={`chip ${value.includes(g.id) ? "active" : ""}`}
            aria-pressed={value.includes(g.id)}
            onClick={() => toggle(g.id)}
            title="Можно выбрать несколько групп — останутся каналы, которые есть во всех выбранных"
          >
            {g.name} · {count(g)}
          </button>
        ))}
        <button
          type="button"
          className={`chip ${value.includes(NO_GROUP) ? "active" : ""}`}
          aria-pressed={value.includes(NO_GROUP)}
          onClick={() => toggle(NO_GROUP)}
        >
          Без группы · {loose}
        </button>
        {right}
      </div>
    </div>
  );
}
