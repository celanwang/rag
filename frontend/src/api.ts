export type Mode = "vector" | "keyword" | "rrf" | "rerank";

export interface Library {
  id: string;
  code: string;
  name: string;
}

export interface Catalog {
  tenant_id: string;
  libraries: Library[];
}

export interface Workspace {
  id: string;
  label: string;
  writable: boolean;
}

export interface Session extends Catalog {
  workspace: Workspace;
}

export interface Evidence {
  chunk_id: string;
  document_id: string;
  version_id: string;
  filename: string;
  heading: string | null;
  body: string;
  line_start: number;
  line_end: number;
}

export interface Retrieval {
  mode: Mode;
  results: Evidence[];
  degraded: string[];
  debug?: {
    service_ms: number;
    stage_ms: Record<string, number>;
    recall: Partial<Record<"vector" | "keyword", string[]>>;
    prefinal: string[];
  };
}

export interface DocumentSummary {
  document_id: string;
  version_id: string;
  filename: string;
  mime: string;
  version: number;
  bytes: number;
  synced: boolean;
}

export interface Submission {
  job_id: string;
  document_id: string;
  version_id: string;
}

export interface Job extends Submission {
  state: "queued" | "running" | "retry" | "done" | "failed";
  stage: string | null;
  attempts: number;
  error: string | null;
  available_at: string;
  published_version_id: string | null;
  keyword_status: "not_published" | "pending" | "retry" | "failed" | "synced";
  keyword_attempts: number | null;
  keyword_error: string | null;
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly trace: string | null) {
    super(message);
  }
}

const errorMessages: Record<number, string> = {
  401: "工作区凭据已失效，请更新本地配置。",
  403: "当前应用缺少执行此操作所需的权限。",
  404: "资源不存在或当前应用无权访问。",
  409: "相同的上传请求标识已用于不同的文件。",
  413: "文件超过 256 KiB。",
  415: "仅支持 Markdown 和 TXT 原件。",
  422: "请检查输入内容是否符合要求。",
  500: "服务处理请求时发生错误，请稍后重试。",
  502: "现在无法连接 RAG API，请检查服务是否已启动。",
  503: "RAG 服务暂不可用，请稍后重试。",
};

export function displayError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof TypeError) return "网络连接中断，请检查服务后重试。";
  return error instanceof Error ? error.message : "请求失败，请重试。";
}

async function request(path: string, workspaceId: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  headers.set("X-RAG-Workspace", workspaceId);
  const response = await fetch(`/v1${path}`, {
    ...init, headers, cache: "no-store", redirect: "error",
  });
  if (!response.ok) {
    throw new ApiError(
      errorMessages[response.status] ?? `请求失败（${response.status}）。`,
      response.status, response.headers.get("x-request-id"),
    );
  }
  return response;
}

async function json<T>(path: string, workspaceId: string, init?: RequestInit): Promise<T> {
  return (await request(path, workspaceId, init)).json();
}

function jsonBody(method: string, body: unknown): RequestInit {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

const libraryPath = (code: string) => `/knowledge-bases/${encodeURIComponent(code)}/documents`;
const documentPath = (id: string) => `/documents/${encodeURIComponent(id)}`;

export async function workspaces(signal?: AbortSignal): Promise<Workspace[]> {
  const response = await fetch("/local/workspaces", { cache: "no-store", signal });
  if (!response.ok) throw new ApiError("本地工作区配置不可用。", response.status, null);
  return response.json();
}

export const api = {
  catalog: (workspaceId: string, signal?: AbortSignal) => json<Catalog>("/knowledge-bases", workspaceId, { signal }),
  documents: (workspaceId: string, code: string, q: string, offset: number, limit: number, signal?: AbortSignal) => {
    const search = new URLSearchParams({ q, offset: String(offset), limit: String(limit) });
    return json<{ items: DocumentSummary[]; has_more: boolean }>(`${libraryPath(code)}?${search}`, workspaceId, { signal });
  },
  retrieve: (
    workspaceId: string, code: string, question: string, mode: Mode, top_k: number, debug: boolean, signal: AbortSignal,
  ) => json<Retrieval>("/retrieve", workspaceId, {
    ...jsonBody("POST", { knowledge_base_code: code, question, mode, top_k, debug }), signal,
  }),
  citation: (workspaceId: string, id: string, signal?: AbortSignal) =>
    json<Evidence>(`/citations/${encodeURIComponent(id)}`, workspaceId, { signal }),
  download: async (workspaceId: string, id: string, fallback: string, signal?: AbortSignal) => {
    const response = await request(`${documentPath(id)}/file`, workspaceId, { signal });
    const encoded = response.headers.get("content-disposition")?.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
    const blob = await response.blob();
    if (signal?.aborted) return;
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.download = encoded ? decodeURIComponent(encoded) : fallback;
    link.href = url;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
  upload: (workspaceId: string, code: string, file: File, identifier: string, documentId?: string, signal?: AbortSignal) => {
    const query = new URLSearchParams({ filename: file.name });
    if (documentId) query.set("document_id", documentId);
    return json<Submission>(`${libraryPath(code)}?${query}`, workspaceId, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream", "Idempotency-Key": identifier },
      body: file,
      signal,
    });
  },
  job: (workspaceId: string, id: string, signal?: AbortSignal) =>
    json<Job>(`/ingestion-jobs/${encodeURIComponent(id)}`, workspaceId, { signal }),
  setReaders: (workspaceId: string, id: string, readers: string[] | null) =>
    json(`${documentPath(id)}/readers`, workspaceId, jsonBody("PATCH", { readers })),
  deleteDocument: (workspaceId: string, id: string) => json(documentPath(id), workspaceId, { method: "DELETE" }),
};
