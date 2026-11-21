import { useCallback, useEffect, useState, type ReactNode } from "react";
import {
  ArrowUpRight, CheckCircle2, ClipboardList, Clock, Hand, Loader2, TriangleAlert, Wrench, XCircle,
} from "lucide-react";
import { api, type Report, type ReportItem, type ReportProblem } from "../lib/api";
import { useToasts } from "../lib/store";
import { Modal } from "./Modal";

const clock = (t: number) => new Date(t * 1000).toLocaleString("ru-RU", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
const span = (a: number, b: number) => {
  const m = Math.max(0, Math.round((b - a) / 60));
  if (!a || m < 1) return "";
  return m >= 60 ? `${Math.floor(m / 60)} ч ${m % 60} мин` : `${m} мин`;
};

/** Кнопка «Итоги работы» в боковой панели + окно итогов, которое само открывается, когда очередь закончила. */
export function ReportHost() {
  const [report, setReport] = useState<Report | null>(null);
  const { push } = useToasts();

  const show = useCallback((r: Report) => {
    setReport(r);
    void api.reportSeen(r.id).catch(() => undefined);
  }, []);

  // закончили, пока приложение работало — сервер присылает отчёт сам; приложение было закрыто — берём непрочитанный при запуске
  useEffect(() => {
    const on = (e: Event) => show((e as CustomEvent<Report>).detail);
    window.addEventListener("sv-report", on);
    api.reportUnseen().then((r) => r && show(r)).catch(() => undefined);
    return () => window.removeEventListener("sv-report", on);
  }, [show]);

  const openLast = async () => {
    try {
      const list = await api.reports();
      if (!list.length) {
        push({ kind: "info", title: "Итогов пока нет", text: "Окно появится само, когда очередь закончит работу." });
        return;
      }
      show(await api.report(list[0].id));
    } catch (e) {
      push({ kind: "error", title: "Итоги не открылись", text: (e as Error).message });
    }
  };

  return (
    <>
      <button className="ver-btn" onClick={() => void openLast()} title="Что сделано, что нет и как исправить — по последней работе">
        <ClipboardList size={13} />
        <span>Итоги работы</span>
      </button>
      {report && <ReportDialog report={report} onClose={() => setReport(null)} />}
    </>
  );
}

function ReportDialog({ report, onClose }: { report: Report; onClose: () => void }) {
  const { push } = useToasts();
  const [busy, setBusy] = useState(false);

  const fix = async (jobs: string[], collabs: boolean, what: string) => {
    setBusy(true);
    try {
      const r = await api.reportFix(jobs, collabs);
      const parts = [];
      if (r.resumed.length) parts.push(`продолжаю задач: ${r.resumed.length}`);
      if (r.collabs) parts.push("принимаю соавторства");
      push({
        kind: r.errors.length && !parts.length ? "error" : "ok",
        title: what,
        text: [parts.join(", "), ...r.errors].filter(Boolean).join(". ") || "Готово",
      });
      if (parts.length) onClose();
    } catch (e) {
      push({ kind: "error", title: "Не получилось запустить исправление", text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const manual = report.problems.filter((p) => p.mode === "manual" && p.severity === "bad");
  const auto = report.problems.filter((p) => p.mode === "auto" && p.severity === "bad");
  const wait = report.problems.filter((p) => p.mode === "wait" && p.severity === "bad");
  const notes = report.problems.filter((p) => p.severity === "warn");
  const c = report.counts;
  const chips: [string, number][] = (
    [["Выложено роликов", c.videos], ["Голосований", c.polls], ["Каналов с комментариями", c.comments], ["Текстов", c.texts], ["Обложек", c.covers]] as [string, number][]
  ).filter(([, n]) => n > 0);
  const took = span(report.started, report.finished);

  return (
    <Modal
      title="Итоги работы"
      subtitle={`Завершено ${clock(report.finished)}${took ? ` · работа заняла ${took}` : ""}`}
      onClose={onClose}
      width={800}
      footer={
        <>
          {report.fix.count > 0 && (
            <button className="btn btn-primary" disabled={busy} onClick={() => void fix(report.fix.jobs, report.fix.collabs, "Исправляю автоматически")}>
              {busy ? <Loader2 size={15} className="spin" /> : <Wrench size={15} />} Исправить всё автоматически ({report.fix.count})
            </button>
          )}
          <button className="btn btn-ghost" onClick={onClose}>Закрыть</button>
        </>
      }
    >
      <div className="rp">
        <div className={`rp-hero rp-t-${report.tone}`}>
          <div className="rp-hero-ico">
            {report.tone === "ok" ? <CheckCircle2 size={28} /> : report.tone === "warn" ? <TriangleAlert size={28} /> : <XCircle size={28} />}
          </div>
          <div className="rp-hero-main">
            <div className="rp-hero-title">{report.title}</div>
            <div className="rp-hero-text">{report.text}</div>
            {chips.length > 0 && (
              <div className="rp-chips">
                {chips.map(([label, n]) => (
                  <span key={label} className="rp-chip"><b>{n}</b> {label.toLowerCase()}</span>
                ))}
              </div>
            )}
          </div>
        </div>

        {manual.length > 0 && (
          <Section title="Нужны ваши действия" hint="Программа сама это сделать не может — ниже по шагам, что именно сделать." tone="bad" icon={<Hand size={16} />}>
            {manual.map((p) => <ProblemCard key={p.id} p={p} busy={busy} onFix={fix} />)}
          </Section>
        )}
        {auto.length > 0 && (
          <Section title="Можно исправить автоматически" hint="Нажмите «Исправить» — программа продолжит с места, где остановилась." tone="warn" icon={<Wrench size={16} />}>
            {auto.map((p) => <ProblemCard key={p.id} p={p} busy={busy} onFix={fix} />)}
          </Section>
        )}
        {wait.length > 0 && (
          <Section title="Пройдёт само" tone="info" icon={<Clock size={16} />}>
            {wait.map((p) => <ProblemCard key={p.id} p={p} busy={busy} onFix={fix} />)}
          </Section>
        )}
        {notes.length > 0 && (
          <Section title="Замечания" tone="info" icon={<TriangleAlert size={16} />}>
            {notes.map((p) => (
              <div key={p.id} className="rp-note"><b>{p.title}.</b> {p.text}</div>
            ))}
          </Section>
        )}

        <div className="rp-sec-title"><CheckCircle2 size={16} /> Что сделано</div>
        <div className="rp-items">
          {report.items.map((it) => <ItemBlock key={it.job_id} it={it} />)}
        </div>
      </div>
    </Modal>
  );
}

function Section({ title, hint, tone, icon, children }: { title: string; hint?: string; tone: "bad" | "warn" | "info"; icon: ReactNode; children: ReactNode }) {
  return (
    <div className={`rp-sec rp-t-${tone}`}>
      <div className="rp-sec-title">{icon} {title}</div>
      {hint && <div className="muted small rp-sec-hint">{hint}</div>}
      <div className="rp-cards">{children}</div>
    </div>
  );
}

function ProblemCard({ p, busy, onFix }: { p: ReportProblem; busy: boolean; onFix: (jobs: string[], collabs: boolean, what: string) => Promise<void> }) {
  const act = p.action;
  const label = p.mode === "manual"
    ? act?.type === "collabs" ? "Я подтвердил вход — принять соавторства" : "Я сделал — продолжить"
    : act?.type === "collabs" ? "Принять соавторства сейчас" : "Исправить";
  return (
    <div className={`rp-card ${p.mode}`}>
      <div className="rp-card-head">
        <div className="rp-card-title">{p.title}</div>
        <span className={`rp-badge ${p.mode}`}>{p.mode === "manual" ? "нужен вход / ручное действие" : p.mode === "auto" ? "исправится автоматически" : "пройдёт само"}</span>
      </div>
      <div className="rp-cause"><b>Причина:</b> {p.cause}</div>
      {p.text && <div className="rp-raw" title={p.text}>{p.text}</div>}
      {p.steps.length > 0 && (
        <div className="rp-steps">
          <div className="rp-steps-title">{p.mode === "wait" ? "Что делать" : "Как исправить"}</div>
          <ol>{p.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
        </div>
      )}
      {act && (
        <div className="rp-card-actions">
          <button
            className={`btn btn-sm ${p.mode === "manual" ? "btn-ghost" : "btn-primary"}`}
            disabled={busy}
            onClick={() => void onFix(act.type === "resume" ? [act.job_id] : [], act.type === "collabs", p.mode === "manual" ? "Продолжаю после вашего действия" : "Исправляю")}
          >
            {busy ? <Loader2 size={14} className="spin" /> : <Wrench size={14} />} {label}
          </button>
        </div>
      )}
    </div>
  );
}

function ItemBlock({ it }: { it: ReportItem }) {
  const bad = it.status === "failed";
  const hasBody = it.rows.length > 0 || it.details.length > 0;
  const head = (
    <div className="rp-item-head">
      {bad ? <XCircle size={16} className="rp-ico bad" /> : <CheckCircle2 size={16} className="rp-ico ok" />}
      <span className="rp-kind">{it.kind_label}</span>
      <span className="rp-item-title ellipsis" title={it.title}>{it.title}</span>
      {it.summary && <span className="muted small rp-item-sum">{it.summary}</span>}
    </div>
  );
  if (!hasBody) return <div className="rp-item">{head}</div>;
  return (
    <details className="rp-item" open={it.rows.length <= 8}>
      <summary>{head}</summary>
      <div className="rp-rows">
        {it.rows.some((r) => r.note) && (
          <div className="rp-row rp-row-note">
            Studio ещё обрабатывала {it.rows.filter((r) => r.note).length > 1 ? "некоторые видео" : "видео"} и дату выхода не показала — ролики сохранены, дату можно сверить позже.
          </div>
        )}
        {it.rows.map((r, i) =>
          it.kind === "publish" ? (
            <div key={i} className="rp-row">
              <CheckCircle2 size={14} className="rp-ico ok" />
              <div className="rp-row-main">
                <div><b>{r.channel}</b>{r.when && <span className="muted"> · выход {r.when}</span>}</div>
                {r.title && <div className="muted small ellipsis" title={r.title}>{r.title}</div>}
              </div>
              {(r.coauthors_total ?? 0) > 0 && (
                <span className={`rp-chip ${r.coauthors_ok === r.coauthors_total ? "ok" : "warn"}`} title="Соавторы, принявшие приглашение">
                  соавторы {r.coauthors_ok}/{r.coauthors_total}
                </span>
              )}
              {r.url && (
                <a className="rp-link" href={r.url} target="_blank" rel="noreferrer" title={r.url}>
                  открыть <ArrowUpRight size={12} />
                </a>
              )}
            </div>
          ) : (
            <div key={i} className="rp-row">
              {r.ok ? <CheckCircle2 size={14} className={`rp-ico ${r.info ? "info" : "ok"}`} /> : <XCircle size={14} className="rp-ico bad" />}
              <div className="rp-row-main">{r.text}</div>
            </div>
          ),
        )}
        {it.details.map((d, i) => (
          <div key={`d${i}`} className="rp-row muted small">{d}</div>
        ))}
      </div>
    </details>
  );
}
