export type NamedListValue = { items: { id: string; name: string }[]; selected: string };

export type FieldSpec = {
  key: string;
  label: string;
  kind: "secret" | "text" | "slider" | "named_list" | "remote_list" | "multi_choice";
  hint: string;
  placeholder: string;
  quick: boolean;
  hidden: boolean;
  required: boolean;
  min: number;
  max: number;
  step: number;
  default: unknown;
  item_label: string;
  id_label: string;
  fallback_id: string;
  options: [string, string][];
};

export type TtsProviderSpec = { id: string; label: string; description: string; fields: FieldSpec[] };

export type ProviderValues = Record<string, unknown>;

export type Settings = {
  tts_provider: string;
  tts: Record<string, ProviderValues>;
  llm_api_key: string;
  llm_base_url: string;
  llm_model: string;
  pexels_api_keys: string;
  pixabay_api_keys: string;
  unsplash_api_keys: string;
  youtube_api_keys: string;
  subtitles: boolean;
  transition: "calm" | "flash";
  subscribe: boolean;
  subscribe_path: string;
  speed_copies: boolean;
  speed_copies_count: number;
  broll_output_dir: string;
  slides_output_dir: string;
  overlay_text: string;
  broll_dir: string;
  head_dir: string;
  intro_dir: string;
  outro_dir: string;
  media_mode: "theme" | "strict_3min" | "strict_full" | "stars";
  stars_notes: string;
  adspower_url: string;
  adspower_api_key: string;
  broll_publish: boolean;
  broll_publish_channels: string[];
  broll_publish_coauthors: string[];
  broll_publish_coauthors_on: boolean | null;
  broll_publish_materials: string;
  broll_publish_thumbs: string;
  broll_publish_titles: string;
  broll_publish_descs: string;
  broll_publish_dates: string[];
  broll_publish_time: string;
  broll_publish_step: number;
  broll_publish_visible: boolean;
  broll_publish_keep: boolean;
  broll_publish_comment: string;
  slides_publish: boolean;
  slides_publish_channels: string[];
  slides_publish_coauthors: string[];
  slides_publish_coauthors_on: boolean | null;
  slides_publish_dates: string[];
  slides_publish_time: string;
  slides_publish_step: number;
  slides_publish_visible: boolean;
  slides_publish_keep: boolean;
  slides_publish_comment: string;
  slides_publish_desc_cta: string;
  slides_publish_cover: boolean;
  slides_publish_title_variants: number;
  cover_model: string;
  cover_variants: number;
  cover_prompts: CoverPrompt[];
  cover_prompt: Record<string, string>;
  broll_text_start: string;
  broll_text_mid: string;
  broll_text_end: string;
  slides_text_start: string;
  slides_text_mid: string;
  slides_text_end: string;
  slides_intro_dir: string;
  slides_outro_dir: string;
  music_track: string;
  ai_budget_usd: number;
  rewrite_engine: string;
  rewrite_review: boolean;
  rewrite_prompts: RewritePrompt[];
  rewrite_prompt: Record<string, string>;
  rewrite_language: string;
  rewrite_words: string;
  rewrite_channels: Record<string, string>;
  rewrite_channels_count: number;
  claude_model: string;
  codex_model?: string;
  analytics_groups?: AnalyticsGroup[];
  last_seen_version?: string; // какую версию «Что нового» человек уже видел
  analytics_view?: { group: string; profile?: string; sort: string; dir: "asc" | "desc" | string };
  analytics_auto?: { enabled: boolean; group: string; time: string };
  update_auto?: { enabled: boolean; time: string };
  rewrite_llm_model: string;
  rewrite_fallback: boolean;
  source_mix: Partial<Record<MixKey, number>>;
  royaltechno_api_key: string;
  ai_gen_enabled: boolean;
  ai_image_model: string;
  ai_video_model: string;
};

export type ChannelPick = { url: string; channel?: string; taken?: number; left?: number; titles?: string[]; error?: string };
export type ChannelStat = { title: string; url: string; total: number; left: number; taken: number; checked_at: number };

export type AnalyticsPoint = { t: number; subscribers: number | null; views_48h: number | null; watch_hours: number | null; views_total: number | null };
/** Автообновление аналитики по расписанию: ближайший и прошлый запуск (unix-время, 0 — нет). */
export type AnalyticsAuto = { next_at: number; last_at: number; last_job: string };

export type TemplateSection = "broll" | "slides";
export type TemplateSummary = {
  publish: boolean;
  channels: string[];
  step: number;
  time: string;
  competitors: string[];
  ai: boolean;
};
export type Template = {
  id: string;
  name: string;
  section: TemplateSection;
  created: number;
  updated: number;
  summary: TemplateSummary;
};
export type TemplateLaunchIn = { count: number; start: string; step: number; channels?: string[]; save_channels?: boolean };
export type TemplateLaunchOut = {
  jobs: Job[];
  notes: string[];
  channels: { url: string; channel?: string; taken?: number; left?: number; error?: string }[];
};

export type ChangeItem = { tag: "new" | "better" | "fix"; title: string; text: string };
export type Release = { version: string; date: string; title: string; summary: string; items: ChangeItem[] };
export type WhatsNew = { version: string; unseen: Release[]; history: Release[] };

export type AnalyticsRow = {
  channel_id: string;
  name: string;
  handle: string;
  profile_id: string;
  profile: string;
  language: string;
  subscribers: number | null;
  views_48h: number | null;
  watch_hours: number | null;
  views_total: number | null;
  scheduled: number | null;
  public: number | null; // вышло в открытый доступ
  unlisted: number | null; // по ссылке
  private: number | null; // ограниченный доступ
  synced_at: number;
  error: string;
  prev: AnalyticsPoint | null;
};

export type Meta = {
  llm_models: string[];
  llm_default_model: string;
  llm_default_base_url: string;
  tts_providers: TtsProviderSpec[];
  max_copies: number;
  source_mix_default: Record<MixKey, number>;
  rewrite_default_prompts: RewritePrompt[];
  cover_default_prompts: CoverPrompt[];
  cover_prompt_roles: { id: string; label: string; hint: string; variables: string[] }[];
  rewrite_sections: Record<string, string>;
  rewrite_languages: { id: string; label: string }[];
  rewrite_engines: Record<string, string>;
  rewrite_auto_model: string;
  claude_models: { id: string; label: string }[];
  ffmpeg: boolean;
  desktop: boolean;
};

export type RewritePrompt = { id: string; name: string; section: string; text: string };
export type CoverPrompt = { id: string; name: string; role: string; text: string };

export type MixKey = "ai" | "stocks" | "youtube" | "google" | "wikimedia" | "archive";

/** Группа каналов во вкладке «Аналитика» (канал может быть в нескольких). */
export type AnalyticsGroup = { id: string; name: string; channels: string[] };

/** Автосмена инструмента или модели в задаче: что делалось, с чего на что и почему. */
export type ToolSwitch = { task: string; from: string; to: string; reason: string; at: number; times?: number };

export type TokenRow = { name: string; calls: number; in: number; cached: number; out: number };
export type TokenUsage = { calls: number; in: number; cached: number; out: number; by: TokenRow[] };

export type JobStatus = "queued" | "running" | "done" | "failed" | "cancelled";

/** Обновление программы (git pull + перезапуск). */
export type UpdateStatus = {
  ok: boolean;
  error: string;
  current: string; // короткий номер текущей сборки
  behind: number; // сколько новых изменений на сервере
  commits: string[];
  version: string; // версия, до которой обновимся
  title: string;
  dirty: string[]; // правки в папке программы, которые могут помешать
  checked_at: number;
  busy: boolean; // идёт или ждёт работа в очереди
  pending: { mode?: string; since?: number; source?: string };
  next_at: number;
  last_applied: number;
  last_error: string;
  last_error_at: number;
};

/** Итоговый отчёт по пачке задач: что сделано, что нет, почему и как исправить. */
export type ReportRow = {
  ok: boolean;
  info?: boolean;
  text?: string;
  channel?: string;
  title?: string;
  url?: string;
  when?: string;
  coauthors_ok?: number;
  coauthors_total?: number;
  note?: string;
};
export type ReportItem = {
  job_id: string;
  kind: string;
  kind_label: string;
  title: string;
  status: "ok" | "failed";
  summary: string;
  rows: ReportRow[];
  details: string[];
};
export type ReportProblem = {
  id: string;
  scope: "publish" | "job" | "collabs" | "warn" | "wait";
  title: string;
  text: string;
  job_id: string;
  severity: "bad" | "warn";
  tag: string;
  cause: string;
  mode: "auto" | "wait" | "manual";
  steps: string[];
  action: { type: "resume"; job_id: string } | { type: "collabs" } | null;
};
export type Report = {
  id: string;
  created: number;
  started: number;
  finished: number;
  tone: "ok" | "warn" | "bad";
  title: string;
  text: string;
  counts: { jobs: number; ok: number; failed: number; videos: number; polls: number; comments: number; covers: number; texts: number };
  items: ReportItem[];
  problems: ReportProblem[];
  fix: { jobs: string[]; collabs: boolean; count: number };
  manual: number;
  seen: boolean;
};
export type ReportBrief = Pick<Report, "id" | "created" | "finished" | "tone" | "title" | "text" | "seen" | "manual">;
export type FixOut = { resumed: string[]; collabs: boolean; errors: string[] };

export type Job = {
  id: string;
  notice?: string; // предупреждение при постановке (например, мало денег на RoyalTechno)
  switches?: ToolSwitch[]; // автосмены инструмента/модели: Claude → Codex → встроенная…
  stop_at?: number | null; // когда нажали «Стоп»
  stopping?: boolean; // «Стоп» нажат, текущий шаг ещё доделывается
  tokens?: TokenUsage | null; // расход токенов встроенной нейросети по этапам
  targets?: string[]; // каналы, на которые идёт работа (выкладка, голосования, комментарии, выкладка после сборки)
  targets_after?: boolean; // true — ролик ещё собирается, выложится на эти каналы после сборки
  kind: string;
  title: string;
  params: { run?: string; [k: string]: unknown };
  status: JobStatus;
  progress: number;
  message: string;
  result: {
    summary?: string;
    folder?: string;
    video?: string;
    run?: string;
    lines?: string[];
    warnings?: string[];
    image?: string;
    images?: string[];
    variants?: string[];
    model?: string;
    text_file?: string;
    follow?: JobIn;
  } | null;
  error: string | null;
  // выкладка: живой статус по каналам
  live?: { channel: string; status: string; pct: number; stage: string; when: string; speed?: number; updated: number }[] | null;
  attempts: number;
  resumable: boolean;
  run: string;
  created: number;
  started: number | null;
  finished: number | null;
};

export type LogLine = { line: string; job_id: string | null };

export type Run = {
  name: string;
  path: string;
  stage: string;
  stage_label: string;
  updated: string;
  snippet: string;
  has_final: boolean;
  stock_bytes: number;
};

export type AiModelOption = { id: string; label: string; price_cents: number; low_res: boolean };
export type AiModels = { share: number; share_max: number; image: AiModelOption[]; video: AiModelOption[] };

export type MusicTrack = { id: string; title: string; group: string; duration: number; claimed?: boolean };
export type MusicCatalog = { groups: Record<string, string>; tracks: MusicTrack[] };

export type PublishChannel = {
  id: string; name: string; handle: string; account: string; playlists: string[];
  language: string; language_auto: boolean; titles_language: string;
  subscribers?: number | null; stats_at?: number;
};
export type PublishProfile = {
  id: string; number: string; name: string; group: string; channels: PublishChannel[]; scanned_at: number; missing: boolean;
  studio?: { lang?: string; hour12?: boolean; tz?: string; offset?: number; platform?: string; checked_at?: number };
};

export type JobIn = { kind: string; params: Record<string, unknown> };

export type PickKind = "folder" | "audio" | "video" | "text" | "any";

/** Сервис по подписке (Claude Code / Codex): установлен ли, вход, чей аккаунт. */
export interface SubStatus {
  installed: boolean;
  logged_in?: boolean;
  email?: string;
  plan?: string;
  method?: string;
  path?: string;
  models?: { id: string; label: string }[];
}
export type SubAction = "login" | "switch" | "logout";

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      if (typeof data?.detail === "string") detail = data.detail;
    } catch {
      /* не JSON */
    }
    throw Object.assign(new Error(detail), { status: res.status });
  }
  return res.json() as Promise<T>;
}

export const api = {
  meta: () => request<Meta>("GET", "/api/meta"),
  settings: () => request<Settings>("GET", "/api/settings"),
  saveSettings: (patch: Partial<Settings>) => request<Settings>("PATCH", "/api/settings", patch),
  refreshTtsList: (provider: string, key: string) =>
    request<{ count: number; settings: Settings }>("POST", `/api/tts/${provider}/refresh/${key}`),
  music: () => request<MusicCatalog>("GET", "/api/music"),
  aiModels: () => request<AiModels>("GET", "/api/ai/models"),
  rewriteChannels: (channels: string[], count: number, params: Record<string, unknown>) =>
    request<{ jobs: Job[]; channels: ChannelPick[] }>("POST", "/api/rewrite/channels", { channels, count, params }),
  rewriteChannelStats: () => request<{ channels: ChannelStat[] }>("GET", "/api/rewrite/channels"),
  analytics: () => request<{ channels: AnalyticsRow[]; auto?: AnalyticsAuto }>("GET", "/api/analytics"),
  templates: () => request<Template[]>("GET", "/api/templates"),
  createTemplate: (name: string, section: TemplateSection) => request<Template>("POST", "/api/templates", { name, section }),
  renameTemplate: (id: string, name: string) => request<Template>("PATCH", `/api/templates/${id}`, { name }),
  saveTemplate: (id: string) => request<Template>("POST", `/api/templates/${id}/save`),
  applyTemplate: (id: string) => request<Settings>("POST", `/api/templates/${id}/apply`),
  copyTemplate: (id: string) => request<Template>("POST", `/api/templates/${id}/copy`),
  deleteTemplate: (id: string) => request<{ ok: boolean }>("DELETE", `/api/templates/${id}`),
  launchTemplate: (id: string, body: TemplateLaunchIn) => request<TemplateLaunchOut>("POST", `/api/templates/${id}/launch`, body),
  updateStatus: () => request<UpdateStatus>("GET", "/api/update"),
  updateCheck: () => request<UpdateStatus>("POST", "/api/update/check"),
  updateApply: (when: "now" | "idle") => request<UpdateStatus & { started: boolean }>("POST", "/api/update/apply", { when }),
  updateCancel: () => request<UpdateStatus>("POST", "/api/update/cancel"),
  reports: () => request<ReportBrief[]>("GET", "/api/reports"),
  reportUnseen: () => request<Report | null>("GET", "/api/reports/unseen"),
  report: (id: string) => request<Report>("GET", `/api/reports/${id}`),
  reportSeen: (id: string) => request<{ ok: boolean }>("POST", `/api/reports/${id}/seen`),
  reportFix: (jobs: string[], collabs: boolean) => request<FixOut>("POST", "/api/reports/fix", { jobs, collabs }),
  whatsNew: () => request<WhatsNew>("GET", "/api/whats-new"),
  whatsNewSeen: () => request<{ version: string }>("POST", "/api/whats-new/seen"),
  coverUpload: (data: string, name = "") => request<{ path: string }>("POST", "/api/covers/upload", { data, name }),
  coverImageUrl: (path: string) => `/api/cover-image?path=${encodeURIComponent(path)}`,
  ping: (service: string) =>
    request<{ ok: boolean; message: string }>("POST", `/api/ping/${service}`),
  addJobs: (items: JobIn[]) => request<Job[]>("POST", "/api/jobs", items),
  cancelJob: (id: string) => request<{ ok: boolean }>("POST", `/api/jobs/${id}/cancel`),
  resumeJob: (id: string) => request<Job>("POST", `/api/jobs/${id}/resume`),
  publishLanguage: (channel_id: string, language: string) =>
    request<{ profiles: PublishProfile[] }>("POST", "/api/publish/language", { channel_id, language }),
  publishProfiles: () => request<{ profiles: PublishProfile[] }>("GET", "/api/publish/profiles"),
  publishSync: () => request<{ profiles: PublishProfile[] }>("POST", "/api/publish/sync"),
  publishScan: (id: string) => request<{ job: Job }>("POST", `/api/publish/scan/${id}`),
  moveJob: (id: string, to: "up" | "down" | "top" | "bottom") =>
    request<{ ok: boolean }>("POST", `/api/jobs/${id}/move`, { to }),
  resetJob: (id: string) => request<{ ok: boolean }>("POST", `/api/jobs/${id}/reset`),
  clearJobs: () => request<{ ok: boolean }>("POST", "/api/jobs/clear"),
  runs: () => request<Run[]>("GET", "/api/runs"),
  freeRunSpace: () => request<{ freed: number; runs: number }>("POST", "/api/runs/free-space"),
  pick: (kind: PickKind, title = "", multiple = false) =>
    request<{ paths: string[]; texts?: { name: string; text: string }[] }>("POST", "/api/pick", {
      kind,
      title,
      multiple,
    }),
  reveal: (path: string) => request<{ ok: boolean }>("POST", "/api/reveal", { path }),
  open: (path: string) => request<{ ok: boolean }>("POST", "/api/open", { path }),
  claudeStatus: () => request<SubStatus>("GET", "/api/claude/status"),
  claudeCheck: () => request<{ ok: boolean; message: string }>("POST", "/api/claude/check"),
  claudeTerminal: (action: SubAction = "login") => request<{ ok: boolean }>("POST", "/api/claude/terminal", { action }),
  codexStatus: () => request<SubStatus>("GET", "/api/codex/status"),
  codexCheck: () => request<{ ok: boolean; message: string }>("POST", "/api/codex/check"),
  codexTerminal: (action: SubAction = "login") => request<{ ok: boolean }>("POST", "/api/codex/terminal", { action }),
  checkPath: (path: string) =>
    request<{ exists: boolean; is_dir: boolean; is_file: boolean }>("POST", "/api/check-path", { path }),
};
