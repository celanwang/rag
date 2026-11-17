\set ON_ERROR_STOP on
\getenv worker_password RAG_WORKER_PASSWORD

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE ROLE rag_worker LOGIN PASSWORD :'worker_password' NOSUPERUSER NOBYPASSRLS;
CREATE ROLE rag_queue NOLOGIN NOSUPERUSER NOBYPASSRLS;
GRANT USAGE ON SCHEMA rag TO rag_worker, rag_queue;

-- Track the latest requested version and the version available to readers.
CREATE TABLE rag.documents (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant     uuid NOT NULL,
    library    uuid NOT NULL,
    readers    uuid[],
    target     uuid,
    published  uuid,
    revision   bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    synced     bigint CHECK (synced BETWEEN 0 AND revision),
    deleted    timestamptz,
    UNIQUE (tenant, library, id),
    FOREIGN KEY (tenant, library) REFERENCES rag.libraries (tenant, id)
);

-- Keep immutable original metadata and the configuration for each version.
CREATE TABLE rag.versions (
    id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant    uuid NOT NULL,
    library   uuid NOT NULL,
    document  uuid NOT NULL,
    number    integer NOT NULL CHECK (number > 0),
    filename  text NOT NULL,
    mime      text NOT NULL,
    path      text NOT NULL UNIQUE,
    checksum  bytea NOT NULL CHECK (octet_length(checksum) = 32),
    config    jsonb NOT NULL,
    UNIQUE (document, number),
    UNIQUE (tenant, library, document, id),
    FOREIGN KEY (tenant, library, document) REFERENCES rag.documents (tenant, library, id)
);

ALTER TABLE rag.documents
    ADD FOREIGN KEY (tenant, library, id, target)
        REFERENCES rag.versions (tenant, library, document, id),
    ADD FOREIGN KEY (tenant, library, id, published)
        REFERENCES rag.versions (tenant, library, document, id);

-- Store prepared text, original line ranges, and document embeddings.
CREATE TABLE rag.chunks (
    id         uuid PRIMARY KEY,
    tenant     uuid NOT NULL,
    library    uuid NOT NULL,
    document   uuid NOT NULL,
    version    uuid NOT NULL,
    ordinal    integer NOT NULL CHECK (ordinal >= 0),
    heading    text,
    body       text NOT NULL,
    location   jsonb NOT NULL,
    embedding  vector(512) NOT NULL,
    UNIQUE (version, ordinal),
    FOREIGN KEY (tenant, library, document, version)
        REFERENCES rag.versions (tenant, library, document, id)
);

-- Persist ingest requests, worker leases, and pending index changes.
CREATE TABLE rag.jobs (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant       uuid NOT NULL,
    library      uuid NOT NULL,
    document     uuid NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('ingest', 'sync')),
    app          uuid,
    version      uuid,
    revision     bigint CHECK (revision > 0),
    request      text,
    fingerprint  bytea,
    state        text NOT NULL DEFAULT 'queued'
                     CHECK (state IN ('queued', 'running', 'retry', 'done', 'failed')),
    stage        text,
    attempts     integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available    timestamptz NOT NULL DEFAULT now(),
    lease        uuid,
    deadline     timestamptz,
    error        text,
    UNIQUE (tenant, library, app, request),
    UNIQUE (document, revision),
    FOREIGN KEY (tenant, app) REFERENCES rag.apps (tenant, id),
    FOREIGN KEY (tenant, library, document) REFERENCES rag.documents (tenant, library, id),
    FOREIGN KEY (tenant, library, document, version)
        REFERENCES rag.versions (tenant, library, document, id),
    CHECK ((request IS NULL) = (fingerprint IS NULL)),
    CHECK ((lease IS NULL) = (deadline IS NULL)),
    CHECK ((state = 'running') = (lease IS NOT NULL)),
    CHECK (
        (kind = 'ingest' AND app IS NOT NULL AND version IS NOT NULL AND revision IS NULL)
        OR (kind = 'sync' AND app IS NULL AND version IS NULL AND revision IS NOT NULL AND request IS NULL)
    )
);

CREATE INDEX ON rag.jobs (kind, available) WHERE state IN ('queued', 'retry');
CREATE INDEX ON rag.jobs (kind, deadline) WHERE state = 'running';

CREATE FUNCTION rag.current_tenant() RETURNS uuid
LANGUAGE sql STABLE
RETURN NULLIF(current_setting('rag.tenant_id', true), '')::uuid;

ALTER TABLE rag.documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE rag.documents FORCE ROW LEVEL SECURITY;
ALTER TABLE rag.versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE rag.versions FORCE ROW LEVEL SECURITY;
ALTER TABLE rag.chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE rag.chunks FORCE ROW LEVEL SECURITY;
ALTER TABLE rag.jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE rag.jobs FORCE ROW LEVEL SECURITY;

GRANT SELECT, INSERT, UPDATE (target) ON rag.documents TO rag_api;
GRANT SELECT, INSERT ON rag.versions, rag.jobs TO rag_api;
GRANT SELECT (path) ON rag.versions TO rag_admin;
GRANT SELECT, UPDATE (published, revision) ON rag.documents TO rag_worker;
GRANT SELECT ON rag.versions TO rag_worker;
GRANT INSERT ON rag.chunks TO rag_worker;
GRANT SELECT, INSERT, UPDATE (state, stage, available, lease, deadline, error) ON rag.jobs TO rag_worker;

CREATE POLICY document_scope ON rag.documents TO rag_api, rag_worker
    USING (tenant = rag.current_tenant()) WITH CHECK (tenant = rag.current_tenant());
CREATE POLICY version_scope ON rag.versions TO rag_api, rag_worker
    USING (tenant = rag.current_tenant()) WITH CHECK (tenant = rag.current_tenant());
CREATE POLICY version_paths ON rag.versions FOR SELECT TO rag_admin USING (true);
CREATE POLICY chunk_scope ON rag.chunks FOR INSERT TO rag_worker
    WITH CHECK (tenant = rag.current_tenant());
CREATE POLICY api_jobs ON rag.jobs TO rag_api
    USING (tenant = rag.current_tenant() AND kind = 'ingest')
    WITH CHECK (tenant = rag.current_tenant() AND kind = 'ingest');
CREATE POLICY worker_read_jobs ON rag.jobs FOR SELECT TO rag_worker
    USING (tenant = rag.current_tenant() AND kind = 'ingest');
CREATE POLICY worker_update_jobs ON rag.jobs FOR UPDATE TO rag_worker
    USING (tenant = rag.current_tenant() AND kind = 'ingest')
    WITH CHECK (tenant = rag.current_tenant() AND kind = 'ingest');
CREATE POLICY worker_enqueue_sync ON rag.jobs FOR INSERT TO rag_worker
    WITH CHECK (tenant = rag.current_tenant() AND kind = 'sync');

-- Claim tasks across tenants without exposing documents or job payloads.
GRANT SELECT (id, tenant, kind, state, stage, attempts, available, lease, deadline),
    UPDATE (state, stage, attempts, lease, deadline, error) ON rag.jobs TO rag_queue;
CREATE POLICY queue_ingest ON rag.jobs TO rag_queue
    USING (kind = 'ingest') WITH CHECK (kind = 'ingest');

CREATE FUNCTION rag.claim_ingest(max_attempts integer, lease_seconds integer)
RETURNS TABLE (id uuid, tenant uuid, lease uuid)
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    WITH next_job AS (
        SELECT j.id
        FROM rag.jobs AS j
        WHERE j.kind = 'ingest' AND (
            (j.state IN ('queued', 'retry') AND j.available <= statement_timestamp())
            OR (j.state = 'running' AND j.deadline <= statement_timestamp())
        )
        ORDER BY CASE WHEN j.state = 'running' THEN j.deadline ELSE j.available END, j.id
        LIMIT 1 FOR UPDATE SKIP LOCKED
    ), claimed AS (
        UPDATE rag.jobs AS j
        SET state = CASE WHEN j.attempts < max_attempts THEN 'running' ELSE 'failed' END,
            stage = CASE WHEN j.attempts < max_attempts THEN 'fetch' ELSE j.stage END,
            attempts = LEAST(j.attempts + 1, max_attempts),
            lease = CASE WHEN j.attempts < max_attempts THEN gen_random_uuid() END,
            deadline = CASE WHEN j.attempts < max_attempts
                            THEN clock_timestamp() + lease_seconds * interval '1 second' END,
            error = CASE WHEN j.attempts >= max_attempts THEN 'lease_expired' END
        FROM next_job
        WHERE j.id = next_job.id
        RETURNING j.id, j.tenant, j.lease
    )
    SELECT * FROM claimed
$$;

ALTER FUNCTION rag.claim_ingest(integer, integer) OWNER TO rag_queue;
REVOKE ALL ON FUNCTION rag.claim_ingest(integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION rag.claim_ingest(integer, integer) TO rag_worker;

COMMIT;
