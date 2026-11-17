import {
  ArrowRight, ArrowUpRight, BookOpen, Braces, Clock3, Download, FileText, Fingerprint, GitMerge,
  ListFilter, Search, ShieldCheck, SlidersHorizontal, Sparkles,
} from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, type Evidence, type Library, type Mode, type Retrieval, type Session } from "./api";
import { Busy, Dialog, Empty, Notice, PageHeading } from "./ui";

const modes = [
  { value: "rrf", label: "混合检索", sub: "向量 + 关键词", icon: GitMerge },
  { value: "rerank", label: "混合 + 精排", sub: "百炼精排", icon: Sparkles },
  { value: "vector", label: "向量检索", sub: "语义相似度", icon: Fingerprint },
  { value: "keyword", label: "关键词检索", sub: "词项匹配", icon: ListFilter },
] as const;
const examples = ["RET-ALPHA-07 如何申请退货？", "修改配送地址有哪些限制？", "退货需要提供哪些材料？"];
const stages: Record<string, string> = {
  embedding: "问题向量化", keyword: "关键词召回", vector: "向量召回", recall_wall: "并行召回总耗时",
  authorization: "候选权限复核", rerank: "百炼精排", authorization_final: "结果权限复核",
};

export function LibrarySelect({ library, libraries, onChange, id }: {
  library?: Library; libraries: Library[]; onChange: (library: Library) => void; id?: string;
}) {
  return (
    <select id={id} value={library?.id ?? ""} disabled={!libraries.length} onChange={(event) => {
      const selected = libraries.find((entry) => entry.id === event.target.value);
      if (selected) onChange(selected);
    }}>
      {!libraries.length && <option value="">暂无可选知识库</option>}
      {libraries.map((entry) => <option value={entry.id} key={entry.id}>{entry.name} · {entry.code}</option>)}
    </select>
  );
}

export function RetrievePage({ session, library, setLibrary, refresh, question, setQuestion }: {
  session: Session | null; library?: Library; setLibrary: (library: Library) => void; refresh: () => void;
  question: string; setQuestion: (value: string) => void;
}) {
  const [mode, setMode] = useState<Mode>("rrf");
  const [top, setTop] = useState(5);
  const [debug, setDebug] = useState(true);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Retrieval>();
  const [error, setError] = useState<unknown>(null);
  const [source, setSource] = useState<Evidence>();
  const controller = useRef<AbortController | null>(null);
  const questionInput = useRef<HTMLTextAreaElement>(null);
  const submitButton = useRef<HTMLButtonElement>(null);
  useEffect(() => () => controller.current?.abort(), []);
  const currentMode = modes.find((entry) => entry.value === mode)!;

  async function search(event: FormEvent) {
    event.preventDefault();
    if (!session) { refresh(); return; }
    if (!library || !question.trim() || busy) return;
    const operation = new AbortController();
    controller.current = operation;
    setBusy(true);
    setError(null);
    setResult(undefined);
    try {
      setResult(await api.retrieve(session.workspace.id, library.code, question.trim(), mode, top, debug, operation.signal));
    } catch (cause) {
      if (!operation.signal.aborted) setError(cause);
    } finally {
      if (!operation.signal.aborted) setBusy(false);
    }
  }

  return <>
    <PageHeading title="检索工作台" action={<Link className="button heading-action" to="/knowledge"><BookOpen size={16} />浏览知识库<ArrowUpRight size={15} /></Link>}>
      对照不同召回策略，查看经授权的证据和原文引用。
    </PageHeading>
    <div className="workbench retrieval-workbench">
      <div className="work-main">
        <form id="retrieval-form" className="panel query-panel" onSubmit={search}>
          <div className="panel-header">
            <div className="section-icon blue"><Search size={17} /></div>
            <div><h2>发起检索</h2><p>在选定知识库中查找相关证据</p></div>
          </div>
          <div className="form-body">
            <label className="field" htmlFor="question">检索问题</label>
            <div className="question-box">
              <textarea id="question" ref={questionInput} value={question} maxLength={240}
                placeholder="例如：订单签收后，如何申请退货？" onChange={(e) => setQuestion(e.target.value)}
                onKeyDown={(e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); submitButton.current?.click(); } }} />
              <div className="question-footer"><span><kbd>Ctrl</kbd> / <kbd>⌘</kbd> + <kbd>↵</kbd></span><span>{question.length} / 240</span></div>
            </div>
            <div className="examples"><span>试试这些问题</span>{examples.map((example) =>
              <button type="button" key={example} onClick={() => { setQuestion(example); questionInput.current?.focus(); }}>{example}<ArrowUpRight size={12} /></button>,
            )}</div>
          </div>
          <div className="panel-footer">
            <span className="footer-context"><ShieldCheck size={15} />{library ? `检索范围 · ${library.name}` : "按应用权限检索"}</span>
            <button ref={submitButton} className="button button-primary" disabled={busy || Boolean(session && (!library || !question.trim()))}>
              {busy ? <Busy>正在检索</Busy> : <><Search size={16} />{session ? "开始检索" : "加载工作区"}</>}
            </button>
          </div>
        </form>
        <section className="panel results-panel" aria-live="polite">
          <div className="panel-header">
            <div className="section-icon neutral"><FileText size={17} /></div>
            <div><h2>检索结果</h2><p>按召回策略排序的知识库证据</p></div>
            {result && <span className="counter">{modes.find((entry) => entry.value === result.mode)?.label} · {result.results.length} 条证据</span>}
          </div>
          <div className="results-body">
            <Notice error={error} />
            {busy ? <div className="pending-state"><Busy>正在召回并复核证据…</Busy></div> : result ? <>
              {result.degraded.length > 0 && <div className="notice notice-warning">已降级运行 · {result.degraded.map((name) => stages[name] ?? name).join("、")}暂不可用</div>}
              {result.results.length === 0
                ? <Empty icon={<Search size={20} />} title="未找到相关证据">可尝试缩短问题，或切换检索方式。</Empty>
                : <ol className="evidence-list">{result.results.map((evidence, index) =>
                  <li key={evidence.chunk_id}>
                    <div className="evidence-top"><span className="rank">{index + 1}</span><h3>{evidence.heading || evidence.filename}</h3></div>
                    <p className="evidence-text">{evidence.body}</p>
                    <div className="evidence-footer">
                      <span><FileText size={14} />{evidence.filename} <span className="line-ref">L{evidence.line_start}–{evidence.line_end}</span></span>
                      <button className="text-button" onClick={() => setSource(evidence)}>查看引用<ArrowRight size={15} /></button>
                    </div>
                  </li>,
                )}</ol>}
            </> : !error && <Empty icon={<FileText size={24} />} title="证据将在这里显示">输入问题并开始检索，查看匹配的切片和来源。</Empty>}
          </div>
        </section>
      </div>
      <aside className="work-side" aria-label="检索策略和耗时">
        <section className="panel options-panel">
          <div className="panel-header"><div className="section-icon neutral"><SlidersHorizontal size={17} /></div><div><h2>检索设置</h2><p>指定范围与召回策略</p></div></div>
          <div className="options-body">
            <label className="field">知识库<LibrarySelect library={library} libraries={session?.libraries ?? []} onChange={setLibrary} /></label>
            <fieldset className="mode-field"><legend>召回方式</legend>
              <div className="mode-grid">{modes.map(({ value, label, sub, icon: Icon }) =>
                <label className={`mode-option ${mode === value ? "mode-selected" : ""}`} key={value}>
                  <input type="radio" name="mode" value={value} checked={mode === value} onChange={() => setMode(value)} />
                  <Icon size={18} /><strong>{label}</strong><small>{sub}</small>
                </label>,
              )}</div>
            </fieldset>
            <label className="field top-field">返回条数
              <select value={top} onChange={(e) => setTop(Number(e.target.value))}>{[3, 5, 10, 20, 30].map((value) => <option key={value} value={value}>{value} 条</option>)}</select>
            </label>
            <label className="check-field"><input type="checkbox" checked={debug} onChange={(e) => setDebug(e.target.checked)} /><span><strong>记录检索诊断</strong><small>显示服务耗时与召回情况</small></span></label>
          </div>
        </section>
        <section className="panel metrics-panel">
          <div className="panel-header"><div className="section-icon neutral"><Clock3 size={17} /></div><div><h2>诊断摘要</h2></div></div>
          {result?.debug ? <dl className="metrics">
            <div className="main-metric"><dt>服务耗时</dt><dd>{result.debug.service_ms.toFixed(0)} <small>ms</small></dd></div>
            {Object.entries(result.debug.recall).map(([name, ids]) =>
              <div key={name}><dt>{stages[name]} · 已授权</dt><dd>{ids.length} 条</dd></div>,
            )}
            {Object.entries(result.debug.stage_ms).map(([name, value]) =>
              <div key={name}><dt>{stages[name] ?? name}</dt><dd>{value.toFixed(1)} ms</dd></div>,
            )}
          </dl> : <div className="metrics-placeholder"><Braces size={19} />{!debug ? "本次配置已关闭诊断。" : `${currentMode.label} · 完成检索后显示耗时`}</div>}
        </section>
      </aside>
    </div>
    {source && session && <CitationDialog evidence={source} workspaceId={session.workspace.id} onClose={() => setSource(undefined)} />}
  </>;
}

function CitationDialog({ evidence, workspaceId, onClose }: { evidence: Evidence; workspaceId: string; onClose: () => void }) {
  const [citation, setCitation] = useState<Evidence>();
  const [error, setError] = useState<unknown>(null);
  const [downloading, setDownloading] = useState(false);
  const abort = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    abort.current = controller;
    api.citation(workspaceId, evidence.chunk_id, controller.signal).then(setCitation).catch((cause) => {
      if (!controller.signal.aborted) setError(cause);
    });
    return () => controller.abort();
  }, [workspaceId, evidence.chunk_id]);

  async function download() {
    setDownloading(true);
    setError(null);
    try {
      await api.download(workspaceId, evidence.document_id, evidence.filename, abort.current?.signal);
    } catch (cause) {
      if (!abort.current?.signal.aborted) setError(cause);
    } finally { setDownloading(false); }
  }

  return <Dialog title="原文引用" onClose={onClose}>
    <div className="dialog-form">
      <Notice error={error} />
      {citation ? <>
        <div className="citation-meta"><FileText size={18} /><div><strong>{citation.filename}</strong><small>原文第 {citation.line_start}–{citation.line_end} 行</small></div></div>
        {citation.heading && <h3 className="citation-title">{citation.heading}</h3>}
        <pre className="citation-text">{citation.body}</pre>
        <footer className="dialog-footer"><button className="button button-primary" onClick={download} disabled={downloading}>{downloading ? <Busy>下载中</Busy> : <><Download size={16} />下载原件</>}</button></footer>
      </> : !error && <div className="pending-state"><Busy>正在获取引用</Busy></div>}
    </div>
  </Dialog>;
}
