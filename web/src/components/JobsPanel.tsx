import { useEffect, useMemo, useRef, useState } from "react";
import {
  CheckCircle2,
  ChevronDown,
  Circle,
  Copy,
  FolderOpen,
  Inbox,
  Loader2,
  RotateCcw,
  Square,
  Trash2,
  XCircle,
} from "lucide-react";
import { api, type Job } from "../lib/api";
import { useJobs, useSettings, useToasts } from "../lib/store";
import { Badge, Button, Empty, Progress } from "./ui";

const KIND_LABEL: Record<string, string> = {
  broll: "Футажи",
  slides_full: "Слайды",
  slides_resume: "Продолжение",
  slides_voice: "Шаг 1",
  slides_media: "Шаг 2",
  slides_render: "Шаг 3",
  copies: "Копии",
};

const STATUS: Record<Job["status"], { label: string; tone: string }> = {
  queued: { label: "в очереди", tone: "neutral" },
  running: { label: "в работе", tone: "accent" },
  done: { label: "готово", tone: "ok" },
  failed: { label: "ошибка", tone: "bad" },
  cancelled: { label: "остановлено", tone: "warn" },
};

function elapsed(job: Job, now: number) {
  if (!job.started) return "";
  const end = job.finished ?? now / 1000;
  const s = Math.max(0, Math.round(end - job.started));
  const m = Math.floor(s / 60);
  return m ? `${m} мин ${s % 60} с` : `${s} с`;
}

function StatusIcon({ status }: { status: Job["status"] }) {
  if (status === "running") return <Loader2 size={18} className="spin accent" />;
  if (status === "done") return <CheckCircle2 size={18} className="ok" />;
  if (status === "failed") return <XCircle size={18} className="bad" />;
  if (status === "cancelled") return <XCircle size={18} className="warn" />;
  return <Circle size={18} className="muted" />;
}

function JobCard({ job, now, onShowLog }: { job: Job; now: number; onShowLog: (id: string) => void }) {
  const [open, setOpen] = useState(job.resumable);
  const [confirmReset, setConfirmReset] = useState(false);
  const [busy, setBusy] = useState(false);
  const { push } = useToasts();
  const { flush } = useSettings();
  const st = STATUS[job.status];

  useEffect(() => {
    if (job.resumable) setOpen(true);
    setConfirmReset(false);
  }, [job.resumable, job.status]);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      push({ kind: "error", title: "Не получилось", text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={`job job-${job.status}`}>
      <div className="job-top" onClick={() => setOpen((v) => !v)}>
        <StatusIcon status={job.status} />
        <div className="job-main">
          <div className="job-title" title={job.title}>
            {job.title}
          </div>
          <div className="job-meta">
            <Badge>{KIND_LABEL[job.kind] ?? job.kind}</Badge>
            <Badge tone={st.tone}>{st.label}</Badge>
            {job.attempts > 1 && <span className="muted small">попытка {job.attempts}</span>}
            {job.started && <span className="muted small">{elapsed(job, now)}</span>}
          </div>
        </div>
        <ChevronDown size={16} className={`chev ${open ? "open" : ""}`} />
      </div>

      {job.status === "running" && (
        <div className="job-progress">
          <Progress value={job.progress} />
          <div className="job-msg">
            <span className="ellipsis">{job.message}</span>
            <span className="muted">{Math.round(job.progress * 100)}%</span>
          </div>
        </div>
      )}
      {!open && job.status !== "running" && job.status !== "queued" && (
        <div className="job-msg muted ellipsis">{job.error || job.message}</div>
      )}
      {job.status === "done" &&
        job.result?.warnings?.map((w, i) => (
          <div key={i} className="notice warn small">
            ⚠ {w}
          </div>
        ))}

      {/* в работе / в очереди — только стоп */}
      {(job.status === "running" || job.status === "queued") && (
        <div className="row gap-sm">
          <Button
            size="sm"
            variant="danger"
            icon={<Square size={13} />}
            loading={busy}
            onClick={() => act(() => api.cancelJob(job.id))}
          >
            {job.status === "running" ? "Остановить" : "Отменить"}
          </Button>
          {job.status === "running" && (
            <span className="muted small">Готовые этапы сохранятся</span>
          )}
        </div>
      )}

      {open && job.resumable && (
        <div className="job-details">
          {job.error ? (
            <div className="job-error">{job.error}</div>
          ) : (
            <div className="muted small">{job.message}</div>
          )}
          <div className="muted small">
            Готовые этапы сохранены. Можно поменять ключи или настройки и продолжить с места остановки.
          </div>
          {confirmReset ? (
            <div className="confirm">
              <span className="small">Удалить задачу и всё, что она успела сделать?</span>
              <div className="row gap-sm">
                <Button size="sm" variant="danger" loading={busy} onClick={() => act(() => api.resetJob(job.id))}>
                  Да, сбросить
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setConfirmReset(false)}>
                  Нет
                </Button>
              </div>
            </div>
          ) : (
            <div className="row gap-sm wrap">
              <Button
                size="sm"
                variant="primary"
                icon={<RotateCcw size={14} />}
                loading={busy}
                onClick={() =>
                  act(async () => {
                    await flush();
                    await api.resumeJob(job.id);
                  })
                }
              >
                Продолжить с места падения
              </Button>
              <Button size="sm" variant="secondary" icon={<Trash2 size={14} />} onClick={() => setConfirmReset(true)}>
                Полный сброс
              </Button>
            </div>
          )}
          <button className="link" onClick={() => onShowLog(job.id)}>
            Журнал задачи
          </button>
        </div>
      )}

      {open && job.status === "done" && (
        <div className="job-details">
          {job.result?.lines?.map((l, i) => (
            <div key={i} className="small mono-soft">
              {l}
            </div>
          ))}
          <div className="row gap-sm wrap">
            {job.result?.folder && (
              <Button size="sm" icon={<FolderOpen size={15} />} onClick={() => act(() => api.reveal(job.result!.folder!))}>
                Показать в Finder
              </Button>
            )}
            <Button size="sm" variant="ghost" onClick={() => onShowLog(job.id)}>
              Журнал
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

export function JobsPanel() {
  const { jobs, logs, connected } = useJobs();
  const [tab, setTab] = useState<"jobs" | "log">("jobs");
  const [logJob, setLogJob] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());
  const logRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const { push } = useToasts();

  const running = jobs.some((j) => j.status === "running");
  useEffect(() => {
    if (!running) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [running]);

  const ordered = useMemo(() => {
    const rank = { running: 0, queued: 1, failed: 2, done: 2, cancelled: 2 } as const;
    return jobs.slice().sort((a, b) => rank[a.status] - rank[b.status] || (rank[a.status] === 2 ? b.created - a.created : a.created - b.created));
  }, [jobs]);

  const shownLogs = useMemo(() => (logJob ? logs.filter((l) => l.job_id === logJob) : logs), [logs, logJob]);

  useEffect(() => {
    const el = logRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [shownLogs, tab]);

  const active = jobs.filter((j) => j.status === "running" || j.status === "queued").length;
  const doneCount = jobs.filter((j) => j.status === "done").length;

  return (
    <aside className="side-panel">
      <div className="panel-tabs">
        <button className={tab === "jobs" ? "active" : ""} onClick={() => setTab("jobs")}>
          Очередь {active > 0 && <span className="count">{active}</span>}
        </button>
        <button className={tab === "log" ? "active" : ""} onClick={() => setTab("log")}>
          Журнал
        </button>
        <span className={`conn ${connected ? "on" : "off"}`} title={connected ? "Связь с сервером есть" : "Нет связи с сервером"} />
      </div>

      {tab === "jobs" ? (
        <div className="panel-body">
          {jobs.length === 0 ? (
            <Empty icon={<Inbox size={32} />} title="Очередь пуста" text="Запущенные ролики появятся здесь" />
          ) : (
            <>
              {ordered.map((j) => (
                <JobCard
                  key={j.id}
                  job={j}
                  now={now}
                  onShowLog={(id) => {
                    setLogJob(id);
                    setTab("log");
                  }}
                />
              ))}
              {doneCount > 0 && (
                <Button size="sm" variant="ghost" className="clear-btn" onClick={() => void api.clearJobs()}>
                  Убрать готовые ({doneCount})
                </Button>
              )}
            </>
          )}
        </div>
      ) : (
        <div className="panel-body log-wrap">
          <div className="log-bar">
            {logJob ? (
              <>
                <span className="small ellipsis">
                  Задача: {jobs.find((j) => j.id === logJob)?.title ?? logJob}
                </span>
                <Button size="sm" variant="ghost" onClick={() => setLogJob(null)}>
                  Весь журнал
                </Button>
              </>
            ) : (
              <span className="small muted">Последние {shownLogs.length} строк</span>
            )}
            <Button
              size="sm"
              variant="ghost"
              icon={<Copy size={14} />}
              onClick={() => {
                void navigator.clipboard.writeText(shownLogs.map((l) => l.line).join("\n"));
                push({ kind: "info", title: "Журнал скопирован" });
              }}
            />
          </div>
          <div
            className="log"
            ref={logRef}
            onScroll={(e) => {
              const el = e.currentTarget;
              stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
            }}
          >
            {shownLogs.length === 0 ? (
              <div className="muted small">Пока пусто</div>
            ) : (
              shownLogs.map((l, i) => (
                <div key={i} className={`log-line ${/✗|Error|Traceback|ошибк/i.test(l.line) ? "bad" : ""}`}>
                  {l.line}
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </aside>
  );
}
