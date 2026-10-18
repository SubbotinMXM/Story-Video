import { useState } from "react";
import { Check, Pencil, RotateCcw, Save, Trash2, X } from "lucide-react";
import type { Meta, NamedListValue, Settings, TemplateKind, VideoTemplate } from "../lib/api";
import { useLoadedSettings, useToasts } from "../lib/store";
import { Button, Card, Select, TextInput } from "./ui";

/**
 * Шаблон оформления — именованный набор настроек ролика (интро/аутро, голова, субтитры,
 * подписка, копии, режим кадров, озвучка). Выбор шаблона заполняет поля страницы;
 * дальше их можно менять вручную. Шаблон необязателен.
 */

const MODE_TITLES: Record<string, string> = {
  theme: "по смыслу ролика",
  strict_3min: "строго первые 2 минуты",
  strict_full: "строго весь ролик",
};

function asList(v: unknown): NamedListValue {
  const raw = (v ?? {}) as Partial<NamedListValue>;
  return { items: Array.isArray(raw.items) ? raw.items : [], selected: raw.selected ?? "" };
}

function selectedId(list: NamedListValue) {
  return list.items.some((i) => i.id === list.selected) ? list.selected : list.items[0]?.id ?? "";
}

/** Выбор озвучки без ключей: выбранные голос/шаблон, скорость, языки перевода. */
function captureVoice(settings: Settings, meta: Meta): Record<string, unknown> {
  const spec = meta.tts_providers.find((p) => p.id === settings.tts_provider);
  const values = settings.tts[settings.tts_provider] ?? {};
  const out: Record<string, unknown> = {};
  for (const f of spec?.fields ?? []) {
    if (f.hidden || f.kind === "secret" || f.kind === "text") continue;
    const v = values[f.key];
    out[f.key] = f.kind === "named_list" || f.kind === "remote_list" ? selectedId(asList(v)) : v;
  }
  return out;
}

export function captureTemplate(settings: Settings, meta: Meta, kind: TemplateKind): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const key of meta.template_keys[kind]) {
    out[key] = key === "voice" ? captureVoice(settings, meta) : (settings as Record<string, unknown>)[key];
  }
  return out;
}

export function templatePatch(values: Record<string, unknown>, settings: Settings, meta: Meta): Partial<Settings> {
  const patch: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(values)) {
    if (key !== "voice") patch[key] = value;
  }
  const provider = String(values.tts_provider ?? settings.tts_provider);
  const voice = values.voice as Record<string, unknown> | undefined;
  const spec = meta.tts_providers.find((p) => p.id === provider);
  if (voice && spec) {
    const current = settings.tts[provider] ?? {};
    const next: Record<string, unknown> = { ...current };
    for (const f of spec.fields) {
      if (!(f.key in voice)) continue;
      next[f.key] =
        f.kind === "named_list" || f.kind === "remote_list"
          ? { ...asList(current[f.key]), selected: String(voice[f.key] ?? "") }
          : voice[f.key];
    }
    patch.tts = { ...settings.tts, [provider]: next };
  }
  return patch as Partial<Settings>;
}

function folder(path: unknown) {
  const p = String(path ?? "").trim();
  return p ? p.split("/").filter(Boolean).pop() ?? p : "нет";
}

/** Что внутри шаблона — одной строкой. */
function summary(t: VideoTemplate, meta: Meta, settings: Settings): string {
  const v = t.values;
  const parts: string[] = [];
  if (t.kind === "broll") {
    parts.push(`футажи: ${folder(v.broll_dir)}`, `голова: ${folder(v.head_dir)}`);
    parts.push(`интро: ${folder(v.intro_dir)}`, `аутро: ${folder(v.outro_dir)}`);
    if (String(v.overlay_text ?? "").trim()) parts.push(`надпись «${v.overlay_text}»`);
  } else {
    parts.push(`кадры: ${MODE_TITLES[String(v.media_mode)] ?? "по смыслу ролика"}`);
    parts.push(`интро: ${folder(v.slides_intro_dir)}`, `аутро: ${folder(v.slides_outro_dir)}`);
    parts.push(`музыка: ${String(v.music_track ?? "") || "нет"}`);
    if (v.ai_gen_enabled) parts.push(`AI-кадры: ${[v.ai_image_model, v.ai_video_model].filter(Boolean).join(" + ") || "—"}`);
  }
  parts.push(`субтитры: ${v.subtitles ? "да" : "нет"}`, `подписка: ${v.subscribe ? "да" : "нет"}`);
  parts.push(`копии: ${v.speed_copies ? v.speed_copies_count : "нет"}`);
  const provider = meta.tts_providers.find((p) => p.id === v.tts_provider);
  if (provider) {
    const voice = (v.voice ?? {}) as Record<string, unknown>;
    let label = provider.label;
    for (const f of provider.fields) {
      if (f.kind === "named_list" || f.kind === "remote_list") {
        const id = String(voice[f.key] ?? "");
        const name = asList(settings.tts[provider.id]?.[f.key]).items.find((i) => i.id === id)?.name;
        if (name) label += ` «${name}»`;
      }
      if (f.kind === "slider" && typeof voice[f.key] === "number") label += ` ${(voice[f.key] as number).toFixed(2)}×`;
      if (f.kind === "multi_choice" && Array.isArray(voice[f.key]) && (voice[f.key] as string[]).length)
        label += ` + ${(voice[f.key] as string[]).map((x) => x.toUpperCase()).join(", ")}`;
    }
    parts.push(`озвучка: ${label}`);
  }
  return parts.join(" · ");
}

function newId() {
  return `tpl-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

export function TemplateCard({ kind }: { kind: TemplateKind }) {
  const { settings, meta, update } = useLoadedSettings();
  const { push } = useToasts();
  const [naming, setNaming] = useState<"new" | "rename" | null>(null);
  const [name, setName] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);

  const mine = settings.templates.filter((t) => t.kind === kind);
  const activeId = settings.active_template[kind] ?? "";
  const active = mine.find((t) => t.id === activeId) ?? null;
  const current = captureTemplate(settings, meta, kind);
  const changed = active !== null && JSON.stringify(current) !== JSON.stringify(captureTemplate({ ...settings, ...templatePatch(active.values, settings, meta) }, meta, kind));

  const setActive = (id: string) => ({ active_template: { ...settings.active_template, [kind]: id } });

  const choose = (id: string) => {
    setConfirmDelete(false);
    setNaming(null);
    const t = mine.find((x) => x.id === id);
    if (!t) {
      update(setActive(""));
      return;
    }
    update({ ...templatePatch(t.values, settings, meta), ...setActive(t.id) });
    push({ kind: "ok", title: `Шаблон «${t.name}»: настройки заполнены` });
  };

  const saveName = () => {
    const clean = name.trim();
    if (!clean) {
      push({ kind: "error", title: "Дай шаблону название" });
      return;
    }
    if (naming === "rename" && active) {
      update({ templates: settings.templates.map((t) => (t.id === active.id ? { ...t, name: clean } : t)) });
    } else {
      const t: VideoTemplate = { id: newId(), name: clean, kind, values: current };
      update({ templates: [...settings.templates, t], ...setActive(t.id) });
      push({ kind: "ok", title: `Шаблон «${clean}» сохранён` });
    }
    setNaming(null);
    setName("");
  };

  const overwrite = () => {
    if (!active) return;
    update({ templates: settings.templates.map((t) => (t.id === active.id ? { ...t, values: current } : t)) });
    push({ kind: "ok", title: `Шаблон «${active.name}» обновлён` });
  };

  const remove = () => {
    if (!active) return;
    if (!confirmDelete) {
      setConfirmDelete(true);
      return;
    }
    update({ templates: settings.templates.filter((t) => t.id !== active.id), ...setActive("") });
    setConfirmDelete(false);
    push({ kind: "info", title: `Шаблон «${active.name}» удалён`, text: "Настройки на странице остались как были" });
  };

  const options = [
    { value: "", label: "Без шаблона — настраиваю вручную" },
    ...mine.map((t) => ({ value: t.id, label: t.name })),
  ];

  return (
    <Card
      title="Шаблон"
      description="Необязательно. Выбери шаблон — поля ниже заполнятся его настройками; дальше их можно менять как обычно."
    >
      <div className="stack-sm">
        <div className="row gap-sm template-row">
          <div style={{ flex: 1, minWidth: 0 }}>
            <Select value={active ? active.id : ""} onChange={choose} options={options} />
          </div>
          {active && (
            <>
              <Button
                variant="ghost"
                icon={<Pencil size={15} />}
                title="Переименовать"
                onClick={() => {
                  setNaming("rename");
                  setName(active.name);
                }}
              />
              <Button
                variant={confirmDelete ? "danger" : "ghost"}
                icon={<Trash2 size={15} />}
                title="Удалить шаблон"
                onClick={remove}
              >
                {confirmDelete ? "Точно удалить?" : undefined}
              </Button>
            </>
          )}
          <Button
            icon={<Save size={15} />}
            onClick={() => {
              setNaming("new");
              setName("");
            }}
            title="Запомнить текущие настройки страницы как новый шаблон"
          >
            Сохранить как шаблон
          </Button>
        </div>

        {naming && (
          <div className="row gap-sm">
            <div style={{ flex: 1, minWidth: 0 }}>
              <TextInput
                value={name}
                onChange={setName}
                placeholder={naming === "new" ? "Название, например «Рассказы с интро»" : "Новое название"}
              />
            </div>
            <Button variant="primary" icon={<Check size={15} />} onClick={saveName}>
              {naming === "new" ? "Сохранить" : "Переименовать"}
            </Button>
            <Button variant="ghost" icon={<X size={15} />} onClick={() => setNaming(null)} />
          </div>
        )}

        {active && <div className="muted small">{summary(active, meta, settings)}</div>}

        {active && changed && (
          <div className="notice warn small row gap-sm template-changed">
            <span style={{ flex: 1 }}>Настройки изменены после выбора шаблона — сам шаблон пока прежний.</span>
            <Button size="sm" icon={<Save size={14} />} onClick={overwrite}>
              Обновить шаблон
            </Button>
            <Button size="sm" variant="ghost" icon={<RotateCcw size={14} />} onClick={() => choose(active.id)}>
              Вернуть как в шаблоне
            </Button>
          </div>
        )}
      </div>
    </Card>
  );
}
