import { useEffect, useRef, useState } from "react";
import { Pause, Play } from "lucide-react";
import { api, type MusicCatalog } from "../lib/api";
import { useLoadedSettings } from "../lib/store";
import { Button, Card } from "./ui";

/** Фоновая музыка стоковых роликов: необязательный выбор трека с прослушиванием. */
export function MusicCard() {
  const { settings, update } = useLoadedSettings();
  const [cat, setCat] = useState<MusicCatalog | null>(null);
  const [playing, setPlaying] = useState(false);
  const audio = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {
    api.music().then(setCat).catch(() => setCat({ groups: {}, tracks: [] }));
    return () => audio.current?.pause();
  }, []);

  const selected = cat?.tracks.find((t) => t.id === settings.music_track) ?? null;

  const stop = () => {
    audio.current?.pause();
    setPlaying(false);
  };

  const toggle = () => {
    if (!selected) return;
    if (playing) {
      stop();
      return;
    }
    if (!audio.current || !audio.current.src.endsWith(`/api/music/${selected.id}`)) {
      audio.current?.pause();
      audio.current = new Audio(`/api/music/${selected.id}`);
      audio.current.onended = () => setPlaying(false);
    }
    void audio.current.play();
    setPlaying(true);
  };

  const groups = cat ? Object.entries(cat.groups) : [];
  return (
    <Card
      title="Фоновая музыка"
      description="Необязательно. Тихо, ровно и по кругу на весь ролик — голос чтеца всегда слышен. Треки в общественном достоянии (CC0). Если YouTube всё же поставит претензию на трек, программа сама исключит его и дальше возьмёт другой."
    >
      <div className="row gap-sm">
        <div style={{ flex: 1, minWidth: 0 }}>
          <select
            className="input select"
            value={settings.music_track}
            onChange={(e) => {
              stop();
              update({ music_track: e.target.value });
            }}
          >
            <option value="">Без музыки</option>
            {groups.map(([gid, label]) => (
              <optgroup key={gid} label={label}>
                {cat!.tracks
                  .filter((t) => t.group === gid)
                  .map((t) => (
                    <option key={t.id} value={t.id} disabled={t.claimed && t.id !== settings.music_track}>
                      {t.title} · {Math.floor(t.duration / 60)}:{String(Math.round(t.duration % 60)).padStart(2, "0")}
                      {t.claimed ? " · ⚠ претензия YouTube — не используется" : ""}
                    </option>
                  ))}
              </optgroup>
            ))}
          </select>
        </div>
        <Button
          variant="ghost"
          icon={playing ? <Pause size={15} /> : <Play size={15} />}
          onClick={toggle}
          disabled={!selected}
          title={playing ? "Остановить" : "Послушать"}
        >
          {playing ? "Стоп" : "Послушать"}
        </Button>
      </div>
      {selected?.claimed && (
        <div className="notice warn small" style={{ marginTop: 10 }}>
          На «{selected.title}» YouTube ставит претензию (Content ID) — в ролики он не идёт, программа берёт другой
          трек из той же группы. Выбери другой трек сам.
        </div>
      )}
    </Card>
  );
}
