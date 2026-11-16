import { Card, Field, PathInput, Textarea, TextInput } from "./ui";
import { useLoadedSettings } from "../lib/store";

/** Блоки настроек разделов — общие для страниц «Рассказы» / «Стоковые слайды» и «Генерации по шагам». */

export const MEDIA_MODES = [
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
  {
    id: "stars",
    title: "Звёзды",
    text: "Ролик про нескольких людей: на часть текста про каждого — только его настоящие фото (проверка по лицу), иногда видео. Вступление и заключение — фото всех героев. Главы — по героям, плюс превью для YouTube. AI-кадров нет.",
  },
] as const;

/** Рассказы: футажи, голова диктора, надпись в углу. */
export function BrollFootageCard() {
  const { settings, update } = useLoadedSettings();
  return (
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
      </div>
    </Card>
  );
}

/** Рассказы: интро, аутро, папка готовых роликов. */
export function BrollIntroCard() {
  const { settings, update } = useLoadedSettings();
  return (
    <Card title="Интро, аутро и папка роликов">
      <div className="grid grid-2">
        <Field label="Интро" hint="Случайный клип из папки в начало">
          <PathInput value={settings.intro_dir} onChange={(v) => update({ intro_dir: v })} kind="folder" title="Интро" />
        </Field>
        <Field label="Аутро" hint="Случайный клип из папки в конец">
          <PathInput value={settings.outro_dir} onChange={(v) => update({ outro_dir: v })} kind="folder" title="Аутро" />
        </Field>
        <Field label="Куда сохранять ролики" hint="Не выбрано — на рабочий стол" span>
          <PathInput value={settings.broll_output_dir} onChange={(v) => update({ broll_output_dir: v })} kind="folder" title="Папка для готовых роликов" placeholder="Рабочий стол" />
        </Field>
      </div>
    </Card>
  );
}

/** Стоковые слайды: режим подбора кадров. */
export function MediaModeCard() {
  const { settings, update } = useLoadedSettings();
  return (
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
        {settings.media_mode === "stars" && (
          <Field
            label="Кто эти люди (необязательно)"
            hint="Тёзки и однофамильцы: программа берёт людей по контексту ролика (все советские артисты — значит, и этот советский). Если не уверена — остановится и спросит: впиши сюда уточнение и нажми «Продолжить»."
          >
            <Textarea
              value={settings.stars_notes}
              onChange={(v) => update({ stars_notes: v })}
              rows={3}
              placeholder={"Андрей Миронов — советский актёр, 1941–1987\nЛюдмила Гурченко — советская актриса"}
            />
          </Field>
        )}
        <div className="muted small">
          Кадры ищутся везде: YouTube, Pexels, Pixabay, Unsplash, Google, Wikimedia, Archive. Доля источников
          подстраивается под тему ролика. Видео — цель 1080p, не ниже 720p.
        </div>
      </div>
    </Card>
  );
}

/** Стоковые слайды: интро, аутро, папка готовых роликов. */
export function SlidesIntroCard() {
  const { settings, update } = useLoadedSettings();
  return (
    <Card title="Интро, аутро и папка роликов">
      <div className="grid grid-2">
        <Field label="Интро" hint="Случайный клип из папки">
          <PathInput value={settings.slides_intro_dir} onChange={(v) => update({ slides_intro_dir: v })} kind="folder" title="Интро" />
        </Field>
        <Field label="Аутро" hint="Случайный клип из папки">
          <PathInput value={settings.slides_outro_dir} onChange={(v) => update({ slides_outro_dir: v })} kind="folder" title="Аутро" />
        </Field>
        <Field label="Куда сохранять ролики" hint="Не выбрано — на рабочий стол" span>
          <PathInput value={settings.slides_output_dir} onChange={(v) => update({ slides_output_dir: v })} kind="folder" title="Папка для готовых роликов" placeholder="Рабочий стол" />
        </Field>
      </div>
    </Card>
  );
}
