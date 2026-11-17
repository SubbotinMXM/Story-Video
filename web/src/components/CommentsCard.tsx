import { useState } from "react";
import { Play } from "lucide-react";
import { useJobs } from "../lib/store";
import { Button, Card } from "./ui";
import { ChannelPicker } from "./ChannelPicker";

/** Ответы зрителям по выбранным каналам: сердечко + ответ каждому комментарию без ответа. */
export function CommentsCard() {
  const { enqueue } = useJobs();
  const [picked, setPicked] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  return (
    <Card
      title="Каналы"
      description="Отметь каналы, где ответить зрителям, — галочка у профиля отмечает все его каналы. Спам, ссылки и оскорбления пропускаются; сначала — вариант ответа, который предложил YouTube, иначе пишет нейросеть в стиле канала."
    >
      <div className="stack">
        <ChannelPicker value={picked} onChange={setPicked} />
        <div className="row gap-sm">
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
      </div>
    </Card>
  );
}
