import { useMemo, useState } from "react";
import { Subs } from "./Subs";
import { RefreshCw, RotateCcw, Search, Square, X } from "lucide-react";
import { api, type PublishProfile, type Settings } from "../lib/api";
import {
  useJobs,
  useLoadedSettings,
  useProfiles,
  useToasts,
} from "../lib/store";
import {
  Badge,
  Button,
  Card,
  Disclosure,
  Field,
  NumberInput,
  PathInput,
  Progress,
  Select,
  Textarea,
  Toggle,
} from "./ui";
import { goTo } from "../lib/nav";
import { LANGS } from "../lib/langs";
import { CoverSettingsFields } from "./CoverSettings";
import { GroupFilter, inFilter, useGroupFilter } from "./GroupFilter";

export type PublishSection = "broll" | "slides";

/** Ключи настроек выкладки раздела: «broll_publish_channels», «slides_publish_channels»… */
function keys(section: PublishSection) {
  const k = <T extends string>(name: T) =>
    `${section}_publish${name}` as keyof Settings;
  return {
    on: k(""),
    channels: k("_channels"),
    coauthors: k("_coauthors"),
    coauthorsOn: k("_coauthors_on"),
    dates: k("_dates"),
    time: k("_time"),
    step: k("_step"),
    visible: k("_visible"),
    keep: k("_keep"),
    comment: k("_comment"),
  };
}

/** Варианты шага между роликами (дней). Своё сохранённое значение — тоже в списке. */
export function stepOptions(current: number) {
  const base: Record<number, string> = {
    1: "Каждый день",
    2: "Через день (раз в 2 дня)",
    3: "Раз в 3 дня",
    4: "Раз в 4 дня",
    5: "Раз в 5 дней",
    7: "Раз в неделю",
    10: "Раз в 10 дней",
    14: "Раз в 2 недели",
  };
  const days = Object.keys(base).map(Number);
  if (!days.includes(current)) days.push(current);
  return days.sort((a, b) => a - b).map((d) => ({ value: String(d), label: base[d] ?? `Раз в ${d} дн.` }));
}

const key = (profileId: string, channelId: string) =>
  `${profileId}/${channelId}`;

/** Сколько каналов отмечено для выкладки (копий скорости будет столько же). */
export function usePublishCount(section: PublishSection = "broll"): number {
  const { settings } = useLoadedSettings();
  const K = keys(section);
  return settings[K.on] ? (settings[K.channels] as string[]).length : 0;
}

/** Ролики раздела → YouTube через профили AdsPower: каналы, соавторы, материалы, даты.
 *  Каналы (профили) общие для разделов, галочки «куда» и «соавтор» — у каждого раздела свои. */
export function PublishCard({
  section = "broll",
}: {
  section?: PublishSection;
}) {
  const { settings: all, update: updateAll } = useLoadedSettings();
  const K = keys(section);
  const slides = section === "slides";
  // поля выкладки раздела под «обычными» именами — дальше разметка одна для обоих разделов
  const settings = {
    ...all,
    broll_publish: Boolean(all[K.on]),
    broll_publish_channels: all[K.channels] as string[],
    broll_publish_coauthors: all[K.coauthors] as string[],
    broll_publish_coauthors_on: all[K.coauthorsOn] as boolean | null,
    broll_publish_dates: all[K.dates] as string[],
    broll_publish_time: all[K.time] as string,
    broll_publish_step: Math.max(1, Number(all[K.step]) || 1),
    broll_publish_visible: Boolean(all[K.visible]),
    broll_publish_keep: Boolean(all[K.keep]),
    broll_publish_comment: all[K.comment] as string,
  };
  const map: Record<string, keyof Settings> = {
    broll_publish: K.on,
    broll_publish_channels: K.channels,
    broll_publish_coauthors: K.coauthors,
    broll_publish_coauthors_on: K.coauthorsOn,
    broll_publish_dates: K.dates,
    broll_publish_time: K.time,
    broll_publish_step: K.step,
    broll_publish_visible: K.visible,
    broll_publish_keep: K.keep,
    broll_publish_comment: K.comment,
  };
  const update = (patch: Partial<Settings>) =>
    updateAll(
      Object.fromEntries(
        Object.entries(patch).map(([k, v]) => [map[k] ?? k, v]),
      ) as Partial<Settings>,
    );
  const { push } = useToasts();
  const { profiles, setProfiles } = useProfiles();
  // фильтры по группам из «Аналитики»: отдельно для каналов выкладки и для соавторов (у каждого раздела свои)
  const [fTarget, setFTarget, groups] = useGroupFilter(`publish-${section}`);
  const [fCo, setFCo] = useGroupFilter(`coauthors-${section}`);
  const [busy, setBusy] = useState("");
  const [day, setDay] = useState("");

  const { jobs } = useJobs();
  // «Найти каналы» — задача в очереди: последняя такая у профиля (ход, «Стоп», «Продолжить»)
  const scanOf = (pid: string) =>
    [...jobs]
      .reverse()
      .find((j) => j.kind === "scan" && j.params.profile_id === pid);

  const scan = async (pid: string) => {
    try {
      await api.publishScan(pid);
    } catch (e) {
      push({
        kind: "error",
        title: "Поиск каналов не запустился",
        text: (e as Error).message,
      });
    }
  };

  const act = async (
    what: string,
    fn: () => Promise<{ profiles: PublishProfile[] }>,
  ) => {
    setBusy(what);
    try {
      setProfiles((await fn()).profiles);
    } catch (e) {
      push({
        kind: "error",
        title: "Не получилось",
        text: (e as Error).message,
      });
    } finally {
      setBusy("");
    }
  };

  const toggle = (
    field: "broll_publish_channels" | "broll_publish_coauthors",
    k: string,
    on: boolean,
  ) => {
    const cur = settings[field];
    update({ [field]: on ? [...cur, k] : cur.filter((x) => x !== k) });
  };

  const known = useMemo(
    () =>
      new Set(profiles.flatMap((p) => p.channels.map((c) => key(p.id, c.id)))),
    [profiles],
  );
  const stale = settings.broll_publish_channels.filter(
    (k) => !known.has(k),
  ).length;
  const count = settings.broll_publish_channels.length;
  const allChannelIds = useMemo(
    () => profiles.filter((p) => !p.missing).flatMap((p) => p.channels.map((c) => c.id)),
    [profiles],
  );
  /** Отметить / снять сразу все показанные каналы (после фильтра по группам). */
  const setMany = (
    field: "broll_publish_channels" | "broll_publish_coauthors",
    keys: string[],
    on: boolean,
  ) => {
    const cur = new Set(settings[field]);
    keys.forEach((k) => (on ? cur.add(k) : cur.delete(k)));
    update({ [field]: [...cur] });
  };

  return (
    <Card
      title="Выложить на YouTube"
      description={
        slides
          ? "После сборки ролик и его копии сами загружаются на выбранные каналы через профили AdsPower — с отложенной публикацией. Название, обложка и описание готовятся под каждый ролик сами."
          : "После сборки ролик и его копии сами загружаются на выбранные каналы через профили AdsPower — с отложенной публикацией."
      }
    >
      <div className="stack">
        <Toggle
          checked={settings.broll_publish}
          onChange={(v) => update({ broll_publish: v })}
          label="Выкладывать на YouTube"
          description="Выключено — ролики, как обычно, просто сохраняются на компьютер."
        />
        {settings.broll_publish && (
          <>
            <div className="pub-toolbar">
              <Button
                size="sm"
                variant="secondary"
                icon={<RefreshCw size={14} />}
                loading={busy === "sync"}
                title="Подтянуть из AdsPower актуальный список профилей: новые, переименованные, удалённые"
                onClick={() => act("sync", api.publishSync)}
              >
                Синхронизировать с AdsPower
              </Button>
              <span className="pub-count">
                Выбрано каналов: <b>{count}</b> → копий скорости {count} (100%,
                99%, 101%…)
              </span>
            </div>
            <ul className="pub-hints muted small">
              <li>
                «Синхронизировать с AdsPower» — только список профилей (новые,
                переименованные, удалённые); каналы внутри не перечитывает.
              </li>
              <li>
                «Обновить каналы» у профиля — открывает его скрыто (1–3 мин) и
                перечитывает каналы, плейлисты и даты выхода. Нажимай после
                изменений на канале.
              </li>
              <li>
                Язык канала — предохранитель: ролик другого языка на канал не
                заливается (переводы идут на каналы своего языка).
              </li>
            </ul>
            {stale > 0 && (
              <div className="notice warn small">
                Выбрано каналов, которых больше нет в профилях: {stale} — сними
                их
              </div>
            )}
            {profiles.length === 0 && (
              <div className="muted small">
                Профилей пока нет — нажми «Синхронизировать с AdsPower».
              </div>
            )}
            {(() => {
              const keysShown = profiles
                .filter((p) => !p.missing)
                .flatMap((p) =>
                  p.channels.filter((c) => inFilter(fTarget, c.id, groups)).map((c) => key(p.id, c.id)),
                );
              const on = keysShown.filter((k) => settings.broll_publish_channels.includes(k)).length;
              return (
                <GroupFilter
                  value={fTarget}
                  onChange={setFTarget}
                  groups={groups}
                  channelIds={allChannelIds}
                  right={
                    <span className="gf-bulk">
                      <button
                        type="button"
                        className="link-btn small"
                        disabled={keysShown.length === 0 || on === keysShown.length}
                        onClick={() => setMany("broll_publish_channels", keysShown, true)}
                      >
                        {fTarget.length ? "Отметить показанные" : "Отметить все"}
                      </button>
                      <button
                        type="button"
                        className="link-btn small"
                        disabled={on === 0}
                        onClick={() => setMany("broll_publish_channels", keysShown, false)}
                      >
                        {fTarget.length ? "Снять показанные" : "Снять все"}
                      </button>
                    </span>
                  }
                />
              );
            })()}
            <div className="pub-profiles">
              {profiles
                .filter((p) => fTarget.length === 0 || p.channels.some((c) => inFilter(fTarget, c.id, groups)))
                .map((p) => {
                const j = scanOf(p.id);
                const active =
                  j && (j.status === "running" || j.status === "queued");
                return (
                  <div key={p.id} className="publish-profile">
                    <div className="pp-head">
                      <div className="pp-title">
                        <b>
                          №{p.number} {p.name}
                        </b>
                        {p.group && <Badge>{p.group}</Badge>}
                        {p.missing && <Badge tone="bad">нет в AdsPower</Badge>}
                      </div>
                      {active ? (
                        <Button
                          size="sm"
                          variant="danger"
                          icon={<Square size={13} />}
                          onClick={() => void api.cancelJob(j.id)}
                        >
                          Остановить
                        </Button>
                      ) : (
                        <Button
                          size="sm"
                          variant="ghost"
                          icon={
                            j && j.status !== "done" ? (
                              <RotateCcw size={14} />
                            ) : (
                              <Search size={14} />
                            )
                          }
                          disabled={p.missing}
                          title="Открыть профиль (скрыто, 1–3 мин) и заново прочитать его каналы, плейлисты и последние даты выхода"
                          onClick={() => void scan(p.id)}
                        >
                          {j && j.status !== "done"
                            ? "Искать заново"
                            : p.scanned_at
                              ? "Обновить каналы"
                              : "Найти каналы"}
                        </Button>
                      )}
                    </div>
                    <div className="pp-meta muted small">
                      {p.scanned_at
                        ? `прочитано ${new Date(p.scanned_at * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}`
                        : "каналы ещё не читались"}
                      {p.studio?.lang && (
                        <>
                          {" "}
                          · Studio {p.studio.lang},{" "}
                          {p.studio.hour12 ? "12 ч" : "24 ч"},{" "}
                          {p.studio.tz || "?"}, {p.studio.platform || "?"}
                        </>
                      )}
                      {p.studio?.lang &&
                        !["en", "en-us"].includes(
                          p.studio.lang.toLowerCase(),
                        ) && (
                          <>
                            {" "}
                            <Badge tone="warn">поставь English (US)</Badge>
                          </>
                        )}
                    </div>
                    {j &&
                      j.status !== "done" &&
                      (j.status === "queued" ? (
                        <div className="muted small">
                          Ждёт очереди выкладки (профили открываются по одному)…
                        </div>
                      ) : j.status === "running" ? (
                        <div className="stack" style={{ gap: 4 }}>
                          <Progress value={j.progress} />
                          <div className="muted small">
                            {j.message || "Открываю профиль…"}
                          </div>
                        </div>
                      ) : (
                        <div className="notice warn small">
                          {j.status === "cancelled"
                            ? "Поиск каналов остановлен."
                            : `Поиск каналов не удался: ${j.error}`}
                        </div>
                      ))}
                    {p.channels.length === 0 ? (
                      <div className="muted small">
                        {p.scanned_at
                          ? "Каналов не найдено"
                          : "Нажми «Найти каналы» — программа откроет профиль и посмотрит аккаунты"}
                      </div>
                    ) : (
                      <div className="pub-rows">
                        {p.channels.filter((c) => inFilter(fTarget, c.id, groups)).map((c) => {
                          const k = key(p.id, c.id);
                          const warns = [
                            c.language && c.language_auto
                              ? "язык определён по названиям роликов — проверь"
                              : "",
                            c.playlists.length === 0
                              ? "на канале нет плейлиста"
                              : c.playlists.length > 1 && !c.playlist
                                ? `плейлистов: ${c.playlists.length} — выбери, в какой класть ролики`
                                : "",
                          ].filter(Boolean);
                          return (
                            <div key={c.id} className="pub-row">
                              <input
                                type="checkbox"
                                checked={settings.broll_publish_channels.includes(
                                  k,
                                )}
                                onChange={(e) =>
                                  toggle(
                                    "broll_publish_channels",
                                    k,
                                    e.target.checked,
                                  )
                                }
                                aria-label={`Выкладывать на ${c.name}`}
                              />
                              <div
                                className="pub-name"
                                title={`${c.name} ${c.handle}${c.account ? ` · ${c.account}` : ""}`}
                              >
                                <span className="pub-title">{c.name}</span>
                                <span className="muted">{c.handle}</span>
                              </div>
                              <Subs c={c} />
                              <select
                                className="input select lang-select"
                                value={c.language || ""}
                                title="Язык канала: ролик другого языка на этот канал не заливается. Можно сменить в любой момент (смена ниши)"
                                onChange={(e) =>
                                  void api
                                    .publishLanguage(c.id, e.target.value)
                                    .then((r) => setProfiles(r.profiles))
                                    .catch((err) =>
                                      push({
                                        kind: "error",
                                        title: "Язык не сохранился",
                                        text: (err as Error).message,
                                      }),
                                    )
                                }
                              >
                                <option value="">язык?</option>
                                {LANGS.map((l) => (
                                  <option key={l.code} value={l.code}>
                                    {l.code.toUpperCase()} — {l.name}
                                  </option>
                                ))}
                              </select>
                              {c.playlists.length > 1 && (
                                <select
                                  className="input select lang-select pl-select"
                                  value={c.playlist || ""}
                                  title="Плейлист, в который класть ролики этого канала (на канале их несколько)"
                                  onChange={(e) =>
                                    void api
                                      .publishPlaylist(c.id, e.target.value)
                                      .then((r) => setProfiles(r.profiles))
                                      .catch((err) =>
                                        push({
                                          kind: "error",
                                          title: "Плейлист не сохранился",
                                          text: (err as Error).message,
                                        }),
                                      )
                                  }
                                >
                                  <option value="">плейлист?</option>
                                  {c.playlists.map((pl) => (
                                    <option key={pl} value={pl}>
                                      {pl}
                                    </option>
                                  ))}
                                </select>
                              )}
                              {warns.length > 0 && (
                                <div className="pub-warns">
                                  {warns.map((w) => (
                                    <Badge key={w} tone="warn">
                                      {w}
                                    </Badge>
                                  ))}
                                </div>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>

            {(() => {
              const coOn =
                settings.broll_publish_coauthors_on ??
                settings.broll_publish_coauthors.length > 0;
              const all = profiles
                .filter((p) => !p.missing)
                .flatMap((p) =>
                  p.channels.map((c) => ({ p, c, k: key(p.id, c.id) })),
                );
              const picked = settings.broll_publish_coauthors.filter((k) =>
                all.some((x) => x.k === k),
              ).length;
              return (
                <div className="pub-section">
                  <Toggle
                    checked={coOn}
                    onChange={(v) => update({ broll_publish_coauthors_on: v })}
                    label="Добавить соавторов"
                    description={
                      coOn
                        ? `Выбрано: ${picked}. Программа пригласит эти каналы в соавторы каждого ролика и сама примет приглашения в их профилях.`
                        : "Выключено — ролики выкладываются без соавторов."
                    }
                  />
                  {coOn && (
                    <div className="pub-co-sync">
                      <Button
                        size="sm"
                        variant="secondary"
                        icon={<RefreshCw size={14} />}
                        loading={busy === "sync"}
                        title="Подтянуть из AdsPower актуальный список профилей: новые, переименованные, удалённые"
                        onClick={() => act("sync", api.publishSync)}
                      >
                        Синхронизировать с AdsPower
                      </Button>
                      <span className="muted small">
                        Нет нужного канала? Синхронизируй, затем «Найти каналы»
                        у нового профиля — его каналы появятся в этом списке.
                      </span>
                    </div>
                  )}
                  {coOn &&
                    profiles
                      .filter((p) => !p.missing && p.channels.length === 0)
                      .map((p) => {
                        const j = scanOf(p.id);
                        const active =
                          j &&
                          (j.status === "running" || j.status === "queued");
                        return (
                          <div key={p.id} className="pub-co-empty">
                            <span className="small">
                              <b>
                                №{p.number} {p.name}
                              </b>{" "}
                              <span className="muted">
                                {active
                                  ? j.status === "queued"
                                    ? "— ждёт очереди…"
                                    : `— ${j.message}`
                                  : p.scanned_at
                                    ? "— каналов не найдено"
                                    : "— каналы ещё не читались"}
                              </span>
                            </span>
                            {active ? (
                              <Button
                                size="sm"
                                variant="danger"
                                icon={<Square size={13} />}
                                onClick={() => void api.cancelJob(j.id)}
                              >
                                Остановить
                              </Button>
                            ) : (
                              <Button
                                size="sm"
                                variant="ghost"
                                icon={<Search size={14} />}
                                title="Открыть профиль (скрыто, 1–3 мин) и прочитать его каналы"
                                onClick={() => void scan(p.id)}
                              >
                                Найти каналы
                              </Button>
                            )}
                          </div>
                        );
                      })}
                  {coOn && (() => {
                    const keysShown = all
                      .filter(({ c, k }) => inFilter(fCo, c.id, groups) && !settings.broll_publish_channels.includes(k))
                      .map((x) => x.k);
                    const on = keysShown.filter((k) => settings.broll_publish_coauthors.includes(k)).length;
                    return (
                      <GroupFilter
                        value={fCo}
                        onChange={setFCo}
                        groups={groups}
                        channelIds={all.map((x) => x.c.id)}
                        right={
                          <span className="gf-bulk">
                            <button
                              type="button"
                              className="link-btn small"
                              disabled={keysShown.length === 0 || on === keysShown.length}
                              onClick={() => setMany("broll_publish_coauthors", keysShown, true)}
                            >
                              {fCo.length ? "Отметить показанные" : "Отметить все"}
                            </button>
                            <button
                              type="button"
                              className="link-btn small"
                              disabled={on === 0}
                              onClick={() => setMany("broll_publish_coauthors", keysShown, false)}
                            >
                              {fCo.length ? "Снять показанные" : "Снять все"}
                            </button>
                          </span>
                        }
                      />
                    );
                  })()}
                  {coOn && (
                    <div className="pub-rows pub-co">
                      {all.filter(({ c }) => inFilter(fCo, c.id, groups)).map(({ p, c, k }) => {
                        const target =
                          settings.broll_publish_channels.includes(k);
                        return (
                          <label
                            key={k}
                            className={`pub-row ${target ? "is-target" : ""}`}
                            title={
                              target
                                ? "Этот канал — цель выкладки, соавтором быть не может"
                                : undefined
                            }
                          >
                            <input
                              type="checkbox"
                              disabled={target}
                              checked={
                                !target &&
                                settings.broll_publish_coauthors.includes(k)
                              }
                              onChange={(e) =>
                                toggle(
                                  "broll_publish_coauthors",
                                  k,
                                  e.target.checked,
                                )
                              }
                            />
                            <div className="pub-name">
                              <span className="pub-title">{c.name}</span>
                              <span className="muted">{c.handle}</span>
                            </div>
                            <Subs c={c} />
                            <span className="muted small pub-prof">
                              {target
                                ? "сюда выкладываем"
                                : `№${p.number} ${p.name}`}
                            </span>
                          </label>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })()}

            <div className="pub-divider" />
            {slides ? (
              <div className="stack">
                <Toggle
                  checked={all.slides_publish_cover}
                  onChange={(v) => updateAll({ slides_publish_cover: v })}
                  label="Генерировать обложку"
                  description="Выключено — ролик выкладывается без обложки, поставишь её сам в Studio. Название и описание готовятся всё равно."
                />
                <Disclosure
                  id="slides-publish-materials"
                  summary="Название, описание и обложка — настройки и как готовятся"
                >
                  <div className="stack">
                    <div className="notice small">
                      <b>Название</b> — по названию оригинала (ролика из
                      рерайта): несколько вариантов другими словами с тем же
                      крючком (капслок, «!», «ШОК» — где уместно) и ключевым
                      словом для поиска в начале; нейросеть выбирает лучший.{" "}
                      <b>Обложка</b> — по обложке оригинала: те же композиция и
                      подача, другие люди и текст на языке ролика; несколько
                      вариантов, нейросеть выбирает лучший (все — в папке
                      ролика, «обложки»). Ролик не из рерайта — название и
                      обложку нейросеть придумает по тексту.
                      <b> Описание</b> — призыв ниже, затем главы с тайм-кодами
                      и источники (у каждой копии свои).
                    </div>
                    <Field
                      label="Призыв в начале описания"
                      hint="Первым абзацем, крупно (ЗАГЛАВНЫМИ и с эмодзи). Переводится на язык ролика."
                    >
                      <Textarea
                        value={all.slides_publish_desc_cta}
                        onChange={(v) =>
                          updateAll({ slides_publish_desc_cta: v })
                        }
                        rows={2}
                      />
                    </Field>
                    <Field
                      label="Вариантов названия"
                      hint="Нейросеть пишет столько вариантов, отдельный шаг выбирает лучший"
                    >
                      <NumberInput
                        value={all.slides_publish_title_variants}
                        onChange={(v) =>
                          updateAll({ slides_publish_title_variants: v })
                        }
                        min={1}
                        max={8}
                      />
                    </Field>
                    {all.slides_publish_cover && <CoverSettingsFields />}
                    <div className="muted small">
                      Промпты названия и обложки:{" "}
                      <button
                        type="button"
                        className="link-btn"
                        onClick={() => goTo("settings", "cover-prompts")}
                      >
                        Настройки → Промпты названий и обложек
                      </button>
                    </div>
                  </div>
                </Disclosure>
              </div>
            ) : (
              <div className="stack">
                <Field label="Обложка и название">
                  <div className="provider-picker" role="radiogroup">
                    {[
                      [
                        "files",
                        "Взять обложки и названия с компа",
                        "Папка обложек и файл названий — случайные, без повторов до конца круга.",
                      ],
                      [
                        "competitor",
                        "Переделать конкурента",
                        "Обложка ролика-оригинала (из рерайта) переделывается: тот же стиль и подача, другой ракурс, люди и текст. Название — кликбейтный рерайт названия оригинала по смыслу. Ролик не из рерайта — придумаются по тексту рассказа.",
                      ],
                    ].map(([id, label, text]) => {
                      const on =
                        (all.broll_publish_materials || "files") === id;
                      return (
                        <button
                          key={id}
                          type="button"
                          role="radio"
                          aria-checked={on}
                          className={`provider ${on ? "active" : ""}`}
                          onClick={() =>
                            updateAll({ broll_publish_materials: id })
                          }
                        >
                          <span className="radio" />
                          <span className="provider-text">
                            <span className="provider-name">{label}</span>
                            <span className="muted small">{text}</span>
                          </span>
                        </button>
                      );
                    })}
                  </div>
                </Field>
                {all.broll_publish_materials === "competitor" ? (
                  <>
                    <Field
                      label="Файл описаний"
                      hint="Описания универсальные — берутся из файла: пункты «1. Описание…», по кругу без повторов. На другом языке ролика — переводятся."
                    >
                      <PathInput
                        value={settings.broll_publish_descs}
                        onChange={(v) => update({ broll_publish_descs: v })}
                        kind="text"
                        title="Описания"
                      />
                    </Field>
                    <Disclosure
                      id="broll-publish-competitor"
                      summary="Настройки генерации обложки и названия"
                    >
                      <div className="stack">
                        <Field
                          label="Вариантов названия"
                          hint="Нейросеть пишет столько вариантов, отдельный шаг выбирает лучший"
                        >
                          <NumberInput
                            value={all.slides_publish_title_variants}
                            onChange={(v) =>
                              updateAll({ slides_publish_title_variants: v })
                            }
                            min={1}
                            max={8}
                          />
                        </Field>
                        <CoverSettingsFields />
                        <div className="muted small">
                          Общие со «Стоковыми слайдами». Промпты названия и
                          обложки:{" "}
                          <button
                            type="button"
                            className="link-btn"
                            onClick={() => goTo("settings", "cover-prompts")}
                          >
                            Настройки → Промпты названий и обложек
                          </button>
                        </div>
                      </div>
                    </Disclosure>
                  </>
                ) : (
                  <div className="grid grid-2">
                    <Field
                      label="Папка обложек"
                      hint="Случайная, без повторов: использованная получает «✓ » в имени, круг пройден — пометки снимаются"
                      span
                    >
                      <PathInput
                        value={settings.broll_publish_thumbs}
                        onChange={(v) => update({ broll_publish_thumbs: v })}
                        kind="folder"
                        title="Обложки"
                      />
                    </Field>
                    <Field
                      label="Файл названий"
                      hint="Пункты «1. Название», «2. …» — случайное, без повторов до конца круга"
                    >
                      <PathInput
                        value={settings.broll_publish_titles}
                        onChange={(v) => update({ broll_publish_titles: v })}
                        kind="text"
                        title="Названия"
                      />
                    </Field>
                    <Field
                      label="Файл описаний"
                      hint="Пункты «1. Описание…» (можно в несколько строк) — так же по кругу"
                    >
                      <PathInput
                        value={settings.broll_publish_descs}
                        onChange={(v) => update({ broll_publish_descs: v })}
                        kind="text"
                        title="Описания"
                      />
                    </Field>
                  </div>
                )}
              </div>
            )}

            <div className="pub-divider" />
            <div className="grid grid-3">
              <Field
                label="Даты выхода (необязательно)"
                hint="Пусто — после последнего запланированного на канале ролика, с выбранным шагом. Свои даты — ролики займут их по порядку готовности; роликов больше — дальше с тем же шагом. На канал — не больше одного ролика в день."
              >
                <div className="row gap-sm wrap">
                  <input
                    className="input pub-date"
                    type="date"
                    value={day}
                    onChange={(e) => setDay(e.target.value)}
                  />
                  <Button
                    size="sm"
                    disabled={
                      !day || settings.broll_publish_dates.includes(day)
                    }
                    onClick={() => {
                      update({
                        broll_publish_dates: [
                          ...settings.broll_publish_dates,
                          day,
                        ].sort(),
                      });
                      setDay("");
                    }}
                  >
                    Добавить дату
                  </Button>
                  {settings.broll_publish_dates.map((d) => (
                    <Badge key={d} tone="accent">
                      {d.split("-").reverse().join(".")}{" "}
                      <X
                        size={12}
                        style={{ cursor: "pointer" }}
                        onClick={() =>
                          update({
                            broll_publish_dates:
                              settings.broll_publish_dates.filter(
                                (x) => x !== d,
                              ),
                          })
                        }
                      />
                    </Badge>
                  ))}
                </div>
              </Field>
              <Field
                label="Шаг между роликами"
                hint="Через сколько дней друг от друга выходят ролики на одном канале. Свои даты из календаря шаг не меняет — он работает без дат и для роликов сверх выбранных дат."
              >
                <Select
                  value={String(settings.broll_publish_step)}
                  onChange={(v) => update({ broll_publish_step: Number(v) })}
                  options={stepOptions(settings.broll_publish_step)}
                />
              </Field>
              <Field
                label="Время выхода по Москве (необязательно)"
                hint="Пусто — случайное с 16:00 до 20:00"
              >
                <div className="row gap-sm">
                  <input
                    className="input pub-date"
                    type="time"
                    step={900}
                    value={settings.broll_publish_time}
                    onChange={(e) =>
                      update({ broll_publish_time: e.target.value })
                    }
                  />
                  {settings.broll_publish_time && (
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => update({ broll_publish_time: "" })}
                    >
                      Случайное
                    </Button>
                  )}
                </div>
              </Field>
            </div>
            <Field
              label="Комментарий-призыв от имени канала"
              hint="Пишется и закрепляется под вышедшими роликами канала (под запланированным YouTube комментировать не даёт — он получит его при следующей выкладке). Пусто — не писать."
            >
              <Textarea
                value={settings.broll_publish_comment}
                onChange={(v) => update({ broll_publish_comment: v })}
                rows={2}
              />
            </Field>
            <div className="pub-divider" />
            <div className="grid grid-2">
              <Toggle
                checked={settings.broll_publish_visible}
                onChange={(v) => update({ broll_publish_visible: v })}
                label="Показывать браузер при выкладке"
                description="Выключено — профиль работает в фоне, без окна"
              />
              <Toggle
                checked={settings.broll_publish_keep}
                onChange={(v) => update({ broll_publish_keep: v })}
                label="Оставлять ролики на компьютере"
                description="Выключено — после выкладки на все каналы удаляется только видео: ролик, копии скорости и копия ролика в рабочей папке программы (они весят гигабайты). Текст, озвучка, главы и описание остаются в папке ролика с понятными именами (тема и дата) — по ним ролик можно пересобрать. Хоть один канал не удался — ничего не удаляется."
              />
            </div>
            <div className="muted small">
              «Не для детей» — всегда. Отметка ИИ-контента — никогда. Плейлист —
              единственный плейлист канала.
            </div>
          </>
        )}
      </div>
    </Card>
  );
}
