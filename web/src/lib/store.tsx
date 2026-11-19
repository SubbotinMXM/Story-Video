import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { api, type Job, type JobIn, type LogLine, type Meta, type PublishProfile, type Settings } from "./api";

// ---------- toasts ----------

export type Toast = { id: number; kind: "ok" | "error" | "info"; title: string; text?: string };

type ToastApi = { toasts: Toast[]; push: (t: Omit<Toast, "id">) => void; dismiss: (id: number) => void };

const ToastCtx = createContext<ToastApi | null>(null);

export function useToasts() {
  const ctx = useContext(ToastCtx);
  if (!ctx) throw new Error("ToastCtx");
  return ctx;
}

// ---------- settings ----------

type SaveState = "idle" | "saving" | "saved" | "error";

type SettingsApi = {
  meta: Meta | null;
  settings: Settings | null;
  saveState: SaveState;
  update: (patch: Partial<Settings>) => void;
  flush: () => Promise<void>;
  /** Настройки целиком пришли с сервера (загрузили шаблон) — показать их, ничего не сохраняя. */
  replace: (s: Settings) => void;
};

const SettingsCtx = createContext<SettingsApi | null>(null);

export function useSettings() {
  const ctx = useContext(SettingsCtx);
  if (!ctx) throw new Error("SettingsCtx");
  return ctx;
}

/** settings гарантированно загружены (страницы рендерятся только после загрузки) */
export function useLoadedSettings() {
  const ctx = useSettings();
  return ctx as SettingsApi & { meta: Meta; settings: Settings };
}

// ---------- jobs ----------

type JobsApi = {
  jobs: Job[];
  logs: LogLine[];
  connected: boolean;
  enqueue: (items: JobIn[]) => Promise<Job[] | null>;
};

const JobsCtx = createContext<JobsApi | null>(null);

export function useJobs() {
  const ctx = useContext(JobsCtx);
  if (!ctx) throw new Error("JobsCtx");
  return ctx;
}

// ---------- профили AdsPower и каналы — один список на всё приложение ----------

type ProfilesApi = {
  profiles: PublishProfile[];
  loaded: boolean;
  /** перечитать с сервера */
  reload: () => Promise<void>;
  /** свежий список, который уже вернул сервер (синхронизация, смена языка) */
  setProfiles: (p: PublishProfile[]) => void;
};

const ProfilesCtx = createContext<ProfilesApi | null>(null);

/** Профили и их каналы. Где бы ни прошла синхронизация (вручную, по расписанию, «Найти каналы»),
 *  список обновляется сразу во всех разделах: выкладка, комментарии, голосования. */
export function useProfiles() {
  const ctx = useContext(ProfilesCtx);
  if (!ctx) throw new Error("ProfilesCtx");
  return ctx;
}

/** Задачи, которые меняют учёт каналов: синхронизация «Аналитики» (в т. ч. по расписанию) и поиск каналов профиля. */
const REGISTRY_KINDS = new Set(["analytics", "scan"]);

const LOG_LIMIT = 2000;
const SAVE_DELAY = 500;

export function AppProvider({ children }: { children: ReactNode }) {
  // toasts
  const [toasts, setToasts] = useState<Toast[]>([]);
  const toastSeq = useRef(0);
  const dismiss = useCallback((id: number) => setToasts((all) => all.filter((t) => t.id !== id)), []);
  const push = useCallback(
    (t: Omit<Toast, "id">) => {
      const id = ++toastSeq.current;
      setToasts((all) => [...all.slice(-4), { ...t, id }]);
      window.setTimeout(() => dismiss(id), t.kind === "error" ? 9000 : 4500);
    },
    [dismiss],
  );

  // settings
  const [meta, setMeta] = useState<Meta | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const pending = useRef<Partial<Settings>>({});
  const timer = useRef<number | null>(null);

  useEffect(() => {
    Promise.all([api.meta(), api.settings()])
      .then(([m, s]) => {
        setMeta(m);
        setSettings(s);
      })
      .catch((e: Error) => push({ kind: "error", title: "Не удалось загрузить настройки", text: e.message }));
  }, [push]);

  const flush = useCallback(async () => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
    const patch = pending.current;
    if (Object.keys(patch).length === 0) return;
    pending.current = {};
    setSaveState("saving");
    try {
      await api.saveSettings(patch);
      setSaveState("saved");
    } catch (e) {
      setSaveState("error");
      push({ kind: "error", title: "Настройки не сохранились", text: (e as Error).message });
    }
  }, [push]);

  const update = useCallback(
    (patch: Partial<Settings>) => {
      setSettings((s) => (s ? { ...s, ...patch } : s));
      pending.current = { ...pending.current, ...patch };
      setSaveState("saving");
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => void flush(), SAVE_DELAY);
    },
    [flush],
  );

  // jobs + logs через SSE
  const [jobs, setJobs] = useState<Job[]>([]);
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [connected, setConnected] = useState(false);
  const prevStatus = useRef<Map<string, string>>(new Map());

  useEffect(() => {
    const es = new EventSource("/api/events");
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.onmessage = (ev) => {
      const data = JSON.parse(ev.data);
      if (data.type === "snapshot") {
        setJobs(data.jobs);
        setLogs(data.logs);
        prevStatus.current = new Map((data.jobs as Job[]).map((j) => [j.id, j.status]));
      } else if (data.type === "job") {
        const job: Job = data.job;
        const before = prevStatus.current.get(job.id);
        prevStatus.current.set(job.id, job.status);
        if (before && before !== job.status) {
          if (job.status === "done") push({ kind: "ok", title: job.title, text: job.result?.summary || "Готово" });
          if (job.status === "done" && job.result?.warnings?.length)
            push({ kind: "error", title: `${job.title}: не всё получилось`, text: job.result.warnings.join("\n") });
          if (job.status === "failed") push({ kind: "error", title: job.title, text: job.error || "Ошибка" });
        }
        setJobs((all) => {
          const i = all.findIndex((j) => j.id === job.id);
          if (i === -1) return [...all, job];
          const next = all.slice();
          next[i] = job;
          return next;
        });
      } else if (data.type === "switch") {
        // автосмена инструмента/модели — сразу на виду: что, на что, почему, в какой задаче
        push({
          kind: "info",
          title: `${data.title ? `${data.title}: ` : ""}${data.from} → ${data.to}`,
          text: `${data.task}: ${data.reason}`,
        });
      } else if (data.type === "jobs_order") {
        // порядок очереди (приоритет) — как на сервере
        const pos = new Map((data.ids as string[]).map((id, i) => [id, i]));
        setJobs((all) => all.slice().sort((a, b) => (pos.get(a.id) ?? 1e9) - (pos.get(b.id) ?? 1e9)));
      } else if (data.type === "job_removed") {
        prevStatus.current.delete(data.id);
        setJobs((all) => all.filter((j) => j.id !== data.id));
      } else if (data.type === "log") {
        setLogs((all) => {
          const next = all.length >= LOG_LIMIT ? all.slice(-LOG_LIMIT + 1) : all.slice();
          next.push({ line: data.line, job_id: data.job_id });
          return next;
        });
      }
    };
    return () => es.close();
  }, [push]);

  // профили и каналы
  const [profiles, setProfilesState] = useState<PublishProfile[]>([]);
  const [profilesLoaded, setProfilesLoaded] = useState(false);
  const setProfiles = useCallback((p: PublishProfile[]) => {
    setProfilesState(p);
    setProfilesLoaded(true);
  }, []);
  const reloadProfiles = useCallback(async () => {
    try {
      setProfiles((await api.publishProfiles()).profiles);
    } catch {
      /* сервер недоступен — оставляем прежний список */
    }
  }, [setProfiles]);
  // синхронизация началась или закончилась — перечитать
  const registryKey = jobs
    .filter((j) => REGISTRY_KINDS.has(j.kind))
    .map((j) => `${j.id}:${j.status}`)
    .join(",");
  useEffect(() => {
    void reloadProfiles();
  }, [registryKey, reloadProfiles]);
  // пока идёт — новые каналы появляются по ходу (профиль за профилем)
  const registryRunning = jobs.some((j) => REGISTRY_KINDS.has(j.kind) && j.status === "running");
  useEffect(() => {
    if (!registryRunning) return;
    const t = window.setInterval(() => void reloadProfiles(), 15000);
    return () => window.clearInterval(t);
  }, [registryRunning, reloadProfiles]);
  // вернулись в окно приложения — вдруг что-то менялось
  useEffect(() => {
    const onFocus = () => void reloadProfiles();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [reloadProfiles]);

  // повтор постановки после вопроса «переписать ещё раз?» — тем же enqueue
  const enqueueRef = useRef<(items: JobIn[]) => Promise<Job[] | null>>(async () => null);
  const enqueue = useCallback(
    async (items: JobIn[]): Promise<Job[] | null> => {
      await flush();
      try {
        const added = await api.addJobs(items);
        // выкладка: даты календаря отданы этому запуску — сервер их очистил, здесь тоже
        if (items.some((i) => i.kind === "broll" || (i.kind === "rewrite" && (i.params.section ?? "broll") === "broll")))
          setSettings((s) => (s && s.broll_publish ? { ...s, broll_publish_dates: [] } : s));
        push({
          kind: "info",
          title: added.length === 1 ? "Задача в очереди" : `В очереди: ${added.length}`,
          text: added.map((j) => j.title).join("\n"),
        });
        const notice = added.find((j) => j.notice)?.notice;
        if (notice) push({ kind: "error", title: "RoyalTechno: может не хватить денег", text: notice });
        return added;
      } catch (e) {
        const err = e as Error & { status?: number };
        if (err.status === 409 && window.confirm(`${err.message}\n\nПереписать ещё раз?`)) {
          return enqueueRef.current(items.map((i) => (i.kind === "rewrite" ? { ...i, params: { ...i.params, force: true } } : i)));
        }
        if (err.status !== 409) push({ kind: "error", title: "Не запустилось", text: err.message });
        else push({ kind: "info", title: "Не запускаю", text: "Повторные ролики пропущены — убери их из списка ссылок" });
        return null;
      }
    },
    [flush, push],
  );

  enqueueRef.current = enqueue;

  const toastApi = useMemo(() => ({ toasts, push, dismiss }), [toasts, push, dismiss]);
  const settingsApi = useMemo(
    () => ({ meta, settings, saveState, update, flush, replace: setSettings }),
    [meta, settings, saveState, update, flush],
  );
  const jobsApi = useMemo(() => ({ jobs, logs, connected, enqueue }), [jobs, logs, connected, enqueue]);
  const profilesApi = useMemo(
    () => ({ profiles, loaded: profilesLoaded, reload: reloadProfiles, setProfiles }),
    [profiles, profilesLoaded, reloadProfiles, setProfiles],
  );

  return (
    <ToastCtx.Provider value={toastApi}>
      <SettingsCtx.Provider value={settingsApi}>
        <JobsCtx.Provider value={jobsApi}>
          <ProfilesCtx.Provider value={profilesApi}>{children}</ProfilesCtx.Provider>
        </JobsCtx.Provider>
      </SettingsCtx.Provider>
    </ToastCtx.Provider>
  );
}
