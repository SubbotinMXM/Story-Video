import { useEffect, useRef } from "react";
import { Sparkles, TrendingUp, Wrench, X } from "lucide-react";
import type { ChangeItem, Release } from "../lib/api";

const TAGS: Record<ChangeItem["tag"], { label: string; icon: JSX.Element; cls: string }> = {
  new: { label: "Новое", icon: <Sparkles size={18} />, cls: "wn-new" },
  better: { label: "Улучшено", icon: <TrendingUp size={18} />, cls: "wn-better" },
  fix: { label: "Исправлено", icon: <Wrench size={18} />, cls: "wn-fix" },
};

const fmtDate = (iso: string) => {
  const d = new Date(`${iso}T00:00:00Z`);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
};

/** Большое окно «Что нового»: показывается один раз после обновления (mode «auto» — только невиданное)
 *  и по кнопке «Версия» в боковой панели (mode «history» — вся история). Закрытие в режиме «auto» = «увидел». */
export function WhatsNewDialog({ releases, mode, onClose }: { releases: Release[]; mode: "auto" | "history"; onClose: () => void }) {
  const btn = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    btn.current?.focus();
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [onClose]);
  if (!releases.length) return null;
  const latest = releases[0];
  const counts = (["new", "better", "fix"] as const).map((t) => ({
    t,
    n: releases.reduce((a, r) => a + r.items.filter((i) => i.tag === t).length, 0),
  }));
  const multi = releases.length > 1;
  return (
    <div className="wn-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="wn-dialog" role="dialog" aria-modal="true" aria-labelledby="wn-title">
        <div className="wn-head">
          <div className="wn-mark">SV</div>
          <div className="wn-head-text">
            <div className="wn-kicker">{mode === "auto" ? "Приложение обновлено" : "История версий"}</div>
            <h2 id="wn-title">Что нового в Story Video</h2>
            <div className="wn-sub">
              Версия {latest.version}
              {latest.date ? ` · ${fmtDate(latest.date)}` : ""}
            </div>
          </div>
          <button className="icon-btn wn-x" onClick={onClose} aria-label="Закрыть">
            <X size={18} />
          </button>
        </div>
        <div className="wn-counts">
          {counts
            .filter((c) => c.n > 0)
            .map((c) => (
              <span key={c.t} className={`wn-chip ${TAGS[c.t].cls}`}>
                {TAGS[c.t].label} · {c.n}
              </span>
            ))}
        </div>
        <div className="wn-body">
          {releases.map((r) => (
            <section key={r.version} className="wn-release">
              {multi && (
                <div className="wn-rel-head">
                  <span className="wn-ver">{r.version}</span>
                  {r.date && <span className="muted small">{fmtDate(r.date)}</span>}
                </div>
              )}
              <h3>{r.title}</h3>
              {r.summary && <p className="wn-summary">{r.summary}</p>}
              <div className="wn-items">
                {r.items.map((it, k) => (
                  <div key={k} className="wn-item">
                    <div className={`wn-ico ${TAGS[it.tag].cls}`}>{TAGS[it.tag].icon}</div>
                    <div className="wn-item-body">
                      <div className="wn-item-title">
                        {it.title}
                        <span className={`wn-tag ${TAGS[it.tag].cls}`}>{TAGS[it.tag].label}</span>
                      </div>
                      {it.text && <div className="wn-item-text">{it.text}</div>}
                    </div>
                  </div>
                ))}
              </div>
            </section>
          ))}
        </div>
        <div className="wn-foot">
          <span className="muted small">{mode === "auto" ? "Это окно больше не появится до следующего обновления. Историю версий можно открыть внизу слева." : "Все выпуски, новые сверху."}</span>
          <button ref={btn} className="btn btn-primary btn-lg" onClick={onClose}>
            {mode === "auto" ? "Понятно, поехали" : "Закрыть"}
          </button>
        </div>
      </div>
    </div>
  );
}
