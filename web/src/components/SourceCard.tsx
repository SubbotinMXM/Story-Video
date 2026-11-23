import { useEffect, useState, type DragEvent, type ReactNode } from "react";
import { AudioLines, FilePlus2, FileText, Link2, Play, Plus, Trash2, Tv, Upload } from "lucide-react";
import { api, type ChannelStat, type JobIn } from "../lib/api";
import { useJobs, useLoadedSettings, useToasts } from "../lib/store";
import { Button, Card, Field, NumberInput, PathInput, Select, TextInput, Textarea, Toggle } from "./ui";

export type Section = "broll" | "slides";
type Kind = "text" | "audio" | "links" | "channels";

const KINDS: { id: Kind; label: string; text: string; icon: ReactNode }[] = [
  { id: "text", label: "Текст", text: "Готовый текст — сразу в ролик, или сначала рерайт по промпту", icon: <FileText size={16} /> },
  { id: "audio", label: "Аудио", text: "Готовая озвучка — без озвучки сервисом", icon: <AudioLines size={16} /> },
  { id: "links", label: "Ссылки на ролики", text: "Ролики YouTube → текст → рерайт → ролик", icon: <Link2 size={16} /> },
  { id: "channels", label: "Ссылки на каналы", text: "Самые залетевшие ролики конкурента → рерайт, без повторов", icon: <Tv size={16} /> },
];

const TEXT_EXT = /\.(txt|md|srt|vtt|text)$/i;
let seq = 0;
const nid = () => `s${Date.now().toString(36)}${(seq++).toString(36)}`;
const wordCount = (t: string) => (t.trim() ? t.trim().split(/\s+/).length : 0);

function readKind(section: Section): Kind {
  try {
    const v = localStorage.getItem(`source-${section}`);
    if (v && KINDS.some((k) => k.id === v)) return v as Kind;
  } catch {
    /* storage недоступен */
  }
  return "text";
}

type TextEntry = { id: string; text: string; name: string | null };

/** Исходник ролика: текст / аудио / ссылки на ролики / ссылки на каналы — выбор радиокнопкой, разворачивается
 *  только нужное. → карточка + число роликов + запуск (кнопка — в панели действий страницы). */
export function useSource(section: Section) {
  const { settings, meta, update } = useLoadedSettings();
  const { enqueue } = useJobs();
  const { push } = useToasts();
  const [kind, setKindState] = useState<Kind>(() => readKind(section));
  const [texts, setTexts] = useState<TextEntry[]>([{ id: nid(), text: "", name: null }]);
  const [audios, setAudios] = useState<{ id: string; path: string }[]>([{ id: nid(), path: "" }]);
  const [links, setLinks] = useState("");
  const [rewriteText, setRewriteText] = useState(false);
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [stats, setStats] = useState<ChannelStat[]>([]);

  const setKind = (k: Kind) => {
    setKindState(k);
    try {
      localStorage.setItem(`source-${section}`, k);
    } catch {
      /* storage недоступен */
    }
  };

  const channelText = settings.rewrite_channels?.[section] ?? "";
  const channelUrls = channelText.split(/[\s,]+/).map((s) => s.trim()).filter((s) => /youtube\.com|^@/.test(s));
  const perChannel = settings.rewrite_channels_count || 5;
  const linkUrls = [...new Set(links.split(/[\s,]+/).map((s) => s.trim()).filter((s) => /^https?:\/\//.test(s)))];
  const filledTexts = texts.filter((t) => t.text.trim());
  const filledAudios = audios.filter((a) => a.path.trim());

  useEffect(() => {
    if (kind === "channels") void api.rewriteChannelStats().then((r) => setStats(r.channels)).catch(() => undefined);
  }, [kind]);

  const prompts = (settings.rewrite_prompts?.length ? settings.rewrite_prompts : meta.rewrite_default_prompts).filter(
    (p) => p.section === section,
  );
  const promptId = prompts.some((p) => p.id === settings.rewrite_prompt?.[section])
    ? settings.rewrite_prompt[section]
    : prompts[0]?.id ?? "";
  const rewriting = kind === "links" || kind === "channels" || (kind === "text" && rewriteText);

  const count =
    kind === "text" ? filledTexts.length : kind === "audio" ? filledAudios.length : kind === "links" ? linkUrls.length : channelUrls.length ? perChannel : 0;

  // ---------- тексты ----------
  const addTexts = (list: { text: string; name: string | null }[]) => {
    const good = list.filter((t) => t.text.trim());
    if (!good.length) return;
    setTexts((prev) => {
      const kept = prev.filter((t) => t.text.trim());
      return [...kept, ...good.map((t) => ({ id: nid(), text: t.text.trim(), name: t.name }))];
    });
  };
  const onDrop = async (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    const files = Array.from(e.dataTransfer.files).filter((f) => TEXT_EXT.test(f.name));
    if (files.length) {
      addTexts(await Promise.all(files.map(async (f) => ({ text: await f.text(), name: f.name }))));
      push({ kind: "info", title: `Добавлено текстов: ${files.length}` });
    }
  };
  const loadFiles = async () => {
    const res = await api.pick("text", "Тексты", true);
    if (res.texts?.length) addTexts(res.texts.map((t) => ({ text: t.text, name: t.name })));
  };

  // ---------- запуск ----------
  const reset = () => {
    setTexts([{ id: nid(), text: "", name: null }]);
    setAudios([{ id: nid(), path: "" }]);
    setLinks("");
  };

  const start = async (videoKind: string) => {
    setBusy(videoKind);
    try {
      const common = {
        section,
        prompt_id: promptId,
        language: settings.rewrite_language,
        words: settings.rewrite_words,
        engine: settings.rewrite_engine,
        review: settings.rewrite_review,
      };
      if (kind === "channels") {
        const r = await api.rewriteChannels(channelUrls, perChannel, common);
        push({
          kind: "info",
          title: `В очереди: ${r.jobs.length}`,
          text: [...(r.notes ?? []), ...r.channels.map((c) => (c.error ? `✕ ${c.url}: ${c.error}` : `${c.channel}: взято ${c.taken}, осталось ~${c.left}`))].join("\n"),
        });
        void api.rewriteChannelStats().then((s) => setStats(s.channels)).catch(() => undefined);
        return;
      }
      let jobs: JobIn[];
      if (kind === "links") jobs = linkUrls.map((url) => ({ kind: "rewrite", params: { ...common, url } }));
      else if (kind === "text" && rewriteText)
        jobs = filledTexts.map((t, i) => ({ kind: "rewrite", params: { ...common, text: t.text, name: t.name || `Текст ${i + 1}` } }));
      else if (kind === "text") jobs = filledTexts.map((t) => ({ kind: videoKind, params: { text: t.text, source: t.name, audio: "" } }));
      else jobs = filledAudios.map((a) => ({ kind: videoKind, params: { text: "", source: null, audio: a.path } }));
      if (await enqueue(jobs)) reset();
    } catch (e) {
      push({ kind: "error", title: "Не запустилось", text: (e as Error).message });
    } finally {
      setBusy(null);
    }
  };

  // ---------- разметка ----------
  const body =
    kind === "text" ? (
      <div
        className={`stack dropzone-plain ${over ? "over" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={onDrop}
      >
        {texts.map((t, i) => (
          <div key={t.id} className="source-item">
            <div className="row gap-sm source-item-head">
              <span className="muted small">
                {t.name || `Текст ${i + 1}`} · {wordCount(t.text)} слов
              </span>
              {texts.length > 1 && (
                <button className="icon-btn" title="Убрать" onClick={() => setTexts((prev) => prev.filter((x) => x.id !== t.id))}>
                  <Trash2 size={15} />
                </button>
              )}
            </div>
            <Textarea
              value={t.text}
              onChange={(v) => setTexts((prev) => prev.map((x) => (x.id === t.id ? { ...x, text: v } : x)))}
              rows={texts.length > 1 ? 4 : 7}
              placeholder={rewriteText ? "Вставь текст или расшифровку с таймкодами — программа их уберёт…" : "Вставь текст рассказа…"}
            />
          </div>
        ))}
        {over && (
          <div className="dropzone-hint">
            <Upload size={22} /> Отпусти .txt — каждый файл станет отдельным роликом
          </div>
        )}
        <div className="row gap-sm wrap">
          <Button size="sm" icon={<Plus size={15} />} onClick={() => setTexts((prev) => [...prev, { id: nid(), text: "", name: null }])}>
            Ещё текст
          </Button>
          <Button size="sm" variant="ghost" icon={<FilePlus2 size={15} />} onClick={loadFiles}>
            Загрузить .txt
          </Button>
          <span className="muted small">Каждый текст — отдельный ролик. Можно перетащить сюда .txt файлы.</span>
        </div>
        <Toggle
          checked={rewriteText}
          onChange={setRewriteText}
          label="Сначала переписать по промпту (рерайт)"
          description="Например, расшифровка чужого ролика с таймкодами: программа уберёт таймкоды и перепишет текст по промпту раздела. Выключено — текст идёт в ролик как есть."
        />
      </div>
    ) : kind === "audio" ? (
      <div className="stack">
        {audios.map((a) => (
          <div key={a.id} className="row gap-sm">
            <div style={{ flex: 1 }}>
              <PathInput
                value={a.path}
                onChange={(v) => setAudios((prev) => prev.map((x) => (x.id === a.id ? { ...x, path: v } : x)))}
                kind="audio"
                title="Готовое аудио"
              />
            </div>
            {audios.length > 1 && (
              <button className="icon-btn" title="Убрать" onClick={() => setAudios((prev) => prev.filter((x) => x.id !== a.id))}>
                <Trash2 size={15} />
              </button>
            )}
          </div>
        ))}
        <div className="row gap-sm wrap">
          <Button size="sm" icon={<Plus size={15} />} onClick={() => setAudios((prev) => [...prev, { id: nid(), path: "" }])}>
            Ещё аудио
          </Button>
          <span className="muted small">Каждое аудио — отдельный ролик. Текст для субтитров и темы программа распознает сама.</span>
        </div>
      </div>
    ) : kind === "links" ? (
      <Field
        label="Ссылки на ролики YouTube"
        hint={linkUrls.length > 1 ? `Роликов: ${linkUrls.length} — каждый пройдёт полный цикл по очереди` : "По одной в строке: каждая пройдёт полный цикл по очереди. Уже переписанный ролик — программа спросит."}
      >
        <Textarea value={links} onChange={setLinks} rows={4} placeholder={"https://www.youtube.com/watch?v=…\nhttps://www.youtube.com/watch?v=…"} />
      </Field>
    ) : (
      <div className="stack">
        <Field
          label="Каналы конкурентов"
          hint="По одному в строке: youtube.com/@имя или youtube.com/channel/UC… Берутся самые залетевшие ролики со всех каналов вместе: скорость просмотров × отрыв от обычного уровня своего канала (200 тыс. за 2 дня сильнее 300 тыс. за месяц; старые ролики — тоже по скорости). Shorts, трансляции и короткие пропускаются. Уже переписанные не берутся."
        >
          <Textarea
            value={channelText}
            onChange={(v) => update({ rewrite_channels: { ...(settings.rewrite_channels || {}), [section]: v } })}
            rows={3}
            placeholder={"https://www.youtube.com/@канал1\nhttps://www.youtube.com/@канал2"}
          />
        </Field>
        <Field
          label="Сколько роликов взять"
          hint="Всего за запуск — со всех каналов вместе: с сильного канала может взяться больше, со слабого — ничего; сколько каналов в списке, не важно. Следующий запуск возьмёт следующие по рейтингу — без повторов."
        >
          <NumberInput value={perChannel} onChange={(v) => update({ rewrite_channels_count: v })} min={1} max={50} />
        </Field>
        {stats.length > 0 && (
          <div className="muted small">
            {stats.map((c) => (
              <div key={c.url}>
                <a href={c.url} target="_blank" rel="noreferrer" title={`Открыть канал: ${c.url}`}>
                  {c.title || c.url}
                </a>
                : переписано / в очереди {c.taken}, подходящих осталось ~{c.left}
              </div>
            ))}
          </div>
        )}
      </div>
    );

  const rewriteOptions = rewriting && (
    <div className="stack source-rewrite">
      <div className="grid grid-2">
        <Field label="Язык текста">
          <Select
            value={settings.rewrite_language}
            onChange={(v) => update({ rewrite_language: v })}
            options={[{ value: "", label: "Как в оригинале" }, ...meta.rewrite_languages.map((l) => ({ value: l.id, label: l.label }))]}
          />
        </Field>
        <Field label="Объём, слов">
          <TextInput value={settings.rewrite_words} onChange={(v) => update({ rewrite_words: v })} placeholder="7000–8000" />
        </Field>
        <Field label="Промпт" hint="Редактировать — Настройки → Переписывание">
          <Select
            value={promptId}
            onChange={(v) => update({ rewrite_prompt: { ...settings.rewrite_prompt, [section]: v } })}
            options={prompts.map((p) => ({ value: p.id, label: p.name }))}
          />
        </Field>
        <Field label="Чем переписывать" hint={settings.rewrite_engine === "codex" ? "Не смог — Claude, потом встроенная нейросеть" : "Не смог — Codex, потом встроенная нейросеть"}>
          <Select
            value={settings.rewrite_engine}
            onChange={(v) => update({ rewrite_engine: v })}
            options={Object.entries(meta.rewrite_engines).map(([value, label]) => ({ value, label }))}
          />
        </Field>
      </div>
      <Toggle
        checked={settings.rewrite_review}
        onChange={(v) => update({ rewrite_review: v })}
        label="Проверить текст перед сборкой"
        description={
          settings.rewrite_review
            ? "Готовый текст ляжет в папку «Рерайты» на рабочем столе: открой, поправь, сохрани и нажми «Собрать ролик» в очереди."
            : "Сразу после рерайта ролик собирается следующим в очереди. Удобно на ночь."
        }
      />
    </div>
  );

  const node = (
    <Card title="Исходник" description="Из чего делать ролик — выбери один вариант.">
      <div className="stack">
        <div className="source-picker" role="radiogroup">
          {KINDS.map((k) => (
            <button
              key={k.id}
              type="button"
              role="radio"
              aria-checked={kind === k.id}
              className={`provider ${kind === k.id ? "active" : ""}`}
              onClick={() => setKind(k.id)}
            >
              <span className="radio" />
              <span className="provider-text">
                <span className="provider-name">
                  {k.icon} {k.label}
                </span>
                <span className="muted small">{k.text}</span>
              </span>
            </button>
          ))}
        </div>
        {body}
        {rewriteOptions}
      </div>
    </Card>
  );

  // что произойдёт по «Пуск» — из выбранного исходника и настроек раздела
  const empty =
    kind === "text" ? "Вставь текст" : kind === "audio" ? "Выбери аудио" : kind === "links" ? "Вставь ссылки на ролики" : "Укажи каналы";
  const publishOn = Boolean(settings[`${section}_publish` as keyof typeof settings]);
  const publishTo = ((settings[`${section}_publish_channels` as keyof typeof settings] as string[] | undefined) ?? []).length;
  const engine = settings.rewrite_engine === "claude_code" ? "Claude" : settings.rewrite_engine === "codex" ? "Codex" : "встроенная нейросеть";
  const steps = [
    ...(rewriting ? [`рерайт (${engine})`, ...(settings.rewrite_review ? ["твоя проверка текста"] : [])] : []),
    kind === "audio" ? "твоя озвучка" : "озвучка",
    "ролик",
    publishOn ? `выкладка на ${publishTo} канал(а/ов)` : "без выкладки",
  ];
  const howMany = kind === "channels" ? `${count} ${count === 1 ? "ролик" : "роликов"} (по очереди с ${channelUrls.length} ${channelUrls.length === 1 ? "канала" : "каналов"})` : `роликов: ${count}`;
  const plan = count === 0 ? empty : `${howMany} · ${steps.join(" → ")}`;

  const bar = (
    <div className="action-bar">
      <span className="muted action-plan">{plan}</span>
      <Button
        variant="primary"
        size="lg"
        icon={<Play size={18} />}
        onClick={() => void start(section === "slides" ? "slides_full" : "broll")}
        loading={busy !== null}
        disabled={count === 0 || busy !== null}
      >
        Пуск
      </Button>
    </div>
  );

  const launch = () => void start(section === "slides" ? "slides_full" : "broll");
  const kindLabel = KINDS.find((k) => k.id === kind)?.label ?? "";
  return { node, bar, count, rewriting, kind, kindLabel, plan, launch, busy: busy !== null, perChannel, promptName: prompts.find((p) => p.id === promptId)?.name ?? "" };
}
