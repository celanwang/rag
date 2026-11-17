# RAG Service

The RAG service ingests Markdown and plain text and retrieves authorized evidence with citations. Run the commands from the repository root with Docker Compose, uv, and Python 3.11. [Compose config](../docs/env/rag/compose.yaml)

## Persistent rag-dev

Use this Compose project for the business service, e-commerce samples, integration tests, and benchmarks.

```sh
uv sync --locked --python 3.11
uv run --locked python -m rag.dev up
uv run --locked --env-file .env python -m rag.dev demo
uv run --locked --env-file .env python -m rag.dev serve
```

Run `serve` in a separate terminal to keep the API and both workers running. Open [API documentation](http://127.0.0.1:8001/docs) to browse the endpoints. The demo creates two sample tenants in the main `rag` database and writes read-only keys to the run's local `readers.json`. Use a key from that file in the API docs and query its `shop` knowledge base.

`up` reuses the existing PostgreSQL, S3, and Elasticsearch volumes and applies missing SQL stages. Local credentials are stored in `docs/env/rag/.env`. [Compose config](../docs/env/rag/compose.yaml)

```sh
uv run --locked python -m rag.dev status
uv run --locked python -m rag.dev test
uv run --locked --env-file .env python -m rag.dev benchmark --repeat 3
```

Each integration or benchmark run uses its own database, bucket, and index in the same `rag-dev` services. This keeps queue claims, cleanup tests, and keyword index statistics local to that run. Resources and logs are retained. Find database, bucket, index, and outcome in `.rag/dev/<run>/run.json`. The benchmark also saves `benchmark.json`, including per-question ranks and timings. [Measured results](../docs/retrieval-results.md)

`status` lists published sample documents and the resources created by each run. Inspect other databases by replacing `rag` in the PostgreSQL command with the recorded database name.

```sh
docker compose -p rag-dev --env-file docs/env/rag/.env -f docs/env/rag/compose.yaml \
  exec postgres psql -U postgres -d rag
curl -fsS http://127.0.0.1:59201/_cat/indices?v
```

Browse buckets and originals in the [S3 console](http://127.0.0.1:59002) using `RAG_S3_ACCESS_KEY` and `RAG_S3_SECRET_KEY` from the local configuration. Default host ports are PostgreSQL `55433`, S3 `59001`, S3 console `59002`, and ES `59201`. Compose `stop` pauses the containers while preserving all data volumes.

## Configure access

The CLI uses the provisioning role. Application keys are issued once at registration. Tenant and library codes use lowercase letters, digits, and hyphens, starting with a letter or digit. `register-app --expires` accepts an ISO 8601 timestamp with a time zone.

```sh
. docs/env/rag/.env
export RAG_ADMIN_DATABASE_URL="postgresql://rag_admin:$RAG_ADMIN_PASSWORD@127.0.0.1:${RAG_POSTGRES_PORT:-55433}/rag"
uv run --locked python -m rag.cli create-tenant alpha Alpha
uv run --locked python -m rag.cli create-library alpha demo 'Alpha demo'
rag_application="$(uv run --locked python -m rag.cli register-app alpha Reader)"
rag_app_id="$(printf '%s' "$rag_application" | uv run --locked python -c 'import json, sys; print(json.load(sys.stdin)["id"])')"
rag_app_key="$(printf '%s' "$rag_application" | uv run --locked python -c 'import json, sys; print(json.load(sys.stdin)["key"])')"
unset rag_application
uv run --locked python -m rag.cli grant alpha "$rag_app_id" demo read
```

## Read a knowledge base

If the API is running through the development launcher, use that session. To run just the API, start it with the restricted role.

```sh
. docs/env/rag/.env
export RAG_ES_URL="http://127.0.0.1:${RAG_ES_PORT:-59201}"
export RAG_S3_ENDPOINT="http://127.0.0.1:${RAG_S3_PORT:-59001}" RAG_S3_ACCESS_KEY RAG_S3_SECRET_KEY
RAG_DATABASE_URL="postgresql://rag_api:$RAG_API_PASSWORD@127.0.0.1:${RAG_POSTGRES_PORT:-55433}/rag" \
  uv run --locked --env-file .env uvicorn rag.api:app --host 127.0.0.1 --port 8001
```

In the first terminal, the request returns `200` with the library `id`, `code`, and `name`. Repeat it after revoking `read` to see `403`.

```sh
curl -sS -i -H "Authorization: Bearer $rag_app_key" http://127.0.0.1:8001/v1/knowledge-bases/demo
uv run --locked python -m rag.cli revoke alpha "$rag_app_id" demo read
curl -sS -i -H "Authorization: Bearer $rag_app_key" http://127.0.0.1:8001/v1/knowledge-bases/demo
```

`read` and `write` are independent grants. Missing, invalid, expired, or disabled keys return `401`. A code absent from the caller's tenant returns `404`.

## Upload and publish

With the API running and the storage variables exported in its terminal, grant the application `write`. Send the original bytes with a `filename` query parameter and an `Idempotency-Key` header. The upload accepts UTF-8 `.md` and `.txt` up to 256 KiB.

```sh
uv run --locked python -m rag.cli grant alpha "$rag_app_id" demo write
rag_submission="$(curl -fsS -X POST \
  -H "Authorization: Bearer $rag_app_key" \
  -H 'Content-Type: text/markdown' -H 'Idempotency-Key: alpha-returns-v1' \
  --data-binary @docs/samples/ecommerce/alpha/returns.md \
  'http://127.0.0.1:8001/v1/knowledge-bases/demo/documents?filename=returns.md')"
rag_job_id="$(printf '%s' "$rag_submission" | uv run --locked python -c 'import json, sys; print(json.load(sys.stdin)["job_id"])')"
```

The API returns `202` with `job_id`, `document_id`, and `version_id`. Repeating the same upload with the same key returns those IDs. A different upload with the same key returns `409`. To update an existing document, pass its `document_id` as another query parameter and use a new key.

Run the worker in a terminal with the storage variables exported and the Bailian key in the repository root `.env`.

```sh
. docs/env/rag/.env
export RAG_WORKER_DATABASE_URL="postgresql://rag_worker:$RAG_WORKER_PASSWORD@127.0.0.1:${RAG_POSTGRES_PORT:-55433}/rag"
uv run --locked --env-file .env python -m rag.worker
```

Check the job from the upload terminal. After publication it has `state=done` and `stage=published`. A temporary model or storage error schedules a retry, up to four total attempts. An expired lease makes the job claimable again.

```sh
curl -fsS -H "Authorization: Bearer $rag_app_key" "http://127.0.0.1:8001/v1/ingestion-jobs/$rag_job_id"
```

With the admin database and storage variables exported, `uv run --locked python -m rag.objects --prune` removes unreferenced originals older than 24 hours.

## Synchronize the keyword index

Start the independent sync worker. It uses PostgreSQL and Elasticsearch. Set `RAG_ES_INDEX` to override the default index `rag-chunks-v1` and `RAG_ES_API_KEY` when the cluster requires API key authentication.

```sh
. docs/env/rag/.env
export RAG_SYNC_DATABASE_URL="postgresql://rag_sync:$RAG_SYNC_PASSWORD@127.0.0.1:${RAG_POSTGRES_PORT:-55433}/rag"
export RAG_ES_URL="http://127.0.0.1:${RAG_ES_PORT:-59201}"
uv run --locked python -m rag.sync
```

Add `--once` to process one available task. Query the ingestion job again to see `keyword_status=synced` after the worker writes the current version, removes older chunks, and refreshes the index. `keyword_attempts` and `keyword_error` describe the sync task for the current revision. Failed Elasticsearch operations retry independently of ingestion, up to four attempts.

## Retrieve evidence and download a source

After the sample is synchronized, grant read access and query the knowledge base. The API accepts `vector`, `keyword`, `rrf`, and `rerank` as `mode`. API and sync worker must use the same `RAG_ES_URL` and `RAG_ES_INDEX`.

```sh
uv run --locked python -m rag.cli grant alpha "$rag_app_id" demo read
curl -fsS http://127.0.0.1:8001/v1/retrieve \
  -H "Authorization: Bearer $rag_app_key" -H 'Content-Type: application/json' \
  -d '{"knowledge_base_code":"demo","question":"RET-ALPHA-07 如何申请退货？","mode":"rerank"}'
```

Use a result's `chunk_id` with `GET /v1/citations/{chunk_id}` for the source lines and its `document_id` with `GET /v1/documents/{document_id}/file` for the current original. Both endpoints require the API key and return `404` when the evidence or file is outside its current read scope.

Use `PATCH /v1/documents/{document_id}/readers` with a write key to change the reader list. Send `{"readers":null}` to inherit library access, `{"readers":[]}` to deny reads, or a list of same-tenant application IDs. `DELETE /v1/documents/{document_id}` makes the document unavailable immediately and queues index cleanup.

Hybrid responses report a failed recall path in `degraded`. A failed reranker returns the pre-reranking order. If PostgreSQL authorization cannot be verified, the service returns `503`.

## Verify Bailian model access

The model client uses `text-embedding-v4` for 512-dimensional document and query vectors and `qwen3-rerank` for reranking. Put `DASHSCOPE_API_KEY` in the local, Git-ignored `.env` file at the repository root. Set `DASHSCOPE_HTTP_BASE_URL` if your key uses a different DashScope region or a workspace endpoint. [Bailian endpoints](https://help.aliyun.com/zh/model-studio/base-url)

Run this check with synthetic text to verify both models.

```sh
uv run --locked --env-file .env python -m rag.models
```
