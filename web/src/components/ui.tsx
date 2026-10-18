import { useEffect, useId, useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Check, FileAudio, FileVideo, Folder, Loader2, X } from "lucide-react";
import { api, type PickKind } from "../lib/api";

// ---------- Button ----------

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md" | "lg";
  icon?: ReactNode;
  loading?: boolean;
};

export function Button({
  variant = "secondary",
  size = "md",
  icon,
  loading,
  children,
  className = "",
  disabled,
  ...rest
}: ButtonProps) {
  return (
    <button
      className={`btn btn-${variant} btn-${size} ${className}`}
      disabled={disabled || loading}
      {...rest}
    >
      {loading ? <Loader2 className="spin" size={16} /> : icon}
      {children && <span>{children}</span>}
    </button>
  );
}

// ---------- layout ----------

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <header className="page-header">
      <div>
        <h1>{title}</h1>
        {subtitle && <p className="muted">{subtitle}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}

export function Card({
  title,
  description,
  actions,
  children,
  className = "",
}: {
  title?: string;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <div className="card-head">
          <div>
            {title && <h2>{title}</h2>}
            {description && <p className="muted">{description}</p>}
          </div>
          {actions && <div className="card-actions">{actions}</div>}
        </div>
      )}
      <div className="card-body">{children}</div>
    </section>
  );
}

export function Grid({ cols = 2, children }: { cols?: 1 | 2 | 3; children: ReactNode }) {
  return <div className={`grid grid-${cols}`}>{children}</div>;
}

export function Field({
  label,
  hint,
  children,
  htmlFor,
  span,
}: {
  label?: ReactNode;
  hint?: ReactNode;
  children: ReactNode;
  htmlFor?: string;
  span?: boolean;
}) {
  return (
    <div className={`field ${span ? "span-all" : ""}`}>
      {label && (
        <label className="field-label" htmlFor={htmlFor}>
          {label}
        </label>
      )}
      {children}
      {hint && <div className="field-hint">{hint}</div>}
    </div>
  );
}

// ---------- inputs ----------

type InputProps = {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  id?: string;
  mono?: boolean;
};

export function TextInput({ value, onChange, placeholder, id, mono }: InputProps) {
  return (
    <input
      id={id}
      className={`input ${mono ? "mono" : ""}`}
      value={value}
      placeholder={placeholder}
      spellCheck={false}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

/** Поле ключа API: показывается открытым текстом (ключи лежат только на этом компьютере). */
export function SecretInput({ value, onChange, placeholder, id }: InputProps) {
  return (
    <input
      id={id}
      className="input mono"
      type="text"
      value={value}
      placeholder={placeholder}
      autoComplete="off"
      spellCheck={false}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

export function Textarea({
  value,
  onChange,
  placeholder,
  rows = 6,
  mono,
}: InputProps & { rows?: number }) {
  return (
    <textarea
      className={`input textarea ${mono ? "mono" : ""}`}
      value={value}
      rows={rows}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

export function Select({
  value,
  onChange,
  options,
  id,
}: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
  id?: string;
}) {
  return (
    <select id={id} className="input select" value={value} onChange={(e) => onChange(e.target.value)}>
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

export function NumberInput({
  value,
  onChange,
  min,
  max,
  step = 1,
  width = 96,
}: {
  value: number;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
  step?: number;
  width?: number;
}) {
  const [raw, setRaw] = useState(String(value));
  useEffect(() => setRaw(String(value)), [value]);
  const commit = (text: string) => {
    const n = Number(text.replace(",", "."));
    if (!Number.isFinite(n)) {
      setRaw(String(value));
      return;
    }
    const clamped = Math.min(max ?? n, Math.max(min ?? n, n));
    setRaw(String(clamped));
    if (clamped !== value) onChange(clamped);
  };
  return (
    <input
      className="input"
      style={{ width }}
      inputMode="decimal"
      value={raw}
      step={step}
      onChange={(e) => setRaw(e.target.value)}
      onBlur={(e) => commit(e.target.value)}
      onKeyDown={(e) => e.key === "Enter" && commit((e.target as HTMLInputElement).value)}
    />
  );
}

export function Toggle({
  checked,
  onChange,
  label,
  description,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: ReactNode;
  description?: ReactNode;
}) {
  const id = useId();
  return (
    <label className="toggle" htmlFor={id}>
      <input id={id} type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span className="toggle-track" aria-hidden>
        <span className="toggle-thumb" />
      </span>
      <span className="toggle-text">
        <span className="toggle-label">{label}</span>
        {description && <span className="toggle-desc">{description}</span>}
      </span>
    </label>
  );
}

const PICK_ICON: Record<PickKind, ReactNode> = {
  folder: <Folder size={16} />,
  audio: <FileAudio size={16} />,
  video: <FileVideo size={16} />,
  text: <FileAudio size={16} />,
  any: <Folder size={16} />,
};

/** Путь к папке/файлу: ручной ввод + системный диалог + проверка существования. */
export function PathInput({
  value,
  onChange,
  kind,
  placeholder,
  title,
}: {
  value: string;
  onChange: (v: string) => void;
  kind: PickKind;
  placeholder?: string;
  title?: string;
}) {
  const [busy, setBusy] = useState(false);
  const [state, setState] = useState<"none" | "ok" | "missing">("none");

  useEffect(() => {
    if (!value.trim()) {
      setState("none");
      return;
    }
    const t = window.setTimeout(() => {
      api
        .checkPath(value)
        .then((r) => {
          const ok = kind === "folder" ? r.is_dir : r.is_file;
          setState(ok ? "ok" : "missing");
        })
        .catch(() => setState("none"));
    }, 300);
    return () => window.clearTimeout(t);
  }, [value, kind]);

  const pick = async () => {
    setBusy(true);
    try {
      const res = await api.pick(kind, title);
      if (res.paths[0]) onChange(res.paths[0]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={`input-group path-${state}`}>
      <span className="input-prefix">{PICK_ICON[kind]}</span>
      <input
        className="input mono"
        value={value}
        placeholder={placeholder || (kind === "folder" ? "Папка не выбрана" : "Файл не выбран")}
        spellCheck={false}
        onChange={(e) => onChange(e.target.value)}
        title={state === "missing" ? "Не найдено" : value}
      />
      {state === "ok" && (
        <span className="input-status ok" title="Найдено">
          <Check size={15} />
        </span>
      )}
      {state === "missing" && <span className="input-status bad">не найдено</span>}
      {value && (
        <button type="button" className="input-addon" title="Очистить" onClick={() => onChange("")}>
          <X size={15} />
        </button>
      )}
      <button type="button" className="input-addon strong" onClick={pick} disabled={busy}>
        {busy ? <Loader2 size={15} className="spin" /> : "Выбрать…"}
      </button>
    </div>
  );
}

// ---------- misc ----------

export function Badge({ tone = "neutral", children }: { tone?: string; children: ReactNode }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function Progress({ value, tone }: { value: number; tone?: string }) {
  return (
    <div className={`progress ${tone ? `progress-${tone}` : ""}`}>
      <div className="progress-bar" style={{ width: `${Math.round(value * 100)}%` }} />
    </div>
  );
}

export function Empty({ icon, title, text }: { icon?: ReactNode; title: string; text?: ReactNode }) {
  return (
    <div className="empty">
      {icon}
      <div className="empty-title">{title}</div>
      {text && <div className="muted">{text}</div>}
    </div>
  );
}
