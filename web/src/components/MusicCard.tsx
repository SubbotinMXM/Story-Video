import { useEffect, useRef, useState } from "react";
import { Dices, Pause, Play } from "lucide-react";
import { api, type MusicCatalog } from "../lib/api";
import { groupOf, isGroup, GROUP_PREFIX } from "../lib/music";
import { useLoadedSettings } from "../lib/store";
import { Button, Card } from "./ui";

const mmss = (s: number) => {
  const t = Math.round(s);
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`;
};

/** Фоновая музыка стоковых роликов: конкретный трек или случайный из категории (по нише), с прослушиванием. */
export function MusicCard() {
  const { settings, update } = useLoadedSettings();
  const [cat, setCat] = useState<MusicCatalog | null>(null);
  const [playing, setPlaying] = useState<string>("");
  const audio = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {
    api.music().then(setCat).catch(() => setCat({ groups: {}, tracks: [] }));
    return () => audio.current?.pause();
  }, []);

  const value = settings.music_track;
  const random = isGroup(value);
  const gid = random ? groupOf(value) : "";
  const selected = !random ? cat?.tracks.find((t) => t.id === value) ?? null : null;
  const tracksOf = (g: string) => (cat?.tracks ?? []).filter((t) => t.group === g);

  const stop = () => {
    audio.current?.pause();
    setPlaying("");
  };
  const play = (id: string) => {
    if (playing === id) {
      stop();
      return;
    }
    audio.current?.pause();
    audio.current = new Audio(`/api/music/${id}`);
    audio.current.onended = () => setPlaying("");
    void audio.current.play();
    setPlaying(id);
  };

  const groups = cat ? Object.entries(cat.groups) : [];
  const poolOf = (g: string) => tracksOf(g).filter((t) => !t.claimed).length;
  return (
    <Card
      title="Фоновая музыка"
      description="Необязательно. Тихо, ровно и по кругу на весь ролик — голос чтеца всегда слышен. Все треки без слов, из Фонотеки YouTube — претензий на них не бывает. Выбери конкретный трек или «случайный из категории»: тогда каждый ролик берёт свой трек подходящей ниши (пока не прозвучат все — без повторов)."
    >
      <div className="row gap-sm">
        <div style={{ flex: 1, minWidth: 0 }}>
          <select
            className="input select"
            value={value}
            onChange={(e) => {
              stop();
              update({ music_track: e.target.value });
            }}
          >
            <option value="">Без музыки</option>
            <optgroup label="🎲 Случайный трек из категории">
              {groups.map(([g, label]) => (
                <option key={g} value={`${GROUP_PREFIX}${g}`} disabled={poolOf(g) === 0}>
                  Случайный: {label} ({poolOf(g)})
                </option>
              ))}
            </optgroup>
            {groups.map(([g, label]) => (
              <optgroup key={g} label={label}>
                {tracksOf(g).map((t) => (
                  <option key={t.id} value={t.id} disabled={t.claimed && t.id !== value}>
                    {t.title} · {mmss(t.duration)}
                    {t.claimed ? " · ⚠ претензия YouTube — не используется" : ""}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
        </div>
        {!random && (
          <Button
            variant="ghost"
            icon={playing === selected?.id ? <Pause size={15} /> : <Play size={15} />}
            onClick={() => selected && play(selected.id)}
            disabled={!selected}
            title={playing ? "Остановить" : "Послушать"}
          >
            {playing === selected?.id ? "Стоп" : "Послушать"}
          </Button>
        )}
      </div>

      {random && cat && (
        <div className="music-pool">
          <div className="music-pool-head">
            <Dices size={15} />
            <span>
              Каждый ролик берёт случайный трек категории <b>«{cat.groups[gid] ?? gid}»</b> — треков: {poolOf(gid)}.
            </span>
          </div>
          {cat.hints?.[gid] && <div className="muted small">{cat.hints[gid]}</div>}
          <div className="music-pool-list">
            {tracksOf(gid).map((t) => (
              <div key={t.id} className={`music-pool-row ${t.claimed ? "claimed" : ""}`}>
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => play(t.id)} title="Послушать">
                  {playing === t.id ? <Pause size={13} /> : <Play size={13} />}
                </button>
                <span className="music-pool-title">{t.title}</span>
                <span className="muted small">{mmss(t.duration)}</span>
                {t.claimed && <span className="muted small">⚠ претензия — не берётся</span>}
              </div>
            ))}
          </div>
        </div>
      )}
      {!random && selected && cat?.hints?.[selected.group] && (
        <div className="muted small" style={{ marginTop: 8 }}>
          Категория «{cat.groups[selected.group]}»: {cat.hints[selected.group]}
        </div>
      )}
      {selected?.claimed && (
        <div className="notice warn small" style={{ marginTop: 10 }}>
          На «{selected.title}» YouTube ставит претензию (Content ID) — в ролики он не идёт, программа берёт другой
          трек из той же группы. Выбери другой трек сам.
        </div>
      )}
    </Card>
  );
}
