import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowUp, BarChart3, Check, Clock, Pencil, Plus, RefreshCw, Tags, Trash2, X } from "lucide-react";
import { Badge, Button, Card, Empty, PageHeader, Progress, Select, Toggle } from "../components/ui";
import { api, type AnalyticsAuto, type AnalyticsGroup, type AnalyticsRow } from "../lib/api";
import { useJobs, useLoadedSettings, useProfiles, useToasts } from "../lib/store";
import { inFilter, NO_GROUP } from "../components/GroupFilter";

type SortKey = "name" | "subscribers" | "watch_hours" | "views_48h" | "scheduled" | "synced_at";
const SORTS: { key: SortKey; label: string }[] = [
  { key: "views_48h", label: "Просмотры, 48 ч" },
  { key: "subscribers", label: "Подписчики" },
  { key: "watch_hours", label: "Часы просмотра" },
  { key: "scheduled", label: "Запланировано" },
  { key: "name", label: "Название" },
  { key: "synced_at", label: "Обновлено" },
];
/** Выбранные фильтры хранятся одной строкой «id1,id2» (в настройках — обычная строка). */
const splitSel = (v: string | undefined) => (v ?? "").split(",").map((x) => x.trim()).filter(Boolean);
const joinSel = (v: string[]) => v.join(",");
const newGroupId = () => `g-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}`;

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
 *  «Синхронизировать всё» — задача в очереди выкладки: профили открываются скрыто, цифры читаются из Studio. */
export function AnalyticsPage({ active }: { active: boolean }) {
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

  const sync = () => void enqueue([{ kind: "analytics", params: {} }]);
  const { push } = useToasts();
  const { setProfiles } = useProfiles();
  const [adsBusy, setAdsBusy] = useState(false);
  // «Только AdsPower»: список профилей без открытия — у новых профилей каналов ещё нет, их найдёт «Синхронизировать всё»
  const syncAds = async () => {
    setAdsBusy(true);
    try {
      const { profiles } = await api.publishSync();
      setProfiles(profiles); // список профилей — сразу во всех разделах
      const live = profiles.filter((p) => !p.missing);
      const fresh = live.filter((p) => !p.scanned_at).length;
      const gone = profiles.length - live.length;
      push({
        kind: "ok",
        title: `AdsPower: профилей ${live.length}`,
        text: [
          fresh ? `без прочитанных каналов: ${fresh} — их каналы появятся после «Синхронизировать всё» (или «Найти каналы» у профиля при выкладке)` : "",
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
  const { settings, update } = useLoadedSettings();
  const groups: AnalyticsGroup[] = settings.analytics_groups ?? [];
  const view = { group: "", profile: "", sort: "views_48h", dir: "desc", ...(settings.analytics_view ?? {}) };
  const setView = (patch: Partial<typeof view>) => update({ analytics_view: { ...view, ...patch } });
  const saveGroups = (next: AnalyticsGroup[]) => update({ analytics_groups: next });
  const groupsOf = (id: string) => groups.filter((g) => g.channels.includes(id));
  const all = rows ?? [];
  // можно включить сразу несколько групп (показываются каналы любой из них) и несколько профилей;
  // группы и профили вместе — «и»: каналы выбранных групп внутри выбранных профилей
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
      const x = a[key] as number | null | undefined;
      const y = b[key] as number | null | undefined;
      if (x == null && y == null) return 0;
      if (x == null) return 1; // нет цифр — всегда внизу
      if (y == null) return -1;
      return sign * (x - y);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [all, view.group, view.profile, groups, view.sort, view.dir]);
  const filtered = groupSel.length > 0 || profileSel.length > 0;
  const sortBy = (key: SortKey) =>
    setView(view.sort === key ? { dir: view.dir === "asc" ? "desc" : "asc" } : { sort: key, dir: key === "name" ? "asc" : "desc" });
  const arrow = (key: SortKey) =>
    view.sort === key ? (view.dir === "asc" ? <ArrowUp size={12} /> : <ArrowDown size={12} />) : null;
  const toggleMember = (gid: string, channel: string) =>
    saveGroups(groups.map((g) => g.id !== gid ? g : {
      ...g,
      channels: g.channels.includes(channel) ? g.channels.filter((c) => c !== channel) : [...g.channels, channel],
    }));
  const sum = (k: "subscribers" | "watch_hours" | "views_48h" | "scheduled") =>
    list.some((r) => r[k] != null) ? list.reduce((a, r) => a + (r[k] ?? 0), 0) : null;
  // «Всего» вчера: у канала без вчерашних цифр берём сегодняшние — его рост не попадёт в общую динамику ложно
  const sumPrev = (k: "subscribers" | "watch_hours" | "views_48h") =>
    list.some((r) => r.prev?.[k] != null) ? list.reduce((a, r) => a + (r.prev?.[k] ?? r[k] ?? 0), 0) : null;

  return (
    <div className="page">
      <PageHeader
        title="Аналитика"
        subtitle="«Только AdsPower» — список профилей, ничего не открывая. «Синхронизировать всё» — ещё и каналы с цифрами (профили открываются скрыто)."
        actions={
          <span className="row gap-sm">
            <Button
              icon={<RefreshCw size={16} />}
              onClick={() => void syncAds()}
              loading={adsBusy}
              disabled={Boolean(running) || adsBusy}
              title="Только список профилей из AdsPower: новые, переименованные, удалённые. Каналы и цифры не трогает, профили не открывает."
            >
              Только AdsPower
            </Button>
            <Button
              variant="primary"
              icon={<RefreshCw size={16} />}
              onClick={sync}
              loading={Boolean(running)}
              disabled={Boolean(running) || adsBusy}
              title="Профили из AdsPower + каналы в каждом профиле + свежие цифры. Профили открываются скрыто по одному."
            >
              {running ? "Синхронизирую…" : "Синхронизировать всё"}
            </Button>
          </span>
        }
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
          text="Нажми «Синхронизировать всё»: программа подтянет профили из AdsPower, найдёт в них каналы и снимет цифры."
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
                  <th className="num an-sort" onClick={() => sortBy("scheduled")} title="Сколько роликов канала запланировано, но ещё не вышло (читается из «Контента» при синхронизации)">Запланировано {arrow("scheduled")}</th>
                  <th className="an-sort" onClick={() => sortBy("synced_at")}>Обновлено {arrow("synced_at")}</th>
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
                    <td className="num">{fmtInt(r.scheduled)}</td>
                    <td className="small">
                      {when(r.synced_at)}
                      {r.error && r.synced_at ? <> <Badge tone="warn">устарело</Badge></> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
              {rows !== null && !list.length && (
                <tbody>
                  <tr>
                    <td colSpan={7} className="muted small">{filtered ? "Под выбранные фильтры каналов нет — сними часть фильтров или добавь каналы в группу кнопкой в столбце «Группы»." : "Каналов пока нет."}</td>
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
                    <td className="num">{fmtInt(sum("scheduled"))}</td>
                    <td />
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
  const toggle = (id: string) => onFilter(filter.includes(id) ? filter.filter((x) => x !== id) : [...filter, id]);

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
              title="Можно включить несколько групп сразу — покажутся каналы любой из них"
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
