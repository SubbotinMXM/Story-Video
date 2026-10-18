import { Card, NumberInput, PathInput, Toggle } from "./ui";
import { ProviderFields, ProviderPicker, useProvider, useTtsNeedsSetup } from "./TtsFields";
import { useLoadedSettings } from "../lib/store";

/** Озвучка: выбор сервиса + его быстрые настройки (голос, шаблон, скорость). */
export function VoiceCard() {
  const { spec } = useProvider();
  const needsSetup = useTtsNeedsSetup();
  return (
    <Card title="Озвучка" description="Одна для всех роликов, которые ставишь в очередь">
      <div className="stack">
        <ProviderPicker />
        {needsSetup && (
          <div className="notice warn small">
            У {spec.label} не всё настроено — открой Настройки → Озвучка
          </div>
        )}
        <ProviderFields spec={spec} mode="quick" />
      </div>
    </Card>
  );
}

/** Субтитры, подписка, копии — общие опции ролика. */
export function VideoOptionsCard() {
  const { settings, meta, update } = useLoadedSettings();
  return (
    <Card title="Оформление ролика">
      <div className="stack">
        <Toggle
          checked={settings.subtitles}
          onChange={(v) => update({ subtitles: v })}
          label="Вшитые субтитры"
          description="Текст из сценария, тайминги по озвучке. Язык определяется автоматически."
        />
        <Toggle
          checked={settings.subscribe}
          onChange={(v) => update({ subscribe: v })}
          label="Призыв подписаться"
          description="Анимация с хромакеем на 4–5 секунде"
        />
        {settings.subscribe && (
          <div className="indent">
            <PathInput
              value={settings.subscribe_path}
              onChange={(v) => update({ subscribe_path: v })}
              kind="video"
              title="Анимация подписки"
            />
          </div>
        )}
        <Toggle
          checked={settings.speed_copies}
          onChange={(v) => update({ speed_copies: v })}
          label="Копии со сдвигом скорости"
          description="1 = 99%, 2 = 99% и 101%, 3 = ещё 98%…"
        />
        {settings.speed_copies && (
          <div className="indent row gap-sm">
            <NumberInput
              value={settings.speed_copies_count}
              min={1}
              max={meta.max_copies}
              onChange={(v) => update({ speed_copies_count: Math.round(v) })}
            />
            <span className="muted">шт.</span>
          </div>
        )}
      </div>
    </Card>
  );
}
