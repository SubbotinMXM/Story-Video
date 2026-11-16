import { useCallback, useEffect, useRef, useState } from "react";
import { FolderOpen, ImagePlus, Sparkles, Trash2, X } from "lucide-react";
import { Badge, Button, Card, Field, PageHeader, TextInput } from "../components/ui";
import { CoverSettingsFields } from "../components/CoverSettings";
import { goTo } from "../lib/nav";
import { LANGS } from "../lib/langs";
import { api } from "../lib/api";
import { useJobs, useToasts } from "../lib/store";


const readAsDataUrl = (file: Blob) =>
  new Promise<string>((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result));
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });

/** Обложки по образцу: прикрепляешь обложку (или ссылку на ролик) → похожие кликбейтные варианты с другими людьми
 *  → лучший → по желанию переводы надписей на другие языки. */
export function CoversPage() {
  const { enqueue, jobs } = useJobs();
  const { push } = useToasts();
  const [image, setImage] = useState("");
  const [url, setUrl] = useState("");
  const [language, setLanguage] = useState("");
  const [translations, setTranslations] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [drag, setDrag] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const pageRef = useRef<HTMLDivElement>(null);

  const upload = useCallback(
    async (file: Blob) => {
      if (!file.type.startsWith("image/")) {
        push({ kind: "error", title: "Это не картинка" });
        return;
      }
      setUploading(true);
      try {
        const r = await api.coverUpload(await readAsDataUrl(file));
        setImage(r.path);
        setUrl("");
      } catch (e) {
        push({ kind: "error", title: "Картинка не загрузилась", text: (e as Error).message });
      } finally {
        setUploading(false);
      }
    },
    [push],
  );

  // вставка скриншота из буфера (Cmd+V) — пока открыта эта страница
  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      if (!pageRef.current || pageRef.current.offsetParent === null) return;
      const item = [...(e.clipboardData?.items ?? [])].find((i) => i.type.startsWith("image/"));
      const file = item?.getAsFile();
      if (file) {
        e.preventDefault();
        void upload(file);
      }
    };
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
  }, [upload]);

  const youtube = /(youtu\.be\/|youtube\.com\/)/.test(url.trim());
  const ready = Boolean(image) || youtube;
  const results = [...jobs].reverse().filter((j) => j.kind === "cover").slice(0, 12);

  const start = async () => {
    setBusy(true);
    const ok = await enqueue([
      { kind: "cover", params: { image: image || "", url: image ? "" : url.trim(), language, translations } },
    ]);
    setBusy(false);
    if (ok) {
      setImage("");
      setUrl("");
    }
  };

  return (
    <div className="page narrow" ref={pageRef}>
      <PageHeader
        title="Обложки"
        subtitle="Прикрепи обложку-образец — получишь свою: та же подача и кликбейт, другие люди и текст. Несколько вариантов, нейросеть выбирает лучший."
      />
      <Card title="Образец">
        <div className="stack">
          <div
            className={`cover-drop ${drag ? "drag" : ""}`}
            onDragOver={(e) => {
              e.preventDefault();
              setDrag(true);
            }}
            onDragLeave={() => setDrag(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDrag(false);
              const f = e.dataTransfer.files?.[0];
              if (f) void upload(f);
            }}
            onClick={() => fileRef.current?.click()}
          >
            {image ? (
              <div className="cover-ref">
                <img src={api.coverImageUrl(image)} alt="Образец" />
                <button
                  className="icon-btn cover-ref-x"
                  title="Убрать образец"
                  onClick={(e) => {
                    e.stopPropagation();
                    setImage("");
                  }}
                >
                  <X size={16} />
                </button>
              </div>
            ) : (
              <div className="muted">
                <ImagePlus size={28} />
                <div>{uploading ? "Загружаю…" : "Перетащи картинку сюда, вставь скриншот (⌘V) или нажми, чтобы выбрать файл"}</div>
              </div>
            )}
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              hidden
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) void upload(f);
                e.target.value = "";
              }}
            />
          </div>
          {!image && (
            <Field label="…или ссылка на ролик YouTube" hint="Программа возьмёт его обложку в максимальном размере">
              <TextInput value={url} onChange={setUrl} placeholder="https://www.youtube.com/watch?v=…" />
            </Field>
          )}
        </div>
      </Card>

      <Card title="Текст и языки">
        <div className="stack">
          <Field label="Язык надписей" hint="Тот же смысл и подача, что на образце, но своими словами — на этом языке">
            <select className="input select" value={language} onChange={(e) => setLanguage(e.target.value)}>
              <option value="">Как на образце</option>
              {LANGS.map((l) => (
                <option key={l.code} value={l.code}>
                  {l.name}
                </option>
              ))}
            </select>
          </Field>
          <Field
            label="Перевести готовую обложку (необязательно)"
            hint="Та же картинка — те же люди, фон и цвета, меняются только надписи. Отдельный файл на каждый язык."
          >
            <div className="row gap-sm wrap">
              {LANGS.filter((l) => l.code !== language).map((l) => (
                <label key={l.code} className="row gap-sm small">
                  <input
                    type="checkbox"
                    checked={translations.includes(l.code)}
                    onChange={(e) =>
                      setTranslations((cur) => (e.target.checked ? [...cur, l.code] : cur.filter((x) => x !== l.code)))
                    }
                  />
                  {l.name}
                </label>
              ))}
            </div>
          </Field>
          <CoverSettingsFields />
          <div className="muted small">
            По каким промптам делаются обложки:{" "}
            <button type="button" className="link-btn" onClick={() => goTo("settings", "cover-prompts")}>
              Настройки → Промпты названий и обложек
            </button>
          </div>
          <div>
            <Button variant="primary" size="lg" icon={<Sparkles size={17} />} onClick={start} loading={busy} disabled={!ready || uploading}>
              Пуск
            </Button>
          </div>
        </div>
      </Card>

      {results.map((j) => (
        <Card
          key={j.id}
          title={j.title}
          description={j.status === "done" ? j.result?.summary : j.status === "failed" ? j.error ?? "" : j.message}
          actions={
            <span className="row gap-sm">
              {(j.result?.model || (j.params.model as string)) && (
                <Badge tone="accent">{j.result?.model || (j.params.model as string)}</Badge>
              )}
              <span className="muted small nowrap">{new Date(j.created * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}</span>
              {j.status !== "running" && j.status !== "queued" && (
                <button
                  type="button"
                  className="icon-btn"
                  title="Убрать из списка (файлы обложек на компьютере остаются)"
                  onClick={() => void api.resetJob(j.id).catch((e) => push({ kind: "error", title: "Не удалилось", text: (e as Error).message }))}
                >
                  <Trash2 size={15} />
                </button>
              )}
            </span>
          }
        >
          {j.status === "done" && j.result?.images ? (
            <div className="stack">
              <div className="cover-grid">
                {j.result.images.map((p, n) => (
                  <figure key={p}>
                    <img src={api.coverImageUrl(p)} alt="Обложка" />
                    <figcaption className="muted small">{n === 0 ? "Лучшая" : `Перевод: ${p.match(/_(\w+)\.jpg$/)?.[1] ?? ""}`}</figcaption>
                  </figure>
                ))}
              </div>
              {(j.result.variants?.length ?? 0) > 1 && (
                <details>
                  <summary className="muted small">Все варианты ({j.result.variants!.length})</summary>
                  <div className="cover-grid small-tiles">
                    {j.result.variants!.map((p) => (
                      <img key={p} src={api.coverImageUrl(p)} alt="Вариант" />
                    ))}
                  </div>
                </details>
              )}
              {(j.result.warnings ?? []).map((w) => (
                <div key={w} className="notice warn small">{w}</div>
              ))}
              {(j.result.lines ?? []).map((l) => (
                <div key={l} className="muted small">{l}</div>
              ))}
              <div>
                <Button icon={<FolderOpen size={15} />} onClick={() => api.reveal(j.result!.image!)}>
                  Показать в Finder
                </Button>
              </div>
            </div>
          ) : null}
        </Card>
      ))}
    </div>
  );
}
