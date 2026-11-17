# Retrieval PoC

Run the commands from the repository root. Requirements are Docker Compose, uv, and Python 3.11. The first run downloads the service images and about 390 MB of model files. Models run locally on the CPU.

## Local Retrieval PoC

Start PostgreSQL and Elasticsearch, install the locked dependencies, and start the API.

```sh
docker compose up -d --wait
uv sync --locked --python 3.11
uv run --locked python -m poc.models
uv run --locked uvicorn poc.api:app --host 127.0.0.1 --port 8000
```

Run the benchmark and isolation checks in a second terminal.

```sh
uv run --locked python -m poc.benchmark --repeat 3 --out .poc/results.json
uv run --locked python -m poc.verify
```

The benchmark imports 14 files across two tenants and runs 30 questions against four retrieval modes. It uses up to eight candidates per search path, RRF with `k=60`, up to 12 candidates for reranking, and five results per query. The API defaults are 50 candidates per path and up to 30 for reranking. Use dedicated Compose volumes for a fixed corpus. Results include source fingerprints, model versions, ranks, and timings. [Validation report](../docs/poc-results.md)

## API Examples

Upload a document as `alpha-admin` and query it as `alpha-reader`.

```sh
curl -fsS http://127.0.0.1:8000/v1/knowledge-bases/demo/documents \
  -H 'Authorization: Bearer alpha-admin' -H 'Content-Type: application/json' \
  -d '{"filename":"manual.md","content":"# Device Errors\n\nFor DEV-120, rebind the device in the admin console.","readers":["reader"]}'

curl -fsS http://127.0.0.1:8000/v1/retrieve \
  -H 'Authorization: Bearer alpha-reader' -H 'Content-Type: application/json' \
  -d '{"question":"How do I fix DEV-120?","mode":"rerank","debug":true}'
```

- Uploads accept UTF-8 Markdown and plain text. Resubmitting the same file name and content to the same knowledge base reuses the published version. Content changes create a new version.
- Omitting `readers` or sending an empty list permits reads within the knowledge base. Alpha audit files are assigned to `alpha-auditor`. Use `beta-admin` and `beta-reader` for Beta.
- When `keyword_ready` is false, PostgreSQL has published the version. Resubmit the same upload to retry Elasticsearch synchronization.
- Retrieval modes are `vector`, `keyword`, `rrf`, and `rerank`. Hybrid responses report degraded search paths. PostgreSQL checks access before returning evidence.
- `GET /v1/citations/{chunk_id}?knowledge_base_id=demo` checks current access and returns the file name, version, and source line range.

## RAGFlow Baseline

Run the local API first. The baseline uses RAGFlow 1.0.0 in a separate Compose project and calls the local embedding endpoint through Docker Desktop.

```sh
mkdir -p .poc
(
  umask 077
  test -s .poc/ragflow-admin-password || openssl rand -hex 24 > .poc/ragflow-admin-password
  test -s .poc/ragflow-embedding-key || openssl rand -hex 24 > .poc/ragflow-embedding-key
)
export RAGFLOW_ADMIN_PASSWORD="$(cat .poc/ragflow-admin-password)"
docker compose -f poc/ragflow/compose.yaml up -d --wait
curl -fsS --retry 10 --retry-connrefused http://127.0.0.1:19380/api/v1/system/healthz
docker compose -f poc/ragflow/compose.yaml exec -T ragflow cat /ragflow/conf/public.pem > .poc/ragflow-public.pem
```

When the health endpoint reports `status=ok`, start the embedding service.

```sh
RAGFLOW_EMBEDDING_KEY="$(cat .poc/ragflow-embedding-key)" \
  uv run --locked uvicorn poc.ragflow_embedding:app --host 127.0.0.1 --port 18090
```

In another terminal, provision the two tenants and run the comparison. Local credentials and results are stored in `.poc/`.

```sh
uv run --locked python -m poc.ragflow_setup
uv run --locked python -m poc.ragflow_baseline --repeat 3 --out .poc/ragflow-results.json
```

Both systems import the same source files and are scored against evidence anchors in the originals. The comparison also probes tenant isolation and the effect of changing or removing the Alpha reader's document allowlist. [RAGFlow results](../docs/poc-results.md#ragflow-同题对照)
