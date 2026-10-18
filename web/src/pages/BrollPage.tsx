import { useState } from "react";
import { Clapperboard } from "lucide-react";
import { Button, Card, Field, PageHeader, PathInput, TextInput } from "../components/ui";
import { TextComposer, collectTexts, type TextItem } from "../components/TextComposer";
import { VideoOptionsCard, VoiceCard } from "../components/SharedCards";
import { TemplateCard } from "../components/Templates";
import { useJobs, useLoadedSettings } from "../lib/store";

export function BrollPage() {
  const { settings, update } = useLoadedSettings();
  const { enqueue } = useJobs();
  const [draft, setDraft] = useState("");
  const [items, setItems] = useState<TextItem[]>([]);
  const [audio, setAudio] = useState("");
  const [busy, setBusy] = useState(false);

  const texts = collectTexts(draft, items);
  const count = texts.length || (audio.trim() ? 1 : 0);

  const start = async () => {
    setBusy(true);
    const list = texts.length ? texts : [{ text: "", source: null }];
    const added = await enqueue(
      list.map((t) => ({
        kind: "broll",
        params: { text: t.text, source: t.source, audio: list.length === 1 ? audio : "" },
      })),
    );
    setBusy(false);
    if (added) {
      setDraft("");
      setItems(() => []);
      setAudio("");
    }
  };

  return (
    <div className="page">
      <PageHeader
        title="Рассказы с футажами"
        subtitle="Текст → озвучка → нарезка ваших футажей с головой диктора и оверлеем"
      />
      <div className="page-grid">
        <div className="col-main">
          <TemplateCard kind="broll" />
          <Card title="Текст рассказа" description="Один текст — один ролик. Несколько .txt можно перетащить разом.">
            <TextComposer
              draft={draft}
              setDraft={setDraft}
              items={items}
              setItems={setItems}
              audio={audio}
              setAudio={setAudio}
              placeholder="Вставь текст рассказа…"
            />
          </Card>
          <Card title="Видеоряд">
            <div className="grid grid-2">
              <Field label="Папка футажей" span>
                <PathInput value={settings.broll_dir} onChange={(v) => update({ broll_dir: v })} kind="folder" title="Футажи" />
              </Field>
              <Field label="Голова диктора" hint="Необязательно: видео в углу кадра">
                <PathInput value={settings.head_dir} onChange={(v) => update({ head_dir: v })} kind="folder" title="Голова диктора" />
              </Field>
              <Field label="Текст оверлея" hint="Необязательно: надпись в углу">
                <TextInput value={settings.overlay_text} onChange={(v) => update({ overlay_text: v })} placeholder="Например, название канала" />
              </Field>
              <Field label="Интро" hint="Случайный клип из папки в начало">
                <PathInput value={settings.intro_dir} onChange={(v) => update({ intro_dir: v })} kind="folder" title="Интро" />
              </Field>
              <Field label="Аутро" hint="Случайный клип из папки в конец">
                <PathInput value={settings.outro_dir} onChange={(v) => update({ outro_dir: v })} kind="folder" title="Аутро" />
              </Field>
            </div>
          </Card>
        </div>
        <div className="col-side">
          <VoiceCard />
          <VideoOptionsCard />
        </div>
      </div>
      <div className="action-bar">
        <span className="muted">
          {count === 0 ? "Добавь текст или готовое аудио" : `Будет собрано роликов: ${count}`}
        </span>
        <Button variant="primary" size="lg" icon={<Clapperboard size={18} />} onClick={start} loading={busy} disabled={count === 0}>
          {count > 1 ? `Собрать ${count} роликов` : "Собрать ролик"}
        </Button>
      </div>
    </div>
  );
}
