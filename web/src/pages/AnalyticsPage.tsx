import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowUp, BarChart3, Check, Clock, Globe2, Pencil, Plus, RefreshCw, Tags, Trash2, X } from "lucide-react";
import { Badge, Button, Card, Empty, PageHeader, Progress, Select, Toggle } from "../components/ui";
import { api, type AnalyticsAuto, type AnalyticsGroup, type AnalyticsRow, type PublishProfile } from "../lib/api";
import { useJobs, useLoadedSettings, useProfiles, useToasts } from "../lib/store";
import { inAnyGroup, inFilter, NO_GROUP, toggleFilter } from "../lib/groups";
import { CompetitionPanel } from "../components/CompetitionPanel";

type SortKey = "name" | "subscribers" | "watch_hours" | "views_48h" | "public" | "restricted" | "scheduled" | "synced_at";
const SORTS: { key: SortKey; label: string }[] = [
  { key: "views_48h", label: "Просмотры, 48 ч" },
  { key: "subscribers", label: "Подписчики" },
  { key: "watch_hours", label: "Часы просмотра" },
  { key: "public", label: "Вышло в открытый доступ" },
  { key: "restricted", label: "Ограничено" },
  { key: "scheduled", label: "Запланировано" },
  { key: "name", label: "Название" },
  { key: "synced_at", label: "Обновлено" },
];
/** Выбранные фильтры хранятся одной строкой «id1,id2» (в настройках — обычная строка). */
const splitSel = (v: string | undefined) => (v ?? "").split(",").map((x) => x.trim()).filter(Boolean);
const joinSel = (v: string[]) => v.join(",");
const newGroupId = () => `g-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}`;

/** Строка «подпись — число» в столбце «Ролики». */
function VidStat({ label, value, title, strong }: { label: string; value: number | null | undefined; title?: string; strong?: boolean }) {
  return (
    <div className="an-vid-line" title={title}>
      <span className="an-vid-label">{label}</span>
      <span className={`an-vid-num ${strong ? "strong" : ""}`}>{fmtInt(value)}</span>
    </div>
  );
}

/** Ролики с ограниченным просмотром: «по ссылке» и «закрытые» вместе (null — ещё не читали). */
const restrictedOf = (r: Pick<AnalyticsRow, "unlisted" | "private">) =>
  r.unlisted == null && r.private == null ? null : (r.unlisted ?? 0) + (r.private ?? 0);

const fmtInt = (n: number | null | undefined) => (n == null ? "—" : Math.round(n).toLocaleString("ru-RU"));
const fmtHours = (n: number | null | undefined) =>
  n == null ? "—" : n.toLocaleString("ru-RU", { maximumFractionDigits: n < 100 ? 1 : 0 });

/** С какого дня изменение: вчера — «со вчера», раньше — с той даты (вчера не синхронизировали). */
function since(prevT: number | undefined, nowT: number) {
  if (!prevT) return "";
  const day = (ts: number) => new Date(ts * 1000).toLocaleDateString("ru-RU", { timeZone: "Europe/Moscow" });
  const yesterday = day(nowT - 86400);
  return day(prevT) === yesterday
    ? "Изменение со вчера"
    : `Изменение с ${new Date(prevT * 1000).toLocaleDateString("ru-RU", { timeZone: "Europe/Moscow", day: "numeric", month: "short" })} (вчера не синхронизировали)`;
}

/** Динамика относительно вчерашнего дня: «+5», «−120»; без изменений — «0» серым, чтобы было видно, что сравнили. */
function delta(now: number | null | undefined, prev: number | null | undefined, hours = false, title = "") {
  if (now == null || prev == null) return null;
  const d = now - prev;
  if (Math.abs(d) < (hours ? 0.05 : 0.5)) return <span className="an-delta" title={title}>0</span>;
  const text = (d > 0 ? "+" : "−") + (hours ? fmtHours(Math.abs(d)) : fmtInt(Math.abs(d)));
  return <span className={`an-delta ${d > 0 ? "up" : "down"}`} title={title}>{text}</span>;
}

function when(ts: number) {
  if (!ts) return "ещё не было";
  const d = new Date(ts * 1000);
  return d.toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

/** Аналитика каналов из профилей AdsPower: подписчики, часы просмотра за всё время, просмотры за 48 часов.
 *  «Синхронизировать» — задача в очереди выкладки по выбранным группам: профили открываются скрыто, цифры читаются из Studio. */
const TAB_KEY = "analytics-tab";
type Tab = "channels" | "competition";

function savedTab(): Tab {
  try {
    return localStorage.getItem(TAB_KEY) === "competition" ? "competition" : "channels";
  } catch {
    return "channels";
  }
}

export function AnalyticsPage({ active }: { active: boolean }) {
  const [tab, setTabState] = useState<Tab>(savedTab);
  const setTab = (t: Tab) => {
    setTabState(t);
    try {
      localStorage.setItem(TAB_KEY, t);
    } catch {
      /* не запомним — не страшно */
    }
  };
  const { enqueue, jobs } = useJobs();
  const [rows, setRows] = useState<AnalyticsRow[] | null>(null);
  const [auto, setAuto] = useState<AnalyticsAuto | null>(null);
  const [error, setError] = useState("");
  const job = [...jobs].reverse().find((j) => j.kind === "analytics");
  const running = job && (job.status === "running" || job.status === "queued");
  const lastStatus = useRef(job?.status);

  const load = useCallback(() => {
    api.analytics().then((r) => {
      setRows(r.channels);
      setAuto(r.auto ?? null);
      setError("");
    }).catch((e) => setError((e as Error).message));
  }, []);

  useEffect(() => {
    if (active) load();
  }, [active, load]);
  // во время синхронизации цифры появляются по каналу — подтягиваем
  useEffect(() => {
    if (!active) return;
    if (running || lastStatus.current !== job?.status) load();
    lastStatus.current = job?.status;
  }, [active, running, job?.status, job?.progress, load]);

  const { push } = useToasts();
  const { profiles: registry, setProfiles } = useProfiles();
  const [adsBusy, setAdsBusy] = useState(false);
  // «Синхронизация с AdsPower»: только свежий список профилей (названия, новые, удалённые). Профили не открываются:
  // каналы ищутся «Найти все каналы в профилях» или кнопкой у нужного профиля
  const syncAds = async () => {
    setAdsBusy(true);
    try {
      const { profiles } = await api.publishSync();
      setProfiles(profiles); // список профилей — сразу во всех разделах
      const live = profiles.filter((p) => !p.missing);
      const gone = profiles.length - live.length;
      const fresh = live.filter((p) => !p.scanned_at).length;
      push({
        kind: "ok",
        title: `AdsPower: профилей ${live.length}`,
        text: [
          fresh ? `каналы ещё не искали в ${fresh} — найди их в нужных профилях (список ниже)` : "",
          gone ? `нет в AdsPower: ${gone}` : "",
        ].filter(Boolean).join("; ") || "Список профилей обновлён",
      });
      load();
    } catch (e) {
      push({ kind: "error", title: "AdsPower не ответил", text: (e as Error).message });
    } finally {
      setAdsBusy(false);
    }
  };
  // профили, по которым прямо сейчас ищутся каналы (в очереди или идут)
  const scanning = useMemo(() => {
    const ids = new Set<string>();
    jobs.forEach((j) => {
      if (j.kind === "analytics" && (j.status === "running" || j.status === "queued")) {
        ((j.params?.profiles as string[] | undefined) ?? []).forEach((id) => ids.add(id));
      }
    });
    return ids;
  }, [jobs]);
  const { settings, update } = useLoadedSettings();
  const groups: AnalyticsGroup[] = settings.analytics_groups ?? [];
  const view = { group: "", profile: "", sort: "views_48h", dir: "desc", ...(settings.analytics_view ?? {}) };
  const setView = (patch: Partial<typeof view>) => update({ analytics_view: { ...view, ...patch } });
  const saveGroups = (next: AnalyticsGroup[]) => update({ analytics_groups: next });
  const groupsOf = (id: string) => groups.filter((g) => g.channels.includes(id));
  const all = rows ?? [];
  // фильтры сужают: несколько групп — каналы, которые есть во всех выбранных; профили — любой из выбранных
  // (канал в одном профиле); группы и профили вместе — «и»
  const groupSel = splitSel(view.group).filter((g) => g === NO_GROUP || groups.some((x) => x.id === g));
  const profileList = useMemo(() => {
    const seen = new Map<string, string>();
    all.forEach((r) => r.profile_id && !seen.has(r.profile_id) && seen.set(r.profile_id, r.profile || r.profile_id));
    return [...seen.entries()].map(([id, name]) => ({ id, name })).sort((a, b) => a.name.localeCompare(b.name, "ru", { numeric: true }));
  }, [all]);
  const profileSel = splitSel(view.profile).filter((id) => profileList.some((p) => p.id === id));
  const list = useMemo(() => {
    const picked = all.filter(
      (r) => (profileSel.length === 0 || profileSel.includes(r.profile_id)) && inFilter(groupSel, r.channel_id, groups),
    );
    const key = view.sort as SortKey;
    const sign = view.dir === "asc" ? 1 : -1;
    return picked.slice().sort((a, b) => {
      if (key === "name") return sign * a.name.localeCompare(b.name, "ru");
      const x = (key === "restricted" ? restrictedOf(a) : a[key]) as number | null | undefined;
      const y = (key === "restricted" ? restrictedOf(b) : b[key]) as number | null | undefined;
      if (x == null && y == null) return 0;
      if (x == null) return 1; // нет цифр — всегда внизу
      if (y == null) return -1;
      return sign * (x - y);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [all, view.group, view.profile, groups, view.sort, view.dir]);
  const filtered = groupSel.length > 0 || profileSel.length > 0;

  // что синхронизировать: каналы выбранных групп (можно несколько; канал в нескольких группах — один раз).
  // Список профилей из AdsPower обновляется целиком, цифры читаются только у этих каналов — как у автообновления.
  const [syncPick, setSyncPickRaw] = useState<string[]>(() => {
    try {
      const v = JSON.parse(localStorage.getItem("analytics-sync-groups") || "null");
      return Array.isArray(v) ? v.map(String) : [];
    } catch {
      return [];
    }
  });
  const setSyncPick = (v: string[]) => {
    setSyncPickRaw(v);
    try {
      localStorage.setItem("analytics-sync-groups", JSON.stringify(v));
    } catch {
      /* storage недоступен */
    }
  };
  // удалённые группы выкидываем; ничего не выбрано — подсказкой берём группу автообновления
  const autoGroup = settings.analytics_auto?.group;
  const syncSel = syncPick.filter((g) => g === NO_GROUP || groups.some((x) => x.id === g));
  const syncEffective = syncSel.length === 0 && autoGroup && groups.some((x) => x.id === autoGroup) ? [autoGroup] : syncSel;
  const syncIds = useMemo(() => {
    const ids = new Set<string>();
    for (const r of all) if (inAnyGroup(syncEffective, r.channel_id, groups)) ids.add(r.channel_id);
    return [...ids];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [all, groups, syncEffective.join(",")]);
  const sync = () => void enqueue([{ kind: "analytics", params: { groups: syncEffective } }]);
  const syncDisabled = syncEffective.length === 0 || syncIds.length === 0;
  const sortBy = (key: SortKey) =>
    setView(view.sort === key ? { dir: view.dir === "asc" ? "desc" : "asc" } : { sort: key, dir: key === "name" ? "asc" : "desc" });
  const arrow = (key: SortKey) =>
    view.sort === key ? (view.dir === "asc" ? <ArrowUp size={12} /> : <ArrowDown size={12} />) : null;
  const toggleMember = (gid: string, channel: string) =>
    saveGroups(groups.map((g) => g.id !== gid ? g : {
      ...g,
      channels: g.channels.includes(channel) ? g.channels.filter((c) => c !== channel) : [...g.channels, channel],
    }));
  const sum = (k: "subscribers" | "watch_hours" | "views_48h" | "scheduled" | "public") =>
    list.some((r) => r[k] != null) ? list.reduce((a, r) => a + (r[k] ?? 0), 0) : null;
  const sumRestricted = () =>
    list.some((r) => restrictedOf(r) != null) ? list.reduce((a, r) => a + (restrictedOf(r) ?? 0), 0) : null;
  // «Всего» вчера: у канала без вчерашних цифр берём сегодняшние — его рост не попадёт в общую динамику ложно
  const sumPrev = (k: "subscribers" | "watch_hours" | "views_48h") =>
    list.some((r) => r.prev?.[k] != null) ? list.reduce((a, r) => a + (r.prev?.[k] ?? r[k] ?? 0), 0) : null;

  return (
    <div className="page">
      <PageHeader
        title="Аналитика"
        subtitle={
          tab === "competition"
            ? "На каком языке выгоднее делать поднишу канала-образца: где маленькие каналы на этой теме залетают, а серьёзных конкурентов мало."
            : "Подписчики, часы просмотра, просмотры за 48 часов и сколько роликов ждёт выхода — по каналам из профилей AdsPower. Каналы собираются в группы, синхронизация и автообновление работают по группам."
        }
      />
      <div className="tabs niche-views" role="tablist">
        <button type="button" role="tab" aria-selected={tab === "channels"} className={tab === "channels" ? "active" : ""} onClick={() => setTab("channels")}>
          <BarChart3 size={15} /> Мои каналы
        </button>
        <button type="button" role="tab" aria-selected={tab === "competition"} className={tab === "competition" ? "active" : ""} onClick={() => setTab("competition")}>
          <Globe2 size={15} /> Конкуренция по языкам
        </button>
      </div>
      {tab === "competition" && <CompetitionPanel active={active} />}
      <div hidden={tab !== "channels"} className="an-tab">
      <ManualSync
        groups={groups}
        rows={all}
        selected={syncEffective}
        onChange={setSyncPick}
        count={syncIds.length}
        running={Boolean(running)}
        busy={adsBusy}
        disabled={syncDisabled}
        onRun={sync}
        onRunAll={() => void enqueue([{ kind: "analytics", params: {} }])}
        onAds={() => void syncAds()}
        profiles={registry}
        scanning={scanning}
        onScanProfile={(id) => void enqueue([{ kind: "analytics", params: { profiles: [id] } }])}
      />
      {running && job && (
        <Card>
          <div className="stack">
            <div className="muted small">{job.status === "queued" ? "Ждёт очереди выкладки (профили открываются по одному)…" : job.message}</div>
            <Progress value={job.progress} />
          </div>
        </Card>
      )}
      {error && <div className="badge badge-bad">{error}</div>}
      <AutoSync
        setting={{ enabled: false, group: "", time: "07:00", ...(settings.analytics_auto ?? {}) }}
        groups={groups}
        status={auto}
        onChange={(next) => {
          update({ analytics_auto: next });
          window.setTimeout(load, 600); // ближайший запуск пересчитывает сервер
        }}
      />
      {all.length > 0 && (
        <GroupBar
          groups={groups}
          rows={all}
          filter={groupSel}
          onFilter={(sel) => setView({ group: joinSel(sel) })}
          profiles={profileList}
          profile={profileSel}
          onProfile={(sel) => setView({ profile: joinSel(sel) })}
          onSave={saveGroups}
          sort={view.sort}
          dir={view.dir}
          onSort={(sort) => setView({ sort, dir: sort === "name" ? "asc" : "desc" })}
          onDir={() => setView({ dir: view.dir === "asc" ? "desc" : "asc" })}
        />
      )}
      {rows && !all.length ? (
        <Empty
          icon={<BarChart3 size={28} />}
          title="Каналов пока нет"
          text="Нажми «Найти каналы во всех профилях»: программа подтянет профили из AdsPower и найдёт в них каналы. Потом собери каналы в группы — дальше синхронизация и автообновление идут по группам."
        />
      ) : (
        <Card>
          <div className="an-table-wrap">
            <table className="an-table">
              <thead>
                <tr>
                  <th className="an-sort" onClick={() => sortBy("name")}>Канал {arrow("name")}</th>
                  <th>Группы</th>
                  <th className="num an-sort" onClick={() => sortBy("subscribers")}>Подписчики {arrow("subscribers")}</th>
                  <th className="num an-sort" onClick={() => sortBy("watch_hours")} title="За всё время существования канала (YouTube Analytics, задержка до 1–2 суток)">Часы просмотра {arrow("watch_hours")}</th>
                  <th className="num an-sort" onClick={() => sortBy("views_48h")} title="Просмотры за последние 48 часов (в реальном времени)">Просмотры, 48 ч {arrow("views_48h")}</th>
                  <th className="an-vids-head" title="Читается из «Контента» при синхронизации. Сортировка — в списке «Сортировка» выше или по клику на подпись">
                    <span className="an-sort" onClick={() => sortBy("public")}>Вышло {arrow("public")}</span>
                    <span className="an-sort" onClick={() => sortBy("restricted")}>Огранич. {arrow("restricted")}</span>
                    <span className="an-sort" onClick={() => sortBy("scheduled")}>Запланир. {arrow("scheduled")}</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {list.map((r) => (
                  <tr key={r.channel_id}>
                    <td>
                      <div className="an-name">{r.name}</div>
                      <div className="muted small">
                        {r.handle} · {r.profile}
                      </div>
                      <div className="muted small an-upd" title="Когда цифры канала обновлялись в последний раз">
                        обновлено {when(r.synced_at)}
                        {r.error && r.synced_at ? <> <Badge tone="warn">устарело</Badge></> : null}
                      </div>
                      {r.error && <div className="an-error small">{r.error}</div>}
                    </td>
                    <td>
                      <MemberPicker
                        groups={groups}
                        member={groupsOf(r.channel_id).map((g) => g.id)}
                        onToggle={(gid) => toggleMember(gid, r.channel_id)}
                      />
                    </td>
                    <td className="num">
                      {fmtInt(r.subscribers)} {delta(r.subscribers, r.prev?.subscribers, false, since(r.prev?.t, r.synced_at))}
                    </td>
                    <td className="num">
                      {fmtHours(r.watch_hours)} {delta(r.watch_hours, r.prev?.watch_hours, true, since(r.prev?.t, r.synced_at))}
                    </td>
                    <td className="num">
                      {fmtInt(r.views_48h)} {delta(r.views_48h, r.prev?.views_48h, false, since(r.prev?.t, r.synced_at))}
                    </td>
                    <td className="an-vids">
                      <VidStat label="Вышло" value={r.public} title="Роликов сейчас в открытом доступе (Public)" strong />
                      <VidStat
                        label="Огранич."
                        value={restrictedOf(r)}
                        title={r.unlisted != null || r.private != null ? `Ограничено: по ссылке ${r.unlisted ?? 0}, ограниченный доступ ${r.private ?? 0}` : "Роликов с доступом по ссылке или ограниченным"}
                      />
                      <VidStat label="Запланир." value={r.scheduled} title="Запланировано, но ещё не вышло" />
                    </td>
                  </tr>
                ))}
              </tbody>
              {rows !== null && !list.length && (
                <tbody>
                  <tr>
                    <td colSpan={6} className="muted small">{filtered ? "Под выбранные фильтры каналов нет — сними часть фильтров или добавь каналы в группу кнопкой в столбце «Группы»." : "Каналов пока нет."}</td>
                  </tr>
                </tbody>
              )}
              {list.length > 1 && (
                <tfoot>
                  <tr>
                    <td>Всего{filtered ? ` (${list.length})` : ""}</td>
                    <td />
                    <td className="num">{fmtInt(sum("subscribers"))} {delta(sum("subscribers"), sumPrev("subscribers"))}</td>
                    <td className="num">{fmtHours(sum("watch_hours"))} {delta(sum("watch_hours"), sumPrev("watch_hours"), true)}</td>
                    <td className="num">{fmtInt(sum("views_48h"))} {delta(sum("views_48h"), sumPrev("views_48h"))}</td>
                    <td className="an-vids">
                      <VidStat label="Вышло" value={sum("public")} strong />
                      <VidStat label="Огранич." value={sumRestricted()} />
                      <VidStat label="Запланир." value={sum("scheduled")} />
                    </td>
                  </tr>
                </tfoot>
              )}
            </table>
          </div>
          <div className="muted small" style={{ marginTop: 10 }}>
            Рядом с цифрой — изменение со вчерашнего дня (наведи — видно, с какой даты, если вчера не синхронизировали). Часы просмотра — за всё время (YouTube отдаёт их с
            задержкой до 1–2 суток); подписчики и просмотры за 48 часов — в реальном времени.
          </div>
        </Card>
      )}
      </div>
    </div>
  );
}


/** Фильтры (группы и профили — можно включить несколько сразу), создание/переименование/удаление групп и сортировка. */
function GroupBar(props: {
  groups: AnalyticsGroup[];
  rows: AnalyticsRow[];
  filter: string[];
  onFilter: (g: string[]) => void;
  profiles: { id: string; name: string }[];
  profile: string[];
  onProfile: (ids: string[]) => void;
  onSave: (g: AnalyticsGroup[]) => void;
  sort: string;
  dir: string;
  onSort: (k: SortKey) => void;
  onDir: () => void;
}) {
  const { groups, rows, filter, onFilter, profiles, profile, onProfile, onSave, sort, dir, onSort, onDir } = props;
  const [editing, setEditing] = useState<{ id: string; name: string } | null>(null);
  const ids = new Set(rows.map((r) => r.channel_id));
  const count = (g: AnalyticsGroup) => g.channels.filter((c) => ids.has(c)).length;
  const loose = rows.filter((r) => !groups.some((g) => g.channels.includes(r.channel_id))).length;
  // переименовать/удалить можно, когда выбрана ровно одна группа
  const current = filter.length === 1 ? groups.find((g) => g.id === filter[0]) : undefined;
  const toggle = (id: string) => onFilter(toggleFilter(filter, id));

  const commit = () => {
    if (!editing) return;
    const name = editing.name.trim();
    if (!name) return setEditing(null);
    const exists = groups.some((g) => g.id === editing.id);
    onSave(exists ? groups.map((g) => (g.id === editing.id ? { ...g, name } : g)) : [...groups, { id: editing.id, name, channels: [] }]);
    setEditing(null);
  };
  const remove = (g: AnalyticsGroup) => {
    if (!window.confirm(`Удалить группу «${g.name}»? Каналы останутся, пропадёт только группа.`)) return;
    onSave(groups.filter((x) => x.id !== g.id));
    onFilter(filter.filter((x) => x !== g.id));
  };

  return (
    <Card>
      <div className="stack" style={{ gap: 10 }}>
        <div className="chips" style={{ alignItems: "center" }} role="group" aria-label="Фильтр по группам">
          <button className={`chip ${filter.length === 0 ? "active" : ""}`} onClick={() => onFilter([])}>
            Все · {rows.length}
          </button>
          {groups.map((g) => (
            <button
              key={g.id}
              className={`chip ${filter.includes(g.id) ? "active" : ""}`}
              aria-pressed={filter.includes(g.id)}
              onClick={() => toggle(g.id)}
              title="Можно выбрать несколько групп — останутся каналы, которые есть во всех выбранных"
            >
              {g.name} · {count(g)}
            </button>
          ))}
          {groups.length > 0 && (
            <button
              className={`chip ${filter.includes(NO_GROUP) ? "active" : ""}`}
              aria-pressed={filter.includes(NO_GROUP)}
              onClick={() => toggle(NO_GROUP)}
            >
              Без группы · {loose}
            </button>
          )}
          {editing ? (
            <span className="row gap-sm" style={{ alignItems: "center" }}>
              <input
                className="input"
                style={{ width: 180 }}
                autoFocus
                value={editing.name}
                placeholder="Название группы"
                onChange={(e) => setEditing({ ...editing, name: e.target.value })}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commit();
                  if (e.key === "Escape") setEditing(null);
                }}
              />
              <Button size="sm" icon={<Check size={14} />} onClick={commit} title="Сохранить" />
              <Button size="sm" variant="ghost" icon={<X size={14} />} onClick={() => setEditing(null)} title="Отмена" />
            </span>
          ) : (
            <Button size="sm" variant="ghost" icon={<Plus size={14} />} onClick={() => setEditing({ id: newGroupId(), name: "" })}>
              Группа
            </Button>
          )}
          {current && !editing && (
            <>
              <Button size="sm" variant="ghost" icon={<Pencil size={14} />} onClick={() => setEditing({ id: current.id, name: current.name })} title="Переименовать группу" />
              <Button size="sm" variant="ghost" icon={<Trash2 size={14} />} onClick={() => remove(current)} title="Удалить группу" />
            </>
          )}
        </div>
        <div className="row gap-sm" style={{ alignItems: "center", flexWrap: "wrap" }}>
          {profiles.length > 1 && <ProfilePicker profiles={profiles} rows={rows} value={profile} onChange={onProfile} />}
          <span className="muted small">Сортировка:</span>
          <select className="input" style={{ width: "auto" }} value={sort} onChange={(e) => onSort(e.target.value as SortKey)}>
            {SORTS.map((s) => (
              <option key={s.key} value={s.key}>
                {s.label}
              </option>
            ))}
          </select>
          <Button size="sm" variant="ghost" icon={dir === "asc" ? <ArrowUp size={14} /> : <ArrowDown size={14} />} onClick={onDir}>
            {dir === "asc" ? "по возрастанию" : "по убыванию"}
          </Button>
          <span className="muted small">Или нажми на заголовок столбца.</span>
        </div>
      </div>
    </Card>
  );
}

/** Профили AdsPower: можно отметить несколько — покажутся каналы любого из них. Список с галочками и поиском. */
function ProfilePicker({ profiles, rows, value, onChange }: {
  profiles: { id: string; name: string }[];
  rows: AnalyticsRow[];
  value: string[];
  onChange: (ids: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);
  const count = (id: string) => rows.filter((r) => r.profile_id === id).length;
  const shown = profiles.filter((p) => p.name.toLowerCase().includes(q.trim().toLowerCase()));
  const toggle = (id: string) => onChange(value.includes(id) ? value.filter((x) => x !== id) : [...value, id]);
  const label = value.length === 0 ? `Все профили · ${profiles.length}` : value.length === 1 ? profiles.find((p) => p.id === value[0])?.name ?? "1 профиль" : `Профилей: ${value.length}`;
  return (
    <div className="an-groups" ref={ref}>
      <span className="muted small">Профиль:</span>
      <button type="button" className={`chip ${value.length ? "active" : ""}`} aria-expanded={open} onClick={() => setOpen(!open)} title="Можно отметить несколько профилей">
        {label}
      </button>
      {value.length > 0 && <Button size="sm" variant="ghost" icon={<X size={14} />} onClick={() => onChange([])} title="Показать все профили" />}
      {open && (
        <div className="an-groups-menu an-profiles-menu">
          {profiles.length > 8 && (
            <input className="input" autoFocus placeholder="Найти профиль" value={q} onChange={(e) => setQ(e.target.value)} />
          )}
          <div className="an-profiles-list">
            {shown.map((p) => (
              <label key={p.id} className="an-groups-item">
                <input type="checkbox" checked={value.includes(p.id)} onChange={() => toggle(p.id)} />
                {p.name} · {count(p.id)}
              </label>
            ))}
            {!shown.length && <div className="muted small">Не нашлось.</div>}
          </div>
        </div>
      )}
    </div>
  );
}

/** Группы канала: метки и выпадающий список с галочками. */
function MemberPicker({ groups, member, onToggle }: { groups: AnalyticsGroup[]; member: string[]; onToggle: (gid: string) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  const names = groups.filter((g) => member.includes(g.id));
  return (
    <div className="an-groups" ref={ref}>
      {names.map((g) => (
        <Badge key={g.id}>{g.name}</Badge>
      ))}
      <button className="an-groups-btn" onClick={() => setOpen(!open)} title={groups.length ? "Группы канала" : "Сначала создай группу кнопкой «+ Группа»"}>
        <Tags size={13} />
      </button>
      {open && (
        <div className="an-groups-menu">
          {groups.length ? (
            groups.map((g) => (
              <label key={g.id} className="an-groups-item">
                <input type="checkbox" checked={member.includes(g.id)} onChange={() => onToggle(g.id)} />
                {g.name}
              </label>
            ))
          ) : (
            <div className="muted small">Групп пока нет — создай кнопкой «+ Группа» над таблицей.</div>
          )}
        </div>
      )}
    </div>
  );
}

/** Ручная синхронизация: выбираешь группы (одну или несколько) — читаются цифры только у их каналов.
 *  Канал может быть в нескольких группах — синхронизируется один раз. Профили из AdsPower обновляются все. */
function ManualSync(props: {
  groups: AnalyticsGroup[];
  rows: AnalyticsRow[];
  selected: string[];
  onChange: (v: string[]) => void;
  count: number;
  running: boolean;
  busy: boolean;
  disabled: boolean;
  onRun: () => void;
  onRunAll: () => void;
  onAds: () => void;
  profiles: PublishProfile[];
  /** id профилей, по которым сейчас ищутся каналы. */
  scanning: Set<string>;
  onScanProfile: (id: string) => void;
}) {
  const { groups, rows, selected, onChange, count, running, busy, disabled, onRun, onRunAll, onAds, profiles, scanning, onScanProfile } = props;
  const ids = new Set(rows.map((r) => r.channel_id));
  const inGroup = (g: AnalyticsGroup) => g.channels.filter((c) => ids.has(c)).length;
  const loose = rows.filter((r) => !groups.some((g) => g.channels.includes(r.channel_id))).length;
  const toggle = (id: string) => onChange(selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id]);
  const allIds = [...groups.map((g) => g.id)];
  const live = profiles.filter((p) => !p.missing);
  const noChannels = live.filter((p) => p.channels.length === 0).length;
  const [q, setQ] = useState("");
  const [onlyNew, setOnlyNew] = useState(false);
  const shown = live.filter((p) => {
    if (onlyNew && (p.channels.length > 0 || p.scanned_at)) return false;
    const hay = `${p.number} ${p.name} ${p.group} ${p.channels.map((c) => `${c.name} ${c.handle}`).join(" ")}`.toLowerCase();
    return !q.trim() || hay.includes(q.trim().toLowerCase());
  });
  const busyAll = running || busy;
  const runningWide = running && scanning.size === 0; // идёт не поиск в отдельных профилях
  return (
    <Card>
      <div className="stack" style={{ gap: 14 }}>
        <div className="an-sync-title">Синхронизация</div>
        <div className="an-sync-actions">
          <div className="an-sync-action">
            <Button icon={<RefreshCw size={16} />} onClick={onAds} loading={busy} disabled={busyAll}>
              Синхронизация с AdsPower
            </Button>
            <div className="muted small">Подтягивает и обновляет список всех профилей (новые, переименованные, удалённые). Профили не открывает.</div>
          </div>
          <div className="an-sync-action">
            <Button variant="primary" icon={<RefreshCw size={16} />} onClick={onRunAll} loading={runningWide} disabled={busyAll}>
              {runningWide ? "Ищу каналы…" : "Найти все каналы в профилях"}
            </Button>
            <div className="muted small">Сначала обновляет список из AdsPower, потом открывает все профили скрыто, по одному: находит каналы и снимает цифры. Долго.</div>
          </div>
        </div>

        <div className="an-profs">
          <div className="an-profs-head">
            <div>
              <div className="an-sync-title">Профили AdsPower · {live.length}</div>
              <div className="muted small">
                Нужны не все? Найди каналы только у нужных профилей — они сразу появятся в таблице и во всём приложении, в том числе при создании роликов.
                {noChannels > 0 && ` Без каналов: ${noChannels}.`}
              </div>
            </div>
            <div className="an-profs-tools">
              <input className="input" placeholder="Найти профиль или канал" value={q} onChange={(e) => setQ(e.target.value)} />
              <label className="an-profs-check">
                <input type="checkbox" checked={onlyNew} onChange={(e) => setOnlyNew(e.target.checked)} /> только не искали
              </label>
            </div>
          </div>
          {live.length === 0 ? (
            <div className="muted small">Профилей пока нет — нажми «Синхронизация с AdsPower».</div>
          ) : (
            <div className="an-profs-list" role="list">
              {shown.length === 0 && <div className="muted small an-profs-empty">Ничего не найдено</div>}
              {shown.map((p) => {
                const going = scanning.has(p.id);
                const n = p.channels.length;
                return (
                  <div className="an-prof" role="listitem" key={p.id}>
                    <div className="an-prof-main">
                      <div className="an-prof-name" title={p.name}>
                        <span className="muted">№{p.number}</span> {p.name || "без названия"}
                      </div>
                      <div className="muted small an-prof-sub">
                        {p.group && <span>{p.group} · </span>}
                        {n > 0
                          ? p.channels.map((c) => c.name).join(", ")
                          : p.scanned_at
                            ? "каналов в профиле нет"
                            : "каналы ещё не искали"}
                      </div>
                    </div>
                    <Button
                      size="sm"
                      variant={n === 0 && !p.scanned_at ? "primary" : "ghost"}
                      icon={<RefreshCw size={14} />}
                      onClick={() => onScanProfile(p.id)}
                      loading={going}
                      disabled={going || busyAll}
                      title="Открыть этот профиль скрыто, найти его каналы и снять цифры"
                    >
                      {going ? "Ищу…" : n > 0 ? "Обновить" : "Найти каналы"}
                    </Button>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {rows.length > 0 && groups.length > 0 && (
          <div className="stack" style={{ gap: 10 }}>
            <div>
              <div className="an-sync-title">Обновить цифры по группам</div>
              <div className="muted small">Только каналы выбранных групп (канал из нескольких групп берётся один раз). Профили без этих каналов не открываются.</div>
            </div>
            <div className="chips" role="group" aria-label="Группы для синхронизации">
              {groups.map((g) => (
                <button
                  type="button"
                  key={g.id}
                  className={`chip ${selected.includes(g.id) ? "active" : ""}`}
                  aria-pressed={selected.includes(g.id)}
                  onClick={() => toggle(g.id)}
                  title="Можно выбрать несколько групп"
                >
                  {g.name} · {inGroup(g)}
                </button>
              ))}
              <button
                type="button"
                className={`chip ${selected.includes(NO_GROUP) ? "active" : ""}`}
                aria-pressed={selected.includes(NO_GROUP)}
                onClick={() => toggle(NO_GROUP)}
                title="Каналы, которых нет ни в одной группе (например, только что найденные)"
              >
                Без группы · {loose}
              </button>
              <Button size="sm" variant="ghost" onClick={() => onChange(selected.length === allIds.length ? [] : allIds)}>
                {selected.length === allIds.length ? "Снять все" : "Все группы"}
              </Button>
            </div>
            <div className="an-sync-run">
              <Button icon={<RefreshCw size={16} />} onClick={onRun} loading={runningWide} disabled={busyAll || disabled}>
                {runningWide ? "Обновляю…" : `Обновить цифры · ${count} ${count === 1 ? "канал" : count > 1 && count < 5 ? "канала" : "каналов"}`}
              </Button>
              {!running && selected.length === 0 && <span className="muted small">Выбери хотя бы одну группу.</span>}
              {!running && selected.length > 1 && <span className="muted small">Выбрано несколько групп — берутся каналы всех этих групп.</span>}
            </div>
          </div>
        )}
        {rows.length > 0 && groups.length === 0 && (
          <div className="muted small">Групп пока нет — создай группу кнопкой «+ Группа» ниже и добавь в неё каналы (столбец «Группы»).</div>
        )}
      </div>
    </Card>
  );
}

type AutoSetting = { enabled: boolean; group: string; time: string };

const whenMsk = (ts: number) =>
  new Date(ts * 1000).toLocaleString("ru-RU", {
    timeZone: "Europe/Moscow", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  }) + " МСК";

/** Автообновление по расписанию: каждый день в выбранное время (МСК) — синхронизация каналов одной группы.
 *  Ставится в очередь выкладки как обычная «Аналитика»; Mac спал в это время — запуск, когда проснётся. */
function AutoSync({ setting, groups, status, onChange }: {
  setting: AutoSetting;
  groups: AnalyticsGroup[];
  status: AnalyticsAuto | null;
  onChange: (next: AutoSetting) => void;
}) {
  const group = groups.find((g) => g.id === setting.group);
  const set = (patch: Partial<AutoSetting>) => onChange({ ...setting, ...patch });
  const options = [
    ...(group ? [] : [{ value: "", label: groups.length ? "Выбери группу" : "Групп пока нет" }]),
    ...groups.map((g) => ({ value: g.id, label: `${g.name} (${g.channels.length})` })),
  ];
  let note = "Выключено — цифры обновляются только кнопкой.";
  if (setting.enabled && !group) note = "Выбери группу — без неё автообновление не запустится.";
  else if (setting.enabled && group && !group.channels.length) note = "В группе нет каналов — добавь их в столбце «Группы».";
  else if (setting.enabled && status?.next_at) note = `Следующее: ${whenMsk(status.next_at)}.`;
  if (status?.last_at) note += ` Прошлое: ${whenMsk(status.last_at)}.`;
  return (
    <Card>
      <div className="an-auto">
        <Toggle
          checked={setting.enabled}
          onChange={(enabled) => set({ enabled, group: setting.group || (groups.length === 1 ? groups[0].id : "") })}
          label="Автообновление каждый день"
          description="Только каналы выбранной группы. Если Mac спал в это время — обновит, как проснётся."
        />
        <div className="an-auto-fields">
          <span className="an-auto-label"><Clock size={14} /> в</span>
          <input
            className="input an-auto-time"
            type="time"
            step={300}
            value={setting.time}
            onChange={(e) => e.target.value && set({ time: e.target.value })}
          />
          <span className="an-auto-label">МСК, группа</span>
          <div className="an-auto-group">
            <Select value={group ? setting.group : ""} onChange={(g) => g && set({ group: g })} options={options} />
          </div>
        </div>
      </div>
      <div className="muted small an-auto-note">{note}</div>
    </Card>
  );
}
