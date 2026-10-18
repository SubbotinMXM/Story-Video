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
import { api, type Job, type JobIn, type LogLine, type Meta, type Settings } from "./api";

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
          if (job.status === "failed") push({ kind: "error", title: job.title, text: job.error || "Ошибка" });
        }
        setJobs((all) => {
          const i = all.findIndex((j) => j.id === job.id);
          if (i === -1) return [...all, job];
          const next = all.slice();
          next[i] = job;
          return next;
        });
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

  const enqueue = useCallback(
    async (items: JobIn[]) => {
      await flush();
      try {
        const added = await api.addJobs(items);
        push({
          kind: "info",
          title: added.length === 1 ? "Задача в очереди" : `В очереди: ${added.length}`,
          text: added.map((j) => j.title).join("\n"),
        });
        return added;
      } catch (e) {
        push({ kind: "error", title: "Не запустилось", text: (e as Error).message });
        return null;
      }
    },
    [flush, push],
  );

  const toastApi = useMemo(() => ({ toasts, push, dismiss }), [toasts, push, dismiss]);
  const settingsApi = useMemo(
    () => ({ meta, settings, saveState, update, flush }),
    [meta, settings, saveState, update, flush],
  );
  const jobsApi = useMemo(() => ({ jobs, logs, connected, enqueue }), [jobs, logs, connected, enqueue]);

  return (
    <ToastCtx.Provider value={toastApi}>
      <SettingsCtx.Provider value={settingsApi}>
        <JobsCtx.Provider value={jobsApi}>{children}</JobsCtx.Provider>
      </SettingsCtx.Provider>
    </ToastCtx.Provider>
  );
}
