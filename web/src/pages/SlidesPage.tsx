import { useState } from "react";
import { Mic, Sparkles } from "lucide-react";
import { Button, Card, Field, PageHeader, PathInput } from "../components/ui";
import { TextComposer, collectTexts, type TextItem } from "../components/TextComposer";
import { VideoOptionsCard, VoiceCard } from "../components/SharedCards";
import { TemplateCard } from "../components/Templates";
import { MusicCard } from "../components/MusicCard";
import { AiCard } from "../components/AiCard";
import { useJobs, useLoadedSettings } from "../lib/store";

const MEDIA_MODES = [
  {
    id: "theme",
    title: "По смыслу ролика",
    text: "80–100 фото и 10–15 видео по теме всего рассказа, без привязки к секундам. Фото по 3–5 с, видео до 6 с, без повторов подряд. Быстрее всего.",
  },
  {
    id: "strict_3min",
    title: "Строго первые 2 минуты",
    text: "Первые 2 минуты — кадр под каждую фразу, смена каждые 3–5 с: фото и видео вперемешку (видео примерно половина). Дальше 80–100 фото и 10–15 видео по теме ролика, без повторов подряд.",
  },
  {
    id: "strict_full",
    title: "Строго весь ролик",
    text: "Кадр под текст на всю длину, смена каждые 3–5 с, фото и видео вперемешку. Дольше всего.",
  },
] as const;

export function SlidesPage({ goRuns }: { goRuns: () => void }) {
  const { settings, update } = useLoadedSettings();
  const { enqueue } = useJobs();
  const [draft, setDraft] = useState("");
  const [items, setItems] = useState<TextItem[]>([]);
  const [audio, setAudio] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  const texts = collectTexts(draft, items);
  const count = texts.length || (audio.trim() ? 1 : 0);

  const start = async (kind: "slides_full" | "slides_voice") => {
    setBusy(kind);
    const list = texts.length ? texts : [{ text: "", source: null }];
    const added = await enqueue(
      list.map((t) => ({ kind, params: { text: t.text, source: t.source, audio: list.length === 1 ? audio : "" } })),
    );
    setBusy(null);
    if (added) {
      setDraft("");
      setItems(() => []);
      setAudio("");
    }
  };

  return (
    <div className="page">
      <PageHeader
        title="Истории со стоковыми слайдами"
        subtitle="Озвучка → Whisper → тема от LLM → подбор фото и видео со стоков → ролик 1080p"
        actions={
          <Button variant="ghost" onClick={goRuns}>
            Запуски и пошаговый режим →
          </Button>
        }
      />
      <div className="page-grid">
        <div className="col-main">
          <TemplateCard kind="slides" />
          <Card title="Сценарий" description="Текст нужен для темы и субтитров. Без текста — только если выбрано готовое аудио.">
            <TextComposer
              draft={draft}
              setDraft={setDraft}
              items={items}
              setItems={setItems}
              audio={audio}
              setAudio={setAudio}
              placeholder="Вставь текст сценария…"
            />
          </Card>
          <Card title="Подбор кадров">
            <div className="stack">
              <div className="provider-picker" role="radiogroup">
                {MEDIA_MODES.map((m) => (
                  <button
                    key={m.id}
                    role="radio"
                    aria-checked={settings.media_mode === m.id}
                    className={`provider ${settings.media_mode === m.id ? "active" : ""}`}
                    onClick={() => update({ media_mode: m.id })}
                  >
                    <span className="radio" />
                    <span className="provider-text">
                      <span className="provider-name">{m.title}</span>
                      <span className="muted small">{m.text}</span>
                    </span>
                  </button>
                ))}
              </div>
              <div className="muted small">
                Кадры ищутся везде: YouTube, Pexels, Pixabay, Unsplash, Google, Wikimedia, Archive. Доля источников
                подстраивается под тему ролика. Видео — цель 1080p, не ниже 720p.
              </div>
            </div>
          </Card>
          <Card title="Интро и аутро">
            <div className="grid grid-2">
              <Field label="Интро" hint="Случайный клип из папки">
                <PathInput value={settings.slides_intro_dir} onChange={(v) => update({ slides_intro_dir: v })} kind="folder" title="Интро" />
              </Field>
              <Field label="Аутро" hint="Случайный клип из папки">
                <PathInput value={settings.slides_outro_dir} onChange={(v) => update({ slides_outro_dir: v })} kind="folder" title="Аутро" />
              </Field>
            </div>
          </Card>
          <AiCard />
          <MusicCard />
        </div>
        <div className="col-side">
          <VoiceCard />
          <VideoOptionsCard />
        </div>
      </div>
      <div className="action-bar">
        <span className="muted">{count === 0 ? "Добавь сценарий или готовое аудио" : `Роликов: ${count}`}</span>
        <div className="row gap-sm">
          <Button
            icon={<Mic size={16} />}
            onClick={() => start("slides_voice")}
            loading={busy === "slides_voice"}
            disabled={count === 0 || busy !== null}
            title="Только озвучка, тайминги и тема — кадры и сборку запустишь потом на странице «Запуски»"
          >
            Только шаг 1
          </Button>
          <Button
            variant="primary"
            size="lg"
            icon={<Sparkles size={18} />}
            onClick={() => start("slides_full")}
            loading={busy === "slides_full"}
            disabled={count === 0 || busy !== null}
          >
            {count > 1 ? `Полный цикл ×${count}` : "Полный цикл"}
          </Button>
        </div>
      </div>
    </div>
  );
}
