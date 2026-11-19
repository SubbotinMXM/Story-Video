import { useEffect, type ReactNode } from "react";
import { X } from "lucide-react";

/** Окно поверх страницы: заголовок, прокручиваемое содержимое, кнопки внизу. Закрывается по Escape и клику мимо. */
export function Modal({
  title,
  subtitle,
  onClose,
  children,
  footer,
  width = 620,
  closable = true,
}: {
  title: string;
  subtitle?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  width?: number;
  closable?: boolean;
}) {
  useEffect(() => {
    if (!closable) return;
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [onClose, closable]);
  return (
    <div className="md-backdrop" onMouseDown={(e) => closable && e.target === e.currentTarget && onClose()}>
      <div className="md-dialog" role="dialog" aria-modal="true" aria-label={title} style={{ width: `min(${width}px, 100%)` }}>
        <div className="md-head">
          <div>
            <h2>{title}</h2>
            {subtitle && <div className="muted small">{subtitle}</div>}
          </div>
          {closable && (
            <button className="icon-btn" onClick={onClose} aria-label="Закрыть">
              <X size={18} />
            </button>
          )}
        </div>
        <div className="md-body">{children}</div>
        {footer && <div className="md-foot">{footer}</div>}
      </div>
    </div>
  );
}
