import { useEffect, useState, type ReactNode } from "react";
import {
  ListChecks,
  BarChart3,
  CheckCircle2,
  Clapperboard,
  Copy,
  History,
  Images,
  Info,
  LayoutTemplate,
  Loader2,
  MessageCircleHeart,
  Telescope,
  Vote,
  Moon,
  Palette,
  Settings as SettingsIcon,
  Sparkles,
  Sun,
  X,
  XCircle,
} from "lucide-react";
import { JobsPanel } from "./components/JobsPanel";
import { WhatsNewDialog } from "./components/WhatsNew";
import { UpdateButton } from "./components/UpdateButton";
import { ReportHost } from "./components/ReportDialog";
import { api, type WhatsNew } from "./lib/api";
import { useJobs, useSettings, useToasts } from "./lib/store";
import { BrollPage } from "./pages/BrollPage";
import { StepsPage } from "./pages/StepsPage";
import { CopiesPage } from "./pages/CopiesPage";
import { RunsPage } from "./pages/RunsPage";
import { SettingsPage } from "./pages/SettingsPage";
import { SlidesPage } from "./pages/SlidesPage";
import { CoversPage } from "./pages/CoversPage";
import { AnalyticsPage } from "./pages/AnalyticsPage";
import { CommentsPage } from "./pages/CommentsPage";
import { PollsPage } from "./pages/PollsPage";
import { NichesPage } from "./pages/NichesPage";
import { TemplatesPage } from "./pages/TemplatesPage";
import { setEditingTemplate, useEditingTemplate } from "./lib/templateEdit";

import type { PageId } from "./lib/nav";

const NAV: { id: PageId; label: string; icon: ReactNode; group: string }[] = [
  { id: "steps", label: "Генерация по шагам", icon: <ListChecks size={18} />, group: "Создать" },
  { id: "templates", label: "Шаблоны", icon: <LayoutTemplate size={18} />, group: "Создать" },
  { id: "broll", label: "Рассказы", icon: <Clapperboard size={18} />, group: "Создать" },
  { id: "slides", label: "Стоковые слайды", icon: <Images size={18} />, group: "Создать" },
  { id: "covers", label: "Обложки", icon: <Palette size={18} />, group: "Создать" },
  { id: "runs", label: "Запуски слайдов", icon: <History size={18} />, group: "Работа" },
  { id: "copies", label: "Копии ролика", icon: <Copy size={18} />, group: "Работа" },
  { id: "comments", label: "Комментарии", icon: <MessageCircleHeart size={18} />, group: "Работа" },
  { id: "polls", label: "Голосования", icon: <Vote size={18} />, group: "Работа" },
  { id: "niches", label: "Поиск ниш", icon: <Telescope size={18} />, group: "Работа" },
  { id: "analytics", label: "Аналитика", icon: <BarChart3 size={18} />, group: "Работа" },
  { id: "settings", label: "Настройки", icon: <SettingsIcon size={18} />, group: "Приложение" },
];

function readPage(): PageId {
  try {
    const v = localStorage.getItem("page");
    if (v && NAV.some((n) => n.id === v)) return v as PageId;
  } catch {
    /* storage недоступен */
  }
  return "broll";
}

type Theme = "system" | "light" | "dark";

function useTheme(): [Theme, (t: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      return (localStorage.getItem("theme") as Theme) || "system";
    } catch {
      return "system";
    }
  });
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    try {
      localStorage.setItem("theme", theme);
    } catch {
      /* ignore */
    }
  }, [theme]);
  return [theme, setTheme];
}

function Toasts() {
  const { toasts, dismiss } = useToasts();
  return (
    <div className="toasts">
      {toasts.map((t) => (
        <div key={t.id} className={`toast toast-${t.kind}`}>
          {t.kind === "ok" ? <CheckCircle2 size={18} /> : t.kind === "error" ? <XCircle size={18} /> : <Info size={18} />}
          <div className="toast-body">
            <div className="toast-title">{t.title}</div>
            {t.text && <div className="toast-text">{t.text}</div>}
          </div>
          <button className="icon-btn" onClick={() => dismiss(t.id)}>
            <X size={14} />
          </button>
        </div>
      ))}
    </div>
  );
}

function SaveIndicator() {
  const { saveState } = useSettings();
  if (saveState === "saving") return <span className="save-state">Сохраняю…</span>;
  if (saveState === "saved") return <span className="save-state ok">Сохранено</span>;
  if (saveState === "error") return <span className="save-state bad">Ошибка сохранения</span>;
  return null;
}

export default function App() {
  const [page, setPage] = useState<PageId>(readPage);
  const [theme, setTheme] = useTheme();
  const { settings, meta, flush } = useSettings();
  const { push } = useToasts();
  const editing = useEditingTemplate();
  const { jobs } = useJobs();
  const running = jobs.filter((j) => j.status === "running");

  // переход из карточки («Промпты → Настройки»): страница + прокрутка к нужной карточке
  useEffect(() => {
    const on = (e: Event) => {
      const { page: next, anchor } = (e as CustomEvent<{ page: PageId; anchor?: string }>).detail;
      setPage(next);
      if (anchor) setTimeout(() => document.getElementById(anchor)?.scrollIntoView({ behavior: "smooth", block: "start" }), 80);
    };
    window.addEventListener("sv-goto", on);
    return () => window.removeEventListener("sv-goto", on);
  }, []);

  useEffect(() => {
    try {
      localStorage.setItem("page", page);
    } catch {
      /* ignore */
    }
  }, [page]);

  const needsSetup = settings && !settings.llm_api_key;

  // «Что нового»: после обновления один раз показываем окно со всем, что человек ещё не видел
  const [news, setNews] = useState<WhatsNew | null>(null);
  const [newsOpen, setNewsOpen] = useState<"auto" | "history" | null>(null);
  useEffect(() => {
    if (!settings) return;
    void api.whatsNew().then((w) => {
      setNews(w);
      if (w.unseen.length) setNewsOpen("auto");
    }).catch(() => undefined); // нет списка версий — просто без окна
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [Boolean(settings)]);
  const saveEditing = async () => {
    if (!editing) return;
    try {
      await flush();
      await api.saveTemplate(editing.id);
      push({ kind: "ok", title: `Шаблон «${editing.name}» обновлён`, text: "Новые запуски пойдут с этими настройками" });
      setEditingTemplate(null);
    } catch (e) {
      push({ kind: "error", title: "Шаблон не сохранился", text: (e as Error).message });
    }
  };
  const closeNews = () => {
    if (newsOpen === "auto") {
      void api.whatsNewSeen().catch(() => undefined);
      setNews((w) => (w ? { ...w, unseen: [] } : w));
    }
    setNewsOpen(null);
  };

  let group = "";
  return (
    <div className="app">
      <nav className="sidebar">
        <div className="brand">
          <div className="brand-mark">SV</div>
          <div>
            <div className="brand-name">Story Video</div>
            <div className="muted small">рассказы → ролики</div>
          </div>
        </div>
        <div className="nav">
          {NAV.map((n) => {
            const head = n.group !== group ? ((group = n.group), <div className="nav-group">{n.group}</div>) : null;
            return (
              <div key={n.id}>
                {head}
                <button className={`nav-item ${page === n.id ? "active" : ""}`} onClick={() => setPage(n.id)}>
                  {n.icon}
                  <span>{n.label}</span>
                  {n.id === "settings" && needsSetup && <span className="dot" title="Нужно вписать ключи" />}
                </button>
              </div>
            );
          })}
        </div>
        <div className="sidebar-foot">
          {running.map((r) => (
            <div key={r.id} className="now-running" title={r.message}>
              <Loader2 size={14} className="spin" />
              <span className="ellipsis">{Math.round(r.progress * 100)}% · {r.title}</span>
            </div>
          ))}
          <SaveIndicator />
          <ReportHost />
          <UpdateButton />
          {news?.version && (
            <button className="ver-btn" onClick={() => setNewsOpen("history")} title="Что нового: история версий">
              <Sparkles size={13} />
              <span>Версия {news.version}</span>
              {news.unseen.length > 0 && <span className="dot" />}
            </button>
          )}
          <div className="theme-switch">
            {(["light", "system", "dark"] as Theme[]).map((t) => (
              <button key={t} className={theme === t ? "active" : ""} onClick={() => setTheme(t)} title={t === "system" ? "Как в системе" : t === "light" ? "Светлая" : "Тёмная"}>
                {t === "light" ? <Sun size={14} /> : t === "dark" ? <Moon size={14} /> : "Авто"}
              </button>
            ))}
          </div>
        </div>
      </nav>

      {newsOpen && news && (
        <WhatsNewDialog releases={newsOpen === "auto" ? news.unseen : news.history} mode={newsOpen} onClose={closeNews} />
      )}
      <main className="content">
        {editing && (
          <div className="tpl-banner" role="status">
            <LayoutTemplate size={18} />
            <div className="tpl-banner-text">
              <b>Правишь шаблон «{editing.name}».</b> Настройки на экранах сейчас — из шаблона. Поправь и сохрани — или закрой без сохранения.
            </div>
            <button className="btn btn-primary btn-sm" onClick={() => void saveEditing()}>
              Сохранить в шаблон
            </button>
            <button className="btn btn-ghost btn-sm" onClick={() => setEditingTemplate(null)}>
              Закрыть
            </button>
          </div>
        )}
        {!settings || !meta ? (
          <div className="loading">
            <Loader2 className="spin" size={28} />
          </div>
        ) : (
          <>
            {/* все страницы смонтированы: введённый текст не теряется при переключении */}
            <div hidden={page !== "steps"}>
              <StepsPage />
            </div>
            <div hidden={page !== "templates"}>
              <TemplatesPage active={page === "templates"} />
            </div>
            <div hidden={page !== "broll"}>
              <BrollPage />
            </div>
            <div hidden={page !== "slides"}>
              <SlidesPage goRuns={() => setPage("runs")} />
            </div>
            <div hidden={page !== "covers"}>
              <CoversPage />
            </div>
            <div hidden={page !== "runs"}>
              <RunsPage active={page === "runs"} />
            </div>
            <div hidden={page !== "copies"}>
              <CopiesPage />
            </div>
            <div hidden={page !== "comments"}>
              <CommentsPage />
            </div>
            <div hidden={page !== "polls"}>
              <PollsPage />
            </div>
            <div hidden={page !== "niches"}>
              <NichesPage active={page === "niches"} />
            </div>
            <div hidden={page !== "analytics"}>
              <AnalyticsPage active={page === "analytics"} />
            </div>
            <div hidden={page !== "settings"}>
              <SettingsPage />
            </div>
          </>
        )}
      </main>

      <JobsPanel />
      <Toasts />
    </div>
  );
}
