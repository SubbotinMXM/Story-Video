import { useEffect, useState } from "react";
import { WifiOff } from "lucide-react";
import { api } from "../lib/api";
import { useJobs } from "../lib/store";

/** Плашка «нет доступа к YouTube»: отключился VPN — задачи с YouTube на паузе, программа переподключает VPN сама. */
export function NetBanner() {
  const { net } = useJobs();
  const [checking, setChecking] = useState(false);
  const [, tick] = useState(0);

  useEffect(() => {
    if (!net || net.up) return;
    const t = window.setInterval(() => tick((n) => n + 1), 30_000); // «не было N мин» без перезагрузки
    return () => window.clearInterval(t);
  }, [net]);

  if (!net || net.up) return null;
  const since = net.down_since ? new Date(net.down_since * 1000) : null;
  const mins = since
    ? Math.max(0, Math.round((Date.now() - since.getTime()) / 60_000))
    : 0;
  const at = since
    ? since.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })
    : "";

  return (
    <div className="tpl-banner net-banner" role="alert">
      <WifiOff size={18} />
      <div className="tpl-banner-text">
        <b>
          Нет доступа к YouTube{at ? ` с ${at}` : ""}
          {mins ? ` (${mins} мин)` : ""}
        </b>{" "}
        — похоже, отключился VPN. Выкладка и задачи с YouTube ждут (попытки не
        тратятся), ролики собираются дальше: {net.action || "переподключаю VPN"}
        . Продолжит сама, как только связь вернётся.
      </div>
      <button
        className="btn btn-ghost btn-sm"
        disabled={checking}
        title={net.reason || undefined}
        onClick={async () => {
          setChecking(true);
          try {
            await api.netCheck();
          } finally {
            setChecking(false);
          }
        }}
      >
        {checking ? "Проверяю…" : "Проверить сейчас"}
      </button>
    </div>
  );
}
