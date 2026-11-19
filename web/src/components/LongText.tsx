import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Copy } from "lucide-react";
import { useToasts } from "../lib/store";

const URL_RE = /(https?:\/\/[^\s<>«»"')]+[^\s<>«»"'.,;:!?)\]])/g;

/** Ссылка в тексте коротко: «studio.youtube.com/channel/…/UCfev26tSPL2» — полный адрес в подсказке и при копировании. */
function shortUrl(u: string) {
  try {
    const x = new URL(u);
    const path = x.pathname.length > 26 ? `${x.pathname.slice(0, 12)}…${x.pathname.slice(-12)}` : x.pathname;
    return x.host + path + (x.search ? "?…" : "");
  } catch {
    return u.length > 42 ? `${u.slice(0, 40)}…` : u;
  }
}

function UrlLink({ url }: { url: string }) {
  const { push } = useToasts();
  const copy = (e: React.MouseEvent) => {
    e.stopPropagation();
    void navigator.clipboard
      ?.writeText(url)
      .then(() => push({ kind: "ok", title: "Ссылка скопирована" }))
      .catch(() => push({ kind: "error", title: "Не удалось скопировать", text: url }));
  };
  return (
    <span className="lt-url">
      <a href={url} target="_blank" rel="noreferrer" title={url} onClick={(e) => e.stopPropagation()}>
        {shortUrl(url)}
      </a>
      <button type="button" className="lt-copy" onClick={copy} title="Скопировать ссылку" aria-label="Скопировать ссылку">
        <Copy size={11} />
      </button>
    </span>
  );
}

/** Текст со ссылками: адреса — короткими кликабельными ярлыками (с кнопкой «скопировать»), переносы строк сохраняются. */
export function Linkified({ text }: { text: string }) {
  const parts: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(URL_RE)) {
    const at = m.index ?? 0;
    if (at > last) parts.push(text.slice(last, at));
    parts.push(<UrlLink key={at} url={m[0]} />);
    last = at + m[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return <>{parts}</>;
}

/** Длинный текст в узкой панели: переносится по словам (в том числе длинные слова и пути), по умолчанию занимает
 *  не больше `lines` строк; если не влезло — «Показать полностью» / «Свернуть». Ссылки — компактными ярлыками. */
export function LongText({ text, lines = 4, className = "" }: { text: string; lines?: number; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [cut, setCut] = useState(false);

  const measure = () => {
    const el = ref.current;
    if (el && !open) setCut(el.scrollHeight > el.clientHeight + 1);
  };
  useLayoutEffect(measure, [text, open, lines]);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  return (
    <div className={`lt ${className}`}>
      <div ref={ref} className={`lt-text ${open ? "" : "lt-clamp"}`} style={open ? undefined : { WebkitLineClamp: lines }}>
        <Linkified text={text} />
      </div>
      {(cut || open) && (
        <button type="button" className="lt-toggle" onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}>
          {open ? "Свернуть" : "Показать полностью"}
        </button>
      )}
    </div>
  );
}
