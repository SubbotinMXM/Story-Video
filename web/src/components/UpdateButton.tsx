import { useCallback, useEffect, useState } from "react";
import { ArrowDownToLine, CheckCircle2, Clock, Loader2, RefreshCw, TriangleAlert } from "lucide-react";
import { api, type UpdateStatus } from "../lib/api";
import { useSettings, useToasts } from "../lib/store";
import { Modal } from "./Modal";
import { Toggle } from "./ui";

const stamp = (t: number) =>
  new Date(t * 1000).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const whenMsk = (t: number) =>
  new Date(t * 1000).toLocaleString("ru-RU", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit", timeZone: "Europe/Moscow" }) + " МСК";

/** Кнопка обновления в боковой панели — доступна с любой страницы. Значок, когда на сервере есть новое. */
export function UpdateButton() {
  const [st, setSt] = useState<UpdateStatus | null>(null);
  const [open, setOpen] = useState(false);
  const refresh = useCallback(() => api.updateStatus().then(setSt).catch(() => undefined), []);
  useEffect(() => {
    void refresh();
    const t = setInterval(() => void refresh(), 5 * 60 * 1000);
    return () => clearInterval(t);
  }, [refresh]);
  const waiting = Boolean(st?.pending && st.pending.mode);
  return (
    <>
      <button className="ver-btn upd-btn" onClick={() => setOpen(true)} title="Обновление программы">
        {waiting ? <Clock size={13} /> : <ArrowDownToLine size={13} />}
        <span>{waiting ? "Обновление ждёт очередь" : st && st.behind > 0 ? "Есть обновление" : "Обновления"}</span>
        {st && st.behind > 0 && <span className="dot" />}
      </button>
      {open && <UpdateDialog initial={st} onClose={() => setOpen(false)} onChange={setSt} />}
    </>
  );
}

function UpdateDialog({ initial, onClose, onChange }: { initial: UpdateStatus | null; onClose: () => void; onChange: (s: UpdateStatus) => void }) {
  const { settings, update } = useSettings();
  const { push } = useToasts();
  const [st, setSt] = useState<UpdateStatus | null>(initial);
  const [busy, setBusy] = useState<"" | "check" | "apply">("");
  const [restarting, setRestarting] = useState(false);
  const [confirmNow, setConfirmNow] = useState(false);
  const auto = { enabled: false, time: "04:00", ...(settings?.update_auto ?? {}) };

  const set = (s: UpdateStatus) => {
    setSt(s);
    onChange(s);
  };
  const check = async () => {
    setBusy("check");
    try {
      set(await api.updateCheck());
    } catch (e) {
      push({ kind: "error", title: "Проверка не вышла", text: (e as Error).message });
    } finally {
      setBusy("");
    }
  };
  // открыли окно — сразу спрашиваем сервер, чтобы цифры были свежими
  useEffect(() => {
    void check();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // после перезапуска программа вернётся на тот же адрес или откроется новым окном; в браузере — обновим страницу
  const waitForReturn = () => {
    setRestarting(true);
    let down = false;
    const t = setInterval(async () => {
      try {
        await api.meta();
        if (down) {
          clearInterval(t);
          window.location.reload();
        }
      } catch {
        down = true;
      }
    }, 2000);
  };
  const apply = async (when: "now" | "idle") => {
    setBusy("apply");
    try {
      const r = await api.updateApply(when);
      set(r);
      if (when === "idle") push({ kind: "ok", title: "Обновление поставлено в ожидание", text: "Программа обновится и перезапустится, как только очередь освободится" });
      else if (r.started) waitForReturn();
      else push({ kind: "info", title: "Уже последняя версия" });
    } catch (e) {
      push({ kind: "error", title: "Обновление не вышло", text: (e as Error).message });
    } finally {
      setBusy("");
      setConfirmNow(false);
    }
  };
  const cancelPending = async () => set(await api.updateCancel());

  const behind = st?.behind ?? 0;
  const pending = Boolean(st?.pending && st.pending.mode);
  return (
    <Modal
      title="Обновление программы"
      subtitle={st ? `Сейчас: сборка ${st.current || "—"}${st.checked_at ? ` · проверено ${stamp(st.checked_at)}` : ""}` : undefined}
      onClose={restarting ? () => undefined : onClose}
      closable={!restarting}
      width={640}
      footer={
        !restarting && (
          <>
            <button className="btn btn-ghost" onClick={() => void check()} disabled={busy !== ""}>
              {busy === "check" ? <Loader2 size={15} className="spin" /> : <RefreshCw size={15} />} Проверить
            </button>
            <button className="btn btn-ghost" onClick={onClose}>Закрыть</button>
          </>
        )
      }
    >
      {restarting ? (
        <div className="upd-restart" role="status">
          <Loader2 size={22} className="spin" />
          <div>
            <b>Перезапускаю программу…</b>
            <div className="muted small">
              Окно закроется и откроется снова. Если обновление принесло новые библиотеки, первый запуск может занять несколько минут — не закрывай окно терминала.
            </div>
          </div>
        </div>
      ) : (
        <div className="upd-body">
          {st && !st.ok && (
            <div className="upd-alert bad"><TriangleAlert size={16} /> <span>{st.error}</span></div>
          )}
          {st?.last_error && st.last_error_at > (st.last_applied || 0) && (
            <div className="upd-alert bad"><TriangleAlert size={16} /> <span>Последнее автообновление не вышло ({stamp(st.last_error_at)}): {st.last_error}</span></div>
          )}
          {st?.ok && behind === 0 && (
            <div className="upd-alert ok"><CheckCircle2 size={16} /> <span>Установлена последняя версия.</span></div>
          )}
          {st?.ok && behind > 0 && (
            <div className="upd-new">
              <div>
                <b>Доступно обновление{st.version ? `: версия ${st.version}` : ""}</b>
                {st.title && <div className="muted">{st.title}</div>}
              </div>
              <ul className="upd-commits">
                {st.commits.slice(0, 12).map((c, i) => <li key={i}>{c}</li>)}
                {st.commits.length > 12 && <li className="muted">…и ещё {st.commits.length - 12}</li>}
              </ul>
              {st.dirty.length > 0 && (
                <div className="upd-alert warn">
                  <TriangleAlert size={16} />
                  <span>В папке программы есть правки ({st.dirty.slice(0, 3).join(", ")}). Если они мешают, обновление остановится с понятной ошибкой — ничего не потеряется.</span>
                </div>
              )}
              {pending ? (
                <div className="upd-alert warn">
                  <Clock size={16} />
                  <span>Обновление ждёт, пока освободится очередь{st.pending.source === "auto" ? " (по расписанию)" : ""}. Идущая работа не прерывается.</span>
                  <button className="btn btn-ghost btn-sm" onClick={() => void cancelPending()}>Отменить</button>
                </div>
              ) : confirmNow ? (
                <div className="upd-alert warn">
                  <TriangleAlert size={16} />
                  <span>Сейчас идёт работа. Если обновить сразу, она прервётся (потом её можно продолжить кнопкой «Продолжить»). Обновить сейчас?</span>
                  <button className="btn btn-primary btn-sm" onClick={() => void apply("now")} disabled={busy !== ""}>Да, сейчас</button>
                  <button className="btn btn-ghost btn-sm" onClick={() => setConfirmNow(false)}>Нет</button>
                </div>
              ) : (
                <div className="upd-actions">
                  {st.busy ? (
                    <>
                      <button className="btn btn-primary" onClick={() => void apply("idle")} disabled={busy !== ""}>
                        <Clock size={15} /> Обновить, когда очередь освободится
                      </button>
                      <button className="btn btn-ghost" onClick={() => setConfirmNow(true)} disabled={busy !== ""}>Обновить сейчас…</button>
                    </>
                  ) : (
                    <button className="btn btn-primary" onClick={() => void apply("now")} disabled={busy !== ""}>
                      {busy === "apply" ? <Loader2 size={15} className="spin" /> : <ArrowDownToLine size={15} />} Обновить и перезапустить
                    </button>
                  )}
                </div>
              )}
            </div>
          )}

          <div className="upd-auto">
            <Toggle
              checked={auto.enabled}
              onChange={(enabled) => update({ update_auto: { ...auto, enabled } })}
              label="Автообновление каждый день"
              description="В это время программа проверит обновления и, если они есть, установится и перезапустится — но только когда очередь свободна: идущую работу не прерывает."
            />
            <div className="an-auto-fields">
              <span className="an-auto-label"><Clock size={14} /> в</span>
              <input
                className="input an-auto-time"
                type="time"
                step={300}
                value={auto.time}
                onChange={(e) => e.target.value && update({ update_auto: { ...auto, time: e.target.value } })}
              />
              <span className="an-auto-label">МСК</span>
            </div>
            <div className="muted small">
              {auto.enabled
                ? st?.next_at ? `Следующая проверка: ${whenMsk(st.next_at)}.` : "Включено."
                : "Выключено — обновляется только по кнопке."}
              {st && st.last_applied > 0 && ` Последнее обновление: ${stamp(st.last_applied)}.`}
            </div>
          </div>
        </div>
      )}
    </Modal>
  );
}
