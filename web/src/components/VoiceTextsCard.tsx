import { Card, Field, Textarea } from "./ui";
import { useLoadedSettings } from "../lib/store";

// три текста в озвучку раздела: пусто — ничего не вставляется (никаких скрытых призывов)
export function VoiceTextsCard({ section }: { section: "broll" | "slides" }) {
  const { settings, update } = useLoadedSettings();
  const key = (part: "start" | "mid" | "end") => `${section}_text_${part}` as const;
  const field = (part: "start" | "mid" | "end", label: string, hint: string) => (
    <Field label={label} hint={hint}>
      <Textarea
        value={settings[key(part)]}
        onChange={(v) => update({ [key(part)]: v })}
        rows={2}
        placeholder="Пусто — ничего не вставляется"
      />
    </Field>
  );
  return (
    <Card
      title="Тексты в ролике"
      description="Вставляются в озвучку ровно так, как написаны, — и в свой текст, и в рерайт. Рассказ на другом языке — текст переводится на его язык. Пустое поле — ничего. Других призывов в ролике не будет."
    >
      <div className="grid">
        {field("start", "Текст перед началом ролика", "Звучит первым, перед рассказом")}
        {field("mid", "Текст примерно на 30-й секунде", "Встаёт на паузе между абзацами или предложениями, не посреди мысли")}
        {field("end", "Текст в конце ролика", "Звучит после последней фразы рассказа")}
      </div>
    </Card>
  );
}
