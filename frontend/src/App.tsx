import {
  ArrowDownToLine, BookOpen, FlaskConical, Layers3, Menu, PanelLeftClose, RefreshCw, Search,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { api, workspaces, type Library, type Session, type Workspace } from "./api";
import { IngestPage, useIngestion } from "./IngestPage";
import { LibraryPage } from "./LibraryPage";
import { RetrievePage } from "./RetrievePage";
import { Notice } from "./ui";

const navigation = [
  { to: "/", label: "检索工作台", icon: Search },
  { to: "/knowledge", label: "知识库", icon: BookOpen },
  { to: "/ingestion", label: "文档入库", icon: ArrowDownToLine },
];

export function App() {
  const [profiles, setProfiles] = useState<Workspace[]>([]);
  const [activeId, setActiveId] = useState("");
  const [session, setSession] = useState<Session | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [showMenu, setShowMenu] = useState(false);
  const menuTrigger = useRef<HTMLButtonElement>(null);
  const menuClose = useRef<HTMLButtonElement>(null);
  const request = useRef<AbortController>();

  useEffect(() => {
    void loadWorkspace();
    return () => request.current?.abort();
  }, []);
  useEffect(() => {
    if (!showMenu) return;
    menuClose.current?.focus();
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") setShowMenu(false);
    }
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      window.removeEventListener("keydown", closeOnEscape);
      menuTrigger.current?.focus();
    };
  }, [showMenu]);
  const location = useLocation();
  const title = navigation.find((item) => item.to === location.pathname)?.label ?? navigation[0].label;

  async function loadWorkspace(preferred?: string) {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setSession(null);
    setLoading(true);
    setError(null);
    if (preferred && preferred !== activeId) setDraft("");
    try {
      const options = await workspaces(controller.signal);
      if (controller.signal.aborted) return;
      setProfiles(options);
      const workspace = options.find((entry) => entry.id === preferred) ?? options[0];
      setActiveId(workspace?.id ?? "");
      if (!workspace) return;
      const catalog = await api.catalog(workspace.id, controller.signal);
      if (!controller.signal.aborted) {
        if (session && session.tenant_id !== catalog.tenant_id) setDraft("");
        setSession({ ...catalog, workspace });
      }
    } catch (cause) {
      if (!controller.signal.aborted) setError(cause);
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }

  const refresh = () => { void loadWorkspace(activeId); };

  return (
    <div className="app-shell">
      <aside id="workspace-sidebar" className={`sidebar ${showMenu ? "sidebar-open" : ""}`}>
        <div className="brand">
          <span className="brand-icon"><Layers3 size={22} strokeWidth={2.2} /></span>
          <span><strong>RAG 中台</strong><small>知识检索工作区</small></span>
          <button ref={menuClose} className="icon-button close-menu" onClick={() => setShowMenu(false)} aria-label="关闭导航"><PanelLeftClose size={18} /></button>
        </div>
        <nav className="navigation" aria-label="工作区导航">
          <div className="nav-label">工作区</div>
          {navigation.map(({ to, label, icon: Icon }) => (
            <NavLink to={to} end={to === "/"} key={to} onClick={() => setShowMenu(false)}>
              <Icon size={18} /><span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className="environment-icon"><FlaskConical size={17} /></div>
          <span><strong>本地工作区</strong><small>{session ? session.workspace.writable ? "读写模式" : "只读模式" : "等待初始化"}</small></span>
          <span className={`status-dot ${session ? "status-online" : ""}`} aria-label={session ? "可用" : "未就绪"} />
        </div>
      </aside>
      {showMenu && <button className="sidebar-overlay" tabIndex={-1} onClick={() => setShowMenu(false)} aria-label="关闭导航" />}
      <div className="app-main">
        <header className="topbar">
          <div className="breadcrumb">
            <button ref={menuTrigger} className="icon-button open-menu" onClick={() => setShowMenu(true)}
              aria-label="打开导航" aria-expanded={showMenu} aria-controls="workspace-sidebar"><Menu size={20} /></button>
            <span className="crumb-root">工作区</span><span className="crumb-divider">/</span><strong>{title}</strong>
          </div>
          <div className="workspace-switcher">
            <select aria-label="当前工作区" value={activeId} disabled={loading || !profiles.length}
              onChange={(event) => { void loadWorkspace(event.target.value); }}>
              {!profiles.length && <option value="">{loading ? "加载工作区…" : "暂无工作区"}</option>}
              {profiles.map((profile) => <option value={profile.id} key={profile.id}>{profile.label}</option>)}
            </select>
            <button className="icon-button bordered" onClick={refresh} aria-label="刷新工作区" disabled={loading}><RefreshCw size={16} /></button>
          </div>
        </header>
        <main className="main-content">
          {Boolean(error) && <div className="workspace-error"><Notice error={error} /></div>}
          <WorkspaceContent key={session?.workspace.id ?? "unavailable"} {...{ session, refresh, draft, setDraft }} />
        </main>
      </div>
    </div>
  );
}

function WorkspaceContent({ session, refresh, draft, setDraft }: {
  session: Session | null; refresh: () => void; draft: string; setDraft: (value: string) => void;
}) {
  const [library, setLibrary] = useState<Library | undefined>(session?.libraries[0]);
  const queue = useIngestion(session?.workspace.writable ? session.workspace.id : undefined);
  return (
    <Routes>
      <Route path="/" element={<RetrievePage key={library?.id} {...{ session, library, setLibrary, refresh }} question={draft} setQuestion={setDraft} />} />
      <Route path="/knowledge" element={<LibraryPage key={library?.id} {...{ session, library, setLibrary, refresh }} />} />
      <Route path="/ingestion" element={<IngestPage {...{ session, library, queue, refresh }} />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
