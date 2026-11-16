import { useEffect, useState } from "react";
import { Copy, Plus, RotateCcw, Trash2 } from "lucide-react";
import type { CoverPrompt } from "../lib/api";
import { useLoadedSettings } from "../lib/store";
import { Button, Card, Field, Select, Textarea, TextInput } from "./ui";

const TITLE_ROLES = ["title", "title_story", "title_pick"];

function newId() {
  return `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
}

/** Библиотека как на сервере (engine/publish/cover_prompts.library): свои версии + стандартные для ролей без своих. */
function useLibrary(): CoverPrompt[] {
  const { settings, meta } = useLoadedSettings();
  const own = (settings.cover_prompts ?? []).filter((p) => p.text.trim());
  const roles = new Set(own.map((p) => p.role));
  return [...own, ...(meta.cover_default_prompts ?? []).filter((d) => !roles.has(d.role))];
}

/** Промпты обложек: каждый шаг (разбор образца, правила генератора, выбор лучшего…) — свой промпт;
 *  можно завести свои версии и выбрать, какая работает. Действует и в разделе «Обложки», и при выкладке слайдов. */
export function CoverPromptsCard() {
  const { settings, meta, update } = useLoadedSettings();
  const lib = useLibrary();
  const roles = meta.cover_prompt_roles ?? [];
  const [role, setRole] = useState(roles[0]?.id ?? "analyze");
  const inRole = lib.filter((p) => p.role === role);
  const chosen = settings.cover_prompt?.[role];
  const prompt = inRole.find((p) => p.id === chosen) ?? inRole[0];
  const info = roles.find((r) => r.id === role);
  const isDefault = prompt?.id.startsWith("default-") && prompt.text === (meta.cover_default_prompts ?? []).find((d) => d.id === prompt.id)?.text;

  useEffect(() => {
    if (!roles.some((r) => r.id === role) && roles[0]) setRole(roles[0].id);
  }, [roles, role]);

  // храним только свои и изменённые промпты: неизменённая копия стандартного «заморозила» бы его улучшения
  const keep = (p: CoverPrompt) => {
    const d = (meta.cover_default_prompts ?? []).find((x) => x.id === p.id);
    return !(d && d.text === p.text && d.name === p.name);
  };
  const save = (next: CoverPrompt[], select?: string) =>
    update({
      cover_prompts: next.filter(keep),
      ...(select ? { cover_prompt: { ...(settings.cover_prompt ?? {}), [role]: select } } : {}),
    });
  const edit = (patch: Partial<CoverPrompt>) => prompt && save(lib.map((p) => (p.id === prompt.id ? { ...p, ...patch } : p)));
  const add = (copy: boolean) => {
    const p: CoverPrompt =
      copy && prompt
        ? { ...prompt, id: newId(), name: `${prompt.name} (копия)` }
        : { id: newId(), name: "Мой промпт", role, text: prompt?.text ?? "" };
    save([...lib, p], p.id);
  };
  const remove = () => {
    if (!prompt || inRole.length <= 1) return;
    const rest = lib.filter((p) => p.id !== prompt.id);
    save(rest, rest.find((p) => p.role === role)?.id);
  };
  const resetRole = () => {
    const def = (meta.cover_default_prompts ?? []).find((d) => d.role === role)!;
    save([...lib.filter((p) => p.role !== role), def], def.id);
  };
  const missing = prompt ? (info?.variables ?? []).filter((v) => !prompt.text.includes(v)) : [];

  return (
    <Card
      id="cover-prompts"
      collapsible
      defaultOpen={false}
      title="Промпты названий и обложек"
      description="Названия роликов «Стоковых слайдов» и обложки (раздел «Обложки» и выкладка) делаются по шагам — у каждого шага свой промпт. Можно править стандартный или завести свои версии и выбрать, какая работает. Формат ответа нейросети программа дописывает сама."
    >
      {roles.length === 0 ? (
        <div className="muted small">Перезапусти приложение — промпты появятся после перезапуска.</div>
      ) : (
      <div className="stack">
        <Field label="Шаг" hint={info?.hint}>
          <Select
            value={role}
            onChange={setRole}
            options={[...roles]
              .sort((a, b) => Number(!TITLE_ROLES.includes(a.id)) - Number(!TITLE_ROLES.includes(b.id)))
              .map((r) => ({ value: r.id, label: `${TITLE_ROLES.includes(r.id) ? "Название" : "Обложка"}: ${r.label}` }))}
          />
        </Field>
        <Field label="Какой промпт работает" hint={inRole.length > 1 ? "Выбранный здесь — используется при генерации" : undefined}>
          <div className="row gap-sm" style={{ flexWrap: "wrap" }}>
            <div style={{ flex: 1, minWidth: 200 }}>
              <Select
                value={prompt?.id ?? ""}
                onChange={(id) => update({ cover_prompt: { ...(settings.cover_prompt ?? {}), [role]: id } })}
                options={inRole.map((p) => ({ value: p.id, label: p.name }))}
              />
            </div>
            <Button size="sm" variant="ghost" icon={<Plus size={14} />} onClick={() => add(false)} title="Новый промпт (на основе текущего)" />
            <Button size="sm" variant="ghost" icon={<Copy size={14} />} onClick={() => add(true)} title="Копия" />
            <Button size="sm" variant="ghost" icon={<Trash2 size={14} />} onClick={remove} title="Удалить" disabled={inRole.length <= 1} />
          </div>
        </Field>
        {prompt && (
          <>
            <Field label="Название">
              <TextInput value={prompt.name} onChange={(v) => edit({ name: v })} />
            </Field>
            <Field label="Текст промпта" hint={info?.variables.length ? `Переменные: ${info.variables.join(", ")} — программа подставит их при запуске` : undefined}>
              <Textarea value={prompt.text} onChange={(v) => edit({ text: v })} rows={16} />
            </Field>
            {missing.length > 0 && (
              <div className="notice warn small">
                В промпте нет {missing.join(", ")} — эти данные в него не попадут (например, язык надписей).
              </div>
            )}
          </>
        )}
        <div className="row gap-sm wrap">
          {!isDefault && (
            <Button size="sm" variant="ghost" icon={<RotateCcw size={14} />} onClick={resetRole}>
              Вернуть стандартный для этого шага
            </Button>
          )}
          {settings.cover_prompts?.length > 0 && (
            <Button size="sm" variant="ghost" icon={<RotateCcw size={14} />} onClick={() => update({ cover_prompts: [], cover_prompt: {} })}>
              Вернуть все промпты по умолчанию
            </Button>
          )}
        </div>
      </div>
      )}
    </Card>
  );
}
