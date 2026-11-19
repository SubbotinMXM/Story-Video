import { useMemo, useState } from "react";
import { Search } from "lucide-react";
import type { PublishProfile } from "../lib/api";
import { useProfiles } from "../lib/store";
import { Badge } from "./ui";
import { Subs } from "./Subs";
import { GroupFilter, inFilter, useGroupFilter } from "./GroupFilter";

export const channelKey = (profileId: string, channelId: string) =>
  `${profileId}/${channelId}`;

/** Выбор каналов, сгруппированных по профилям AdsPower: видно, в каком профиле какие каналы.
 *  Список общий на всё приложение — после любой синхронизации обновляется сам. */
export function ChannelPicker({
  value,
  onChange,
  scope = "picker",
  emptyText = "Каналов пока нет — нажми «Синхронизировать всё» в «Аналитике»: программа найдёт профили AdsPower и каналы в них.",
}: {
  value: string[];
  onChange: (keys: string[]) => void;
  /** Где используется: фильтр по группам запоминается отдельно для каждого места. */
  scope?: string;
  emptyText?: string;
}) {
  const { profiles, loaded } = useProfiles();
  const [query, setQuery] = useState("");
  const [filter, setFilter, groups] = useGroupFilter(scope);

  const live = useMemo(() => profiles.filter((p) => !p.missing), [profiles]);
  const withChannels = live.filter((p) => p.channels.length > 0);
  const empty = live.filter((p) => p.channels.length === 0);
  const allKeys = useMemo(
    () =>
      withChannels.flatMap((p) =>
        p.channels.map((c) => channelKey(p.id, c.id)),
      ),
    [withChannels],
  );
  const known = new Set(allKeys);
  const picked = value.filter((k) => known.has(k));
  const gone = value.length - picked.length;

  const q = query.trim().toLowerCase();
  const match = (p: PublishProfile, text: string) =>
    !q ||
    text.toLowerCase().includes(q) ||
    `№${p.number} ${p.name} ${p.group}`.toLowerCase().includes(q);
  const shown = withChannels
    .map((p) => ({
      p,
      chans: p.channels.filter(
        (c) =>
          inFilter(filter, c.id, groups) &&
          match(p, `${c.name} ${c.handle} ${c.account}`),
      ),
    }))
    .filter((x) => x.chans.length > 0);
  // «Все» и «Снять» работают по тому, что сейчас показано (фильтр групп и поиск)
  const shownKeys = shown.flatMap(({ p, chans }) =>
    chans.map((c) => channelKey(p.id, c.id)),
  );
  const filtered = filter.length > 0 || q !== "";
  const shownOn = shownKeys.filter((k) => value.includes(k)).length;

  const set = (keys: string[], on: boolean) => {
    const cur = new Set(value.filter((k) => known.has(k)));
    keys.forEach((k) => (on ? cur.add(k) : cur.delete(k)));
    onChange(allKeys.filter((k) => cur.has(k)));
  };

  if (!loaded) return <div className="muted small">Загружаю каналы…</div>;
  if (allKeys.length === 0)
    return <div className="muted small">{emptyText}</div>;

  return (
    <div className="cp">
      <GroupFilter
        value={filter}
        onChange={setFilter}
        groups={groups}
        channelIds={withChannels.flatMap((p) => p.channels.map((c) => c.id))}
      />
      <div className="cp-toolbar">
        {allKeys.length > 6 && (
          <label className="cp-search">
            <Search size={14} />
            <input
              className="input"
              placeholder="Профиль, канал или @адрес"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
        )}
        <span className="cp-count small">
          Выбрано <b>{picked.length}</b> из {allKeys.length}
        </span>
        <span className="cp-bulk">
          <button
            type="button"
            className="link-btn small"
            disabled={shownOn === shownKeys.length}
            onClick={() => set(shownKeys, true)}
            title={filtered ? "Отметить все показанные сейчас каналы" : undefined}
          >
            {filtered ? "Все показанные" : "Все"}
          </button>
          <button
            type="button"
            className="link-btn small"
            disabled={shownOn === 0}
            onClick={() => set(shownKeys, false)}
            title={filtered ? "Снять отметки с показанных сейчас каналов" : undefined}
          >
            {filtered ? "Снять показанные" : "Снять"}
          </button>
        </span>
      </div>
      {gone > 0 && (
        <div className="notice warn small">
          Выбрано каналов, которых больше нет в профилях: {gone}.{" "}
          <button
            type="button"
            className="link-btn small"
            onClick={() => onChange(picked)}
          >
            Убрать их
          </button>
        </div>
      )}
      {shown.length === 0 && (
        <div className="muted small">
          {query ? `Ничего не нашлось по «${query}».` : "В выбранных группах нет каналов."}
        </div>
      )}
      <div className="cp-scroll">
        <div className="cp-groups">
          {shown.map(({ p, chans }) => {
            const keys = chans.map((c) => channelKey(p.id, c.id));
            const on = keys.filter((k) => value.includes(k)).length;
            return (
              <div key={p.id} className={`cp-group ${on ? "has-picked" : ""}`}>
                <label
                  className="cp-head"
                  title="Отметить или снять все каналы профиля"
                >
                  <input
                    type="checkbox"
                    checked={on === keys.length}
                    ref={(el) => {
                      if (el) el.indeterminate = on > 0 && on < keys.length;
                    }}
                    onChange={(e) => set(keys, e.target.checked)}
                  />
                  <span className="cp-prof">
                    №{p.number} {p.name}
                  </span>
                  {p.group && <Badge>{p.group}</Badge>}
                  <span className="cp-n muted small">
                    {on
                      ? `${on} из ${keys.length}`
                      : `${keys.length} ${plural(keys.length)}`}
                  </span>
                </label>
                <div className="cp-rows">
                  {chans.map((c) => {
                    const k = channelKey(p.id, c.id);
                    return (
                      <label
                        key={k}
                        className="cp-row"
                        title={c.account ? `Аккаунт: ${c.account}` : undefined}
                      >
                        <input
                          type="checkbox"
                          checked={value.includes(k)}
                          onChange={(e) => set([k], e.target.checked)}
                        />
                        <span className="pub-name">
                          <span className="pub-title">{c.name}</span>
                          <span className="muted">{c.handle || c.account}</span>
                        </span>
                        <Subs c={c} />
                      </label>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      </div>
      {empty.length > 0 && !q && (
        <div className="muted small cp-empty">
          Без найденных каналов:{" "}
          {empty.map((p) => `№${p.number} ${p.name}`).join(", ")} — их каналы
          появятся здесь после «Синхронизировать всё» в «Аналитике» или «Найти
          каналы» в выкладке.
        </div>
      )}
    </div>
  );
}

function plural(n: number) {
  const d = n % 10;
  const h = n % 100;
  if (d === 1 && h !== 11) return "канал";
  if (d >= 2 && d <= 4 && (h < 12 || h > 14)) return "канала";
  return "каналов";
}
