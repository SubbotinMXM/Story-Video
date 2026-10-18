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

export type TemplateKind = "broll" | "slides";

export type VideoTemplate = { id: string; name: string; kind: TemplateKind; values: Record<string, unknown> };

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
  subscribe: boolean;
  subscribe_path: string;
  speed_copies: boolean;
  speed_copies_count: number;
  overlay_text: string;
  broll_dir: string;
  head_dir: string;
  intro_dir: string;
  outro_dir: string;
  media_mode: "theme" | "strict_3min" | "strict_full";
  slides_intro_dir: string;
  slides_outro_dir: string;
  music_track: string;
  royaltechno_api_key: string;
  ai_gen_enabled: boolean;
  ai_image_model: string;
  ai_video_model: string;
  templates: VideoTemplate[];
  active_template: Record<TemplateKind, string>;
};

export type Meta = {
  llm_models: string[];
  llm_default_model: string;
  llm_default_base_url: string;
  tts_providers: TtsProviderSpec[];
  template_keys: Record<TemplateKind, string[]>;
  max_copies: number;
  ffmpeg: boolean;
  desktop: boolean;
};

export type JobStatus = "queued" | "running" | "done" | "failed" | "cancelled";

export type Job = {
  id: string;
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
  } | null;
  error: string | null;
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
};

export type AiModelOption = { id: string; label: string; price_cents: number; low_res: boolean };
export type AiModels = { share: number; share_max: number; image: AiModelOption[]; video: AiModelOption[] };

export type MusicTrack = { id: string; title: string; group: string; duration: number };
export type MusicCatalog = { groups: Record<string, string>; tracks: MusicTrack[] };

export type JobIn = { kind: string; params: Record<string, unknown> };

export type PickKind = "folder" | "audio" | "video" | "text" | "any";

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
    throw new Error(detail);
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
  ping: (service: string) =>
    request<{ ok: boolean; message: string }>("POST", `/api/ping/${service}`),
  addJobs: (items: JobIn[]) => request<Job[]>("POST", "/api/jobs", items),
  cancelJob: (id: string) => request<{ ok: boolean }>("POST", `/api/jobs/${id}/cancel`),
  resumeJob: (id: string) => request<Job>("POST", `/api/jobs/${id}/resume`),
  resetJob: (id: string) => request<{ ok: boolean }>("POST", `/api/jobs/${id}/reset`),
  clearJobs: () => request<{ ok: boolean }>("POST", "/api/jobs/clear"),
  runs: () => request<Run[]>("GET", "/api/runs"),
  pick: (kind: PickKind, title = "", multiple = false) =>
    request<{ paths: string[]; texts?: { name: string; text: string }[] }>("POST", "/api/pick", {
      kind,
      title,
      multiple,
    }),
  reveal: (path: string) => request<{ ok: boolean }>("POST", "/api/reveal", { path }),
  checkPath: (path: string) =>
    request<{ exists: boolean; is_dir: boolean; is_file: boolean }>("POST", "/api/check-path", { path }),
};
