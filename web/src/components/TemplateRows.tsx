import { ArrowRight, Check, RotateCcw } from "lucide-react";
import { Card } from "./ui";
import type { DiffRow } from "../lib/templateDiff";

/** Настройки шаблона на одном шаге: что было в шаблоне и, если поменял, на что. */
export function TemplateRows({
  name,
  rows,
  title,
  onRevert,
  onGo,
  stepTitle,
}: {
  name: string;
  rows: DiffRow[];
  title?: string;
  onRevert: (row: DiffRow) => void;
  onGo?: (row: DiffRow) => void;
  stepTitle?: (row: DiffRow) => string;
}) {
  if (!rows.length) return null;
  const changed = rows.filter((r) => r.changed).length;
  return (
    <Card
      title={title ?? `Шаблон «${name}» — что было и что стало`}
      description={changed ? `Изменено на этом шаге: ${changed}. Любую правку можно вернуть к шаблону.` : "Здесь всё как в шаблоне — ничего не менял."}
    >
      <div className="tdiff">
        {rows.map((r) => (
          <div key={r.id} className={`tdiff-row ${r.changed ? "changed" : ""}`}>
            <div className="tdiff-label">
              {r.changed ? <ArrowRight size={14} /> : <Check size={14} />}
              <span>{r.label}</span>
              {stepTitle && onGo && (
                <button type="button" className="link-btn small" onClick={() => onGo(r)}>
                  {stepTitle(r)}
                </button>
              )}
            </div>
            <div className="tdiff-vals">
              {r.changed ? (
                <>
                  <div className="tdiff-before" title={r.before}>
                    <span className="tdiff-tag">было</span>
                    {r.before}
                  </div>
                  <div className="tdiff-after" title={r.after}>
                    <span className="tdiff-tag">стало</span>
                    {r.after}
                  </div>
                </>
              ) : (
                <div className="tdiff-same" title={r.before}>
                  {r.before}
                </div>
              )}
            </div>
            {r.changed && (
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => onRevert(r)} title="Вернуть как в шаблоне">
                <RotateCcw size={13} /> Вернуть
              </button>
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}

