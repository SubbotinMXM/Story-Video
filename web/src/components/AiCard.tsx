import { useEffect, useState } from "react";
import { api, type AiModels } from "../lib/api";
import { useLoadedSettings } from "../lib/store";
import { Card, Field, Toggle } from "./ui";

/** AI-кадры: по желанию — генерация фото/видео для слабых кусков (≈10–15% ролика). */
export function AiCard() {
  const { settings, update } = useLoadedSettings();
  const [models, setModels] = useState<AiModels | null>(null);

  useEffect(() => {
    if (settings.ai_gen_enabled && !models) api.aiModels().then(setModels).catch(() => undefined);
  }, [settings.ai_gen_enabled, models]);

  const pct = models ? `${Math.round(models.share * 100)}–${Math.round(models.share_max * 100)}%` : "10–15%";
  const image = models?.image.find((m) => m.id === settings.ai_image_model);
  const video = models?.video.find((m) => m.id === settings.ai_video_model);
  // примерно кадров на ролик: пул «по смыслу» ~110 кадров, «первые 2 мин» ~130; «весь ролик» — ~13 на каждые 10 минут
  const perVideo = Math.round(120 * (models?.share ?? 0.12));
  const noKey = !settings.royaltechno_api_key.trim();
  const nothing = settings.ai_gen_enabled && !settings.ai_image_model && !settings.ai_video_model;

  return (
    <Card title="AI-кадры">
      <div className="stack">
        <Toggle
          checked={settings.ai_gen_enabled}
          onChange={(v) => update({ ai_gen_enabled: v })}
          label="Сгенерированные фото и видео"
          description={`Куски про цифры, графики и статистику получают сгенерированную инфографику обязательно. В остальном приоритет — живым кадрам (фото, стоки, YouTube): AI-кадр ставится только туда, где живой подходит плохо или не нашёлся. Всего не больше ${pct} ролика.`}
        />
        {settings.ai_gen_enabled && (
          <div className="indent stack">
            <Field label="Модель для фото" hint="Не обязательно — можно генерировать только видео">
              <select
                className="input select"
                value={settings.ai_image_model}
                onChange={(e) => update({ ai_image_model: e.target.value })}
              >
                <option value="">Не генерировать фото</option>
                {models?.image.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.label}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Модель для видео" hint="Не обязательно — можно генерировать только фото">
              <select
                className="input select"
                value={settings.ai_video_model}
                onChange={(e) => update({ ai_video_model: e.target.value })}
              >
                <option value="">Не генерировать видео</option>
                {models?.video.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.label}
                    {m.low_res ? " · 480p" : ""}
                  </option>
                ))}
              </select>
            </Field>
            {video?.low_res && <div className="notice warn small">Эта модель видео — 480p: в ролике 1080p будет заметно мыльнее.</div>}
            {noKey && <div className="notice warn small">Нет ключа RoyalTechno — добавь в Настройки → AI-кадры.</div>}
            {nothing && <div className="notice warn small">Выбери модель для фото, для видео или обе.</div>}
            {(image || video) && (
              <div className="muted small">
                Обычно до ~{perVideo} AI-кадров на ролик («Строго весь ролик» — ~13 на каждые 10 минут).
                {image && ` Фото: $${(image.price_cents / 100).toFixed(2)} за кадр.`}
                {video && ` Видео: $${(video.price_cents / 100).toFixed(2)} за клип.`} Слабых кадров меньше — генерируется меньше.
              </div>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}
