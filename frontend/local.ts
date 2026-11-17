import { readFileSync, readdirSync } from "node:fs";
import type { IncomingMessage, ServerResponse } from "node:http";
import { join } from "node:path";
import type { Connect, Plugin } from "vite";

interface Profile {
  label?: string;
  key: string;
  writer_key?: string;
}

interface LocalOptions {
  target: URL;
  runs: string;
  file?: string;
}

function readJson(path: string): unknown {
  return JSON.parse(readFileSync(path, "utf8"));
}

function readProfiles(options: LocalOptions): Record<string, Profile> {
  let data: unknown;
  if (options.file) {
    data = readJson(options.file);
  } else {
    try {
      const runs = readdirSync(options.runs).filter((name) => name.startsWith("demo-")).sort().reverse();
      for (const run of runs) {
        try {
          const folder = join(options.runs, run);
          if ((readJson(join(folder, "run.json")) as { status?: string })?.status !== "passed") continue;
          data = readJson(join(folder, "readers.json"));
          break;
        } catch {
          continue;
        }
      }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
  }

  if (data === undefined) return {};
  if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("Invalid workspace file");
  return Object.fromEntries(Object.entries(data).map(([id, entry]) => {
    if (!/^[a-z0-9][a-z0-9-]{0,62}$/.test(id) || !entry || typeof entry.key !== "string" ||
        !/^[!-~]+$/.test(entry.key) || (entry.writer_key !== undefined &&
        (typeof entry.writer_key !== "string" || !/^[!-~]+$/.test(entry.writer_key)))) {
      throw new Error("Invalid workspace credentials");
    }
    return [id, {
      key: entry.key,
      writer_key: entry.writer_key,
      label: typeof entry.label === "string" && entry.label.trim() ? entry.label : id,
    }];
  }));
}

function respond(res: ServerResponse, status: number, data: unknown) {
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
  res.end(JSON.stringify(data));
}

function sameOrigin(req: IncomingMessage) {
  const { origin, host, "sec-fetch-site": site } = req.headers;
  return /^(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?$/.test(host ?? "") &&
    (!origin || origin === `http://${host}`) && (!site || site === "same-origin" || site === "none");
}

export function localWorkspaces(options: LocalOptions): Plugin {
  const validated = new Map<string, { reader: string; writer: string }>();

  async function validateWriter(id: string, profile: Profile, writer: string) {
    const pair = validated.get(id);
    if (pair?.reader === profile.key && pair.writer === writer) return true;
    async function tenant(key: string) {
      const response = await fetch(new URL("/v1/knowledge-bases", options.target), {
        headers: { Authorization: `Bearer ${key}` }, redirect: "error", signal: AbortSignal.timeout(5000),
      });
      if (!response.ok) return null;
      const data = await response.json() as { tenant_id?: string };
      return data.tenant_id || null;
    }
    const [readTenant, writeTenant] = await Promise.all([tenant(profile.key), tenant(writer)]);
    if (!readTenant || readTenant !== writeTenant) return false;
    validated.set(id, { reader: profile.key, writer });
    return true;
  }

  const middleware: Connect.NextHandleFunction = async (req, res, next) => {
    const path = req.url?.split("?")[0] ?? "";
    if (path !== "/local/workspaces" && !path.startsWith("/v1/")) { next(); return; }
    if (!sameOrigin(req)) { respond(res, 403, { detail: "The local workbench requires same-origin requests." }); return; }
    try {
      const profiles = readProfiles(options);
      if (path === "/local/workspaces") {
        if (req.method !== "GET") { respond(res, 405, { detail: "Method not allowed." }); return; }
        respond(res, 200, Object.entries(profiles).map(([id, profile]) => ({
          id, label: profile.label, writable: Boolean(profile.writer_key),
        })));
        return;
      }
      const id = req.headers["x-rag-workspace"];
      const profile = typeof id === "string" && Object.hasOwn(profiles, id) ? profiles[id] : undefined;
      if (!profile || typeof id !== "string") { respond(res, 401, { detail: "Select a local workspace." }); return; }
      const writes = path.startsWith("/v1/ingestion-jobs/") ||
        (req.method !== "GET" && req.method !== "HEAD" && path !== "/v1/retrieve");
      let key = profile.key;
      if (writes) {
        if (!profile.writer_key) { respond(res, 403, { detail: "Workspace is read-only." }); return; }
        if (profile.writer_key !== key && !await validateWriter(id, profile, profile.writer_key)) {
          respond(res, 403, { detail: "Reader and writer profiles must belong to the same tenant." });
          return;
        }
        key = profile.writer_key;
      }
      delete req.headers["x-rag-workspace"];
      req.headers.authorization = `Bearer ${key}`;
      next();
    } catch {
      respond(res, 503, { detail: "Local workspace configuration or RAG API is unavailable." });
    }
  };
  return {
    name: "local-rag-workspaces",
    configureServer(server) { server.middlewares.use(middleware); },
    configurePreviewServer(server) { server.middlewares.use(middleware); },
  };
}
