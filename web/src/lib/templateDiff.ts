import type { Meta, Settings, TemplateSection } from "./api";

/** Правка шаблона по шагам: что было в шаблоне и что стало сейчас, по каждой настройке. */
export type DiffStep = "source" | "voice" | "texts" | "video" | "look" | "publish";

type Ctx = {
  section: TemplateSection;
  settings: Settings;
  meta: Meta;
  names: Map<string, string>; // «профиль/канал» → читаемое имя
  modeTitle: (id: string) => string;
};

type Def = {
  key: string;
  step: DiffStep;
  label: string;
  /** словарь «по разделам»: берём запись своего раздела */
  per?: boolean;
  fmt?: (v: unknown, ctx: Ctx) => string;
};

export type DiffRow = {
  id: string;
  step: DiffStep;
  label: string;
  before: string;
  after: string;
  changed: boolean;
  revert: () => Record<string, unknown>;
};

const clip = (s: string, n = 160) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);
const onOff = (v: unknown) => (v ? "включено" : "выключено");
const text = (v: unknown) => {
  const s = String(v ?? "").trim();
  return s ? clip(s.replace(/\s+/g, " ")) : "не задано";
};
const path = (v: unknown) => {
  const s = String(v ?? "").trim();
  return s ? s : "не выбрано";
};
const num = (v: unknown) => String(v ?? "—");
const handleOf = (url: string) => {
  let raw = url;
  try {
    raw = decodeURIComponent(url);
  } catch {
    /* как есть */
  }
  const parts = raw.replace(/\/+$/, "").split("/");
  return parts.find((p) => p.startsWith("@")) ?? parts.filter((p) => !/^(videos|shorts|streams|featured|live|playlists)$/i.test(p)).pop() ?? url;
};
const channelsList = (v: unknown, ctx: Ctx) => {
  const keys = Array.isArray(v) ? (v as string[]) : [];
  return keys.length ? keys.map((k) => ctx.names.get(k) ?? `канал …${k.split("/")[1]?.slice(-5) ?? k}`).join(", ") : "нет";
};
const stepText = (v: unknown) => {
  const n = Math.max(1, Number(v) || 1);
  return n === 1 ? "каждый день" : n === 2 ? "через день" : `раз в ${n} дн.`;
};
const links = (v: unknown) => {
  const list = String(v ?? "").split(/[\s,]+/).filter((u) => /youtube\.com|^@/.test(u));
  return list.length ? list.map(handleOf).join(", ") : "не заданы";
};

function defs(section: TemplateSection): Def[] {
  const p = `${section}_publish`;
  const out: Def[] = [
    { key: "rewrite_channels", per: true, step: "source", label: "Каналы-конкуренты", fmt: links },
    { key: "rewrite_channels_count", step: "source", label: "Роликов с канала за запуск", fmt: num },
    { key: "rewrite_engine", step: "source", label: "Чем переписываем", fmt: (v, c) => c.meta.rewrite_engines[String(v)] ?? String(v) },
    { key: "claude_model", step: "source", label: "Модель Claude", fmt: (v, c) => c.meta.claude_models.find((m) => m.id === v)?.label ?? (String(v || "") || "по умолчанию") },
    { key: "codex_model", step: "source", label: "Модель Codex", fmt: (v) => String(v || "") || "по умолчанию" },
    { key: "rewrite_llm_model", step: "source", label: "Модель встроенной нейросети", fmt: (v) => String(v || "") || "авто" },
    { key: "rewrite_fallback", step: "source", label: "Запасная нейросеть при сбое", fmt: onOff },
    { key: "rewrite_language", step: "source", label: "Язык рерайта", fmt: (v, c) => (v ? c.meta.rewrite_languages.find((l) => l.id === v)?.label ?? String(v) : "язык оригинала") },
    { key: "rewrite_words", step: "source", label: "Длина рерайта, слов", fmt: num },
    {
      key: "rewrite_prompt",
      per: true,
      step: "source",
      label: "Промпт рерайта",
      fmt: (v, c) => {
        const id = String(v ?? "");
        const all = [...c.meta.rewrite_default_prompts, ...(c.settings.rewrite_prompts ?? [])];
        return all.find((x) => x.id === id)?.name ?? (id.startsWith("default") ? "стандартный" : id || "стандартный");
      },
    },
    { key: "rewrite_review", step: "source", label: "Показать текст перед сборкой", fmt: onOff },
    { key: "stars_notes", step: "source", label: "Заметки про звёзд", fmt: text },
    { key: "tts_provider", step: "voice", label: "Сервис озвучки", fmt: (v, c) => c.meta.tts_providers.find((x) => x.id === v)?.label ?? String(v) },
    { key: "tts", step: "voice", label: "Параметры озвучки" },
    { key: `${section}_text_start`, step: "texts", label: "Вставка в начале", fmt: text },
    { key: `${section}_text_mid`, step: "texts", label: "Вставка в середине", fmt: text },
    { key: `${section}_text_end`, step: "texts", label: "Вставка в конце", fmt: text },
  ];
  if (section === "broll") {
    out.push(
      { key: "broll_dir", step: "video", label: "Папка футажей", fmt: path },
      { key: "head_dir", step: "video", label: "Голова диктора", fmt: (v) => (String(v ?? "").trim() ? String(v) : "без головы") },
      { key: "overlay_text", step: "video", label: "Надпись в углу", fmt: text },
      { key: "intro_dir", step: "look", label: "Заставка в начале", fmt: path },
      { key: "outro_dir", step: "look", label: "Заставка в конце", fmt: path },
    );
  } else {
    out.push(
      { key: "media_mode", step: "video", label: "Подбор кадров", fmt: (v, c) => c.modeTitle(String(v)) },
      { key: "ai_gen_enabled", step: "video", label: "AI-кадры", fmt: onOff },
      { key: "ai_image_model", step: "video", label: "Модель AI-картинок", fmt: (v) => String(v || "") || "по умолчанию" },
      { key: "ai_video_model", step: "video", label: "Модель AI-видео", fmt: (v) => String(v || "") || "по умолчанию" },
      { key: "ai_budget_usd", step: "video", label: "Бюджет AI на ролик, $", fmt: num },
      {
        key: "source_mix",
        step: "video",
        label: "Соотношение источников кадров",
        fmt: (v, c) => {
          const m = (v && typeof v === "object" && Object.keys(v as object).length ? v : c.meta.source_mix_default) as Record<string, number>;
          return Object.entries(m).map(([k, n]) => `${k} ${n}%`).join(", ");
        },
      },
      { key: "music_track", step: "look", label: "Музыка", fmt: (v) => (v ? String(v) : "подбирается сама") },
      { key: "transition", step: "look", label: "Переходы", fmt: (v) => (v === "flash" ? "со вспышкой" : "плавные") },
      { key: "slides_intro_dir", step: "look", label: "Заставка в начале", fmt: path },
      { key: "slides_outro_dir", step: "look", label: "Заставка в конце", fmt: path },
    );
  }
  out.push(
    { key: "subtitles", step: "look", label: "Субтитры", fmt: onOff },
    { key: "subscribe", step: "look", label: "Призыв подписаться", fmt: onOff },
    { key: "subscribe_path", step: "look", label: "Анимация подписки", fmt: path },
    { key: "speed_copies", step: "look", label: "Копии со скоростью", fmt: onOff },
    { key: "speed_copies_count", step: "look", label: "Сколько копий", fmt: num },
    { key: `${section}_output_dir`, step: "look", label: "Куда сохранять ролики", fmt: (v) => (String(v ?? "").trim() ? String(v) : "на рабочий стол") },
    { key: p, step: "publish", label: "Выкладка на YouTube", fmt: onOff },
    { key: `${p}_channels`, step: "publish", label: "Каналы выкладки", fmt: channelsList },
    {
      key: `${p}_coauthors_on`,
      step: "publish",
      label: "Соавторы",
      fmt: (v) => (v === null || v === undefined ? "как раньше (по выбранным)" : onOff(v)),
    },
    { key: `${p}_coauthors`, step: "publish", label: "Кто соавторы", fmt: channelsList },
    { key: `${p}_step`, step: "publish", label: "Шаг между роликами", fmt: stepText },
    { key: `${p}_time`, step: "publish", label: "Время выхода (МСК)", fmt: (v) => (String(v ?? "").trim() ? String(v) : "случайное 16:00–20:00") },
    { key: `${p}_visible`, step: "publish", label: "Браузер виден при выкладке", fmt: onOff },
    { key: `${p}_keep`, step: "publish", label: "Оставлять ролик на компьютере", fmt: onOff },
    { key: `${p}_comment`, step: "publish", label: "Закреплённый комментарий", fmt: text },
  );
  if (section === "broll") {
    out.push(
      {
        key: "broll_publish_materials",
        step: "publish",
        label: "Обложки и названия",
        fmt: (v) => (v === "competitor" ? "переделка конкурента" : "с компьютера: папка обложек и файл названий"),
      },
      { key: "broll_publish_thumbs", step: "publish", label: "Папка обложек", fmt: path },
      { key: "broll_publish_titles", step: "publish", label: "Файл названий", fmt: path },
      { key: "broll_publish_descs", step: "publish", label: "Файл описаний", fmt: path },
    );
  } else {
    out.push(
      { key: "slides_publish_cover", step: "publish", label: "Обложка генерируется", fmt: onOff },
      { key: "slides_publish_title_variants", step: "publish", label: "Вариантов названия", fmt: num },
      { key: "slides_publish_desc_cta", step: "publish", label: "Призыв в описании", fmt: text },
    );
  }
  out.push(
    { key: "cover_model", step: "publish", label: "Модель обложек", fmt: (v) => String(v || "") || "по умолчанию" },
    { key: "cover_variants", step: "publish", label: "Вариантов обложки", fmt: num },
  );
  return out;
}

const dropSecrets = (opts: Record<string, unknown> | undefined, secretKeys: string[]) => {
  const o = { ...(opts ?? {}) };
  delete o.api_key;
  for (const k of secretKeys) delete o[k];
  return o;
};

/** Параметры озвучки одной строкой: «Голос «X» · Языки: …». */
function ttsLine(providerId: string, opts: Record<string, unknown>, meta: Meta): string {
  const spec = meta.tts_providers.find((p) => p.id === providerId);
  if (!spec) return "—";
  const parts: string[] = [];
  for (const f of spec.fields) {
    if (f.hidden || f.kind === "secret") continue;
    const v = opts[f.key];
    if (v === undefined || v === null || v === "") continue;
    if (f.kind === "named_list" || f.kind === "remote_list") {
      const list = v as { items?: { id: string; name: string }[]; selected?: string };
      const name = list.items?.find((i) => i.id === list.selected)?.name;
      if (name) parts.push(`${f.label}: ${name}`);
    } else if (f.kind === "multi_choice" && Array.isArray(v)) {
      if (v.length) parts.push(`${f.label}: ${v.map((c) => f.options?.find((o: [string, string]) => o[0] === c)?.[1] ?? c).join(", ")}`);
    } else if (typeof v === "boolean") {
      parts.push(`${f.label}: ${onOff(v)}`);
    } else if (typeof v === "number" || typeof v === "string") {
      parts.push(`${f.label}: ${clip(String(v), 60)}`);
    }
  }
  return parts.length ? parts.join(" · ") : "по умолчанию";
}

const same = (a: unknown, b: unknown) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

/** Строки «было в шаблоне → сейчас» по всем настройкам, которые есть в шаблоне. */
export function templateRows(base: Record<string, unknown>, cur: Settings, ctx: Omit<Ctx, "settings">): DiffRow[] {
  const full: Ctx = { ...ctx, settings: cur };
  const section = ctx.section;
  const live = cur as unknown as Record<string, unknown>;
  const rows: DiffRow[] = [];
  for (const d of defs(section)) {
    if (d.key === "tts") {
      const pid = String(base.tts_provider ?? "");
      const baseTts = (base.tts as Record<string, Record<string, unknown>> | undefined)?.[pid];
      if (!pid || baseTts === undefined) continue;
      const spec = ctx.meta.tts_providers.find((p) => p.id === pid);
      const secrets = (spec?.fields ?? []).filter((f) => f.kind === "secret").map((f) => f.key);
      const b = dropSecrets(baseTts, secrets);
      const c = dropSecrets(cur.tts?.[pid], secrets);
      rows.push({
        id: d.key,
        step: d.step,
        label: d.label,
        before: ttsLine(pid, b, ctx.meta),
        after: ttsLine(pid, c, ctx.meta),
        changed: !same(b, c),
        revert: () => ({ tts: { ...cur.tts, [pid]: { ...baseTts, ...(cur.tts?.[pid]?.api_key !== undefined ? { api_key: cur.tts[pid].api_key } : {}) } } }),
      });
      continue;
    }
    const bv = d.per ? (base[d.key] as Record<string, unknown> | undefined)?.[section] : base[d.key];
    if (d.per ? !(base[d.key] && section in (base[d.key] as object)) : !(d.key in base)) continue;
    const cv = d.per ? (live[d.key] as Record<string, unknown> | undefined)?.[section] : live[d.key];
    const fmt = d.fmt ?? text;
    rows.push({
      id: d.key,
      step: d.step,
      label: d.label,
      before: fmt(bv, full),
      after: fmt(cv, full),
      changed: !same(bv, cv),
      revert: () => (d.per ? { [d.key]: { ...((live[d.key] as object) ?? {}), [section]: bv } } : { [d.key]: bv }),
    });
  }
  return rows;
}
