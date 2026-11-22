import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  CheckCircle2,
  ChevronDown,
  ChevronsUp,
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
import { api, type Job, type TokenUsage, type ToolSwitch } from "../lib/api";
import { useJobs, useSettings, useToasts } from "../lib/store";
import { Badge, Button, Empty, Progress } from "./ui";
import { LongText } from "./LongText";

const KIND_LABEL: Record<string, string> = {
  broll: "Рассказы",
  slides_full: "Слайды",
  slides_resume: "Продолжение",
  slides_voice: "Шаг 1",
  slides_media: "Шаг 2",
  slides_render: "Шаг 3",
  copies: "Копии",
  rewrite: "Рерайт",
  publish: "Выкладка",
  comments: "Комментарии",
  polls: "Голосования",
  niches: "Поиск ниш",
  competition: "Конкуренция по языкам",
  fixvideo: "Доделать ролик",
  scan: "Поиск каналов",
  analytics: "Аналитика",
  cover: "Обложка",
};

// подпункт: режим подбора кадров, запомненный в задаче при постановке в очередь
const MODE_LABEL: Record<string, string> = {
  theme: "по смыслу ролика",
  strict_3min: "строго первые 2 минуты",
  strict_full: "строго весь ролик",
  stars: "по смыслу ролика", // старые задачи: раздела «Звёзды» больше нет
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

function stamp(sec: number) {
  const d = new Date(sec * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getDate())}.${p(d.getMonth() + 1)} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// что задача создала: видео, картинка, текст или папка — имя файла
function resultFile(job: Job) {
  const r = job.result;
  const path = r?.video || r?.image || r?.text_file || r?.folder || "";
  return path.split("/").filter(Boolean).pop() || "";
}

const LIVE_ICON: Record<string, string> = { done: "✓", failed: "✗", limit: "⏸", waiting: "⏸", active: "⏳", wait: "•" };

/** Выкладка: по каждому каналу — шаг Studio, процент загрузки, время выхода; обновляется само. */
function LiveTable({ job, now }: { job: Job; now: number }) {
  const rows = job.live ?? [];
  if (!rows.length) return null;
  return (
    <div className="live-table">
      {rows.map((r) => (
        <div key={r.channel} className={`live-row live-${r.status}`}>
          <div className="live-top">
            <span className="live-icon">{LIVE_ICON[r.status] ?? "•"}</span>
            <b className="live-name" title={r.channel}>{r.channel}</b>
          </div>
          {(r.speed || r.when) && (
            <div className="live-meta muted small">
              {r.speed ? <span>копия {r.speed}%</span> : null}
              {r.when && <span>выход {r.when}</span>}
            </div>
          )}
          {r.status === "active" && <Progress value={Math.max(0, Math.min(1, r.pct / 100))} />}
          <LongText className={`small ${r.status === "failed" ? "bad" : "muted"}`} text={r.stage} lines={3} />
          {r.status === "active" && (
            <span className="muted small">{Math.max(0, Math.round(now / 1000 - r.updated))} с назад</span>
          )}
        </div>
      ))}
    </div>
  );
}

function StatusIcon({ status }: { status: Job["status"] }) {
  if (status === "running") return <Loader2 size={18} className="spin accent" />;
  if (status === "done") return <CheckCircle2 size={18} className="ok" />;
  if (status === "failed") return <XCircle size={18} className="bad" />;
  if (status === "cancelled") return <XCircle size={18} className="warn" />;
  return <Circle size={18} className="muted" />;
}

function JobCard({ job, now, onShowLog, queuePos, queueLen }: {
  job: Job; now: number; onShowLog: (id: string) => void; queuePos: number; queueLen: number;
}) {
  const [open, setOpen] = useState(job.resumable);
  const [confirmReset, setConfirmReset] = useState(false);
  const [busy, setBusy] = useState(false);
  const { push } = useToasts();
  const { flush } = useSettings();
  const { enqueue } = useJobs();
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
    <div className={`job job-${job.status} ${job.stopping ? "job-stopping" : ""}`}>
      <div className="job-top" onClick={() => setOpen((v) => !v)}>
        <StatusIcon status={job.status} />
        <div className="job-main">
          <div className="job-title" title={job.title}>
            {job.title}
          </div>
          <div className="job-meta">
            <Badge>{KIND_LABEL[job.kind] ?? job.kind}</Badge>
            {job.kind.startsWith("slides") && job.kind !== "slides_render" && typeof job.params.media_mode === "string" && (
              <Badge tone="accent">{MODE_LABEL[job.params.media_mode] ?? job.params.media_mode}</Badge>
            )}
            <Badge tone={st.tone}>{st.label}</Badge>
            {job.attempts > 1 && <span className="muted small">попытка {job.attempts}</span>}
            {job.started && <span className="muted small">{elapsed(job, now)}</span>}
          </div>
          {job.targets && job.targets.length > 0 && (
            <div className="job-targets small" title={job.targets.join(", ")}>
              <b>{job.targets_after ? "Выложится на:" : "Канал:"}</b> {job.targets.join(", ")}
            </div>
          )}
          <div className="muted small job-when">
            добавлено {stamp(job.created)}
            {job.status === "done" && job.finished && (
              <>
                {" · "}создано {stamp(job.finished)}
                {resultFile(job) && <span title={resultFile(job)}>: {resultFile(job)}</span>}
              </>
            )}
          </div>
        </div>
        <ChevronDown size={16} className={`chev ${open ? "open" : ""}`} />
      </div>

      {(job.kind === "publish" || job.kind === "comments") && <LiveTable job={job} now={now} />}
      {job.status === "running" && (job.kind === "publish" || job.kind === "comments") && job.live?.length ? (
        <div className="muted small">Общий прогресс задачи (каналы грузятся по очереди):</div>
      ) : null}
      {job.status === "running" && job.stopping && (
        <div className="stopping" role="status">
          <div className="stopping-head">
            <Loader2 size={15} className="spin" />
            <b>Останавливаю…</b>
            {job.stop_at ? <span className="muted">{Math.max(0, Math.round(now / 1000 - job.stop_at))} с</span> : null}
          </div>
          <div className="stopping-bar" />
          <div className="small">
            Кнопка сработала: задача доделывает текущий шаг и остановится. Это не зависание, готовые этапы сохранятся.
          </div>
          <div className="muted small ellipsis" title={job.message}>{job.message.replace(/^Останавливаю[^(]*/, "").replace(/^\(|\)$/g, "") || "ждём ответа"}</div>
        </div>
      )}
      {job.status === "running" && !job.stopping && (
        <div className="job-progress">
          <Progress value={job.progress} />
          <div className="job-msg">
            <span className="ellipsis">{job.message}</span>
            <span className="muted">{Math.round(job.progress * 100)}%</span>
          </div>
        </div>
      )}
      {!open && job.status !== "running" && job.status !== "queued" && (
        <LongText className="job-note muted small" text={job.error || job.message} lines={2} />
      )}
      {job.switches?.length ? <Switches items={job.switches} /> : null}
      {job.tokens ? <Tokens usage={job.tokens} /> : null}
      {job.status === "done" &&
        job.result?.warnings?.map((w, i) => (
          <div key={i} className="pn pn-warn small">
            <span className="pn-ico" aria-hidden>⚠</span>
            <LongText text={w} lines={4} />
          </div>
        ))}

      {/* в работе / в очереди — только стоп */}
      {(job.status === "running" || job.status === "queued") && (
        <div className="row gap-sm">
          <Button
            size="sm"
            variant="danger"
            icon={<Square size={13} />}
            loading={busy || job.stopping}
            disabled={job.stopping}
            onClick={() => act(() => api.cancelJob(job.id))}
          >
            {job.stopping ? "Останавливаю…" : job.status === "running" ? "Остановить" : "Отменить"}
          </Button>
          {job.status === "running" && !job.stopping && (
            <span className="muted small">Готовые этапы сохранятся</span>
          )}
          {job.status === "queued" && queueLen > 1 && (
            <>
              <span className="muted small">№{queuePos + 1} из {queueLen}</span>
              <Button size="sm" variant="ghost" icon={<ChevronsUp size={14} />} title="Следующей"
                disabled={queuePos === 0} onClick={() => act(() => api.moveJob(job.id, "top"))} />
              <Button size="sm" variant="ghost" icon={<ArrowUp size={14} />} title="Выше"
                disabled={queuePos === 0} onClick={() => act(() => api.moveJob(job.id, "up"))} />
              <Button size="sm" variant="ghost" icon={<ArrowDown size={14} />} title="Ниже"
                disabled={queuePos === queueLen - 1} onClick={() => act(() => api.moveJob(job.id, "down"))} />
            </>
          )}
        </div>
      )}

      {open && job.resumable && (
        <div className="job-details">
          {job.error ? (
            <div className="job-error"><LongText text={job.error} lines={8} /></div>
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
            <LongText key={i} className="small mono-soft" text={l} lines={3} />
          ))}
          <div className="row gap-sm wrap">
            {job.kind === "rewrite" && job.status === "done" && Boolean(job.params.review) && job.result?.follow && (
              <>
                <Button size="sm" onClick={() => act(() => api.open(job.result!.text_file!))}>
                  Открыть текст
                </Button>
                <Button size="sm" variant="primary" onClick={() => act(() => enqueue([job.result!.follow!]))}>
                  Собрать ролик
                </Button>
              </>
            )}
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
    const pos = new Map(jobs.map((j, i) => [j.id, i]));
    // ждущие — в порядке очереди (приоритет), готовые — свежие сверху
    return jobs.slice().sort((a, b) => rank[a.status] - rank[b.status] ||
      (rank[a.status] === 2 ? b.created - a.created : pos.get(a.id)! - pos.get(b.id)!));
  }, [jobs]);
  const queued = ordered.filter((j) => j.status === "queued");

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
                  queuePos={queued.indexOf(j)}
                  queueLen={queued.length}
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


function tk(n: number) {
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)} млн` : n >= 1000 ? `${Math.round(n / 1000)} тыс.` : String(n);
}

/** Расход токенов встроенной нейросети: итог сразу, по этапам — по клику. */
function Tokens({ usage }: { usage: TokenUsage }) {
  const [all, setAll] = useState(false);
  const fresh = usage.in - usage.cached; // кеш провайдера стоит заметно дешевле
  return (
    <div className="stack" style={{ gap: 4 }}>
      <button className="link small" onClick={() => setAll(!all)} title="Claude Code и Codex работают по подписке и здесь не считаются">
        Нейросеть: {tk(usage.in + usage.out)} токенов, {usage.calls} запросов
        {usage.cached > 0 ? ` (${Math.round((usage.cached / Math.max(1, usage.in)) * 100)}% входа из кеша)` : ""} {all ? "▴" : "▾"}
      </button>
      {all && (
        <div className="muted small">
          {usage.by.map((r) => (
            <div key={r.name}>
              {r.name}: {tk(r.in - r.cached + r.out)} · запросов {r.calls}
            </div>
          ))}
          <div>Без кеша: вход {tk(fresh)}, ответ {tk(usage.out)}</div>
        </div>
      )}
    </div>
  );
}

/** Автосмены инструмента/модели в задаче: последняя видна сразу, остальные — по клику. */
function Switches({ items }: { items: ToolSwitch[] }) {
  const [all, setAll] = useState(false);
  const shown = all ? items : items.slice(-1);
  const time = (t: number) => new Date(t * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  return (
    <div className="stack" style={{ gap: 4 }}>
      {shown.map((s, i) => (
        <div key={i} className="notice small">
          ⇄ {time(s.at)} · {s.task}: <b>{s.from}</b> → <b>{s.to}</b> — {s.reason}
          {s.times && s.times > 1 ? ` (×${s.times})` : ""}
        </div>
      ))}
      {items.length > 1 && (
        <button className="link small" onClick={() => setAll(!all)}>
          {all ? "Свернуть" : `Все смены инструмента (${items.length})`}
        </button>
      )}
    </div>
  );
}
