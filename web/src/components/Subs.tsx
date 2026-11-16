import type { PublishChannel } from "../lib/api";

/** Подписчики канала (из «Аналитики») — чтобы различать каналы с одинаковыми названиями. */
export function Subs({ c }: { c: PublishChannel }) {
  if (c.subscribers == null) {
    // число ещё не известно (не было синхронизации «Аналитики») — пустая ячейка, без «подп. ?»
    return <span className="subs-count" title="Подписчиков пока не знаю — нажми «Синхронизировать всё» в разделе «Аналитика»" />;
  }
  const when = c.stats_at ? new Date(c.stats_at * 1000).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
  return (
    <span className="subs-count" title={when ? `По данным «Аналитики» на ${when}` : undefined}>
      {c.subscribers.toLocaleString("ru-RU")} подп.
    </span>
  );
}
