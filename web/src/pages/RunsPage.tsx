import { useCallback, useEffect, useState } from "react";
import { Clapperboard, Eraser, FolderOpen, Images, Play, RefreshCw, Search } from "lucide-react";
import { api, type Run } from "../lib/api";
import { useJobs, useToasts } from "../lib/store";
import { Badge, Button, Card, Empty, PageHeader } from "../components/ui";

const STAGES = ["script_saved", "tts_done", "timings_done", "chunks_done", "media_done", "render_done"];
const STAGE_SHORT = ["Текст", "Озвучка", "Тайминги", "Тема", "Кадры", "Ролик"];

function gb(bytes: number) {
  return bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} ГБ` : `${Math.max(1, Math.round(bytes / 1e6))} МБ`;
}

function stageIndex(stage: string) {
  return STAGES.indexOf(stage);
}

function Steps({ stage }: { stage: string }) {
  const idx = stageIndex(stage);
  return (
    <div className="steps">
      {STAGE_SHORT.map((s, i) => (
        <div key={s} className={`step ${i <= idx ? "done" : ""}`} title={s}>
          <span className="step-dot" />
          <span className="step-label">{s}</span>
        </div>
      ))}
    </div>
  );
}

export function RunsPage({ active }: { active: boolean }) {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [confirmFree, setConfirmFree] = useState(false);
  const [freeing, setFreeing] = useState(false);
  const { enqueue, jobs } = useJobs();
  const { push } = useToasts();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setRuns(await api.runs());
    } catch (e) {
      push({ kind: "error", title: "Не удалось загрузить запуски", text: (e as Error).message });
    } finally {
      setLoading(false);
    }
  }, [push]);

  // обновлять при открытии страницы и когда задачи заканчиваются
  const finishedCount = jobs.filter((j) => j.finished).length;
  useEffect(() => {
    if (active) void load();
  }, [active, finishedCount, load]);

  const busyRuns = new Set(
    jobs
      .filter((j) => j.status === "running" || j.status === "queued")
      .map((j) => j.run)
      .filter(Boolean),
  );

  // кадры уже собранных роликов: ролик из них сделан, дальше они нужны только для пересборки
  const freeable = (runs ?? [])
    .filter((r) => r.has_final && !busyRuns.has(r.name))
    .reduce((sum, r) => sum + r.stock_bytes, 0);

  const freeSpace = async () => {
    setFreeing(true);
    try {
      const res = await api.freeRunSpace();
      push({ kind: "ok", title: `Освобождено ${gb(res.freed)}`, text: `Очищено запусков: ${res.runs}. Готовые ролики на месте.` });
      setConfirmFree(false);
      await load();
    } catch (e) {
      push({ kind: "error", title: "Не получилось освободить место", text: (e as Error).message });
    } finally {
      setFreeing(false);
    }
  };

  const shown = (runs ?? []).filter(
    (r) => !query.trim() || `${r.name} ${r.snippet}`.toLowerCase().includes(query.trim().toLowerCase()),
  );

  return (
    <div className="page">
      <PageHeader
        title="Запуски слайдов"
        subtitle="Каждый ролик со стоками хранит промежуточные файлы. Здесь можно продолжить упавший или прогнать шаги по отдельности."
        actions={
          <Button icon={<RefreshCw size={15} className={loading ? "spin" : ""} />} onClick={load}>
            Обновить
          </Button>
        }
      />
      {freeable > 50e6 && (
        <Card>
          {confirmFree ? (
            <div className="stack">
              <div className="small">
                Удалю скачанные фото и видео у собранных роликов — {gb(freeable)}. Сами ролики, озвучка, тексты и субтитры
                останутся. Чтобы пересобрать такой ролик, кадры придётся подобрать заново (шаг 2).
              </div>
              <div className="row gap-sm">
                <Button size="sm" variant="primary" loading={freeing} onClick={() => void freeSpace()}>
                  Освободить {gb(freeable)}
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setConfirmFree(false)}>
                  Отмена
                </Button>
              </div>
            </div>
          ) : (
            <div className="row gap-sm wrap" style={{ justifyContent: "space-between", alignItems: "center" }}>
              <div className="small">
                Скачанные кадры собранных роликов занимают <b>{gb(freeable)}</b>. Если ролик уже выложен, они не нужны.
              </div>
              <Button size="sm" icon={<Eraser size={14} />} onClick={() => setConfirmFree(true)}>
                Освободить место
              </Button>
            </div>
          )}
        </Card>
      )}
      <Card>
        <div className="input-group search">
          <span className="input-prefix">
            <Search size={16} />
          </span>
          <input className="input" placeholder="Поиск по названию или тексту" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
      </Card>
      {runs !== null && shown.length === 0 ? (
        <Empty title={runs.length ? "Ничего не найдено" : "Запусков пока нет"} text="Они появятся после первого ролика со стоками" />
      ) : (
        <div className="run-list">
          {shown.map((r) => {
            const idx = stageIndex(r.stage);
            const busy = busyRuns.has(r.name);
            return (
              <div key={r.name} className="run">
                <div className="run-head">
                  <div className="run-title">
                    <div className="mono-soft">{r.name}</div>
                    <div className="muted small">{new Date(r.updated).toLocaleString("ru-RU")}</div>
                  </div>
                  <Badge tone={r.has_final ? "ok" : busy ? "accent" : "warn"}>{busy ? "в работе" : r.stage_label}</Badge>
                </div>
                {r.snippet && <div className="run-snippet">{r.snippet}</div>}
                <Steps stage={r.stage} />
                {r.stock_bytes > 0 && <div className="muted small">Кадры: {gb(r.stock_bytes)}</div>}
                <div className="row gap-sm wrap">
                  {!r.has_final && (
                    <Button
                      size="sm"
                      variant="primary"
                      icon={<Play size={14} />}
                      disabled={busy}
                      onClick={() => enqueue([{ kind: "slides_resume", params: { run: r.name } }])}
                    >
                      Продолжить до конца
                    </Button>
                  )}
                  <Button
                    size="sm"
                    icon={<Images size={14} />}
                    disabled={busy || idx < stageIndex("chunks_done")}
                    title="Заново подобрать фото и видео"
                    onClick={() => enqueue([{ kind: "slides_media", params: { run: r.name } }])}
                  >
                    Шаг 2: кадры
                  </Button>
                  <Button
                    size="sm"
                    icon={<Clapperboard size={14} />}
                    disabled={busy || idx < stageIndex("media_done") || r.stock_bytes === 0}
                    title={r.stock_bytes === 0 ? "Кадры удалены — сначала шаг 2" : "Заново собрать mp4 из подобранных кадров"}
                    onClick={() => enqueue([{ kind: "slides_render", params: { run: r.name } }])}
                  >
                    Шаг 3: собрать
                  </Button>
                  <Button size="sm" variant="ghost" icon={<FolderOpen size={14} />} onClick={() => void api.reveal(r.path)}>
                    Папка
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
