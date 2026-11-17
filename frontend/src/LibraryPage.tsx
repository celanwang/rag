import { ArrowUpRight, BookOpen, ChevronLeft, ChevronRight, CloudUpload, Download, FileText, Search } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, type DocumentSummary, type Library, type Session } from "./api";
import { LibrarySelect } from "./RetrievePage";
import { Busy, Dialog, Empty, Notice, PageHeading, UUID_PATTERN } from "./ui";

const pageSize = 20;
type Mutation = { kind: "readers" | "delete"; item: DocumentSummary };

export function LibraryPage({ session, library, setLibrary, refresh }: {
  session: Session | null; library?: Library; setLibrary: (library: Library) => void; refresh: () => void;
}) {
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const [reload, setReload] = useState(0);
  const [rows, setRows] = useState<{ items: DocumentSummary[]; has_more: boolean }>();
  const [error, setError] = useState<unknown>(null);
  const [actionError, setActionError] = useState<unknown>(null);
  const [mutation, setMutation] = useState<Mutation>();
  const [downloading, setDownloading] = useState<string>();
  const [pending, setPending] = useState(true);
  const downloadRequest = useRef<AbortController>();
  useEffect(() => () => downloadRequest.current?.abort(), []);

  useEffect(() => {
    if (!session || !library) return;
    const abort = new AbortController();
    setPending(true);
    setError(null);
    const timeout = setTimeout(() => {
      api.documents(session.workspace.id, library.code, query, page * pageSize, pageSize, abort.signal).then((next) => {
        if (!abort.signal.aborted) {
          if (!next.items.length && page > 0) setPage(page - 1);
          setRows(next);
        }
      }).catch((cause) => {
        if (!abort.signal.aborted) { setRows(undefined); setError(cause); }
      }).finally(() => { if (!abort.signal.aborted) setPending(false); });
    }, 150);
    return () => { clearTimeout(timeout); abort.abort(); };
  }, [session, library, query, page, reload]);

  async function download(row: DocumentSummary) {
    if (!session) return;
    const abort = new AbortController();
    downloadRequest.current = abort;
    setActionError(null);
    setDownloading(row.document_id);
    try { await api.download(session.workspace.id, row.document_id, row.filename, abort.signal); }
    catch (cause) { if (!abort.signal.aborted) setActionError(cause); }
    finally { if (!abort.signal.aborted) setDownloading(undefined); }
  }

  return <>
    <PageHeading title="知识库" action={session?.workspace.writable && <Link className="button button-primary heading-action" to="/ingestion"><CloudUpload size={17} />上传文档</Link>}>
      浏览获授权的知识库和已发布文档，查看版本与索引状态。
    </PageHeading>
    <section className="panel library-panel">
      <div className="panel-header"><div className="section-icon blue"><BookOpen size={18} /></div><div><h2>{library?.name ?? "知识库文档"}</h2><p>{library ? `知识库短码 · ${library.code}` : "查看当前应用可读的文档"}</p></div></div>
      {!session ? <Empty icon={<BookOpen size={22} />} title="暂无本地工作区" action={<button className="button button-primary" onClick={refresh}>重新加载<ArrowUpRight size={15} /></button>}>
        先初始化 rag-dev 样例，再刷新工作区。
      </Empty> : !library ? <Empty icon={<BookOpen size={22} />} title="当前工作区没有可读知识库">授予应用读取权限后，刷新工作区。</Empty> : <>
        <div className="library-toolbar">
          <label className="search-field"><Search size={17} /><input aria-label="按文件名查找" placeholder="按文件名查找" maxLength={255} value={query}
            onChange={(e) => { setQuery(e.target.value); setPage(0); }} /></label>
          <LibrarySelect id="catalog-library" library={library} libraries={session.libraries} onChange={setLibrary} />
          <label className="sr-only" htmlFor="catalog-library">当前知识库</label>
        </div>
        <div className="library-notice"><Notice error={error} /><Notice error={actionError} /></div>
        {pending ? <div className="pending-state"><Busy>正在加载文档</Busy></div> : !error && (rows?.items.length ? <>
          <div className="table-scroll"><table role="table">
            <thead role="rowgroup"><tr role="row"><th>文档</th><th>当前版本</th><th>大小</th><th>索引状态</th><th>操作</th></tr></thead>
            <tbody role="rowgroup">{rows.items.map((row) => <tr role="row" key={row.document_id}>
              <td role="cell"><div className="table-file"><span className="section-icon neutral"><FileText size={16} /></span><strong>{row.filename}</strong></div></td>
              <td role="cell"><span className="version">v{row.version}</span></td>
              <td role="cell">{(row.bytes / 1024).toFixed(1)} KiB</td>
              <td role="cell"><span className={`badge ${row.synced ? "badge-success" : ""}`}>{row.synced ? "已同步" : "待同步"}</span></td>
              <td role="cell"><div className="row-actions">
                <button className="text-button" type="button" disabled={Boolean(downloading)} onClick={() => download(row)}>
                  {downloading === row.document_id ? <Busy>下载中</Busy> : <><Download size={14} />原件</>}</button>
                {session.workspace.writable && <>
                  <Link className="text-button" to={`/ingestion?${new URLSearchParams({ code: library.code, document: row.document_id })}`}>更新</Link>
                  <button className="text-button" type="button" onClick={() => setMutation({ kind: "readers", item: row })}>权限</button>
                  <button className="text-button danger-text" type="button" onClick={() => setMutation({ kind: "delete", item: row })}>删除</button>
                </>}
              </div></td>
            </tr>)}</tbody>
          </table></div>
          <div className="table-footer"><span>第 {page + 1} 页 · 当前 {rows.items.length} 份文档</span><div>
            <button className="icon-button bordered" type="button" aria-label="上一页" disabled={page === 0} onClick={() => setPage(page - 1)}><ChevronLeft size={17} /></button>
            <button className="icon-button bordered" type="button" aria-label="下一页" disabled={!rows.has_more} onClick={() => setPage(page + 1)}><ChevronRight size={17} /></button>
          </div></div>
        </> : <Empty icon={<FileText size={23} />} title={query.trim() ? "没有匹配的文档" : "暂无可读的已发布文档"}>
          {query.trim() ? "试试其他文件名。" : "文档发布后将在这里显示。"}
        </Empty>)}
      </>}
    </section>
    {mutation && session && <DocumentDialog mutation={mutation} workspaceId={session.workspace.id} onClose={() => setMutation(undefined)}
      onDone={() => { setMutation(undefined); setReload((value) => value + 1); }} />}
  </>;
}

function DocumentDialog({ mutation, workspaceId, onClose, onDone }: {
  mutation: Mutation; workspaceId: string; onClose: () => void; onDone: () => void;
}) {
  const [policy, setPolicy] = useState("");
  const [list, setList] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const deleting = mutation.kind === "delete";

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || (!deleting && !policy)) return;
    const ids = Array.from(new Set(list.split(/[\s,，]+/).filter(Boolean)));
    if (!deleting && policy === "selected" && (!ids.length || ids.length > 64 || ids.some((id) => !UUID_PATTERN.test(id)))) {
      setError(new Error("请填写 1 至 64 个有效的应用 ID。"));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      if (deleting) await api.deleteDocument(workspaceId, mutation.item.document_id);
      else await api.setReaders(workspaceId, mutation.item.document_id, policy === "inherit" ? null : policy === "deny" ? [] : ids);
      onDone();
    } catch (cause) { setError(cause); }
    finally { setBusy(false); }
  }

  return <Dialog title={deleting ? "删除文档" : "更新读取权限"} onClose={() => { if (!busy) onClose(); }}>
    <form className="dialog-form" onSubmit={submit}>
      <p className="dialog-description">{deleting
        ? <>删除 <strong>{mutation.item.filename}</strong> 后，检索、引用和原件下载会立即不可用。</>
        : <>为 <strong>{mutation.item.filename}</strong> 设置新的读取规则。此操作需要知识库写入权限。</>}</p>
      {!deleting && <label className="field">读取规则
        <select value={policy} onChange={(e) => setPolicy(e.target.value)} required>
          <option value="">选择规则</option><option value="inherit">继承知识库权限</option>
          <option value="selected">仅允许指定应用</option><option value="deny">拒绝所有应用</option>
        </select>
      </label>}
      {!deleting && policy === "selected" && <label className="field">应用 ID
        <textarea rows={3} value={list} onChange={(e) => setList(e.target.value)} placeholder="同租户应用 ID，支持逗号或换行分隔" required />
      </label>}
      <Notice error={error} />
      <footer className="dialog-footer"><button type="button" className="button" onClick={onClose} disabled={busy}>取消</button>
        <button className={`button ${deleting ? "button-danger" : "button-primary"}`} disabled={busy || (!deleting && !policy)}>
          {busy ? <Busy>正在提交</Busy> : deleting ? "确认删除" : "保存权限"}
        </button></footer>
    </form>
  </Dialog>;
}
