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

Run `serve` in a separate terminal to keep the API and both workers running. The demo creates two sample tenants in the main `rag` database and saves their read and write application keys in `.rag/dev/<run>/readers.json`. The local workbench loads the latest successful demo. For direct API calls, use the `key` field to read the tenant's `shop` knowledge base or `writer_key` for writes. [Local workbench](../frontend/README.md) · [API documentation](http://127.0.0.1:8001/docs)

`up` reuses the PostgreSQL and Elasticsearch volumes. An empty database initializes from `schema.sql`. The launcher applies ordered migrations from the version recorded in the `rag` schema comment. Local credentials are stored in `docs/env/rag/.env`. [Compose config](../docs/env/rag/compose.yaml) · [Database design](../docs/plan/schema.md)

```sh
uv run --locked python -m rag.dev status
uv run --locked python -m rag.dev test
uv run --locked --env-file .env python -m rag.dev benchmark --repeat 3
uv run --locked --env-file .env python -m rag.dev benchmark --bad-cases --repeat 3
```

Each integration or benchmark run initializes its own database from `schema.sql` and uses its own Elasticsearch index in the same `rag-dev` services. This keeps queue claims and keyword index statistics local to that run. Resources and logs are retained. Find the database, index, and outcome in `.rag/dev/<run>/run.json`. The benchmark also saves `benchmark.json`, including per-question ranks and timings. [Measured results](../docs/other/retrieval-results.md)

The default benchmark uses the original 30 questions and their original corpus. `--bad-cases` runs 19 labelled cases with four additional policy fixtures. Each `[filename, text fragment]` gold anchor identifies a single range in the original file. Cases can pin document keys, SHA-256 and decoded character offsets using `gold_spans`. The benchmark validates those fingerprints and scores the union of returned ranges. `avoid_top1` marks misleading chunks. [Original questions](../poc/questions.json) · [Bad cases](benchmark_cases/questions.json)

The report lists gold chunks absent from all active candidates in `missing_from_candidates` and recalled gold chunks outside the prefinal cutoff in `outside_prefinal_top_k`. It also reports actual rerank inputs, first sources, full evidence coverage and model calls. `--paired` runs five parameter profiles on a shared index within each suite. `--replay` verifies recorded fusion and recomputes RRF from preserved branch rankings. Its reranking results come from the recorded API calls. `--evidence` exercises the explicit policy dates, conflicts and clarification cases and fails the run if an expected behavior fails.

```sh
uv run --locked python -m rag.benchmark --replay .rag/dev/<run>/benchmark.json --rrf-k 1 60 --out .rag/replay.json
uv run --locked --env-file .env python -m rag.dev benchmark --paired --repeat 2
uv run --locked --env-file .env python -m rag.dev benchmark --paired --bad-cases --repeat 2
uv run --locked --env-file .env python -m rag.dev benchmark --evidence --repeat 2
```

`status` lists published sample documents and the resources created by each run. Inspect other databases by replacing `rag` in the PostgreSQL command with the recorded database name.

```sh
docker compose -p rag-dev --env-file docs/env/rag/.env -f docs/env/rag/compose.yaml \
  exec postgres psql -U postgres -d rag
curl -fsS http://127.0.0.1:59201/_cat/indices?v
```

Default host ports are PostgreSQL `55433` and Elasticsearch `59201`. Compose `stop` pauses both containers and retains their data volumes.

## Apply version retention

Use a tenant code from `status`. The first command previews the eligible versions and chunks. Use `--apply` to delete a batch. Run another batch when there are more eligible versions. The defaults are 30 days and at most 100 versions per run. The command uses the local PostgreSQL owner connection.

```sh
uv run --locked python -m rag.dev retain alpha
uv run --locked python -m rag.dev retain alpha --days 30 --limit 100 --apply
```

An inactive version ages from the moment the last target or published pointer to it moves. Deleted documents age from deletion and require Elasticsearch to confirm the deletion revision. Nonterminal ingestion jobs keep their originals. Cleanup removes the original, chunks, and version from the live database, while upload receipts and idempotency keys remain available. Backup and WAL expiration follow the database backup policy.

## Trace requests and audit changes

Use the `X-Request-ID` response header to find JSON events in `.rag/dev/serve.log`. Uploads carry the same `trace_id` through ingestion and keyword indexing. Reader changes and document deletion carry their own request ID through the resulting index update. The event records include the application, tenant, affected resource, and outcome. A cleanup batch returns a trace ID and writes one `document.purge` event per deleted version to stderr.

The provisioning CLI writes grant, revocation, and disable events as JSON to stderr and keeps command results on stdout. Capture stderr to retain CLI audit events when running commands manually. Demo commands and services write to their run's `services.log`.

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
RAG_DATABASE_URL="postgresql://rag_api:$RAG_API_PASSWORD@127.0.0.1:${RAG_POSTGRES_PORT:-55433}/rag" \
  uv run --locked --env-file .env uvicorn rag.api:app --host 127.0.0.1 --port 8001 --no-access-log
```

In the first terminal, the request returns `200` with the library `id`, `code`, and `name`. Repeat it after revoking `read` to see `403`.

```sh
curl -sS -i -H "Authorization: Bearer $rag_app_key" http://127.0.0.1:8001/v1/knowledge-bases/demo
uv run --locked python -m rag.cli revoke alpha "$rag_app_id" demo read
curl -sS -i -H "Authorization: Bearer $rag_app_key" http://127.0.0.1:8001/v1/knowledge-bases/demo
```

`read` and `write` are independent grants. Missing, invalid, expired, or disabled keys return `401`. A code absent from the caller's tenant returns `404`.

## Upload and publish

With the API running, grant the application `write`. Send the original bytes with a `filename` query parameter and an `Idempotency-Key` header. The upload accepts UTF-8 `.md` and `.txt` up to 256 KiB.

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

Run the worker with the Bailian key in the repository root `.env`.

```sh
. docs/env/rag/.env
export RAG_WORKER_DATABASE_URL="postgresql://rag_worker:$RAG_WORKER_PASSWORD@127.0.0.1:${RAG_POSTGRES_PORT:-55433}/rag"
uv run --locked --env-file .env python -m rag.worker
```

Check the job from the upload terminal. After publication it has `state=done` and `stage=published`. A temporary model error schedules a retry, up to four total attempts. An expired lease makes the job claimable again.

```sh
curl -fsS -H "Authorization: Bearer $rag_app_key" "http://127.0.0.1:8001/v1/ingestion-jobs/$rag_job_id"
```

## Synchronize the keyword index

Start the independent sync worker. It uses PostgreSQL and Elasticsearch. Set `RAG_ES_INDEX` to override the default index `rag-chunks-v1` and `RAG_ES_API_KEY` when the cluster requires API key authentication.

```sh
. docs/env/rag/.env
export RAG_SYNC_DATABASE_URL="postgresql://rag_sync:$RAG_SYNC_PASSWORD@127.0.0.1:${RAG_POSTGRES_PORT:-55433}/rag"
export RAG_ES_URL="http://127.0.0.1:${RAG_ES_PORT:-59201}"
uv run --locked python -m rag.sync
```

Add `--once` to process one available task. Query the ingestion job again to see `keyword_status=synced` after the worker writes the current version, removes older chunks, and refreshes the index. `keyword_attempts` and `keyword_error` describe the sync task for the current revision. Failed Elasticsearch operations retry independently of ingestion, up to four attempts.

Run a document repair with the sync database role and the API's configured index. The repair reads the current revision and ACL under the document lock and refreshes Elasticsearch even when the recorded revision already matches.

```sh
uv run --locked python -m rag.sync --repair "$rag_tenant_id" "$rag_document_id"
```

### Select a worker executor

The default executor for each job type is `pg`. Choose `celery` for either queue to start the Redis broker, the dispatcher and its dedicated worker. PostgreSQL leases, attempts and job status remain the recovery record. The dispatcher reserves a short delivery window before publishing and can reissue a due task when the window expires. Each delivery runs through the exact-ID claim for its job type.

```sh
RAG_SYNC_EXECUTOR=celery uv run --locked python -m rag.dev up
RAG_SYNC_EXECUTOR=celery RAG_INGEST_EXECUTOR=celery \
  uv run --locked --env-file .env python -m rag.dev serve
uv run --locked --env-file .env python -m rag.dev sync-drill
```

To roll back, stop the service launcher and restart it with `RAG_SYNC_EXECUTOR=pg` or `RAG_INGEST_EXECUTOR=pg` for the queue being switched. Pending and expired jobs are claimed from the same PostgreSQL tables. Each isolated drill uses its own database, index, Redis container and queue names.

### Rebuild and activate a keyword index

Set `RAG_INDEX_ADMIN_DATABASE_URL` to the PostgreSQL maintenance connection. Choose an alias that the API and sync worker will use and a distinct concrete name for each generation. Build in batches until the status is `ready`. The job checkpoints each document revision. Activation checks current publication under a PostgreSQL lock, verifies the indexed chunk count and swaps the alias.

```sh
export RAG_ES_INDEX=rag-search
uv run --locked python -m rag.rebuild build rag-search rag-search-g1 --batch 25 --interval-ms 50
uv run --locked python -m rag.rebuild activate rag-search rag-search-g1
```

For the first activation, create the alias before starting the API and Worker with `RAG_ES_INDEX=rag-search RAG_ES_REQUIRE_ALIAS=1`. If documents changed after the build reached ready, run the build again before retrying activation. To roll back, build the archived generation until it catches up, then activate it with the same command.

## Retrieve evidence and download a source

Set `expand_neighbors: true` to request bounded supplemental evidence from the same document version and section. New uploads record section positions. Reingest an older version to enable expansion for it. Set `as_of` to an ISO calendar date to select applicable policy evidence, and `interpret: true` to request identifier and scope clarification hints. The API returns the candidates for a policy conflict and leaves the original query intact. Policy authors can annotate Markdown sections with a first-line `<!-- rag-policy {…} -->` object keyed by the full heading path. [Policy fixture](benchmark_cases/corpus/alpha/returns-current.md)

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
