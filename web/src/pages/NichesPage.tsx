import { useEffect, useMemo, useRef, useState } from "react";
import { ExternalLink, Flame, Search, Trash2, X } from "lucide-react";
import { useJobs, useToasts } from "../lib/store";
import { Badge, Button, Card, Disclosure, Field, NumberInput, PageHeader, Select } from "../components/ui";

type Niche = { name: string; why: string; keywords: string[] };
type Channel = {
  id: string; title: string; url: string; subs: number; videos: number; age_days: number;
  best_title: string; best_url: string; best_views: number; ratio: number; recent_median: number;
  recent_count: number; hit_share?: number; hit_count?: number; top_views?: number; per_week: number; avg_minutes: number; anomaly: boolean;
};
type Run = {
  id: string; niche: string; keywords?: string[]; subniches?: string[]; queries: string[]; at: number;
  filters?: { max_subs?: number };
  stats: { videos: number; channels: number; checked?: number; passed: number; dropped?: Record<string, number> };
  channels: Channel[];
};

const LANGS = [
  { value: "ru", label: "Русский" }, { value: "en", label: "Английский" }, { value: "es", label: "Испанский" },
  { value: "pt", label: "Португальский" }, { value: "de", label: "Немецкий" }, { value: "fr", label: "Французский" },
  { value: "it", label: "Итальянский" }, { value: "pl", label: "Польский" }, { value: "tr", label: "Турецкий" }, { value: "uk", label: "Украинский" },
];

const DROP_LABELS: Record<string, string> = {
  short: "короткие ролики (Shorts)",
  hidden: "скрыто число подписчиков",
  small_views: "мало просмотров у найденного ролика",
  big: "больше подписчиков, чем потолок",
  skew: "просмотры ниже числа подписчиков",
  unstable: "ни стабильных 30 тыс.+, ни залётов",
  low_ratio: "просмотры меньше чем ×3 к подписчикам, залётов нет",
  few_long: "мало длинных роликов",
  not_checked: "не дошла очередь разбора",
};

const MAX_SUBS_KEY = "niches-max-subs";
const MAX_SUBS_DEFAULT = 10000;

function savedMaxSubs(): number {
  try {
    const v = Number(localStorage.getItem(MAX_SUBS_KEY));
    return Number.isFinite(v) && v > 0 ? v : MAX_SUBS_DEFAULT;
  } catch {
    return MAX_SUBS_DEFAULT;
  }
}

const num = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)} млн` : n >= 1e4 ? `${Math.round(n / 1e3)} тыс.` : n.toLocaleString("ru-RU"));
const when = (ts: number) => new Date(ts * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

async function call<T>(method: string, url: string, body?: unknown): Promise<T> {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data as T;
}

function dropped(r: Run): string[] {
  return Object.entries(r.stats.dropped ?? {})
    .filter(([k, n]) => n > 0 && DROP_LABELS[k])
    .sort((a, b) => b[1] - a[1])
    .map(([k, n]) => `${DROP_LABELS[k]} — ${n}`);
}

/** Поиск ниш: ниша из списка или свои ключевые слова → каналы-ориентиры (небольшие, много просмотров: стабильно или залётами). */
export function NichesPage({ active }: { active: boolean }) {
  const { enqueue, jobs } = useJobs();
  const { push } = useToasts();
  const [language, setLanguage] = useState("ru");
  const [maxSubs, setMaxSubsState] = useState(savedMaxSubs);
  const setMaxSubs = (v: number) => {
    setMaxSubsState(v);
    try {
      localStorage.setItem(MAX_SUBS_KEY, String(v));
    } catch {
      /* без запоминания */
    }
  };
  const [evergreen, setEvergreen] = useState<Niche[]>([]);
  const [picked, setPicked] = useState("");
  const [keywords, setKeywords] = useState<string[]>([]);
  const [draft, setDraft] = useState("");
  const [runs, setRuns] = useState<Run[]>([]);
  const [shown, setShown] = useState("");
  const [busy, setBusy] = useState(false);

  const loadRuns = () =>
    void call<{ runs: Run[]; evergreen: Niche[] }>("GET", "/api/niches/runs")
      .then((r) => {
        setRuns(r.runs);
        setEvergreen(r.evergreen ?? []);
      })
      .catch(() => undefined);
  useEffect(() => {
    if (active) loadRuns();
  }, [active]);
  // поиск закончился — показать свежий результат
  const doneKey = jobs.filter((j) => j.kind === "niches" && j.status === "done").map((j) => j.id).join(",");
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    loadRuns();
    setShown("");
  }, [doneKey]);
  const searching = jobs.some((j) => j.kind === "niches" && (j.status === "running" || j.status === "queued"));

  const pick = (n: Niche) => {
    if (picked === n.name) {
      setPicked("");
      setKeywords([]);
      return;
    }
    setPicked(n.name);
    setKeywords([n.name, ...n.keywords]);
  };
  const addDraft = () => {
    const words = draft.split(/[,\n;]/).map((s) => s.trim()).filter(Boolean);
    if (!words.length) return;
    setKeywords((cur) => [...cur, ...words.filter((w) => !cur.includes(w))]);
    setDraft("");
  };
  const removeWord = (w: string) => {
    setKeywords((cur) => cur.filter((x) => x !== w));
  };

  const all = useMemo(() => {
    const extra = draft.split(/[,\n;]/).map((s) => s.trim()).filter(Boolean);
    return [...keywords, ...extra.filter((w) => !keywords.includes(w))];
  }, [keywords, draft]);

  const run = async () => {
    setBusy(true);
    await enqueue([{ kind: "niches", params: { niche: picked, keywords: all, filters: { language, max_subs: maxSubs } } }]);
    setDraft("");
    setBusy(false);
  };

  const res = runs.find((r) => r.id === shown) ?? runs[0];

  const removeRun = async (r: Run) => {
    try {
      setRuns((await call<{ runs: Run[] }>("DELETE", `/api/niches/runs/${encodeURIComponent(r.id)}`)).runs);
      setShown("");
    } catch (e) {
      push({ kind: "error", title: "Не удалилось", text: (e as Error).message });
    }
  };
  const removeChannel = async (r: Run, c: Channel) => {
    try {
      setRuns((await call<{ runs: Run[] }>("DELETE", `/api/niches/runs/${encodeURIComponent(r.id)}/channels/${encodeURIComponent(c.id)}`)).runs);
    } catch (e) {
      push({ kind: "error", title: "Не удалилось", text: (e as Error).message });
    }
  };

  return (
    <div className="page">
      <PageHeader
        title="Поиск ниш"
        subtitle="Каналы-ориентиры: небольшие каналы, ролики которых набирают десятки и сотни тысяч просмотров — в разы больше, чем у канала подписчиков. До 15 каналов — формат оцениваешь сам."
      />
      <div className="niche-layout">
        <div className="stack">
          <Card title="1. Что ищем" description="Выбери нишу из списка — её ключевые слова подставятся сами. Или впиши свои: по ним ищутся ролики, а по роликам — каналы.">
            <div className="stack">
              <div className="niche-words-row">
                <Field label="Ключевые слова" hint="Enter или запятая — добавить; мало слов — нейросеть добавит похожие">
                  <div className="niche-words">
                    {keywords.map((w) => (
                      <span key={w} className="chip active niche-word">
                        {w}
                        <button type="button" className="niche-word-x" title="Убрать" onClick={() => removeWord(w)}>
                          <X size={12} />
                        </button>
                      </span>
                    ))}
                    <input
                      className="niche-words-input"
                      value={draft}
                      placeholder={keywords.length ? "ещё слово…" : "например: судьба актрисы"}
                      onChange={(e) => setDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === ",") {
                          e.preventDefault();
                          addDraft();
                        } else if (e.key === "Backspace" && !draft && keywords.length) {
                          removeWord(keywords[keywords.length - 1]);
                        }
                      }}
                      onBlur={addDraft}
                    />
                  </div>
                </Field>
                <div className="niche-side">
                  <Field label="Язык аудитории">
                    <Select value={language} onChange={setLanguage} options={LANGS} />
                  </Field>
                  <Field label="Подписчиков до" hint={`с запасом: до ${Math.round(maxSubs * 1.15).toLocaleString("ru-RU")}`}>
                    <NumberInput value={maxSubs} min={100} max={10000000} step={1000} onChange={setMaxSubs} width={180} />
                  </Field>
                </div>
              </div>
              <Field label={`Вечнозелёные ниши (${evergreen.length})`}>
                <div className="niche-grid" role="radiogroup">
                  {evergreen.map((n) => (
                    <button
                      key={n.name}
                      type="button"
                      role="radio"
                      aria-checked={picked === n.name}
                      className={`provider ${picked === n.name ? "active" : ""}`}
                      onClick={() => pick(n)}
                    >
                      <span className="radio" />
                      <span className="provider-text">
                        <span className="provider-name">{n.name}</span>
                        <span className="muted small">{n.why}</span>
                      </span>
                    </button>
                  ))}
                </div>
              </Field>
            </div>
          </Card>

          <div className="niche-run">
            <span className="muted small action-plan">
              {all.length ? `Ищем: ${picked || all[0]} · ключевых слов: ${all.length}` : "Выбери нишу или впиши ключевые слова"}
            </span>
            <Button variant="primary" icon={<Search size={16} />} disabled={!all.length} loading={busy || searching} onClick={() => void run()}>
              {searching ? "Ищу…" : "Найти каналы"}
            </Button>
          </div>
          <Disclosure id="niche-rules" summary="Какие каналы подходят">
            <ul className="muted small niche-rules">
              <li>Небольшой канал: подписчиков не больше потолка (по умолчанию 10 тыс.; на 15% выше — ещё берём, вдвое — уже нет).</li>
              <li>Стабильно много просмотров: медиана последних длинных роликов — от 30 тыс. и минимум в 3 раза больше подписчиков.</li>
              <li>Или канал «стреляет»: за ~полгода минимум 2 ролика набрали от 50 тыс. — в 10+ раз больше подписчиков.</li>
              <li>Выше в списке — у кого больше просмотров на подписчика, больше залётов и меньше роликов на канале.</li>
              <li>Только длинные ролики (от 5 минут), Shorts не считаются. Возраст канала и сложность формата не учитываются.</li>
            </ul>
          </Disclosure>
        </div>

        <Card
          title="2. Каналы"
          className="niche-results"
          actions={
            res ? (
              <span className="row gap-sm">
                {runs.length > 1 && (
                  <select className="input niche-run-pick" value={res.id} onChange={(e) => setShown(e.target.value)}>
                    {runs.map((r) => (
                      <option key={r.id} value={r.id}>
                        {r.niche || r.subniches?.[0]} · {when(r.at)}
                      </option>
                    ))}
                  </select>
                )}
                <button type="button" className="icon-btn" title="Убрать этот поиск из истории" onClick={() => void removeRun(res)}>
                  <Trash2 size={16} />
                </button>
              </span>
            ) : undefined
          }
          description={
            res
              ? `«${res.niche || res.subniches?.[0]}»: роликов ${res.stats.videos}, каналов ${res.stats.channels}${res.stats.checked != null ? `, разобрано ${res.stats.checked}` : ""}, подошли ${res.stats.passed}`
              : "Здесь появятся найденные каналы."
          }
        >
          {res && res.channels.length === 0 && (
            <div className="muted small">
              {res.stats.videos === 0
                ? "YouTube ничего не нашёл по этим словам — возьми слова короче и шире."
                : res.stats.passed
                  ? "Все каналы этого поиска убраны из списка."
                  : "Подходящих каналов не нашлось — попробуй другие ключевые слова или соседнюю нишу."}
            </div>
          )}
          {res && dropped(res).length > 0 && <div className="muted small">Отсеяно: {dropped(res).join(" · ")}</div>}
          <div className="niche-list">
            {res?.channels.map((c) => (
              <div key={c.id} className="niche-item">
                <div className="niche-head">
                  <a className="an-name" href={c.url} target="_blank" rel="noreferrer">
                    {c.title} <ExternalLink size={13} />
                  </a>
                  <span className="row gap-sm">
                    {c.anomaly && <Badge tone="warn"><Flame size={12} /> аномалия</Badge>}
                    <button type="button" className="icon-btn" title="Убрать канал из списка" onClick={() => void removeChannel(res, c)}>
                      <X size={15} />
                    </button>
                  </span>
                </div>
                <div className="niche-metrics">
                  <span><b>{num(c.subs)}</b> подп.</span>
                  <span>медиана <b>{num(c.recent_median)}</b></span>
                  <span><b>×{c.ratio}</b> просм./подп.</span>
                  {c.hit_count != null ? (
                    <span title="Длинные ролики за ~полгода: от 50 тыс. просмотров и в 10+ раз больше подписчиков">
                      залётов <b>{c.hit_count}</b>{c.top_views ? <>, лучший <b>{num(c.top_views)}</b></> : null}
                    </span>
                  ) : (
                    c.hit_share != null && <span><b>{Math.round(c.hit_share * 100)}%</b> роликов 100 тыс.+</span>
                  )}
                  <span>канал <b>{c.age_days}</b> дн.</span>
                  <span><b>{c.videos}</b> роликов</span>
                  <span><b>{c.per_week}</b> в неделю</span>
                  <span><b>{c.avg_minutes}</b> мин</span>
                </div>
                {c.best_url && (
                  <a className="small niche-best" href={c.best_url} target="_blank" rel="noreferrer">
                    Лучший ролик: {c.best_title} — {num(c.best_views)} просм.
                  </a>
                )}
              </div>
            ))}
          </div>
          {res && res.queries.length > 0 && (
            <Disclosure id="niche-queries" summary={`Запросы (${res.queries.length})`}>
              <div className="muted small">{res.queries.join(" · ")}</div>
            </Disclosure>
          )}
        </Card>
      </div>
    </div>
  );
}
