import { useState, type DragEvent } from "react";
import { FilePlus2, FileText, ListPlus, Trash2, Upload } from "lucide-react";
import { api } from "../lib/api";
import { useToasts } from "../lib/store";
import { Button, PathInput } from "./ui";

export type TextItem = { id: string; text: string; source: string | null };

const TEXT_EXT = /\.(txt|md|text)$/i;

let seq = 0;
const newId = () => `t${Date.now().toString(36)}${(seq++).toString(36)}`;

export function words(text: string) {
  const t = text.trim();
  return t ? t.split(/\s+/).length : 0;
}

/**
 * Ввод текста + очередь текстов + готовое аудио.
 * Каждый текст в очереди → отдельный ролик.
 */
export function TextComposer({
  draft,
  setDraft,
  items,
  setItems,
  audio,
  setAudio,
  placeholder,
}: {
  draft: string;
  setDraft: (v: string) => void;
  items: TextItem[];
  setItems: (fn: (prev: TextItem[]) => TextItem[]) => void;
  audio: string;
  setAudio: (v: string) => void;
  placeholder: string;
}) {
  const { push } = useToasts();
  const [over, setOver] = useState(false);

  const addTexts = (list: { text: string; source: string | null }[]) => {
    const good = list.filter((t) => t.text.trim());
    if (!good.length) return;
    setItems((prev) => [...prev, ...good.map((t) => ({ ...t, text: t.text.trim(), id: newId() }))]);
  };

  const addDraft = () => {
    if (!draft.trim()) return;
    addTexts([{ text: draft, source: null }]);
    setDraft("");
  };

  const onDrop = async (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    const files = Array.from(e.dataTransfer.files).filter((f) => TEXT_EXT.test(f.name));
    if (files.length) {
      const read = await Promise.all(files.map(async (f) => ({ text: await f.text(), source: f.name })));
      addTexts(read);
      push({ kind: "info", title: `Добавлено текстов: ${read.length}` });
      return;
    }
    const plain = e.dataTransfer.getData("text/plain");
    if (plain.trim()) setDraft(draft ? `${draft}\n${plain}` : plain);
  };

  const loadFiles = async () => {
    const res = await api.pick("text", "Тексты рассказов", true);
    if (res.texts?.length) addTexts(res.texts.map((t) => ({ text: t.text, source: t.name })));
  };

  const n = words(draft);

  return (
    <div className="composer">
      <div
        className={`dropzone ${over ? "over" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={onDrop}
      >
        <textarea
          className="input textarea composer-text"
          value={draft}
          placeholder={placeholder}
          onChange={(e) => setDraft(e.target.value)}
        />
        {over && (
          <div className="dropzone-hint">
            <Upload size={22} /> Отпусти .txt — каждый файл станет отдельным роликом
          </div>
        )}
      </div>
      <div className="composer-bar">
        <span className="muted small">
          {n ? `${n} слов · ${draft.trim().length} символов` : "Вставь текст или перетащи .txt файлы"}
        </span>
        <div className="row gap-sm">
          <Button size="sm" variant="ghost" icon={<FilePlus2 size={15} />} onClick={loadFiles}>
            Загрузить .txt
          </Button>
          <Button size="sm" icon={<ListPlus size={15} />} onClick={addDraft} disabled={!draft.trim()}>
            В очередь
          </Button>
        </div>
      </div>

      {items.length > 0 && (
        <div className="text-list">
          <div className="text-list-head">
            <span>
              Очередь текстов <b>{items.length}</b>
            </span>
            <Button size="sm" variant="ghost" onClick={() => setItems(() => [])}>
              Очистить
            </Button>
          </div>
          {items.map((it, i) => (
            <div key={it.id} className="text-item">
              <span className="text-item-num">{i + 1}</span>
              <FileText size={16} className="muted" />
              <div className="text-item-body">
                <div className="text-item-title">{it.source || it.text.slice(0, 60)}</div>
                <div className="muted small ellipsis">
                  {words(it.text)} слов · {it.text.slice(0, 140)}
                </div>
              </div>
              <button
                className="icon-btn"
                title="Убрать"
                onClick={() => setItems((prev) => prev.filter((x) => x.id !== it.id))}
              >
                <Trash2 size={15} />
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="field">
        <label className="field-label">Готовое аудио (необязательно)</label>
        <PathInput value={audio} onChange={setAudio} kind="audio" title="Готовое аудио" placeholder="Если выбрано — озвучку сервисом пропускаем" />
        <div className="field-hint">Только для одного ролика. Текст тогда нужен лишь для субтитров и темы.</div>
      </div>
    </div>
  );
}

/** Тексты для постановки в очередь: очередь + неотправленный черновик. */
export function collectTexts(draft: string, items: TextItem[]): { text: string; source: string | null }[] {
  const out = items.map((i) => ({ text: i.text, source: i.source }));
  if (draft.trim()) out.push({ text: draft.trim(), source: null });
  return out;
}
