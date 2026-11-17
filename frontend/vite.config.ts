import react from "@vitejs/plugin-react";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import { localWorkspaces } from "./local";

const target = new URL(process.env.RAG_API_TARGET || "http://127.0.0.1:8001");
if (target.protocol !== "http:" || target.username || target.password ||
    !["127.0.0.1", "localhost", "[::1]"].includes(target.hostname)) {
  throw new Error("RAG_API_TARGET must be a local HTTP address.");
}
const proxy = { "^/v1/": { target: target.origin, changeOrigin: true } };
const configFile = process.env.RAG_WEB_WORKSPACES ? resolve(process.env.RAG_WEB_WORKSPACES) : undefined;
const workspaces = localWorkspaces({
  target,
  runs: fileURLToPath(new URL("../.rag/dev/", import.meta.url)),
  file: configFile,
});
const deny = [".env", ".env.*", "*.{crt,pem}", "**/.git/**", "**/.rag/**", "**/readers.json"];
if (configFile) deny.push(configFile);

export default defineConfig({
  plugins: [react(), workspaces],
  server: { host: "127.0.0.1", port: 5173, strictPort: true, cors: false, proxy, fs: { deny } },
  preview: { host: "127.0.0.1", strictPort: true, cors: false, proxy },
});
