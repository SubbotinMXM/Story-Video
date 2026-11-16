import { useEffect, useState } from "react";
import { Subs } from "./Subs";
import { Play } from "lucide-react";
import { api, type PublishProfile } from "../lib/api";
import { useJobs } from "../lib/store";
import { Button, Card } from "./ui";

/** Ответы зрителям по выбранным каналам: сердечко + ответ каждому комментарию без ответа. */
export function CommentsCard() {
  const { enqueue } = useJobs();
  const [profiles, setProfiles] = useState<PublishProfile[]>([]);
  const [picked, setPicked] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void api.publishProfiles().then((r) => setProfiles(r.profiles)).catch(() => undefined);
  }, []);

  const channels = profiles.filter((p) => !p.missing).flatMap((p) => p.channels.map((c) => ({ key: `${p.id}/${c.id}`, p, c })));
  const toggle = (k: string, on: boolean) => setPicked((cur) => (on ? [...cur, k] : cur.filter((x) => x !== k)));

  return (
    <Card
      title="Каналы"
      description="Отметь каналы, где ответить зрителям. Спам, ссылки и оскорбления пропускаются; сначала — вариант ответа, который предложил YouTube, иначе пишет нейросеть в стиле канала."
    >
      <div className="stack">
        {channels.length === 0 ? (
          <div className="muted small">Каналов пока нет — нажми «Синхронизировать всё» в «Аналитике» (найдёт профили и каналы) или «Синхронизировать с AdsPower» в «Выложить на YouTube».</div>
        ) : (
          <div className="publish-channels">
            {channels.map(({ key, p, c }) => (
              <label key={key} className="row gap-sm small">
                <input type="checkbox" checked={picked.includes(key)} onChange={(e) => toggle(key, e.target.checked)} />
                {c.name} <span className="muted">{c.handle} · №{p.number} {p.name}</span> <Subs c={c} />
              </label>
            ))}
          </div>
        )}
        <Button
          variant="primary"
          icon={<Play size={16} />}
          disabled={picked.length === 0}
          loading={busy}
          onClick={async () => {
            setBusy(true);
            await enqueue([{ kind: "comments", params: { channels: picked } }]);
            setBusy(false);
          }}
        >
          Пуск{picked.length ? ` (${picked.length})` : ""}
        </Button>
      </div>
    </Card>
  );
}
