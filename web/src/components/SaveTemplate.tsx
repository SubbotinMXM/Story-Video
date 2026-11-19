import { useState } from "react";
import { LayoutTemplate } from "lucide-react";
import { api, type TemplateSection } from "../lib/api";
import { goTo } from "../lib/nav";
import { useLoadedSettings, useToasts } from "../lib/store";
import { setEditingTemplate } from "../lib/templateEdit";
import { Button } from "./ui";
import { Modal } from "./Modal";

const NAMES: Record<TemplateSection, string> = { broll: "Рассказы", slides: "Стоковые слайды" };

/** «Сохранить как шаблон»: текущие настройки раздела (источник, озвучка, каналы выкладки, расписание…) → шаблон. */
export function SaveTemplateButton({ section }: { section: TemplateSection }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const { flush } = useLoadedSettings();
  const { push } = useToasts();
  const close = () => {
    setOpen(false);
    setError("");
  };
  const save = async () => {
    setBusy(true);
    setError("");
    try {
      await flush(); // настройки, которые ещё не успели сохраниться, — тоже в шаблон
      const t = await api.createTemplate(name, section);
      setEditingTemplate(null);
      push({ kind: "ok", title: `Шаблон «${t.name}» сохранён`, text: "Запускать — на странице «Шаблоны»" });
      setName("");
      setOpen(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <Button icon={<LayoutTemplate size={16} />} onClick={() => setOpen(true)} title="Запомнить все настройки этого раздела как шаблон — потом запускать в один клик">
        Сохранить как шаблон
      </Button>
      {open && (
        <Modal
          title="Сохранить как шаблон"
          subtitle={`${NAMES[section]}: источник, озвучка, каналы выкладки, расписание — всё, что настроено сейчас`}
          onClose={close}
          width={520}
          footer={
            <>
              <Button variant="ghost" onClick={() => goTo("templates")}>
                К шаблонам
              </Button>
              <Button variant="primary" loading={busy} disabled={!name.trim()} onClick={() => void save()}>
                Сохранить
              </Button>
            </>
          }
        >
          <label className="md-label" htmlFor="tpl-name">
            Название шаблона
          </label>
          <input
            id="tpl-name"
            className="input"
            autoFocus
            maxLength={80}
            placeholder="Например: Жизненные рассказы · 4 канала"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && name.trim() && void save()}
          />
          {error && <div className="badge badge-bad md-error">{error}</div>}
          <div className="muted small md-note">
            В шаблон входят каналы-конкуренты («Источник → Ссылки на каналы»), промпт рерайта, озвучка, режим подбора, AI, каналы и соавторы
            выкладки, шаг и время. Даты из календаря и ключи сервисов не сохраняются.
          </div>
        </Modal>
      )}
    </>
  );
}
