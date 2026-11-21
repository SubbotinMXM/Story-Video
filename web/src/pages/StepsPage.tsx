import { useEffect, useMemo, useState, type ReactNode } from "react";
import { AlertTriangle, ArrowLeft, ArrowRight, Check, Clapperboard, Images, Play, Save } from "lucide-react";
import { Button, Card, PageHeader } from "../components/ui";
import { SaveTemplateButton } from "../components/SaveTemplate";
import { useSource } from "../components/SourceCard";
import { VoiceTextsCard } from "../components/VoiceTextsCard";
import { VideoOptionsCard, VoiceCard } from "../components/SharedCards";
import { BrollFootageCard, BrollIntroCard, MediaModeCard, MEDIA_MODES, SlidesIntroCard } from "../components/SectionCards";
import { AiCard } from "../components/AiCard";
import { MixCard } from "../components/MixCard";
import { MusicCard } from "../components/MusicCard";
import { PublishCard, usePublishCount } from "../components/PublishCard";
import { useProvider, useTtsNeedsSetup } from "../components/TtsFields";
import { TemplateRows } from "../components/TemplateRows";
import { api, type MusicCatalog } from "../lib/api";
import { useLoadedSettings, useProfiles, useToasts } from "../lib/store";
import { setEditingTemplate, useEditingBase, useEditingTemplate } from "../lib/templateEdit";
import { musicName } from "../lib/music";
import { templateRows, type DiffRow, type DiffStep } from "../lib/templateDiff";

type Section = "broll" | "slides";
type StepId = "section" | "source" | "voice" | "texts" | "video" | "look" | "publish" | "summary";

const STEPS: { id: StepId; title: string; hint: string }[] = [
  { id: "section", title: "Что делаем", hint: "Рассказы или стоковые слайды" },
  { id: "source", title: "Исходник", hint: "Текст, аудио, ссылки или каналы" },
  { id: "voice", title: "Озвучка", hint: "Сервис, голос, языки" },
  { id: "texts", title: "Тексты в ролике", hint: "Призывы в начале, середине, конце" },
  { id: "video", title: "Видеоряд", hint: "Из чего собирается картинка" },
  { id: "look", title: "Оформление", hint: "Субтитры, подписка, копии, музыка" },
  { id: "publish", title: "Выкладка", hint: "Каналы, соавторы, обложки, даты" },
  { id: "summary", title: "Итог и пуск", hint: "Что получится и куда уйдёт" },
];

const SECTIONS: { id: Section; title: string; text: string; icon: ReactNode }[] = [
  {
    id: "broll",
    title: "Рассказы",
    text: "Озвучка + нарезка ваших футажей, голова диктора и надпись в углу. Обложки и названия — с компа или переделка конкурента.",
    icon: <Clapperboard size={22} />,
  },
  {
    id: "slides",
    title: "Стоковые слайды",
    text: "Озвучка + фото и видео со стоков по смыслу текста, AI-кадры, фоновая музыка. Обложка и название готовятся сами.",
    icon: <Images size={22} />,
  },
];

function stored<T extends string>(key: string, fallback: T, allowed: readonly T[]): T {
  try {
    const v = localStorage.getItem(key) as T | null;
    return v && allowed.includes(v) ? v : fallback;
  } catch {
    return fallback;
  }
}
function keep(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* storage недоступен */
  }
}

type Status = { ok: boolean; note: string };

/** «Генерация по шагам»: все настройки, которые влияют на ролик, — по порядку. Шаги можно проходить в любом
 *  порядке; в конце — сводка (что получится, куда уйдёт) и «Пуск». Настройки общие со страницами разделов. */
export function StepsPage() {
  const { settings, meta, update, flush } = useLoadedSettings();
  const { push } = useToasts();
  const editing = useEditingTemplate();
  const base = useEditingBase(editing);
  const [section, setSectionState] = useState<Section>(() => stored("steps-section", "broll", ["broll", "slides"]));
  const [step, setStepState] = useState<StepId>(() => stored("steps-step", "section", STEPS.map((s) => s.id)));
  const broll = useSource("broll");
  const slides = useSource("slides");
  const src = section === "slides" ? slides : broll;
  const publishCount = usePublishCount(section);
  const { spec, values } = useProvider();
  const ttsMissing = useTtsNeedsSetup();
  const { profiles } = useProfiles();
  const [music, setMusic] = useState<MusicCatalog | null>(null);

  const setSection = (s: Section) => {
    setSectionState(s);
    keep("steps-section", s);
  };
  const go = (s: StepId) => {
    setStepState(s);
    keep("steps-step", s);
    document.querySelector(".content")?.scrollTo({ top: 0 });
  };
  const index = STEPS.findIndex((s) => s.id === step);

  // «Изменить» в шаблонах: открываем шаблон с первого шага и его раздела
  const editId = editing?.id;
  useEffect(() => {
    if (!editing) return;
    setSection(editing.section);
    go("section");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editId]);

  useEffect(() => {
    if (section === "slides" && !music) void api.music().then(setMusic).catch(() => undefined);
  }, [section, music]);

  // ---------- что выбрано на каждом шаге (строка под шагом и сводка) ----------
  const publishOn = Boolean(settings[`${section}_publish`]);
  const channels = (settings[`${section}_publish_channels`] as string[]) ?? [];
  const coauthorsOn = (settings[`${section}_publish_coauthors_on`] as boolean | null) ?? false;
  const coauthors = coauthorsOn ? ((settings[`${section}_publish_coauthors`] as string[]) ?? []) : [];
  const dates = (settings[`${section}_publish_dates`] as string[]) ?? [];
  const time = settings[`${section}_publish_time`] as string;
  const pubStep = Math.max(1, Number(settings[`${section}_publish_step`]) || 1);
  const outDir = section === "slides" ? settings.slides_output_dir : settings.broll_output_dir;
  const engineName = settings.rewrite_engine === "claude_code" ? "Claude" : settings.rewrite_engine === "codex" ? "Codex" : "встроенная нейросеть";

  const voiceLine = useMemo(() => {
    const parts: string[] = [spec.label];
    for (const f of spec.fields) {
      if (!f.quick) continue;
      const v = values[f.key] as { items?: { id: string; name: string }[]; selected?: string } | string[] | undefined;
      if (f.kind === "named_list" || f.kind === "remote_list") {
        const list = (v as { items?: { id: string; name: string }[]; selected?: string }) ?? {};
        const name = list.items?.find((i) => i.id === list.selected)?.name;
        if (name) parts.push(`${f.label.toLowerCase()} «${name}»`);
      } else if (f.kind === "multi_choice" && Array.isArray(v) && v.length) {
        const names = v.map((code) => f.options.find((o) => o[0] === code)?.[1] ?? code);
        parts.push(`+ языки: ${names.join(", ")}`);
      }
    }
    return parts.join(" · ");
  }, [spec, values]);
  const extraLangs = (spec.fields.find((f) => f.kind === "multi_choice" && f.quick)
    ? (values[spec.fields.find((f) => f.kind === "multi_choice" && f.quick)!.key] as string[] | undefined) ?? []
    : []).length;

  const texts = (["start", "mid", "end"] as const).filter((k) => String(settings[`${section}_text_${k}`] ?? "").trim()).length;
  const mode = MEDIA_MODES.find((m) => m.id === settings.media_mode);
  const copies = publishOn ? publishCount : settings.speed_copies ? settings.speed_copies_count : 0;

  const status: Record<StepId, Status> = {
    section: { ok: true, note: section === "slides" ? "Стоковые слайды" : "Рассказы" },
    source: src.count > 0
      ? { ok: true, note: `${src.kindLabel} · ${src.plan}` }
      : { ok: false, note: `${src.kindLabel}: пока пусто` },
    voice: src.kind === "audio"
      ? { ok: true, note: "своя озвучка (аудио) — сервис не нужен" }
      : ttsMissing ? { ok: false, note: `${spec.label}: не всё настроено (Настройки → Озвучка)` } : { ok: true, note: voiceLine },
    texts: { ok: true, note: texts ? `вставок: ${texts} из 3` : "без своих вставок" },
    video: section === "broll"
      ? settings.broll_dir.trim()
        ? { ok: true, note: `футажи: ${short(settings.broll_dir)}${settings.head_dir ? " · голова диктора" : ""}${settings.overlay_text ? ` · надпись «${settings.overlay_text}»` : ""}` }
        : { ok: false, note: "не выбрана папка футажей" }
      : { ok: true, note: `${mode?.title ?? "подбор кадров"}${settings.ai_gen_enabled ? " · AI-кадры" : ""}` },
    look: settings.subscribe && !settings.subscribe_path.trim()
      ? { ok: false, note: "включён призыв подписаться, но не выбрана анимация" }
      : {
          ok: true,
          note: [
            settings.subtitles ? "субтитры" : "без субтитров",
            settings.subscribe ? "призыв подписаться" : "",
            copies ? `копий скорости: ${copies}` : "",
            outDir ? `в ${short(outDir)}` : "на рабочий стол",
          ].filter(Boolean).join(" · "),
        },
    publish: !publishOn
      ? { ok: true, note: "без выкладки — ролики только на компьютер" }
      : channels.length
        ? { ok: true, note: `каналов: ${channels.length}${coauthors.length ? ` · соавторов: ${coauthors.length}` : ""}` }
        : { ok: false, note: "выкладка включена, но не выбран ни один канал" },
    summary: { ok: true, note: "" },
  };
  const problems = STEPS.filter((s) => !status[s.id].ok);

  const names = useMemo(() => {
    const out = new Map<string, string>();
    for (const p of profiles) for (const c of p.channels) out.set(`${p.id}/${c.id}`, `${c.name} ${c.handle}`.trim());
    return out;
  }, [profiles]);

  // ---------- правка шаблона: что было в шаблоне и что стало ----------
  const rows: DiffRow[] = useMemo(
    () => (editing && base ? templateRows(base, settings, { section, meta, names, music, modeTitle: (id) => MEDIA_MODES.find((m) => m.id === id)?.title ?? id }) : []),
    [editing, base, settings, section, meta, names, music],
  );
  const changedRows = rows.filter((r) => r.changed);
  const rowsOf = (id: StepId) => rows.filter((r) => r.step === (id as DiffStep));
  const changedOf = (id: StepId) => (id === "summary" ? changedRows.length : rowsOf(id).filter((r) => r.changed).length);
  const revert = (r: DiffRow) => update(r.revert() as never);
  const stepOfRow = (r: DiffRow) => STEPS.find((s) => s.id === (r.step as string)) ?? STEPS[0];
  const saveTemplate = async () => {
    if (!editing) return;
    try {
      await flush();
      await api.saveTemplate(editing.id);
      push({ kind: "ok", title: `Шаблон «${editing.name}» обновлён`, text: "Новые запуски пойдут с этими настройками" });
      setEditingTemplate(null);
    } catch (e) {
      push({ kind: "error", title: "Шаблон не сохранился", text: (e as Error).message });
    }
  };

  // ---------- шаги ----------
  const body: Record<StepId, ReactNode> = {
    section: (
      <Card title="Что делаем" description="От этого зависят остальные шаги: видеоряд, оформление и выкладка у разделов разные.">
        <div className="steps-choice" role="radiogroup">
          {SECTIONS.map((s) => (
            <button
              key={s.id}
              type="button"
              role="radio"
              aria-checked={section === s.id}
              className={`provider steps-choice-item ${section === s.id ? "active" : ""}`}
              disabled={Boolean(editing) && editing?.section !== s.id}
              title={editing && editing.section !== s.id ? "У шаблона раздел не меняется" : undefined}
              onClick={() => setSection(s.id)}
            >
              <span className="radio" />
              <span className="steps-choice-icon">{s.icon}</span>
              <span className="provider-text">
                <span className="provider-name">{s.title}</span>
                <span className="muted small">{s.text}</span>
              </span>
            </button>
          ))}
        </div>
      </Card>
    ),
    source: src.node,
    voice:
      src.kind === "audio" ? (
        <Card title="Озвучка">
          <div className="notice small">Исходник — готовое аудио: озвучка сервисом не нужна, этот шаг можно пропустить.</div>
        </Card>
      ) : (
        <VoiceCard />
      ),
    texts: <VoiceTextsCard section={section} />,
    video:
      section === "broll" ? (
        <BrollFootageCard />
      ) : (
        <div className="stack-lg">
          <MediaModeCard />
          <div className="page-cards-2">
            <AiCard />
            <MixCard />
          </div>
        </div>
      ),
    look: (
      <div className="page-cards-2">
        <VideoOptionsCard
          copiesNote={
            publishOn ? `Копий будет ${publishCount} — по числу каналов для выкладки (своя настройка копий не действует)` : undefined
          }
        />
        {section === "slides" ? (
          <div className="col-stack">
            <MusicCard />
            <SlidesIntroCard />
          </div>
        ) : (
          <BrollIntroCard />
        )}
      </div>
    ),
    publish: <PublishCard section={section} />,
    summary: (
      <div className="stack-lg">
        {problems.length > 0 && (
          <div className="notice warn">
            <b>Проверь перед пуском:</b>
            <ul className="steps-problems">
              {problems.map((s) => (
                <li key={s.id}>
                  {status[s.id].note} —{" "}
                  <button type="button" className="link-btn" onClick={() => go(s.id)}>
                    шаг «{s.title}»
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
        {editing && changedRows.length === 0 && (
          <div className="notice small">
            Шаблон «{editing.name}» пока без изменений — настройки такие же, как были сохранены. Поменяй нужное на шагах слева.
          </div>
        )}
        {editing && changedRows.length > 0 && (
          <TemplateRows
            name={editing.name}
            title={`Что изменится в шаблоне «${editing.name}»`}
            rows={changedRows}
            onRevert={revert}
            onGo={(r) => go(stepOfRow(r).id)}
            stepTitle={(r) => `шаг «${stepOfRow(r).title}»`}
          />
        )}
        <Card title="Что получится">
          <div className="steps-summary">
            <Row label="Раздел" value={status.section.note} onEdit={() => go("section")} />
            <Row
              label="Роликов"
              value={
                src.count === 0
                  ? "исходник пуст"
                  : `${src.kind === "channels" ? `${src.count} (по очереди с каналов-доноров)` : src.count}` +
                    (extraLangs && src.kind !== "audio" ? ` + по ${extraLangs} на других языках` : "") +
                    (copies ? ` · у каждого копий скорости: ${copies}` : "")
              }
              warn={src.count === 0}
              onEdit={() => go("source")}
            />
            <Row
              label="Исходник"
              value={
                src.kind === "audio"
                  ? "готовое аудио"
                  : src.rewriting
                    ? `${src.kindLabel} → рерайт: ${engineName}, промпт «${src.promptName}», ` +
                      `${settings.rewrite_language ? meta.rewrite_languages.find((l) => l.id === settings.rewrite_language)?.label ?? settings.rewrite_language : "язык оригинала"}, ${settings.rewrite_words} слов`
                    : `${src.kindLabel} — идёт в ролик как есть`
              }
              onEdit={() => go("source")}
            />
            <Row label="Озвучка" value={status.voice.note} warn={!status.voice.ok} onEdit={() => go("voice")} />
            <Row label="Вставки" value={status.texts.note} onEdit={() => go("texts")} />
            <Row label="Видеоряд" value={status.video.note} warn={!status.video.ok} onEdit={() => go("video")} />
            {section === "slides" && (
              <Row
                label="Музыка"
                value={musicName(settings.music_track, music)}
                onEdit={() => go("look")}
              />
            )}
            <Row label="Оформление" value={status.look.note} warn={!status.look.ok} onEdit={() => go("look")} />
            <Row
              label="Выкладка"
              value={
                !publishOn
                  ? "не выкладываем — ролики остаются на компьютере"
                  : channels.length
                    ? channels.map((k) => names.get(k) ?? k).join(", ")
                    : "не выбран ни один канал"
              }
              warn={!status.publish.ok}
              onEdit={() => go("publish")}
            />
            {publishOn && (
              <>
                <Row
                  label="Соавторы"
                  value={coauthors.length ? coauthors.map((k) => names.get(k) ?? k).join(", ") : "без соавторов"}
                  onEdit={() => go("publish")}
                />
                <Row
                  label="Обложка и название"
                  value={
                    section === "slides"
                      ? settings.slides_publish_cover ? "генерируются по оригиналу (или по тексту)" : "название готовится, обложку ставишь сам"
                      : settings.broll_publish_materials === "competitor" ? "переделка конкурента" : "с компа: папка обложек и файл названий"
                  }
                  onEdit={() => go("publish")}
                />
                <Row
                  label="Когда выходят"
                  value={`${dates.length ? `даты: ${dates.map((d) => d.split("-").reverse().join(".")).join(", ")}` : pubStep === 1 ? "каждый день после последнего запланированного" : pubStep === 2 ? "через день после последнего запланированного" : `раз в ${pubStep} дн. после последнего запланированного`}` +
                    ` · ${time ? `в ${time} МСК` : "время случайное 16:00–20:00 МСК"}`}
                  onEdit={() => go("publish")}
                />
              </>
            )}
          </div>
        </Card>
      </div>
    ),
  };

  const cur = STEPS[index];
  return (
    <div className="page">
      <PageHeader
        title={editing ? `Правка шаблона «${editing.name}»` : "Генерация по шагам"}
        subtitle={editing ? "Настройки загружены из шаблона — так он был сохранён. Меняй по шагам: у каждой правки видно «было → стало». В конце — «Сохранить в шаблон»." : "Все настройки ролика по порядку — ничего не пропустить. Шаги можно проходить в любом порядке."}
        actions={editing ? <Button variant="primary" icon={<Save size={16} />} onClick={() => void saveTemplate()}>Сохранить в шаблон</Button> : <SaveTemplateButton section={section} />}
      />
      <ol className="steps-rail">
        {STEPS.map((s, i) => {
          const st = status[s.id];
          return (
            <li key={s.id}>
              <button
                type="button"
                className={`steps-rail-item ${s.id === step ? "active" : ""} ${i < index ? "past" : ""}`}
                onClick={() => go(s.id)}
                title={st.note || s.hint}
              >
                <span className={`steps-num ${st.ok ? "" : "warn"}`}>
                  {s.id === "summary" ? i + 1 : st.ok && i < index ? <Check size={13} /> : !st.ok ? <AlertTriangle size={13} /> : i + 1}
                </span>
                <span className="steps-rail-text">
                  <span className="steps-rail-title">{s.title}</span>
                  <span className="steps-rail-hint">{s.hint}</span>
                </span>
                {editing && changedOf(s.id) > 0 && <span className="steps-rail-changed" title="Изменений относительно шаблона">{changedOf(s.id)}</span>}
              </button>
            </li>
          );
        })}
      </ol>

      <div className="steps-body">
        {body[step]}
        {editing && step !== "summary" && step !== "section" && (
          <TemplateRows name={editing.name} rows={rowsOf(step)} onRevert={revert} />
        )}
        {step === "section" && (
          <Card title="Сейчас выбрано по шагам" description="Короткая сводка по всем шагам — нажми на строку, чтобы перейти к шагу.">
            <div className="steps-summary">
              {STEPS.filter((s) => s.id !== "section" && s.id !== "summary").map((s) => (
                <Row key={s.id} label={s.title} value={status[s.id].note} warn={!status[s.id].ok} onEdit={() => go(s.id)} />
              ))}
            </div>
          </Card>
        )}
        {step !== "summary" && step !== "section" && status[step].note && (
          <div className={`steps-chosen ${status[step].ok ? "" : "warn"}`}>
            {status[step].ok ? <Check size={14} /> : <AlertTriangle size={14} />}
            <span>
              <b>Выбрано:</b> {status[step].note}
            </span>
          </div>
        )}
      </div>

      <div className="action-bar steps-bar">
        <Button variant="ghost" icon={<ArrowLeft size={16} />} disabled={index === 0} onClick={() => go(STEPS[index - 1].id)}>
          Назад
        </Button>
        <span className="muted action-plan">
          Шаг {index + 1} из {STEPS.length} · {cur.title}
          {step === "summary" ? ` · ${src.plan}` : ""}
        </span>
        {step === "summary" && editing ? (
          <Button variant="primary" size="lg" icon={<Save size={18} />} onClick={() => void saveTemplate()}>
            Сохранить в шаблон
          </Button>
        ) : step === "summary" ? (
          <Button
            variant="primary"
            size="lg"
            icon={<Play size={18} />}
            loading={src.busy}
            disabled={src.count === 0 || src.busy}
            onClick={src.launch}
          >
            Пуск
          </Button>
        ) : (
          <Button variant="primary" icon={<ArrowRight size={16} />} onClick={() => go(STEPS[index + 1].id)}>
            Далее: {STEPS[index + 1].title}
          </Button>
        )}
      </div>
    </div>
  );
}

function short(path: string) {
  const parts = path.split("/").filter(Boolean);
  return parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : path;
}

function Row({ label, value, warn, onEdit }: { label: string; value: string; warn?: boolean; onEdit: () => void }) {
  return (
    <div className={`steps-row ${warn ? "warn" : ""}`}>
      <span className="steps-row-label">{label}</span>
      <span className="steps-row-value">
        {warn && <AlertTriangle size={14} />} {value}
      </span>
      <button type="button" className="link-btn small" onClick={onEdit}>
        изменить
      </button>
    </div>
  );
}
