import { ArrowRight, CircleCheck, CloudUpload, FileText, History, KeyRound, X } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiError, type Job, type Library, type ParseReport, type Session } from "./api";
import { Busy, Empty, Notice, PageHeading, UUID_PATTERN } from "./ui";

interface TrackedJob {
  id: string;
  filename?: string;
  job?: Job;
  error?: unknown;
  misses?: number;
}

const finished = (job?: Job) => Boolean(job &&
  (job.state === "failed" || (job.state === "done" && ["failed", "synced"].includes(job.keyword_status))));

export function useIngestion(workspaceId?: string) {
  const [entries, setEntries] = useState<TrackedJob[]>([]);
  const operations = useRef(new AbortController());
  useEffect(() => () => operations.current.abort(), []);

  function track(id: string, filename?: string) {
    setEntries((current) => {
      const previous = current.find((item) => item.id === id);
      return [{ ...previous, id, filename: filename ?? previous?.filename, error: undefined, misses: 0 },
        ...current.filter((item) => item.id !== id)];
    });
  }

  useEffect(() => {
    const waiting = entries.filter((entry) => !entry.error && !finished(entry.job)).slice(0, 8);
    if (!workspaceId || !waiting.length) return;
    const controller = new AbortController();
    const timeout = setTimeout(async () => {
      const updates = await Promise.all(waiting.map(async (entry): Promise<TrackedJob> => {
        try {
          return { ...entry, job: await api.job(workspaceId, entry.id, controller.signal), misses: 0 };
        } catch (error) {
          const permanent = error instanceof ApiError && [401, 403, 404, 422].includes(error.status);
          const misses = (entry.misses ?? 0) + 1;
          return { ...entry, misses, error: permanent || misses >= 3 ? error : undefined };
        }
      }));
      if (!controller.signal.aborted) {
        setEntries((current) => current.map((entry) => updates.find((update) => update.id === entry.id) ?? entry));
      }
    }, 1300);
    return () => { clearTimeout(timeout); controller.abort(); };
  }, [workspaceId, entries]);

  async function upload(code: string, file: File, request: string, documentId?: string) {
    if (!workspaceId) return;
    const result = await api.upload(workspaceId, code, file, request, documentId, operations.current.signal);
    track(result.job_id, file.name);
  }

  return { entries, upload, track };
}

const jobStates = { queued: "排队中", running: "处理中", retry: "等待重试", done: "已发布", failed: "入库失败" };
const syncStates = { not_published: "待发布", pending: "等待同步", retry: "同步重试", failed: "索引同步失败", synced: "已同步" };

const reasons: Record<string, string> = {
  format_mismatch: "文件内容与扩展名不符。",
  content_encrypted: "文件已加密，请上传未加密原件。",
  content_corrupt: "无法读取原件，文件可能已损坏。",
  content_limit: "文件超出解析限制。",
  content_no_text: "文件没有可入库的文本。",
  pdf_scanned: "PDF 仅含扫描或图片页，当前不执行 OCR。",
  parser_failed: "解析器未能处理该文件。",
  docx_skipped_content: "已跳过不支持的 DOCX 内容。",
  docx_skipped_secondary_parts: "已跳过页眉、页脚或附注等次要内容。",
  pdf_parser_warning: "PDF 提取过程中遇到无法完整处理的内容。",
};

function jobReason(reason: string | null) {
  return reason ? (reasons[reason] ?? reason) : null;
}

function ParseDetails({ report }: { report: ParseReport }) {
  const details = [
    report.scan_pages.length > 0 && `图片页：${report.scan_pages.join("、")}`,
    report.no_text_pages.length > 0 && `其他无可提取文字的页：${report.no_text_pages.join("、")}`,
    report.skipped_tables.length > 0 && `跳过复杂表格：${report.skipped_tables.join("、")}`,
    ...report.warnings.filter((code) => Object.hasOwn(reasons, code)).map(jobReason),
  ].filter(Boolean);
  const label = { complete: "解析完成", partial: "部分解析", failed: "解析失败" }[report.status];
  return <small className={report.status === "partial" ? "job-reason" : undefined}>
    {label}{details.length ? ` · ${details.join("；")}` : ""}</small>;
}

export function IngestPage({ session, library, queue, refresh }: {
  session: Session | null; library?: Library; queue: ReturnType<typeof useIngestion>; refresh: () => void;
}) {
  const [params] = useSearchParams();
  const [code, setCode] = useState(params.get("code") ?? library?.code ?? "");
  const [documentId, setDocumentId] = useState(params.get("document") ?? "");
  const [file, setFile] = useState<File>();
  const [drag, setDrag] = useState(false);
  const [busy, setBusy] = useState(false);
  const [jobId, setJobId] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [trackingError, setTrackingError] = useState<unknown>(null);
  const [success, setSuccess] = useState(false);
  const readOnly = session?.workspace.writable === false;
  const key = useRef<string>();
  const input = useRef<HTMLInputElement>(null);

  function choose(next?: File) {
    setError(null);
    setSuccess(false);
    key.current = undefined;
    setFile(undefined);
    if (!next) return;
    if (!/\.(md|txt|docx|pdf)$/i.test(next.name)) { setError(new Error("请选择 .md、.txt、.docx 或 .pdf 文件。")); return; }
    if (!next.size || next.size > 256 * 1024) { setError(new Error("文件大小须在 1 字节至 256 KiB 之间。")); return; }
    setFile(next);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!session) { refresh(); return; }
    if (!file || busy || readOnly) return;
    if (documentId.trim() && !UUID_PATTERN.test(documentId.trim())) { setError(new Error("请输入有效的文档 ID。")); return; }
    setError(null);
    setBusy(true);
    key.current ??= crypto.randomUUID();
    try {
      await queue.upload(code.trim(), file, key.current, documentId.trim() || undefined);
      choose();
      setSuccess(true);
      if (input.current) input.current.value = "";
    } catch (cause) {
      setError(cause);
    } finally { setBusy(false); }
  }

  function track(event: FormEvent) {
    event.preventDefault();
    if (!session) { refresh(); return; }
    if (readOnly) return;
    const id = jobId.trim().toLowerCase();
    if (!UUID_PATTERN.test(id)) { setTrackingError(new Error("请输入有效的任务 ID。")); return; }
    queue.track(id);
    setTrackingError(null);
    setJobId("");
  }

  return <>
    <PageHeading title="文档入库" action={<Link className="button heading-action" to="/knowledge"><FileText size={16} />查看文档<ArrowRight size={15} /></Link>}>
      上传原件，跟踪向量化、版本发布与关键词索引同步。
    </PageHeading>
    <div className="workbench">
      <div className="work-main">
        <form className="panel" onSubmit={submit}>
          <div className="panel-header"><div className="section-icon blue"><CloudUpload size={18} /></div><div><h2>上传文档</h2><p>Markdown、TXT、DOCX、PDF · 单文件最大 256 KiB</p></div></div>
          <div className="upload-body">
            <label className="field">目标知识库
              <input list="upload-libraries" placeholder="输入知识库短码" value={code} onChange={(e) => { setCode(e.target.value); key.current = undefined; }} required disabled={busy || readOnly} pattern="[a-z0-9][a-z0-9\-]{0,62}" />
              <datalist id="upload-libraries">{session?.libraries.map((item) => <option value={item.code} key={item.id}>{item.name}</option>)}</datalist>
            </label>
            <label className="field">更新现有版本 <span className="optional">可选</span>
              <input value={documentId} onChange={(e) => { setDocumentId(e.target.value); key.current = undefined; }} placeholder="填写文档 ID 以发布新版本" disabled={busy || readOnly} />
            </label>
            <label className={`drop-zone ${drag ? "drop-active" : ""}`}
              onDragOver={(event) => { event.preventDefault(); if (!busy && !readOnly) setDrag(true); }}
              onDragLeave={() => setDrag(false)}
              onDrop={(event) => {
                event.preventDefault(); setDrag(false);
                if (!busy && !readOnly) choose(event.dataTransfer.files[0]);
              }}>
              <input ref={input} aria-label="选择文件" className="sr-only" type="file" accept=".md,.txt,.docx,.pdf" disabled={busy || readOnly}
                onChange={(e) => choose(e.target.files?.[0])} />
              <span className="drop-icon"><CloudUpload size={26} /></span>
              <strong>点击选择或拖入文件</strong>
              <small>支持 UTF-8 文本、DOCX 和含文本层的 PDF</small>
            </label>
            {file && <div className="selected-file"><FileText size={17} /><span><strong>{file.name}</strong><small>{(file.size / 1024).toFixed(1)} KiB</small></span>
              <button className="icon-button" type="button" disabled={busy} onClick={() => { choose(); if (input.current) input.current.value = ""; }} aria-label="移除文件"><X size={17} /></button>
            </div>}
            <Notice error={error} />
            {success && <div className="notice notice-success" role="status"><CircleCheck size={17} />文件已受理，任务进度见下方。</div>}
          </div>
          <div className="panel-footer">
            <span className="footer-context"><KeyRound size={15} />需要知识库写入权限</span>
            <button className="button button-primary" type={session ? "submit" : "button"} onClick={session ? undefined : refresh}
              disabled={busy || readOnly || Boolean(session && (!file || !code))}>
              {busy ? <Busy>正在上传</Busy> : <><CloudUpload size={17} />{readOnly ? "只读工作区" : session ? "提交入库" : "加载工作区"}</>}
            </button>
          </div>
        </form>
        <section className="panel tasks-panel">
          <div className="panel-header"><div className="section-icon neutral"><History size={17} /></div><div><h2>入库任务</h2><p>当前工作区中跟踪的任务</p></div></div>
          {queue.entries.length === 0 ? <Empty icon={<History size={24} />} title="暂无任务">上传文件或输入任务 ID，开始跟踪进度。</Empty> :
            <ul className="task-list">{queue.entries.map((entry) => <li key={entry.id}>
              <h3>{entry.filename || "入库任务"}</h3>
              <code>{entry.id}</code>
              <div className="task-state">
                <span className={`badge ${entry.job?.state === "failed" ? "badge-error" : ""}`}>{entry.job ? jobStates[entry.job.state] : entry.error ? "查询失败" : "等待状态"}</span>
                {entry.job && <span className={`badge ${entry.job.keyword_status === "synced" ? "badge-success" : ""}`}>{syncStates[entry.job.keyword_status]}</span>}
                {entry.job && <small>入库尝试 {entry.job.attempts} 次</small>}
              </div>
              {entry.job?.state === "retry" && <small>下次尝试 {new Date(entry.job.available_at).toLocaleString("zh-CN")}</small>}
              {(entry.job?.error || entry.job?.keyword_error) && <small className="job-reason">{jobReason(entry.job.error ?? entry.job.keyword_error)}</small>}
              {entry.job?.state === "failed" && entry.job.published_version_id && <small>已发布版本仍可查询。</small>}
              {entry.job?.parse_report && <ParseDetails report={entry.job.parse_report} />}
              {Boolean(entry.error) && <><Notice error={entry.error} /><button type="button" className="text-button" onClick={() => queue.track(entry.id)}>重新查询</button></>}
            </li>)}</ul>}
        </section>
      </div>
      <aside className="work-side" aria-label="入库辅助">
        <section className="panel options-panel">
          <div className="panel-header"><div className="section-icon neutral"><History size={17} /></div><div><h2>跟踪已有任务</h2><p>获取已受理任务的实时状态</p></div></div>
          <form className="options-body" onSubmit={track}>
            <label className="field">任务 ID<input value={jobId} onChange={(e) => setJobId(e.target.value)} placeholder="粘贴任务 ID" required /></label>
            <Notice error={trackingError} />
            <button className="button" disabled={readOnly || !jobId.trim()}>查询任务<ArrowRight size={15} /></button>
          </form>
        </section>
        <section className="panel process-panel">
          <div className="panel-header"><div className="section-icon neutral"><FileText size={17} /></div><div><h2>入库流程</h2></div></div>
          <ol><li><span>1</span><div><strong>接收原件</strong><small>保存文件和任务回执</small></div></li>
            <li><span>2</span><div><strong>发布版本</strong><small>切分和向量化完成后可检索</small></div></li>
            <li><span>3</span><div><strong>同步关键词索引</strong><small>同步完成后支持关键词召回</small></div></li></ol>
        </section>
      </aside>
    </div>
  </>;
}
