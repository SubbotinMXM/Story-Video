import { RotateCcw } from "lucide-react";
import type { MixKey } from "../lib/api";
import { useLoadedSettings } from "../lib/store";
import { Badge, Button, Card, NumberInput } from "./ui";

const ROWS: { key: MixKey; label: string; hint: string }[] = [
  { key: "ai", label: "AI-кадры", hint: "Доля сгенерированных кадров (если AI-кадры включены; потолок — бюджет)" },
  { key: "stocks", label: "Стоки", hint: "Pexels, Pixabay, Unsplash — фото и видео" },
  { key: "youtube", label: "YouTube", hint: "Нарезки видео Creative Commons" },
  { key: "google", label: "Google Images", hint: "Фото из поиска картинок" },
  { key: "wikimedia", label: "Wikimedia", hint: "Фото Wikimedia Commons" },
  { key: "archive", label: "Archive.org", hint: "Архивные фото и видео" },
];

/** Доли источников в ролике, %. Пусто — по умолчанию (подбор сам подстраивает доли под тему ролика). */
export function MixCard() {
  const { settings, meta, update } = useLoadedSettings();
  const defaults = meta.source_mix_default;
  const custom = Object.keys(settings.source_mix ?? {}).length > 0;
  const value = (k: MixKey) => (custom ? settings.source_mix[k] ?? defaults[k] : defaults[k]);
  const total = ROWS.reduce((s, r) => s + value(r.key), 0);

  const set = (k: MixKey, v: number) => {
    const next = Object.fromEntries(ROWS.map((r) => [r.key, value(r.key)])) as Record<MixKey, number>;
    next[k] = Math.max(0, Math.min(100, Math.round(v)));
    const same = ROWS.every((r) => next[r.key] === defaults[r.key]);
    update({ source_mix: same ? {} : next });
  };

  return (
    <Card
      title="Доли источников"
      description="Ориентир, сколько какого контента в ролике, %. Источник с 0% не используется."
      actions={
        custom ? (
          <Button size="sm" variant="ghost" icon={<RotateCcw size={14} />} onClick={() => update({ source_mix: {} })}>
            По умолчанию
          </Button>
        ) : (
          <Badge>по умолчанию</Badge>
        )
      }
    >
      <div className="stack-sm">
        {ROWS.map((r) => (
          <div key={r.key} className="row gap-sm" style={{ alignItems: "center" }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div>{r.label}</div>
              <div className="muted small">
                {r.hint} · по умолчанию {defaults[r.key]}%
              </div>
            </div>
            <div style={{ width: 110 }}>
              <NumberInput value={value(r.key)} min={0} max={100} onChange={(v) => set(r.key, v)} />
            </div>
          </div>
        ))}
        <div className={`small ${total === 100 ? "muted" : "notice warn"}`}>
          Сумма {total}%{total !== 100 ? " — не страшно: доли считаются относительно друг друга" : ""}.
          {custom
            ? " Свои цифры применяются ко всем роликам как есть."
            : " По умолчанию подбор сам сдвигает доли под тему ролика (про человека — больше Google, про природу — больше стоков)."}
          {" "}В режиме «Звёзды» — только настоящие фото героев, доли не применяются.
        </div>
      </div>
    </Card>
  );
}
