import { useState, type DragEvent } from "react";
import { Copy } from "lucide-react";
import { Button, Card, Field, NumberInput, PageHeader, PathInput } from "../components/ui";
import { useJobs, useLoadedSettings } from "../lib/store";

function percents(n: number) {
  const out: number[] = [];
  for (let i = 1; i <= n; i++) {
    const step = Math.floor((i + 1) / 2);
    out.push(i % 2 ? 100 - step : 100 + step);
  }
  return out;
}

export function CopiesPage() {
  const { settings, meta, update } = useLoadedSettings();
  const { enqueue } = useJobs();
  const [video, setVideo] = useState("");
  const [busy, setBusy] = useState(false);
  const count = settings.speed_copies_count;

  const start = async () => {
    setBusy(true);
    await enqueue([{ kind: "copies", params: { video, count } }]);
    setBusy(false);
  };

  // в окне приложения pywebview отдаёт полный путь у брошенного файла
  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    const f = e.dataTransfer.files[0] as (File & { pywebviewFullPath?: string }) | undefined;
    if (f?.pywebviewFullPath) setVideo(f.pywebviewFullPath);
  };

  return (
    <div className="page narrow" onDragOver={(e) => e.preventDefault()} onDrop={onDrop}>
      <PageHeader
        title="Копии ролика"
        subtitle="Копии готового ролика с немного изменённой скоростью. Оригинал не меняется, копии кладутся рядом."
      />
      <Card>
        <div className="stack">
          <Field label="Готовый ролик">
            <PathInput value={video} onChange={setVideo} kind="video" title="Ролик" />
          </Field>
          <Field label="Количество копий" hint={`Скорости: ${percents(count).map((p) => `${p}%`).join(", ")}`}>
            <NumberInput value={count} min={1} max={meta.max_copies} onChange={(v) => update({ speed_copies_count: Math.round(v) })} />
          </Field>
          <div>
            <Button variant="primary" size="lg" icon={<Copy size={17} />} onClick={start} loading={busy} disabled={!video.trim()}>
              Сделать копии
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}
