import { useCallback, useEffect, useRef, useState } from "react";
import { Check, ExternalLink, Globe2, Trash2, Trophy } from "lucide-react";
import { useJobs, useToasts } from "../lib/store";
import { LANGS } from "../lib/langs";
import {
  Badge,
  Button,
  Card,
  Disclosure,
  Field,
  Progress,
  TextInput,
} from "./ui";

type Chan = {
  id: string;
  title: string;
  subs: number;
  hidden?: boolean;
  videos: number;
  views: number;
  best: number;
  best_title: string;
  url: string;
  best_url: string;
  ratio: number;
  best_minutes?: number;
  best_days?: number;
};
type LangResult = {
  language: string;
  queries: string[];
  found: number;
  on_topic: number;
  channels: number;
  winners: number;
  rivals: number;
  big_rivals: number;
  small_channels: number;
  strong_views: number;
  median_views: number;
  fresh: number;
  fresh_hits?: number;
  recent_wins?: number;
  minutes?: number;
  verdict: "best" | "ok" | "hard" | "empty" | "";
  reason: string;
  notes: string[];
  winner_list: Chan[];
  rival_list: Chan[];
  examples: Example[];
  fresh_examples?: Example[];
  error: string;
};
type Example = {
  title: string;
  views: number;
  channel: string;
  subs: number;
  url: string;
  minutes?: number;
  days?: number;
};
type Run = {
  id: string;
  at: number;
  languages: string[];
  source: {
    id: string;
    title: string;
    url: string;
    subs: number;
    language: string;
  };
  subniche: {
    subniche: string;
    broader: string;
    signs: string[];
    not_this: string[];
    given?: boolean;
    reused_at?: number;
  };
  results: LangResult[];
};

const URL_KEY = "competition-url";
const LANGS_KEY = "competition-langs";
const QUOTA_PER_LANG = 1500; // 5 запросов × 3 прохода поиска × 100 единиц (повторная проверка — из кэша)
// критерии — как в engine/research/competition.py
const SMALL_SUBS = 11500;
const FEW_RIVALS = 3;
const SOME_RIVALS = 8;

const VERDICT: Record<string, { label: string; tone: string }> = {
  best: { label: "Выгодно", tone: "ok" },
  ok: { label: "Можно попробовать", tone: "warn" },
  hard: { label: "Тяжело", tone: "bad" },
  empty: { label: "Мало данных", tone: "neutral" },
};

const langName = (code: string) =>
  LANGS.find((l) => l.code === code)?.name ?? code;
const num = (raw: number | undefined) => {
  const n = raw ?? 0;
  return n >= 1e6
    ? `${(n / 1e6).toFixed(1)} млн`
    : n >= 1e4
      ? `${Math.round(n / 1e3)} тыс.`
      : n.toLocaleString("ru-RU");
};
const when = (ts: number) =>
  new Date(ts * 1000).toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
const day = (ts: number) =>
  new Date(ts * 1000).toLocaleDateString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
  });

function read(key: string, fallback: string): string {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}
function write(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* без памяти браузера — просто не запомним */
  }
}

async function call<T>(method: string, url: string): Promise<T> {
  const r = await fetch(url, { method });
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data as T;
}

/** «Аналитика» → «Конкуренция по языкам»: на каком языке подниша канала-образца выгоднее —
 *  маленькие каналы на ней залетают, а серьёзных конкурентов мало. */
export function CompetitionPanel({ active }: { active: boolean }) {
  const { enqueue, jobs } = useJobs();
  const { push } = useToasts();
  const [url, setUrlState] = useState(() => read(URL_KEY, ""));
  const [subniche, setSubniche] = useState("");
  const [fresh, setFresh] = useState(false);
  const [langs, setLangsState] = useState<string[]>(() =>
    read(LANGS_KEY, "en,es,de")
      .split(",")
      .filter((c) => LANGS.some((l) => l.code === c)),
  );
  const [runs, setRuns] = useState<Run[]>([]);
  const [pick, setPick] = useState("");
  const job = [...jobs].reverse().find((j) => j.kind === "competition");
  const running = Boolean(
    job && (job.status === "running" || job.status === "queued"),
  );
  const lastStatus = useRef(job?.status);

  const setUrl = (v: string) => {
    setUrlState(v);
    write(URL_KEY, v);
  };
  const setLangs = (v: string[]) => {
    setLangsState(v);
    write(LANGS_KEY, v.join(","));
  };
  // порядок языков — как в списке, а не как нажимали
  const toggle = (code: string) =>
    setLangs(
      LANGS.map((l) => l.code).filter((c) =>
        c === code ? !langs.includes(c) : langs.includes(c),
      ),
    );

  const load = useCallback(() => {
    call<{ runs: Run[] }>("GET", "/api/competition/runs")
      .then((r) => setRuns(r.runs))
      .catch(() => undefined);
  }, []);
  useEffect(() => {
    if (active) load();
  }, [active, load]);
  useEffect(() => {
    // проверка закончилась — показать её результат
    if (lastStatus.current !== job?.status && job?.status === "done") {
      load();
      setPick("");
    }
    lastStatus.current = job?.status;
  }, [job?.status, load]);

  const run = runs.find((r) => r.id === pick) ?? runs[0];
  const start = async () => {
    const added = await enqueue([
      {
        kind: "competition",
        params: {
          url: url.trim(),
          languages: langs,
          subniche: subniche.trim(),
          fresh,
        },
      },
    ]);
    if (added) {
      setPick("");
      setFresh(false);
    }
  };
  const remove = async (id: string) => {
    try {
      setRuns(
        (await call<{ runs: Run[] }>("DELETE", `/api/competition/runs/${id}`))
          .runs,
      );
      if (pick === id) setPick("");
    } catch (e) {
      push({
        kind: "error",
        title: "Не удалилось",
        text: (e as Error).message,
      });
    }
  };

  return (
    <div className="stack">
      <Card
        title="Что проверяем"
        description="Ссылка на канал-образец и языки. Программа определит узкую поднишу канала и на каждом языке найдёт ролики именно этой подниши. Выгодный язык — где маленькие каналы на этой теме залетают, а серьёзных конкурентов не больше 2–3."
      >
        <div className="stack">
          <div className="cmp-form">
            <Field
              label="Канал или любой его ролик"
              hint="youtube.com/@имя, youtube.com/channel/UC… или ссылка на ролик"
            >
              <TextInput
                value={url}
                onChange={setUrl}
                placeholder="https://www.youtube.com/@имя"
              />
            </Field>
            <Field
              label="Подниша (необязательно)"
              hint="Программа определит сама. Если поймёт неточно — впиши, например «философы античности». Подниша запоминается для канала на 30 дней: повторные проверки сравнимы."
            >
              <TextInput
                value={subniche}
                onChange={setSubniche}
                placeholder="определю по каналу"
              />
            </Field>
          </div>
          <Field
            label={
              <span className="cmp-langs-head">
                Языки
                <span className="cmp-bulk">
                  <button
                    type="button"
                    className="link-btn small"
                    disabled={langs.length === LANGS.length}
                    onClick={() => setLangs(LANGS.map((l) => l.code))}
                  >
                    Все
                  </button>
                  <button
                    type="button"
                    className="link-btn small"
                    disabled={!langs.length}
                    onClick={() => setLangs([])}
                  >
                    Снять
                  </button>
                </span>
              </span>
            }
            hint={`Новый язык — около ${QUOTA_PER_LANG} единиц квоты YouTube API (один ключ — 10 000 в сутки), повторная проверка того же канала — почти бесплатно. Выбрано: ${langs.length}.`}
          >
            <div className="chips">
              {LANGS.map((l) => (
                <button
                  key={l.code}
                  type="button"
                  className={`chip ${langs.includes(l.code) ? "active" : ""}`}
                  onClick={() => toggle(l.code)}
                >
                  <Check size={13} className="chip-check" />
                  {l.name}
                </button>
              ))}
            </div>
          </Field>
          <div className="row gap-sm cmp-actions">
            <Button
              variant="primary"
              icon={<Globe2 size={16} />}
              disabled={!url.trim() || !langs.length || running}
              onClick={() => void start()}
            >
              {running
                ? "Проверяю…"
                : `Проверить ${langs.length ? `(${langs.length})` : ""}`}
            </Button>
            <label
              className="cmp-check small"
              title="Не брать поднишу и запросы из прошлой проверки этого канала"
            >
              <input
                type="checkbox"
                checked={fresh}
                onChange={(e) => setFresh(e.target.checked)}
              />
              Определить поднишу заново
            </label>
          </div>
          {running && job && (
            <div className="stack">
              <div className="muted small">
                {job.status === "queued" ? "Ждёт очереди…" : job.message}
              </div>
              <Progress value={job.progress} />
            </div>
          )}
          {!running && job?.status === "failed" && job.error && (
            <div className="notice warn small">{job.error}</div>
          )}
        </div>
      </Card>

      {runs.length > 1 && (
        <div className="chips cmp-history" aria-label="Прошлые проверки">
          {runs.map((r) => (
            <button
              key={r.id}
              type="button"
              className={`chip ${run?.id === r.id ? "active" : ""}`}
              onClick={() => setPick(r.id)}
              title={`${r.subniche.subniche} · ${r.languages.map(langName).join(", ")}`}
            >
              {r.source.title} · {when(r.at)}
            </button>
          ))}
        </div>
      )}

      {run ? (
        <RunView run={run} onRemove={() => void remove(run.id)} />
      ) : (
        !running && (
          <div className="muted small">
            Проверок пока не было. Вставь ссылку, отметь языки и нажми
            «Проверить».
          </div>
        )
      )}
    </div>
  );
}

function RunView({ run, onRemove }: { run: Run; onRemove: () => void }) {
  const ok = run.results.filter((r) => !r.error);
  const best = ok.filter((r) => r.verdict === "best");
  const next = ok.find((r) => r.verdict === "ok" || r.verdict === "hard");
  const sub = run.subniche;
  return (
    <Card>
      <div className="stack">
        <div className="niche-head">
          <div className="cmp-title">
            <a
              className="an-name"
              href={run.source.url}
              target="_blank"
              rel="noreferrer"
            >
              {run.source.title} <ExternalLink size={13} />
            </a>
            <span className="muted small">
              {num(run.source.subs)} подп. · проверено {when(run.at)}
            </span>
          </div>
          <button
            type="button"
            className="icon-btn"
            title="Убрать проверку из истории"
            onClick={onRemove}
          >
            <Trash2 size={15} />
          </button>
        </div>

        <div className="cmp-sub">
          <div>
            Подниша: <b>{sub.subniche}</b>
            {sub.given ? (
              <span className="muted small"> (задана вручную)</span>
            ) : null}
          </div>
          <div className="muted small">
            {sub.broader
              ? `Считаем только её, не всю нишу «${sub.broader}».`
              : "Считаем только её, не общую нишу."}
            {sub.reused_at
              ? ` Подниша и запросы — из проверки ${day(sub.reused_at)}: цифры сравнимы с ней.`
              : ""}
          </div>
          {(sub.signs.length > 0 || sub.not_this.length > 0) && (
            <Disclosure
              id={`cmp-sub-${run.id}`}
              summary="Какие ролики считались роликами подниши"
            >
              <div className="small stack">
                {sub.signs.length > 0 && (
                  <div>Берём: {sub.signs.join("; ")}.</div>
                )}
                {sub.not_this.length > 0 && (
                  <div className="muted">
                    Не берём: {sub.not_this.join("; ")}.
                  </div>
                )}
              </div>
            </Disclosure>
          )}
        </div>

        <div className={`cmp-answer ${best.length ? "is-best" : ""}`}>
          <Trophy size={20} />
          <div>
            {best.length ? (
              <>
                <div>
                  Выгоднее всего:{" "}
                  <b>{best.map((r) => langName(r.language)).join(", ")}</b>
                </div>
                <div className="small">{best[0].reason}.</div>
              </>
            ) : next ? (
              <>
                <div>
                  Явно выгодного языка нет. Лучше других:{" "}
                  <b>{langName(next.language)}</b>
                </div>
                <div className="small">{next.reason}.</div>
              </>
            ) : (
              <div>
                Ни на одном языке не набралось роликов подниши, чтобы сделать
                вывод.
              </div>
            )}
          </div>
        </div>

        <Disclosure id="cmp-rules" summary="Когда язык «выгодно»">
          <ul className="cmp-rules small">
            <li>
              <b>Маленькие каналы залетают</b> — хотя бы два канала до{" "}
              {num(SMALL_SUBS)} подписчиков, у которых ролик подниши набрал от
              20 тыс. просмотров и в 10+ раз больше, чем подписчиков.
            </li>
            <li>
              <b>Серьёзных конкурентов не больше {FEW_RIVALS}</b>. Конкурент —
              канал, который снимает эту поднишу (2+ ролика с заметными
              просмотрами за полгода), или крупный канал (100 тыс.+) с хитом на
              эту тему. Разовые ролики средних каналов и маленькие каналы — не
              конкуренты.
            </li>
            <li>
              <b>Тема жива сейчас</b> — свежий залёт за последние 2 месяца или
              ролик подниши набрал 100 тыс.+ за месяц: что выстрелило год назад,
              не факт, что зайдёт сейчас.
            </li>
          </ul>
        </Disclosure>

        <div className="niche-list">
          {run.results.map((r, i) => (
            <LangRow key={r.language} r={r} place={i + 1} runId={run.id} />
          ))}
        </div>
      </div>
    </Card>
  );
}

function rivalsTone(n: number) {
  return n <= FEW_RIVALS ? "ok" : n <= SOME_RIVALS ? "warn" : "bad";
}

function LangRow({
  r,
  place,
  runId,
}: {
  r: LangResult;
  place: number;
  runId: string;
}) {
  const v = VERDICT[r.verdict] ?? VERDICT.empty;
  const measured = !r.error && r.on_topic > 0;
  return (
    <div
      className={`niche-item cmp-row cmp-${r.error ? "error" : r.verdict || "empty"}`}
    >
      <div className="niche-head">
        <span className="cmp-lang">
          <span className="cmp-place">{place}</span>
          <b>{langName(r.language)}</b>
        </span>
        {r.error ? (
          <Badge tone="bad">ошибка</Badge>
        ) : (
          <Badge tone={v.tone}>{v.label}</Badge>
        )}
      </div>
      {r.error ? (
        <div className="muted small">{r.error}</div>
      ) : (
        <>
          {measured && (
            <div className="cmp-keys">
              <div
                className={`cmp-key tone-${r.winners ? "ok" : "neutral"}`}
                title="Маленькие каналы (до 11,5 тыс. подписчиков), у которых ролик подниши набрал 20 тыс.+ и в 10+ раз больше подписчиков. Свежие — за последние 2 месяца"
              >
                <b>
                  {r.winners}
                  {r.winners ? (
                    <small> · свежих {r.recent_wins ?? 0}</small>
                  ) : null}
                </b>
                <span>залёты маленьких</span>
              </div>
              <div
                className={`cmp-key tone-${rivalsTone(r.rivals)}`}
                title="Каналы, которые снимают эту поднишу, и крупные каналы с хитом на эту тему"
              >
                <b>
                  {r.rivals}
                  {r.big_rivals ? (
                    <small> · крупных {r.big_rivals}</small>
                  ) : null}
                </b>
                <span>конкуренты</span>
              </div>
              <div
                className="cmp-key"
                title="Медиана 20 самых просматриваемых роликов подниши и типичная длина ролика подниши"
              >
                <b>{num(r.strong_views)}</b>
                <span>
                  сильный ролик
                  {r.minutes ? ` · ~${Math.round(r.minutes)} мин` : ""}
                </span>
              </div>
              <div
                className={`cmp-key tone-${r.fresh_hits ? "ok" : "neutral"}`}
                title="Ролики подниши, которые набрали 100 тыс.+ за последний месяц — тема на подъёме сейчас"
              >
                <b>{r.fresh_hits ?? 0}</b>
                <span>хиты за месяц</span>
              </div>
            </div>
          )}
          <div className="small">
            {r.reason
              ? `${r.reason[0].toUpperCase()}${r.reason.slice(1)}.`
              : ""}
          </div>
          {r.notes.map((n) => (
            <div key={n} className="muted small">
              {n}
            </div>
          ))}
          {(r.winner_list.length > 0 ||
            r.rival_list.length > 0 ||
            r.queries.length > 0) && (
            <Disclosure
              id={`cmp-${runId}-${r.language}`}
              summary="Каналы, ролики и запросы"
            >
              <div className="cmp-details small">
                <ChanList
                  title={`Маленькие каналы, которые залетают (${r.winners})`}
                  empty="Нет: на этом языке маленькие каналы на теме пока не выстреливают."
                  list={r.winner_list}
                  win
                />
                <ChanList
                  title={`Конкуренты (${r.rivals})`}
                  empty="Нет: состоявшиеся каналы эту поднишу не снимают."
                  list={r.rival_list}
                />
                <Videos
                  title="Выстрелило за последний месяц"
                  empty="Свежих роликов подниши с заметными просмотрами нет."
                  list={r.fresh_examples ?? []}
                />
                <Videos
                  title="Самые смотрибельные ролики подниши за полгода"
                  empty=""
                  list={r.examples}
                />
                <div>
                  <div className="cmp-h">Как искали</div>
                  <div className="muted">{r.queries.join(" · ")}</div>
                  <div className="muted">
                    Роликов подниши {r.on_topic} из {r.found} найденных; каналов
                    с ними {r.channels}, из них маленьких {r.small_channels};
                    новых роликов за месяц {r.fresh}.
                  </div>
                </div>
              </div>
            </Disclosure>
          )}
        </>
      )}
    </div>
  );
}

function ChanList({
  title,
  empty,
  list,
  win,
}: {
  title: string;
  empty: string;
  list: Chan[];
  win?: boolean;
}) {
  return (
    <div>
      <div className="cmp-h">{title}</div>
      {list.length === 0 ? (
        <div className="muted">{empty}</div>
      ) : (
        list.map((c) => (
          <div key={c.id} className="cmp-item">
            <div className="cmp-line">
              <a href={c.url} target="_blank" rel="noreferrer">
                {c.title}
              </a>
              <span className="muted">
                {c.hidden ? "подп. скрыты" : `${num(c.subs)} подп.`}
                {!win ? ` · роликов ${c.videos}` : ""}
              </span>
            </div>
            {c.best_url && (
              <>
                <a
                  className="cmp-video"
                  href={c.best_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  {c.best_title}
                </a>
                <div className="cmp-meta">
                  {num(c.best)} просм.
                  {win && c.ratio ? ` · ×${c.ratio} к подписчикам` : ""}
                  {c.best_minutes ? ` · ${c.best_minutes} мин` : ""}
                  {c.best_days ? ` · ${ago(c.best_days)}` : ""}
                </div>
              </>
            )}
          </div>
        ))
      )}
    </div>
  );
}

function ago(days: number) {
  return days < 1
    ? "сегодня"
    : days < 60
      ? `${days} дн. назад`
      : `${Math.round(days / 30)} мес. назад`;
}

function Videos({
  title,
  empty,
  list,
}: {
  title: string;
  empty: string;
  list: Example[];
}) {
  if (!list.length && !empty) return null;
  return (
    <div>
      <div className="cmp-h">{title}</div>
      {list.length === 0 ? (
        <div className="muted">{empty}</div>
      ) : (
        list.map((e) => (
          <div key={e.url} className="cmp-item">
            <a
              className="cmp-video"
              href={e.url}
              target="_blank"
              rel="noreferrer"
            >
              {e.title}
            </a>
            <div className="cmp-meta">
              {num(e.views)} просм.
              {e.minutes ? ` · ${e.minutes} мин` : ""}
              {e.days ? ` · ${ago(e.days)}` : ""} · {e.channel} ({num(e.subs)}{" "}
              подп.)
            </div>
          </div>
        ))
      )}
    </div>
  );
}
