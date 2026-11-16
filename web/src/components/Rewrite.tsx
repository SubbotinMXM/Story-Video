import { useEffect, useState } from "react";
import { CheckCircle2, Copy, LogIn, LogOut, Plus, RefreshCw, RotateCcw, Trash2, UserRoundCog } from "lucide-react";
import { api, type RewritePrompt, type SubAction, type SubStatus } from "../lib/api";
import { useLoadedSettings, useToasts } from "../lib/store";
import { Badge, Button, Card, Field, Select, TextInput, Textarea, Toggle } from "./ui";

const VARIABLES = "{язык} — «русский», {языке} — «на русском языке» → «на {языке} языке», {слов} — объём «7000–8000»";
const STEPS_HINT = "Строка ===ШАГ=== делит промпт на шаги: текст до неё — отдельный разбор исходника (сохраняется в «разбор.txt»), после — рерайт с учётом этого разбора. Без неё — одно задание.";

function usePrompts() {
  const { settings, meta } = useLoadedSettings();
  return settings.rewrite_prompts?.length ? settings.rewrite_prompts : meta.rewrite_default_prompts;
}

function newId() {
  return `p-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
}

/** Настройки: способ переписывания, вход в Claude, библиотека промптов по разделам. */
export function RewriteSettingsCard() {
  const { settings, meta, update } = useLoadedSettings();
  const prompts = usePrompts();
  const [section, setSection] = useState("broll");
  const inSection = prompts.filter((p) => p.section === section);
  const [current, setCurrent] = useState(inSection[0]?.id ?? "");
  const prompt = prompts.find((p) => p.id === current) ?? inSection[0];
  const [codexModels, setCodexModels] = useState<{ id: string; label: string }[]>([]);

  useEffect(() => {
    if (!inSection.some((p) => p.id === current)) setCurrent(inSection[0]?.id ?? "");
  }, [section, inSection, current]);

  const save = (next: RewritePrompt[]) => update({ rewrite_prompts: next });
  const edit = (patch: Partial<RewritePrompt>) => prompt && save(prompts.map((p) => (p.id === prompt.id ? { ...p, ...patch } : p)));
  const add = (copy: boolean) => {
    const p: RewritePrompt = copy && prompt
      ? { ...prompt, id: newId(), name: `${prompt.name} (копия)` }
      : { id: newId(), name: "Новый промпт", section, text: "Перепиши текст на {языке} языке, примерно {слов} слов…" };
    save([...prompts, p]);
    setCurrent(p.id);
  };
  const remove = () => {
    if (!prompt || inSection.length <= 1) return;
    save(prompts.filter((p) => p.id !== prompt.id));
  };
  const missing = prompt
    ? [
        !/\{язык|\{языке|\{указать язык/.test(prompt.text) && "{язык}",
        !/\{слов\}|\{указать количество слов/.test(prompt.text) && "{слов}",
      ].filter(Boolean)
    : [];

  return (
    <Card title="Переписывание сценариев" description="Ролик YouTube по ссылке → уникальный текст по промпту раздела.">
      <div className="stack">
        <div className="muted small">
          Чем переписывать (Claude или Codex) выбирается прямо в разделах «Рассказы», «Стоковые слайды» и «Генерация по
          шагам» — в карточке «Исходник». Не смог выбранный — берётся второй, потом встроенная нейросеть (частями). Здесь —
          вход в аккаунты и модели.
        </div>
        <div className="grid grid-2">
          <Field
            label="Модель встроенной нейросети"
            hint={`Для рерайта и его проверки. Пусто — как в разделе LLM${meta.rewrite_auto_model ? ` (сейчас: ${meta.rewrite_auto_model})` : ""}. Список — от дешёвых к дорогим`}
          >
            <input
              className="input mono"
              list="rewrite-llm-models"
              value={settings.rewrite_llm_model ?? ""}
              placeholder={meta.rewrite_auto_model ? `как в LLM — ${meta.rewrite_auto_model}` : "как в разделе LLM"}
              onChange={(e) => update({ rewrite_llm_model: e.target.value })}
            />
            <datalist id="rewrite-llm-models">
              {meta.llm_models.map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          </Field>
          <Field label="Модель Claude" hint="Через Claude Code по подписке. Можно вписать и точное имя модели">
            <input
              className="input mono"
              list="claude-models"
              value={settings.claude_model ?? ""}
              placeholder="по умолчанию подписки"
              onChange={(e) => update({ claude_model: e.target.value })}
            />
            <datalist id="claude-models">
              {(meta.claude_models ?? []).filter((m) => m.id).map((m) => (
                <option key={m.id} value={m.id} label={m.label} />
              ))}
            </datalist>
          </Field>
          <Field label="Модель Codex" hint="Через Codex по подписке ChatGPT. Список — из Codex после входа">
            <input
              className="input mono"
              list="codex-models"
              value={settings.codex_model ?? ""}
              placeholder="по умолчанию Codex"
              onChange={(e) => update({ codex_model: e.target.value })}
            />
            <datalist id="codex-models">
              {codexModels.map((m) => (
                <option key={m.id} value={m.id} label={m.label} />
              ))}
            </datalist>
          </Field>
        </div>
        <Toggle
          checked={settings.rewrite_fallback}
          onChange={(v) => update({ rewrite_fallback: v })}
          label="Если у подписки кончился лимит — запасной вариант"
          description="Claude или Codex не смогли (лимит, нет входа) — текст пишет другая подписка, если в неё выполнен вход, иначе встроенная нейросеть. Оборвался на середине — начало остаётся, встроенная продолжает с места обрыва (те же имена и стиль). Пока лимит не снимется, следующие ролики сразу идут запасным путём."
        />
        <div className="notice small">
          Для всех задач программы порядок один: <b>Claude → Codex → встроенная нейросеть</b> (для рерайта с выбранным Codex — Codex → Claude → встроенная; разбор, проверка,
          тема, главы, названия, обложки, переводы). Только массовая оценка кадров «Стоков» — сразу встроенной.
          Подписка без входа или с исчерпанным лимитом пропускается, о каждой смене — уведомление.
        </div>
        <div className="grid grid-2">
          <SubAccount
            name="Claude"
            what="подписка Claude (Pro / Max)"
            status={api.claudeStatus}
            terminal={api.claudeTerminal}
            check={api.claudeCheck}
          />
          <SubAccount
            name="Codex"
            what="подписка ChatGPT (Plus / Pro)"
            status={api.codexStatus}
            terminal={api.codexTerminal}
            check={api.codexCheck}
            onModels={setCodexModels}
          />
        </div>

        <Field label="Промпты" hint={`Переменные: ${VARIABLES}. ${STEPS_HINT}`}>
          <div className="row gap-sm" style={{ flexWrap: "wrap" }}>
            <Select
              value={section}
              onChange={setSection}
              options={Object.entries(meta.rewrite_sections).map(([value, label]) => ({ value, label }))}
            />
            <div style={{ flex: 1, minWidth: 200 }}>
              <Select value={prompt?.id ?? ""} onChange={setCurrent} options={inSection.map((p) => ({ value: p.id, label: p.name }))} />
            </div>
            <Button size="sm" variant="ghost" icon={<Plus size={14} />} onClick={() => add(false)} title="Новый промпт" />
            <Button size="sm" variant="ghost" icon={<Copy size={14} />} onClick={() => add(true)} title="Копия" />
            <Button size="sm" variant="ghost" icon={<Trash2 size={14} />} onClick={remove} title="Удалить" disabled={inSection.length <= 1} />
          </div>
        </Field>
        {prompt && (
          <>
            <Field label="Название">
              <TextInput value={prompt.name} onChange={(v) => edit({ name: v })} />
            </Field>
            <Field label="Текст промпта">
              <Textarea value={prompt.text} onChange={(v) => edit({ text: v })} rows={14} />
            </Field>
            {missing.length > 0 && (
              <div className="notice warn small">
                В промпте нет {missing.join(" и ")}: язык и объём из интерфейса к нему не применятся.
              </div>
            )}
          </>
        )}
        {settings.rewrite_prompts?.length > 0 && (
          <div>
            <Button size="sm" variant="ghost" icon={<RotateCcw size={14} />} onClick={() => update({ rewrite_prompts: [] })}>
              Вернуть промпты по умолчанию
            </Button>
          </div>
        )}
      </div>
    </Card>
  );
}


const PLANS: Record<string, string> = { plus: "Plus", pro: "Pro", max: "Max", team: "Team", free: "Free", enterprise: "Enterprise" };

/** Аккаунт сервиса по подписке: чей вход, войти / сменить аккаунт / выйти, проверить. */
function SubAccount(props: {
  name: string;
  what: string;
  status: () => Promise<SubStatus>;
  terminal: (action?: SubAction) => Promise<{ ok: boolean }>;
  check: () => Promise<{ ok: boolean; message: string }>;
  onModels?: (m: { id: string; label: string }[]) => void;
}) {
  const { name, what, status, terminal, check, onModels } = props;
  const { push } = useToasts();
  const [st, setSt] = useState<SubStatus | null>(null);
  const [busy, setBusy] = useState("");

  const load = async () => {
    setBusy("load");
    try {
      const s = await status();
      setSt(s);
      if (s.models && onModels) onModels(s.models);
    } catch {
      setSt(null);
    } finally {
      setBusy("");
    }
  };
  useEffect(() => {
    void load();
    // вернулся из Терминала после входа — обновить, чей вход
    const onFocus = () => void load();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const act = async (action: SubAction) => {
    await terminal(action);
    if (action === "logout") {
      push({ kind: "ok", title: `${name}: вышел из аккаунта` });
      void load();
    } else {
      push({ kind: "ok", title: `${name}: открыл Терминал`, text: "Войди в браузере, потом вернись сюда — статус обновится сам." });
    }
  };
  const test = async () => {
    setBusy("check");
    try {
      const r = await check();
      push({ kind: r.ok ? "ok" : "error", title: r.ok ? `${name} работает` : `${name} не отвечает`, text: r.message });
    } finally {
      setBusy("");
    }
  };

  const plan = st?.plan ? PLANS[st.plan.toLowerCase()] ?? st.plan : "";
  const who = st?.logged_in ? [st.email || "вход выполнен", plan].filter(Boolean).join(" · ") : "";
  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="row gap-sm" style={{ flexWrap: "wrap", alignItems: "center" }}>
        <strong>{name}</strong>
        <Badge tone={!st ? "warn" : st.logged_in ? "ok" : "warn"}>
          {!st ? "…" : !st.installed ? "не установлен" : st.logged_in ? who : "вход не выполнен"}
        </Badge>
        <Button size="sm" variant="ghost" icon={<RefreshCw size={14} />} onClick={() => void load()} loading={busy === "load"} title="Обновить" />
      </div>
      <div className="row gap-sm" style={{ flexWrap: "wrap" }}>
        {!st?.installed ? (
          <Button size="sm" icon={<LogIn size={14} />} onClick={() => void act("login")}>
            Установить и войти
          </Button>
        ) : st.logged_in ? (
          <>
            <Button size="sm" icon={<UserRoundCog size={14} />} onClick={() => void act("switch")}>
              Сменить аккаунт
            </Button>
            <Button size="sm" variant="ghost" icon={<LogOut size={14} />} onClick={() => void act("logout")}>
              Выйти
            </Button>
            <Button size="sm" variant="ghost" icon={<CheckCircle2 size={14} />} onClick={() => void test()} loading={busy === "check"}>
              Проверить
            </Button>
          </>
        ) : (
          <Button size="sm" icon={<LogIn size={14} />} onClick={() => void act("login")}>
            Войти
          </Button>
        )}
      </div>
      <div className="muted small">
        {what}. Вход — в браузере: откроется Терминал и страница входа; пароль вводишь ты, программа его не видит.
      </div>
    </div>
  );
}
