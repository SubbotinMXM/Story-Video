import { useEffect, useState } from "react";
import { api, type TemplateSection } from "./api";

/** Какой шаблон сейчас правится: «Изменить» загружает его в настройки и открывает «Генерацию по шагам», где у
 *  каждой настройки видно «было в шаблоне → стало»; баннер сверху предлагает «Сохранить в шаблон».
 *  Запоминается, чтобы пережить перезагрузку окна. */
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

/** Как шаблон был сохранён (до правок) — основа для «было → стало». null — пока грузится или шаблона уже нет. */
export function useEditingBase(editing: EditingTemplate): Record<string, unknown> | null {
  const [base, setBase] = useState<{ id: string; settings: Record<string, unknown> } | null>(null);
  const id = editing?.id;
  useEffect(() => {
    if (!id) {
      setBase(null);
      return;
    }
    let live = true;
    api.templateBase(id).then((b) => live && setBase({ id, settings: b.settings })).catch(() => live && setBase(null));
    return () => {
      live = false;
    };
  }, [id]);
  return base && base.id === id ? base.settings : null;
}
