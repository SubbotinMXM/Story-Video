import { useEffect, useState } from "react";
import { api, type AiModels } from "../lib/api";
import { useLoadedSettings } from "../lib/store";
import { Field, NumberInput } from "./ui";

/** Обложки по образцу: модель генератора (RoyalTechno) и сколько вариантов делать. Общие для раздела «Обложки»
 *  и выкладки «Стоковых слайдов». */
export function CoverSettingsFields() {
  const { settings, update } = useLoadedSettings();
  const [models, setModels] = useState<AiModels | null>(null);

  useEffect(() => {
    api.aiModels().then(setModels).catch(() => undefined);
  }, []);

  const noKey = !settings.royaltechno_api_key.trim();
  const known = models?.image.some((m) => m.id === settings.cover_model);

  return (
    <div className="stack">
      <div className="grid grid-2">
        <Field label="Модель обложек" hint="Nano Banana Pro лучше всех пишет текст на картинке">
          <select className="input select" value={settings.cover_model} onChange={(e) => update({ cover_model: e.target.value })}>
            {!known && <option value={settings.cover_model}>{settings.cover_model}</option>}
            {models?.image.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Вариантов на обложку" hint="Нейросеть выберет лучший; все плохие — ещё один круг">
          <NumberInput value={settings.cover_variants} onChange={(v) => update({ cover_variants: v })} min={1} max={6} />
        </Field>
      </div>
      {noKey && <div className="notice warn small">Нужен ключ RoyalTechno: Настройки → AI-кадры.</div>}
    </div>
  );
}
