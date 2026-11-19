import { useEffect, useState } from "react";
import type { TemplateSection } from "./api";

/** Какой шаблон сейчас правится: «Изменить» загружает его в настройки, правки идут на привычных экранах,
 *  а баннер сверху предлагает «Сохранить в шаблон». Запоминается, чтобы пережить перезагрузку окна. */
export type EditingTemplate = { id: string; name: string; section: TemplateSection } | null;

const KEY = "editing-template";
const EVENT = "sv-template-edit";

function read(): EditingTemplate {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) || "null");
    return v && v.id && v.name ? (v as EditingTemplate) : null;
  } catch {
    return null;
  }
}

export function setEditingTemplate(v: EditingTemplate) {
  try {
    if (v) localStorage.setItem(KEY, JSON.stringify(v));
    else localStorage.removeItem(KEY);
  } catch {
    /* storage недоступен */
  }
  window.dispatchEvent(new CustomEvent(EVENT));
}

export function useEditingTemplate(): EditingTemplate {
  const [v, setV] = useState<EditingTemplate>(read);
  useEffect(() => {
    const on = () => setV(read());
    window.addEventListener(EVENT, on);
    return () => window.removeEventListener(EVENT, on);
  }, []);
  return v;
}
