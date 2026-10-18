import { useState } from "react";
import { Check, Pencil, Plus, RefreshCw, Trash2, X } from "lucide-react";
import { api, type FieldSpec, type NamedListValue, type ProviderValues, type TtsProviderSpec } from "../lib/api";
import { useLoadedSettings, useToasts } from "../lib/store";
import { Button, Field, SecretInput, Select, TextInput } from "./ui";

/** Настройки сервиса озвучки по его описанию (поля приходят с сервера). */

export function useProvider(providerId?: string) {
  const { settings, meta, update } = useLoadedSettings();
  const id = providerId ?? settings.tts_provider;
  const spec = meta.tts_providers.find((p) => p.id === id) ?? meta.tts_providers[0];
  const values: ProviderValues = settings.tts[spec.id] ?? {};
  const set = (key: string, value: unknown) =>
    update({ tts: { ...settings.tts, [spec.id]: { ...values, [key]: value } } });
  return { spec, values, set };
}

function asList(v: unknown): NamedListValue {
  const raw = (v ?? {}) as Partial<NamedListValue>;
  return { items: Array.isArray(raw.items) ? raw.items : [], selected: raw.selected ?? "" };
}

export function ProviderFields({ spec, mode }: { spec: TtsProviderSpec; mode: "quick" | "full" }) {
  const { values, set } = useProvider(spec.id);
  const fields = spec.fields.filter((f) => !f.hidden && (mode === "full" || f.quick));
  return (
    <div className="stack">
      {fields.map((f) => (
        <FieldControl
          key={f.key}
          providerId={spec.id}
          field={f}
          value={values[f.key]}
          onChange={(v) => set(f.key, v)}
          mode={mode}
        />
      ))}
    </div>
  );
}

function FieldControl({
  providerId,
  field,
  value,
  onChange,
  mode,
}: {
  providerId: string;
  field: FieldSpec;
  value: unknown;
  onChange: (v: unknown) => void;
  mode: "quick" | "full";
}) {
  if (field.kind === "secret") {
    return (
      <Field label={field.label} hint={field.hint}>
        <SecretInput value={String(value ?? "")} onChange={onChange} placeholder={field.placeholder} />
      </Field>
    );
  }
  if (field.kind === "text") {
    return (
      <Field label={field.label} hint={field.hint}>
        <TextInput value={String(value ?? "")} onChange={onChange} placeholder={field.placeholder} />
      </Field>
    );
  }
  if (field.kind === "slider") {
    const num = Number(value ?? field.default ?? field.min);
    return (
      <Field label={`${field.label}: ${num.toFixed(2)}×`} hint={mode === "full" ? field.hint : undefined}>
        <input
          type="range"
          className="range"
          min={field.min}
          max={field.max}
          step={field.step}
          value={num}
          onChange={(e) => onChange(Number(e.target.value))}
        />
      </Field>
    );
  }
  if (field.kind === "multi_choice") {
    return <MultiChoice field={field} value={value} onChange={onChange} />;
  }
  const list = asList(value);
  if (field.kind === "remote_list") {
    return <RemoteList providerId={providerId} field={field} list={list} onChange={onChange} mode={mode} />;
  }
  return mode === "quick" ? (
    <QuickNamedList field={field} list={list} onChange={onChange} />
  ) : (
    <FullNamedList field={field} list={list} onChange={onChange} />
  );
}

function selectedId(list: NamedListValue) {
  return list.items.some((i) => i.id === list.selected) ? list.selected : list.items[0]?.id ?? "";
}

/** Добавить элемент: id + своё название. Повторный id — переименование. */
function addItem(list: NamedListValue, id: string, name: string): NamedListValue {
  const exists = list.items.some((i) => i.id === id);
  const items = exists ? list.items.map((i) => (i.id === id ? { id, name } : i)) : [...list.items, { id, name }];
  return { items, selected: id };
}

function AddForm({
  field,
  onAdd,
  onCancel,
}: {
  field: FieldSpec;
  onAdd: (id: string, name: string) => void;
  onCancel?: () => void;
}) {
  const [name, setName] = useState("");
  const [id, setId] = useState("");
  const { push } = useToasts();
  const submit = () => {
    if (!id.trim() || !name.trim()) {
      push({ kind: "error", title: `Нужны название и ${field.id_label}` });
      return;
    }
    onAdd(id.trim(), name.trim());
    setName("");
    setId("");
  };
  return (
    <div className="named-add">
      <TextInput value={name} onChange={setName} placeholder={`Название, например «${field.item_label} для рассказов»`} />
      <TextInput value={id} onChange={setId} placeholder={field.id_label} mono />
      <div className="row gap-sm">
        <Button icon={<Plus size={15} />} onClick={submit}>
          Добавить
        </Button>
        {onCancel && <Button variant="ghost" icon={<X size={15} />} onClick={onCancel} />}
      </div>
    </div>
  );
}

function QuickNamedList({
  field,
  list,
  onChange,
}: {
  field: FieldSpec;
  list: NamedListValue;
  onChange: (v: NamedListValue) => void;
}) {
  const [adding, setAdding] = useState(false);
  const options = list.items.length
    ? list.items.map((i) => ({ value: i.id, label: i.name }))
    : [{ value: "", label: field.fallback_id ? "по умолчанию" : "— не задано —" }];
  return (
    <Field label={field.label}>
      <div className="row gap-sm">
        <div style={{ flex: 1, minWidth: 0 }}>
          <Select value={selectedId(list)} onChange={(v) => onChange({ ...list, selected: v })} options={options} />
        </div>
        <Button variant="ghost" icon={<Plus size={16} />} title={`Добавить ${field.item_label}`} onClick={() => setAdding((v) => !v)} />
      </div>
      {adding && (
        <AddForm
          field={field}
          onAdd={(id, name) => {
            onChange(addItem(list, id, name));
            setAdding(false);
          }}
          onCancel={() => setAdding(false)}
        />
      )}
    </Field>
  );
}

function FullNamedList({
  field,
  list,
  onChange,
}: {
  field: FieldSpec;
  list: NamedListValue;
  onChange: (v: NamedListValue) => void;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const sel = selectedId(list);

  const rename = (id: string) => {
    const name = draft.trim();
    if (name) onChange({ ...list, items: list.items.map((i) => (i.id === id ? { ...i, name } : i)) });
    setEditing(null);
  };

  return (
    <Field label={field.label} hint={field.hint}>
      <div className="stack-sm">
        {list.items.length > 0 && (
          <div className="named-list">
            {list.items.map((item) => (
              <div key={item.id} className={`named-row ${item.id === sel ? "selected" : ""}`}>
                {editing === item.id ? (
                  <div className="named-edit">
                    <input
                      className="input"
                      autoFocus
                      value={draft}
                      onChange={(e) => setDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") rename(item.id);
                        if (e.key === "Escape") setEditing(null);
                      }}
                    />
                    <button className="icon-btn" title="Сохранить" onClick={() => rename(item.id)}>
                      <Check size={15} />
                    </button>
                  </div>
                ) : (
                  <button className="named-pick" onClick={() => onChange({ ...list, selected: item.id })} title="Выбрать по умолчанию">
                    <span className="radio" />
                    <span className="named-name">{item.name}</span>
                    <span className="mono-soft small muted ellipsis">{item.id}</span>
                  </button>
                )}
                {editing !== item.id && (
                  <button
                    className="icon-btn"
                    title="Переименовать"
                    onClick={() => {
                      setEditing(item.id);
                      setDraft(item.name);
                    }}
                  >
                    <Pencil size={14} />
                  </button>
                )}
                <button
                  className="icon-btn"
                  title="Удалить"
                  onClick={() => onChange({ ...list, items: list.items.filter((i) => i.id !== item.id) })}
                >
                  <Trash2 size={15} />
                </button>
              </div>
            ))}
          </div>
        )}
        {list.items.length === 0 && field.fallback_id && (
          <div className="muted small">Список пуст — используется вариант по умолчанию ({field.fallback_id})</div>
        )}
        <AddForm field={field} onAdd={(id, name) => onChange(addItem(list, id, name))} />
      </div>
    </Field>
  );
}

/** Несколько вариантов галочками-плашками (языки перевода). */
function MultiChoice({
  field,
  value,
  onChange,
}: {
  field: FieldSpec;
  value: unknown;
  onChange: (v: string[]) => void;
}) {
  const picked = new Set(Array.isArray(value) ? value.map(String) : []);
  const toggle = (v: string) => {
    const next = new Set(picked);
    if (next.has(v)) next.delete(v);
    else next.add(v);
    onChange(field.options.map(([o]) => o).filter((o) => next.has(o)));
  };
  return (
    <Field label={field.label} hint={field.hint}>
      <div className="chips" role="group" aria-label={field.label}>
        {field.options.map(([v, label]) => (
          <button
            key={v}
            type="button"
            role="checkbox"
            aria-checked={picked.has(v)}
            className={`chip ${picked.has(v) ? "active" : ""}`}
            onClick={() => toggle(v)}
          >
            <Check size={13} className="chip-check" />
            {label}
          </button>
        ))}
      </div>
    </Field>
  );
}

/** Список из кабинета сервиса (шаблоны бота): выпадающий список + «Обновить». */
function RemoteList({
  providerId,
  field,
  list,
  onChange,
  mode,
}: {
  providerId: string;
  field: FieldSpec;
  list: NamedListValue;
  onChange: (v: NamedListValue) => void;
  mode: "quick" | "full";
}) {
  const { update, flush } = useLoadedSettings();
  const { push } = useToasts();
  const [loading, setLoading] = useState(false);
  const refresh = async () => {
    setLoading(true);
    try {
      await flush(); // свежевставленный ключ должен успеть сохраниться
      const res = await api.refreshTtsList(providerId, field.key);
      update({ tts: res.settings.tts });
      push({
        kind: res.count ? "ok" : "info",
        title: res.count ? `Найдено: ${res.count}` : `Список пуст — создай ${field.item_label} в боте`,
      });
    } catch (e) {
      push({ kind: "error", title: "Не удалось обновить", text: (e as Error).message });
    } finally {
      setLoading(false);
    }
  };
  const options = list.items.length
    ? list.items.map((i) => ({ value: i.id, label: i.name }))
    : [{ value: "", label: "— нажми «Обновить» —" }];
  return (
    <Field label={field.label} hint={mode === "full" ? field.hint : undefined}>
      <div className="row gap-sm">
        <div style={{ flex: 1, minWidth: 0 }}>
          <Select value={selectedId(list)} onChange={(v) => onChange({ ...list, selected: v })} options={options} />
        </div>
        <Button variant="ghost" icon={<RefreshCw size={15} />} loading={loading} onClick={() => void refresh()} title="Подтянуть из бота">
          Обновить
        </Button>
      </div>
    </Field>
  );
}

/** Выбор сервиса: радиокарточки. */
export function ProviderPicker() {
  const { settings, meta, update } = useLoadedSettings();
  return (
    <div className="provider-picker" role="radiogroup">
      {meta.tts_providers.map((p) => {
        const active = p.id === settings.tts_provider;
        return (
          <button
            key={p.id}
            role="radio"
            aria-checked={active}
            className={`provider ${active ? "active" : ""}`}
            onClick={() => update({ tts_provider: p.id })}
          >
            <span className="radio" />
            <span className="provider-text">
              <span className="provider-name">{p.label}</span>
              <span className="muted small">{p.description}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

/** true, если у выбранного сервиса не заполнено обязательное поле (ключ). */
export function useTtsNeedsSetup() {
  const { spec, values } = useProvider();
  return spec.fields.some((f) => {
    if (!f.required || f.kind === "named_list") return false;
    if (f.kind === "remote_list") return asList(values[f.key]).items.length === 0;
    return !String(values[f.key] ?? "").trim();
  });
}
