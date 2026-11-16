import { useState } from "react";
import { FolderOpen, ImageIcon } from "lucide-react";
import { Button, Card, Field, PageHeader, Textarea } from "../components/ui";
import { api } from "../lib/api";
import { useJobs, useLoadedSettings } from "../lib/store";

/** Имена из текста — так же, как их разбирает сервер: по строке или через запятую. */
function parseNames(text: string): string[] {
  const out: string[] = [];
  // запятая внутри скобок — часть уточнения: «Андрей Миронов (советский актёр, 1941–1987)»
  for (const raw of text.split(/\n|[,;](?![^()]*\))/)) {
    const name = raw.replace(/^\s*(\d+[.)]|[-—•*])\s*/, "").trim().replace(/^[.\s]+|[.\s]+$/g, "");
    if (name && !out.some((n) => n.toLowerCase() === name.toLowerCase())) out.push(name);
  }
  return out;
}

function layout(n: number): string {
  if (n === 0) return "Впиши имена — по одному в строке или через запятую.";
  if (n === 1) return "1 человек — одно фото сверху.";
  const even = n % 2 ? n - 1 : n;
  const extra = n % 2 ? ` Нечётное число: последний (${n}-й) не поместится — нужно поровну сверху и снизу.` : "";
  return `${n} — на превью ${even / 2} сверху и ${even / 2} снизу, посередине пустая полоса под текст.${extra}`;
}

export function ThumbnailPage() {
  const { settings, update } = useLoadedSettings();
  const { enqueue, jobs } = useJobs();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const names = parseNames(text);
  const last = [...jobs].reverse().find((j) => j.kind === "thumbnail" && j.status === "done" && j.result?.image);

  const start = async () => {
    setBusy(true);
    await enqueue([{ kind: "thumbnail", params: { names: text, color: settings.thumb_color } }]);
    setBusy(false);
  };

  return (
    <div className="page narrow">
      <PageHeader
        title="Обложки звёзд"
        subtitle="Обложка для YouTube: фото людей из интернета на чёрном фоне — поровну сверху и снизу, посередине пустая полоса под твой текст."
      />
      <Card title="Портреты людей">
        <div className="stack">
          <Field label="Имена" hint={layout(names.length)}>
            <Textarea value={text} onChange={setText} rows={8} placeholder={"Юрий Никулин\nАндрей Миронов (советский актёр)\nАнатолий Папанов\nЕвгений Леонов"} />
          </Field>
          <div className="muted small">
            Тёзки и однофамильцы отсеиваются по подборке: если почти все — советские артисты, остальные тоже ищутся
            среди советских. Если программа не уверена, о ком речь, — спросит. Уточнение — после имени: в скобках, через тире или запятую («Marga Barbu — румынская актриса»).
          </div>
          <div className="provider-picker" role="radiogroup">
            {[
              { id: true, title: "Цветные", text: "Фото как нашлись в интернете." },
              { id: false, title: "Чёрно-белые", text: "Те же фото, переведённые в чёрно-белые — единый стиль." },
            ].map((m) => (
              <button
                key={String(m.id)}
                role="radio"
                aria-checked={settings.thumb_color === m.id}
                className={`provider ${settings.thumb_color === m.id ? "active" : ""}`}
                onClick={() => update({ thumb_color: m.id })}
              >
                <span className="radio" />
                <span className="provider-text">
                  <span className="provider-name">{m.title}</span>
                  <span className="muted small">{m.text}</span>
                </span>
              </button>
            ))}
          </div>
          <div>
            <Button variant="primary" size="lg" icon={<ImageIcon size={17} />} onClick={start} loading={busy} disabled={!names.length}>
              Сделать превью
            </Button>
          </div>
        </div>
      </Card>
      {last?.result?.image && (
        <Card title="Последнее превью" description={last.result.summary}>
          <div className="stack">
            <img
              src={`/api/thumbnail?path=${encodeURIComponent(last.result.image)}`}
              alt="Превью"
              style={{ width: "100%", borderRadius: 8, display: "block" }}
            />
            {(last.result.warnings ?? []).map((w) => (
              <div key={w} className="notice warn small">{w}</div>
            ))}
            <div>
              <Button icon={<FolderOpen size={15} />} onClick={() => api.reveal(last.result!.image!)}>
                Показать в Finder
              </Button>
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}
