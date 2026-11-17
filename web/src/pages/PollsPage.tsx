import { useEffect, useState } from "react";
import { Play, Plus, Sparkles, Trash2, X } from "lucide-react";
import { useJobs, useToasts } from "../lib/store";
import { Button, Card, Field, NumberInput, PageHeader } from "../components/ui";
import { ChannelPicker } from "../components/ChannelPicker";

type Poll = { question: string; options: string[] };
type Past = Poll & { at: string; status: string };

const OPTION_MAX = 65;
const QUESTION_MAX = 140;
const OPTIONS_MAX = 5;

function fmt(d: Date) {
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

/** Когда выйдет каждый опрос: первый — сразу, дальше каждые step дней (время — с шагом 15 минут). */
function planned(count: number, step: number): string[] {
  const now = Date.now();
  return Array.from({ length: count }, (_, k) => {
    if (k === 0) return "сразу";
    const d = new Date(now + k * step * 86400000);
    d.setSeconds(0, 0);
    d.setMinutes(Math.ceil(d.getMinutes() / 15) * 15);
    return fmt(d);
  });
}

/** Голосования в сообществе канала: нейросеть пишет опросы по теме канала, первый выходит сразу, остальные — по расписанию. */
export function PollsPage() {
  const { enqueue } = useJobs();
  const { push } = useToasts();
  const [picked, setPicked] = useState<string[]>([]);
  // посмотреть и поправить опросы до запуска — когда канал один; на несколько каналов нейросеть пишет их в задаче
  const channel = picked.length === 1 ? picked[0] : "";
  const [count, setCount] = useState(3);
  const [step, setStep] = useState(2);
  const [polls, setPolls] = useState<Poll[]>([]);
  const [titles, setTitles] = useState<string[]>([]);
  const [past, setPast] = useState<Past[]>([]);
  const [busy, setBusy] = useState<"" | "gen" | "run">("");

  useEffect(() => {
    setPolls([]);
    setTitles([]);
    setPast([]);
    if (!channel) return;
    void fetch(`/api/polls/history?channel=${encodeURIComponent(channel)}`)
      .then((r) => r.json())
      .then((r: { items: Past[] }) => setPast(r.items || []))
      .catch(() => undefined);
  }, [channel]);

  const total = polls.length || count;
  const when = planned(total, step);
  const bad = polls.some((p) => !p.question.trim() || p.options.filter((o) => o.trim()).length < 2);

  const edit = (k: number, patch: Partial<Poll>) => setPolls((cur) => cur.map((p, i) => (i === k ? { ...p, ...patch } : p)));
  const editOption = (k: number, j: number, text: string) =>
    edit(k, { options: polls[k].options.map((o, i) => (i === j ? text.slice(0, OPTION_MAX) : o)) });

  const generate = async () => {
    setBusy("gen");
    try {
      const r = await fetch("/api/polls/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channel, count }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || r.statusText);
      setPolls(data.polls);
      setTitles(data.titles || []);
    } catch (e) {
      push({ kind: "error", title: "Опросы не сгенерировались", text: (e as Error).message });
    } finally {
      setBusy("");
    }
  };

  const run = async () => {
    setBusy("run");
    const clean = polls.map((p) => ({ question: p.question.trim(), options: p.options.map((o) => o.trim()).filter(Boolean) }));
    // каждый канал — своя задача: опросы по теме именно этого канала
    await enqueue(picked.map((ch) => ({ kind: "polls", params: { channel: ch, count: total, step_days: step, polls: channel ? clean : [] } })));
    setBusy("");
  };

  return (
    <div className="page narrow">
      <PageHeader
        title="Голосования"
        subtitle="Текстовые опросы в сообществе канала: нейросеть смотрит, о чём канал, и пишет вопросы к опыту зрителей. Первый опрос выходит сразу после запуска, остальные — по расписанию."
      />
      <Card
        title="Каналы"
        description="Отметь один канал — опросы можно посмотреть и поправить до запуска. Несколько — на каждом нейросеть напишет свои опросы по теме канала прямо в задаче."
      >
        <ChannelPicker value={picked} onChange={setPicked} />
      </Card>

      <Card title="Расписание">
        <div className="polls-setup">
          <Field label="Сколько опросов">
            <NumberInput value={count} min={1} max={30} onChange={setCount} width={88} />
          </Field>
          <Field label="Шаг, дней">
            <NumberInput value={step} min={0.25} max={60} step={0.5} onChange={setStep} width={88} />
          </Field>
        </div>
        <div className="muted small polls-plan">
          Выход: {when.slice(0, 8).map((w, k) => `${k + 1}) ${w}`).join(" · ")}
          {when.length > 8 ? " · …" : ""}
        </div>
        <div className="row gap-sm polls-actions">
          <Button
            icon={<Sparkles size={16} />}
            disabled={!channel}
            loading={busy === "gen"}
            title={picked.length > 1 ? "Посмотреть заранее можно, когда выбран один канал" : undefined}
            onClick={() => void generate()}
          >
            {polls.length ? "Сгенерировать заново" : "Сгенерировать и посмотреть"}
          </Button>
          <Button variant="primary" icon={<Play size={16} />} disabled={!picked.length || bad} loading={busy === "run"} onClick={() => void run()}>
            {picked.length > 1 ? `Запустить на ${picked.length} каналах (по ${total})` : `Запустить (${total})`}
          </Button>
          {picked.length === 0 && <span className="muted small">выбери канал выше</span>}
          {picked.length > 1 && <span className="muted small">опросы для каждого канала нейросеть напишет в его задаче</span>}
          {!polls.length && channel && <span className="muted small">без просмотра — нейросеть напишет опросы прямо в задаче</span>}
        </div>
      </Card>

      {polls.length > 0 && (
        <Card
          title={`Опросы (${polls.length})`}
          description={titles.length ? `Тема канала — по роликам: ${titles.slice(0, 4).map((t) => `«${t}»`).join(", ")}${titles.length > 4 ? " …" : ""}` : undefined}
        >
          <div className="polls-list">
            {polls.map((p, k) => (
              <div key={k} className="poll-item">
                <div className="poll-head">
                  <span className="badge">{when[k]}</span>
                  <input
                    className="input poll-question"
                    value={p.question}
                    maxLength={QUESTION_MAX}
                    onChange={(e) => edit(k, { question: e.target.value })}
                  />
                  <button className="icon-btn" title="Убрать опрос" onClick={() => setPolls((cur) => cur.filter((_, i) => i !== k))}>
                    <Trash2 size={16} />
                  </button>
                </div>
                {p.options.map((o, j) => (
                  <div key={j} className="poll-option">
                    <input className="input" value={o} maxLength={OPTION_MAX} onChange={(e) => editOption(k, j, e.target.value)} />
                    <span className="muted small poll-count">{o.length}/{OPTION_MAX}</span>
                    <button
                      className="icon-btn"
                      title="Убрать вариант"
                      disabled={p.options.length <= 2}
                      onClick={() => edit(k, { options: p.options.filter((_, i) => i !== j) })}
                    >
                      <X size={14} />
                    </button>
                  </div>
                ))}
                {p.options.length < OPTIONS_MAX && (
                  <button className="link-btn small poll-add" onClick={() => edit(k, { options: [...p.options, ""] })}>
                    <Plus size={14} /> вариант
                  </button>
                )}
              </div>
            ))}
          </div>
        </Card>
      )}

      {past.length > 0 && (
        <Card title="Уже выкладывали на канал" description="Новые опросы эти вопросы не повторяют." collapsible defaultOpen={false} id="polls-history">
          <div className="polls-past">
            {past.map((p, k) => (
              <div key={k} className="row gap-sm small">
                <span className="muted">{fmt(new Date(p.at))}</span>
                <span>{p.question}</span>
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
