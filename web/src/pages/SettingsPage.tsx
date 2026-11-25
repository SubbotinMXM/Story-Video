import { useState } from "react";
import { CheckCircle2, XCircle, Zap } from "lucide-react";
import { api } from "../lib/api";
import { useLoadedSettings } from "../lib/store";
import { ProviderFields, ProviderPicker, useProvider } from "../components/TtsFields";
import { RewriteSettingsCard } from "../components/Rewrite";
import { CoverPromptsCard } from "../components/CoverPrompts";
import { Button, Card, Field, PageHeader, SecretInput, TextInput, Textarea } from "../components/ui";

type PingResult = { ok: boolean; message: string } | null;

function PingButton({ service, label }: { service: string; label: string }) {
  const { flush } = useLoadedSettings();
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<PingResult>(null);
  const run = async () => {
    setBusy(true);
    setRes(null);
    try {
      await flush();
      setRes(await api.ping(service));
    } catch (e) {
      setRes({ ok: false, message: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="ping">
      <Button icon={<Zap size={15} />} onClick={run} loading={busy}>
        {label}
      </Button>
      {res && (
        <div className={`ping-result ${res.ok ? "ok" : "bad"}`}>
          {res.ok ? <CheckCircle2 size={16} /> : <XCircle size={16} />}
          <span>{res.message}</span>
        </div>
      )}
    </div>
  );
}

function keyCount(raw: string) {
  return raw.split(/[\n,;]+/).filter((k) => k.trim()).length;
}

function TtsSettings() {
  const { spec } = useProvider();
  return (
    <Card title="Озвучка" description="Выбери сервис — ниже только его настройки. Тот же выбор на странице ролика.">
      <ProviderPicker />
      <ProviderFields spec={spec} mode="full" />
      <PingButton key={spec.id} service={`tts:${spec.id}`} label={`Проверить ${spec.label}`} />
    </Card>
  );
}

export function SettingsPage() {
  const { settings, meta, update } = useLoadedSettings();
  return (
    <div className="page narrow">
      <PageHeader title="Настройки" subtitle="Сохраняются автоматически в data/config.json" />

      <TtsSettings />

      <Card title="LLM" description="OpenAI-совместимый API: тема ролика и поисковые запросы для стоков">
        <div className="grid grid-2">
          <Field label="API key" span>
            <SecretInput value={settings.llm_api_key} onChange={(v) => update({ llm_api_key: v })} placeholder="sk-…" />
          </Field>
          <Field label="Base URL">
            <TextInput
              value={settings.llm_base_url}
              onChange={(v) => update({ llm_base_url: v })}
              placeholder={meta.llm_default_base_url}
              mono
            />
          </Field>
          <Field label="Модель" hint="Пусто — авто: самая дешёвая подходящая модель, недоступна — следующая по цене. Список — от дешёвых к дорогим">
            <input
              className="input mono"
              list="llm-models"
              value={settings.llm_model}
              placeholder={meta.llm_default_model || "авто — по цене"}
              onChange={(e) => update({ llm_model: e.target.value })}
            />
            <datalist id="llm-models">
              {meta.llm_models.map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          </Field>
        </div>
        <PingButton service="llm" label="Проверить LLM" />
      </Card>

      <RewriteSettingsCard />
      <CoverPromptsCard />
      <Card
        title="AdsPower"
        description="Браузерные профили, в которых выполнен вход в каналы YouTube, — для автоматической выкладки роликов. Программа AdsPower должна быть открыта на этом Mac. Ключ и адрес — в AdsPower → API."
      >
        <div className="grid grid-2">
          <Field label="API key" span>
            <SecretInput value={settings.adspower_api_key} onChange={(v) => update({ adspower_api_key: v })} placeholder="ключ из AdsPower → API" />
          </Field>
          <Field label="Адрес Local API">
            <TextInput value={settings.adspower_url} onChange={(v) => update({ adspower_url: v })} placeholder="http://local.adspower.com:50325" mono />
          </Field>
        </div>
        <PingButton service="adspower" label="Проверить AdsPower" />
      </Card>
      <Card title="Стоки и YouTube" description="По одному ключу на строку. Когда у ключа кончается лимит, берётся следующий.">
        <div className="grid grid-3">
          {(
            [
              ["pexels_api_keys", "Pexels"],
              ["pixabay_api_keys", "Pixabay"],
              ["unsplash_api_keys", "Unsplash"],
            ] as const
          ).map(([key, label]) => (
            <Field key={key} label={`${label} · ключей: ${keyCount(settings[key])}`}>
              <Textarea value={settings[key]} onChange={(v) => update({ [key]: v })} rows={4} mono placeholder="ключ на строку" />
            </Field>
          ))}
        </div>
        <Field
          label={`YouTube Data API · ключей: ${keyCount(settings.youtube_api_keys)}`}
          hint="Видео-нарезки с YouTube (Creative Commons). У ключа ~100 поисков в сутки — кончилась квота, берётся следующий. Пусто — YouTube не используется."
        >
          <Textarea
            value={settings.youtube_api_keys}
            onChange={(v) => update({ youtube_api_keys: v })}
            rows={3}
            mono
            placeholder="ключ на строку"
          />
        </Field>
        <Field
          label={`TMDB · ключей: ${keyCount(settings.tmdb_api_keys)}`}
          hint="Фото людей кино и ТВ и кадры фильмов (только картинки). Вставьте «Ключ доступа к API» (длинный, на eyJ…) или короткий «Ключ API». Пусто — TMDB не используется. This product uses the TMDB API but is not endorsed or certified by TMDB."
        >
          <Textarea
            value={settings.tmdb_api_keys}
            onChange={(v) => update({ tmdb_api_keys: v })}
            rows={2}
            mono
            placeholder="ключ на строку"
          />
        </Field>
        <PingButton service="stocks" label="Проверить стоки, YouTube и TMDB" />
      </Card>

      <Card
        title="AI-кадры (RoyalTechno)"
        description="Генерация фото и видео для кусков, где живой кадр не нашёлся или подходит плохо. Ключ — в боте @royaltechno_ai_bot (/api_key_new). Включается на странице «Стоковые слайды»."
      >
        <Field label="API key">
          <SecretInput
            value={settings.royaltechno_api_key}
            onChange={(v) => update({ royaltechno_api_key: v })}
            placeholder="rt_live_…"
          />
        </Field>
        <PingButton service="ai" label="Проверить ключ и баланс" />
      </Card>

      {!meta.ffmpeg && (
        <Card title="ffmpeg не найден">
          <p>
            Без ffmpeg ролики не соберутся. Установи: <code>brew install ffmpeg</code>
          </p>
        </Card>
      )}
    </div>
  );
}
