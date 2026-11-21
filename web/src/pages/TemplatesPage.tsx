import { useCallback, useEffect, useMemo, useState } from "react";
import { Copy, LayoutTemplate, Pencil, Play, Sparkles, Trash2, Tv, Users } from "lucide-react";
import { Badge, Button, Card, Empty, Field, NumberInput, PageHeader, Select } from "../components/ui";
import { Modal } from "../components/Modal";
import { stepOptions } from "../components/PublishCard";
import { api, type PublishProfile, type Template, type TemplateSection } from "../lib/api";
import { goTo } from "../lib/nav";
import { useLoadedSettings, useProfiles, useToasts } from "../lib/store";
import { setEditingTemplate } from "../lib/templateEdit";

const SECTION: Record<TemplateSection, string> = { broll: "Рассказы", slides: "Стоковые слайды" };

const stepText = (n: number) => (n <= 1 ? "каждый день" : n === 2 ? "через день" : `раз в ${n} дн.`);
/** Читаемое имя канала из ссылки: «…/@Имя/videos» → «@Имя»; без «@» — последняя значащая часть адреса. */
const handleOf = (url: string) => {
  let raw = url;
  try {
    raw = decodeURIComponent(url);
  } catch {
    /* оставляем как есть */
  }
  const parts = raw.replace(/\/+$/, "").split("/");
  return parts.find((p) => p.startsWith("@")) ?? parts.filter((p) => !/^(videos|shorts|streams|featured|live|playlists)$/i.test(p)).pop() ?? url;
};
const ruDate = (iso: string) => iso.split("-").reverse().slice(0, 2).join(".");

/** Каналы выкладки шаблона: «профиль/канал» → читаемое название. Нет в списке профилей — показываем как есть. */
function channelNames(keys: string[], profiles: PublishProfile[]) {
  return keys.map((k) => {
    const [pid, cid] = k.split("/");
    const p = profiles.find((x) => x.id === pid);
    const c = p?.channels.find((x) => x.id === cid);
    return { key: k, channel: c ? `${c.name} ${c.handle}`.trim() : k, profile: p ? `№${p.number} ${p.name}` : "профиль не найден", known: Boolean(c) };
  });
}

/** Шаблоны запуска: «сделать N роликов» по сохранённым настройкам — без повторной настройки каждого запуска. */
export function TemplatesPage({ active }: { active: boolean }) {
  const [items, setItems] = useState<Template[] | null>(null);
  const [launching, setLaunching] = useState<Template | null>(null);
  const [renaming, setRenaming] = useState<Template | null>(null);
  const { profiles } = useProfiles();
  const { push } = useToasts();
  const { flush, replace } = useLoadedSettings();

  const load = useCallback(() => {
    api.templates().then(setItems).catch((e) => push({ kind: "error", title: "Шаблоны не загрузились", text: (e as Error).message }));
  }, [push]);
  useEffect(() => {
    if (active) load();
  }, [active, load]);

  const edit = async (t: Template) => {
    if (!window.confirm(`Загрузить шаблон «${t.name}» в настройки? Настройки раздела «${SECTION[t.section]}» заменятся настройками шаблона — после правок нажми «Сохранить в шаблон» вверху.`)) return;
    try {
      await flush();
      replace(await api.applyTemplate(t.id));
      setEditingTemplate({ id: t.id, name: t.name, section: t.section });
      goTo(t.section);
    } catch (e) {
      push({ kind: "error", title: "Не удалось загрузить шаблон", text: (e as Error).message });
    }
  };
  const copy = async (t: Template) => {
    try {
      await api.copyTemplate(t.id);
      load();
    } catch (e) {
      push({ kind: "error", title: "Не получилось", text: (e as Error).message });
    }
  };
  const remove = async (t: Template) => {
    if (!window.confirm(`Удалить шаблон «${t.name}»? Уже поставленные ролики это не затронет.`)) return;
    try {
      await api.deleteTemplate(t.id);
      load();
    } catch (e) {
      push({ kind: "error", title: "Не получилось", text: (e as Error).message });
    }
  };

  return (
    <div className="page">
      <PageHeader
        title="Шаблоны"
        subtitle="Сохранённые настройки под конкретные каналы: выбираешь шаблон, пишешь «сделать N роликов» — программа сама берёт ролики конкурентов, переписывает, собирает и выкладывает."
      />
      {items && items.length === 0 ? (
        <Card>
          <Empty
            icon={<LayoutTemplate size={28} />}
            title="Шаблонов пока нет"
            text={
              <>
                Настрой запуск как обычно на странице «Рассказы» или «Стоковые слайды»: <b>Источник → Ссылки на каналы</b>, озвучка, каналы выкладки,
                шаг между роликами. Потом нажми <b>«Сохранить как шаблон»</b> вверху страницы.
              </>
            }
          />
          <div className="tpl-empty-actions">
            <Button onClick={() => goTo("broll")}>Рассказы</Button>
            <Button onClick={() => goTo("slides")}>Стоковые слайды</Button>
          </div>
        </Card>
      ) : (
        <div className="tpl-grid">
          {(items ?? []).map((t) => {
            const names = channelNames(t.summary.channels, profiles);
            const profilesCount = new Set(t.summary.channels.map((k) => k.split("/")[0])).size;
            const problems = [
              t.summary.competitors.length === 0 ? "нет каналов-конкурентов" : "",
              t.summary.publish && t.summary.channels.length === 0 ? "не выбраны каналы выкладки" : "",
            ].filter(Boolean);
            return (
              <Card key={t.id} className="tpl-card">
                <div className="tpl-head">
                  <div className="tpl-title">{t.name}</div>
                  <Badge tone="accent">{SECTION[t.section]}</Badge>
                </div>
                <ul className="tpl-lines">
                  <li>
                    <Tv size={14} />
                    <span title={t.summary.competitors.map(handleOf).join(", ")}>
                      {t.summary.competitors.length
                        ? `Конкуренты: ${t.summary.competitors.map(handleOf).join(", ")}`
                        : "Каналы-конкуренты не заданы"}
                    </span>
                  </li>
                  <li>
                    <Users size={14} />
                    <span title={names.map((n) => `${n.channel} — ${n.profile}`).join("\n")}>
                      {t.summary.publish
                        ? t.summary.channels.length
                          ? `Выкладка: ${t.summary.channels.length} ${t.summary.channels.length === 1 ? "канал" : "канала"}, профилей ${profilesCount} · ${stepText(t.summary.step)}${t.summary.time ? ` в ${t.summary.time}` : ""}`
                          : "Выкладка включена, каналов нет"
                        : "Выкладка выключена"}
                    </span>
                  </li>
                  {t.section === "slides" && (
                    <li>
                      <Sparkles size={14} />
                      <span>{t.summary.ai ? "AI-кадры включены" : "Без AI-кадров"}</span>
                    </li>
                  )}
                </ul>
                {problems.length > 0 && (
                  <div className="tpl-problems">
                    {problems.map((p) => (
                      <Badge key={p} tone="warn">
                        {p}
                      </Badge>
                    ))}
                  </div>
                )}
                <div className="tpl-actions">
                  <Button variant="primary" icon={<Play size={14} />} onClick={() => setLaunching(t)} disabled={t.summary.competitors.length === 0}>
                    Запустить
                  </Button>
                  <Button size="sm" icon={<Pencil size={14} />} onClick={() => void edit(t)} title="Загрузить шаблон в настройки и поправить">
                    Изменить
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setRenaming(t)}>
                    Название
                  </Button>
                  <Button size="sm" variant="ghost" icon={<Copy size={14} />} onClick={() => void copy(t)} title="Копия шаблона" />
                  <Button size="sm" variant="ghost" icon={<Trash2 size={14} />} onClick={() => void remove(t)} title="Удалить шаблон" />
                </div>
              </Card>
            );
          })}
        </div>
      )}
      {launching && <LaunchDialog tpl={launching} profiles={profiles} onClose={() => setLaunching(null)} onDone={load} />}
      {renaming && <RenameDialog tpl={renaming} onClose={() => setRenaming(null)} onDone={load} />}
    </div>
  );
}

function RenameDialog({ tpl, onClose, onDone }: { tpl: Template; onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState(tpl.name);
  const [error, setError] = useState("");
  const save = async () => {
    try {
      await api.renameTemplate(tpl.id, name);
      onDone();
      onClose();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <Modal
      title="Название шаблона"
      onClose={onClose}
      width={460}
      footer={
        <Button variant="primary" disabled={!name.trim()} onClick={() => void save()}>
          Сохранить
        </Button>
      }
    >
      <input className="input" autoFocus maxLength={80} value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && void save()} />
      {error && <div className="badge badge-bad md-error">{error}</div>}
    </Modal>
  );
}

/** «Запустить»: сколько роликов, когда начинать, шаг, куда — на этот раз (можно сохранить в шаблон). */
function LaunchDialog({ tpl, profiles, onClose, onDone }: { tpl: Template; profiles: PublishProfile[]; onClose: () => void; onDone: () => void }) {
  const { push } = useToasts();
  const all = tpl.summary.channels;
  const names = useMemo(() => channelNames(all, profiles), [all, profiles]);
  const [count, setCount] = useState(5);
  const [startMode, setStartMode] = useState<"after" | "date">("after");
  const tomorrow = new Date(Date.now() + 86400000).toISOString().slice(0, 10);
  const [start, setStart] = useState(tomorrow);
  const [step, setStep] = useState(tpl.summary.step);
  const [picked, setPicked] = useState<string[]>(all);
  const [saveChannels, setSaveChannels] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const changed = picked.length !== all.length || picked.some((k) => !all.includes(k));
  const publish = tpl.summary.publish;
  const toggle = (k: string) => setPicked((p) => (p.includes(k) ? p.filter((x) => x !== k) : [...p, k]));
  const dates = useMemo(() => {
    if (startMode !== "date" || !start) return [];
    const base = new Date(`${start}T00:00:00Z`).getTime();
    return Array.from({ length: Math.min(count, 12) }, (_, i) => new Date(base + i * step * 86400000).toISOString().slice(0, 10));
  }, [startMode, start, step, count]);

  const run = async () => {
    setBusy(true);
    setError("");
    try {
      const r = await api.launchTemplate(tpl.id, {
        count,
        start: startMode === "date" ? start : "",
        step,
        ...(changed ? { channels: picked, save_channels: saveChannels } : {}),
      });
      const lines = r.channels.map((c) => (c.error ? `✕ ${handleOf(c.url)}: ${c.error}` : `${c.channel ?? handleOf(c.url)}: взято ${c.taken}, осталось ~${c.left}`));
      push({ kind: "ok", title: `В очереди: ${r.jobs.length} ${r.jobs.length === 1 ? "ролик" : "роликов"} по шаблону «${tpl.name}»`, text: [...r.notes, ...lines].join("\n") });
      onDone();
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={`Запустить «${tpl.name}»`}
      subtitle={`Ролики возьмутся с каналов-конкурентов шаблона${tpl.summary.competitors.length > 1 ? " по очереди" : ""}: ${tpl.summary.competitors.map(handleOf).join(", ")}`}
      onClose={onClose}
      width={640}
      footer={
        <>
          <span className="muted small md-foot-note">{publish ? `Выкладка: ${picked.length} ${picked.length === 1 ? "канал" : "канала"}` : "Выкладка в шаблоне выключена — ролики только соберутся"}</span>
          <Button variant="primary" icon={<Play size={14} />} loading={busy} disabled={count < 1 || (publish && picked.length === 0)} onClick={() => void run()}>
            Запустить {count} {count === 1 ? "ролик" : count < 5 ? "ролика" : "роликов"}
          </Button>
        </>
      }
    >
      <div className="md-grid">
        <Field label="Сколько роликов">
          <NumberInput value={count} onChange={setCount} min={1} max={50} />
        </Field>
        {publish && (
          <Field label="Шаг между роликами">
            <Select value={String(step)} onChange={(v) => setStep(Number(v))} options={stepOptions(step)} />
          </Field>
        )}
      </div>
      {publish && (
        <Field label="Когда выходят" hint="«После последнего запланированного» — на каждом канале ролик встанет за последним роликом канала с выбранным шагом.">
          <div className="row gap-sm wrap">
            <Select
              value={startMode}
              onChange={(v) => setStartMode(v as "after" | "date")}
              options={[
                { value: "after", label: "После последнего запланированного на канале" },
                { value: "date", label: "Начать с даты" },
              ]}
            />
            {startMode === "date" && <input className="input pub-date" type="date" min={new Date().toISOString().slice(0, 10)} value={start} onChange={(e) => setStart(e.target.value)} />}
          </div>
          {dates.length > 0 && (
            <div className="muted small md-dates">
              Выйдут: {dates.map(ruDate).join(", ")}
              {count > dates.length ? "…" : ""} (на каждом канале, время чуть разное)
            </div>
          )}
        </Field>
      )}
      {publish && (
        <Field label={`Каналы выкладки (${picked.length} из ${all.length})`} hint="Снимешь галочку — ролики на этот канал в этот раз не пойдут. Скорости копий подберутся сами под число каналов.">
          <div className="md-channels">
            {names.map((n) => (
              <label key={n.key} className="md-channel">
                <input type="checkbox" checked={picked.includes(n.key)} onChange={() => toggle(n.key)} />
                <span>
                  {n.channel}
                  <span className="muted small"> · {n.profile}</span>
                </span>
              </label>
            ))}
          </div>
          {changed && (
            <label className="md-channel md-save-channels">
              <input type="checkbox" checked={saveChannels} onChange={(e) => setSaveChannels(e.target.checked)} />
              <span>Запомнить такой выбор каналов в шаблоне</span>
            </label>
          )}
        </Field>
      )}
      {error && <div className="badge badge-bad md-error">{error}</div>}
    </Modal>
  );
}
