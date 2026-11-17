\set ON_ERROR_STOP on

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

-- Isolated dev databases reuse the roles of the rag-dev PostgreSQL cluster.
\if :{?reuse_roles}
\else
\getenv api_password RAG_API_PASSWORD
\getenv admin_password RAG_ADMIN_PASSWORD
\getenv worker_password RAG_WORKER_PASSWORD
\getenv sync_password RAG_SYNC_PASSWORD

CREATE ROLE rag_api LOGIN PASSWORD :'api_password' NOSUPERUSER NOBYPASSRLS;
CREATE ROLE rag_admin LOGIN PASSWORD :'admin_password' NOSUPERUSER NOBYPASSRLS;
CREATE ROLE rag_worker LOGIN PASSWORD :'worker_password' NOSUPERUSER NOBYPASSRLS;
CREATE ROLE rag_sync LOGIN PASSWORD :'sync_password' NOSUPERUSER NOBYPASSRLS;
CREATE ROLE rag_auth NOLOGIN NOSUPERUSER NOBYPASSRLS;
CREATE ROLE rag_queue NOLOGIN NOSUPERUSER NOBYPASSRLS;
\endif

CREATE SCHEMA rag;
SET LOCAL search_path TO rag, pg_temp;
GRANT USAGE ON SCHEMA rag TO rag_api, rag_admin, rag_auth, rag_worker, rag_queue, rag_sync;

-- Tables, constraints, and indexes.
-- Store tenant codes and activation status.
CREATE TABLE tenants (
    id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code    text NOT NULL UNIQUE CHECK (code ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    name    text NOT NULL,
    active  boolean NOT NULL DEFAULT true
);

-- Bind an application and its API key digest to a tenant.
CREATE TABLE apps (
    id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant   uuid NOT NULL REFERENCES tenants (id),
    name     text NOT NULL,
    digest   bytea NOT NULL UNIQUE CHECK (octet_length(digest) = 32),
    expires  timestamptz,
    active   boolean NOT NULL DEFAULT true,
    UNIQUE (tenant, id)
);

-- Store each tenant's knowledge bases.
CREATE TABLE libraries (
    id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant    uuid NOT NULL REFERENCES tenants (id),
    code      text NOT NULL CHECK (code ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    name      text NOT NULL,
    settings  jsonb NOT NULL DEFAULT '{}',
    UNIQUE (tenant, code),
    UNIQUE (tenant, id)
);

-- Store independent read and write grants within the same tenant.
CREATE TABLE grants (
    tenant   uuid NOT NULL,
    app      uuid NOT NULL,
    library  uuid NOT NULL,
    action   text NOT NULL CHECK (action IN ('read', 'write')),
    PRIMARY KEY (tenant, app, library, action),
    FOREIGN KEY (tenant, app) REFERENCES apps (tenant, id),
    FOREIGN KEY (tenant, library) REFERENCES libraries (tenant, id)
);

-- Track the latest requested version and the version available to readers.
CREATE TABLE documents (
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
    FOREIGN KEY (tenant, library) REFERENCES libraries (tenant, id)
);

-- Keep immutable original metadata and the configuration for each version.
CREATE TABLE versions (
    id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant    uuid NOT NULL,
    library   uuid NOT NULL,
    document  uuid NOT NULL,
    number    integer NOT NULL CHECK (number > 0),
    filename  text NOT NULL,
    mime      text NOT NULL,
    content   bytea NOT NULL CHECK (octet_length(content) BETWEEN 1 AND 262144),
    checksum  bytea NOT NULL CHECK (octet_length(checksum) = 32),
    config    jsonb NOT NULL,
    retired   timestamptz,
    UNIQUE (document, number),
    UNIQUE (tenant, library, document, id),
    FOREIGN KEY (tenant, library, document) REFERENCES documents (tenant, library, id)
);

ALTER TABLE documents
    ADD FOREIGN KEY (tenant, library, id, target)
        REFERENCES versions (tenant, library, document, id),
    ADD FOREIGN KEY (tenant, library, id, published)
        REFERENCES versions (tenant, library, document, id);

-- Store prepared text, original line ranges, and document embeddings.
CREATE TABLE chunks (
    id         uuid PRIMARY KEY,
    tenant     uuid NOT NULL,
    library    uuid NOT NULL,
    document   uuid NOT NULL,
    version    uuid NOT NULL,
    ordinal    integer NOT NULL CHECK (ordinal >= 0),
    heading    text,
    body       text NOT NULL,
    location   jsonb NOT NULL,
    embedding  public.vector(512) NOT NULL,
    UNIQUE (version, ordinal),
    FOREIGN KEY (tenant, library, document, version)
        REFERENCES versions (tenant, library, document, id)
);

-- Persist ingest requests, worker leases, and pending index changes.
CREATE TABLE jobs (
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
    purged       uuid,
    trace        uuid,
    UNIQUE (tenant, library, app, request),
    UNIQUE (document, revision),
    FOREIGN KEY (tenant, app) REFERENCES apps (tenant, id),
    FOREIGN KEY (tenant, library, document) REFERENCES documents (tenant, library, id),
    FOREIGN KEY (tenant, library, document, version)
        REFERENCES versions (tenant, library, document, id),
    CHECK ((request IS NULL) = (fingerprint IS NULL)),
    CHECK ((lease IS NULL) = (deadline IS NULL)),
    CHECK ((state = 'running') = (lease IS NOT NULL)),
    CONSTRAINT jobs_source CHECK (
        (kind = 'ingest' AND app IS NOT NULL AND revision IS NULL AND (
            (version IS NOT NULL AND purged IS NULL)
            OR (version IS NULL AND purged IS NOT NULL AND state IN ('done', 'failed'))
        ))
        OR (kind = 'sync' AND version IS NULL AND purged IS NULL AND revision IS NOT NULL AND request IS NULL)
    )
);

CREATE INDEX ON jobs (kind, available) WHERE state IN ('queued', 'retry');
CREATE INDEX ON jobs (kind, deadline) WHERE state = 'running';
CREATE INDEX jobs_version_idx ON jobs (version) WHERE version IS NOT NULL;

-- Identity resolution, job claims, and version retirement.
CREATE FUNCTION current_tenant() RETURNS uuid
LANGUAGE sql STABLE
RETURN NULLIF(current_setting('rag.tenant_id', true), '')::uuid;

CREATE FUNCTION resolve_key(key_digest bytea)
RETURNS TABLE (app uuid, tenant uuid)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT a.id, a.tenant
    FROM rag.apps AS a
    JOIN rag.tenants AS t ON t.id = a.tenant
    WHERE a.digest = key_digest
      AND a.active AND t.active
      AND (a.expires IS NULL OR a.expires > statement_timestamp())
$$;

ALTER FUNCTION resolve_key(bytea) OWNER TO rag_auth;
REVOKE ALL ON FUNCTION resolve_key(bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_key(bytea) TO rag_api;

-- Only the two role-specific entry points can call the shared scheduler.
CREATE FUNCTION claim_job(job_kind text, max_attempts integer, lease_seconds integer)
RETURNS TABLE (id uuid, tenant uuid, lease uuid)
LANGUAGE sql
SET search_path = pg_catalog, pg_temp
AS $$
    WITH next_job AS (
        SELECT j.id
        FROM rag.jobs AS j
        WHERE j.kind = job_kind AND (
            (j.state IN ('queued', 'retry') AND j.available <= statement_timestamp())
            OR (j.state = 'running' AND j.deadline <= statement_timestamp())
        )
        ORDER BY CASE WHEN j.state = 'running' THEN j.deadline ELSE j.available END, j.id
        LIMIT 1 FOR UPDATE SKIP LOCKED
    )
    UPDATE rag.jobs AS j
    SET state = CASE WHEN j.attempts < max_attempts THEN 'running' ELSE 'failed' END,
        stage = CASE WHEN j.attempts >= max_attempts THEN j.stage
                     WHEN job_kind = 'sync' THEN 'index' ELSE 'fetch' END,
        attempts = LEAST(j.attempts + 1, max_attempts),
        lease = CASE WHEN j.attempts < max_attempts THEN gen_random_uuid() END,
        deadline = CASE WHEN j.attempts < max_attempts
                        THEN clock_timestamp() + lease_seconds * interval '1 second' END,
        error = CASE WHEN j.attempts >= max_attempts THEN 'lease_expired' END
    FROM next_job
    WHERE j.id = next_job.id
    RETURNING j.id, j.tenant, j.lease
$$;

ALTER FUNCTION claim_job(text, integer, integer) OWNER TO rag_queue;
REVOKE ALL ON FUNCTION claim_job(text, integer, integer) FROM PUBLIC;

CREATE FUNCTION claim_ingest(max_attempts integer, lease_seconds integer)
RETURNS TABLE (id uuid, tenant uuid, lease uuid)
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT * FROM rag.claim_job('ingest', max_attempts, lease_seconds)
$$;

ALTER FUNCTION claim_ingest(integer, integer) OWNER TO rag_queue;
REVOKE ALL ON FUNCTION claim_ingest(integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_ingest(integer, integer) TO rag_worker;

CREATE FUNCTION claim_sync(max_attempts integer, lease_seconds integer)
RETURNS TABLE (id uuid, tenant uuid, lease uuid)
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT * FROM rag.claim_job('sync', max_attempts, lease_seconds)
$$;

ALTER FUNCTION claim_sync(integer, integer) OWNER TO rag_queue;
REVOKE ALL ON FUNCTION claim_sync(integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_sync(integer, integer) TO rag_sync;

CREATE FUNCTION retire_versions() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    UPDATE rag.versions SET retired = clock_timestamp()
    WHERE (tenant, library, document) = (NEW.tenant, NEW.library, NEW.id)
      AND id IN (OLD.target, OLD.published)
      AND id IS DISTINCT FROM NEW.target AND id IS DISTINCT FROM NEW.published;
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION retire_versions() FROM PUBLIC;
CREATE TRIGGER retire_versions_after_update
AFTER UPDATE OF target, published ON documents
FOR EACH ROW WHEN (NEW.deleted IS NULL AND
    (OLD.target IS DISTINCT FROM NEW.target OR OLD.published IS DISTINCT FROM NEW.published))
EXECUTE FUNCTION retire_versions();

-- Row-level security and role privileges.
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
ALTER TABLE apps ENABLE ROW LEVEL SECURITY;
ALTER TABLE apps FORCE ROW LEVEL SECURITY;
ALTER TABLE libraries ENABLE ROW LEVEL SECURITY;
ALTER TABLE libraries FORCE ROW LEVEL SECURITY;
ALTER TABLE grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE grants FORCE ROW LEVEL SECURITY;
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;
ALTER TABLE versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE versions FORCE ROW LEVEL SECURITY;
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks FORCE ROW LEVEL SECURITY;
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs FORCE ROW LEVEL SECURITY;

-- The CLI provisions tenants and grants with a separate database role.
GRANT SELECT (id, code), INSERT, UPDATE (active) ON tenants TO rag_admin;
GRANT SELECT (id, tenant), INSERT, UPDATE (active) ON apps TO rag_admin;
GRANT SELECT (id, tenant, code), INSERT ON libraries TO rag_admin;
GRANT SELECT, INSERT, DELETE ON grants TO rag_admin;
CREATE POLICY tenant_admin ON tenants TO rag_admin USING (true) WITH CHECK (true);
CREATE POLICY app_admin ON apps TO rag_admin USING (true) WITH CHECK (true);
CREATE POLICY library_admin ON libraries TO rag_admin USING (true) WITH CHECK (true);
CREATE POLICY grant_admin ON grants TO rag_admin USING (true) WITH CHECK (true);

GRANT SELECT (id, tenant, code, name) ON libraries TO rag_api;
GRANT SELECT ON grants TO rag_api;
CREATE POLICY library_scope ON libraries FOR SELECT TO rag_api
    USING (tenant = NULLIF(current_setting('rag.tenant_id', true), '')::uuid);
CREATE POLICY grant_scope ON grants FOR SELECT TO rag_api
    USING (tenant = NULLIF(current_setting('rag.tenant_id', true), '')::uuid);

-- Resolve a key before a tenant is known. Only the function owner can read digests.
GRANT SELECT (id, active) ON tenants TO rag_auth;
GRANT SELECT (id, tenant, digest, active, expires) ON apps TO rag_auth;
CREATE POLICY tenant_auth ON tenants FOR SELECT TO rag_auth USING (true);
CREATE POLICY app_auth ON apps FOR SELECT TO rag_auth USING (true);

GRANT SELECT, INSERT, UPDATE (target, readers, revision, deleted) ON documents TO rag_api;
GRANT SELECT, INSERT ON versions, jobs TO rag_api;
GRANT SELECT, UPDATE (published, revision) ON documents TO rag_worker;
GRANT SELECT ON versions TO rag_worker;
GRANT INSERT ON chunks TO rag_worker;
GRANT SELECT, INSERT, UPDATE (state, stage, available, lease, deadline, error) ON jobs TO rag_worker;

CREATE POLICY document_scope ON documents TO rag_api, rag_worker
    USING (tenant = current_tenant()) WITH CHECK (tenant = current_tenant());
CREATE POLICY version_scope ON versions TO rag_api, rag_worker
    USING (tenant = current_tenant()) WITH CHECK (tenant = current_tenant());
CREATE POLICY chunk_scope ON chunks FOR INSERT TO rag_worker
    WITH CHECK (tenant = current_tenant());
CREATE POLICY api_jobs ON jobs TO rag_api
    USING (tenant = current_tenant() AND kind = 'ingest')
    WITH CHECK (tenant = current_tenant() AND kind = 'ingest');
CREATE POLICY worker_read_jobs ON jobs FOR SELECT TO rag_worker
    USING (tenant = current_tenant() AND kind = 'ingest');
CREATE POLICY worker_update_jobs ON jobs FOR UPDATE TO rag_worker
    USING (tenant = current_tenant() AND kind = 'ingest')
    WITH CHECK (tenant = current_tenant() AND kind = 'ingest');
CREATE POLICY worker_enqueue_sync ON jobs FOR INSERT TO rag_worker
    WITH CHECK (tenant = current_tenant() AND kind = 'sync');

-- Claim tasks across tenants without exposing documents or job payloads.
GRANT SELECT (id, tenant, kind, state, stage, attempts, available, lease, deadline),
    UPDATE (state, stage, attempts, lease, deadline, error) ON jobs TO rag_queue;
CREATE POLICY queue_ingest ON jobs TO rag_queue
    USING (kind = 'ingest') WITH CHECK (kind = 'ingest');

GRANT SELECT, UPDATE (synced) ON documents TO rag_sync;
GRANT SELECT (tenant, library, document, id, filename) ON versions TO rag_sync;
GRANT SELECT (id, tenant, library, document, version, ordinal, heading, body) ON chunks TO rag_sync;
GRANT SELECT, UPDATE (state, stage, available, lease, deadline, error) ON jobs TO rag_sync;

CREATE POLICY sync_documents ON documents TO rag_sync
    USING (tenant = current_tenant()) WITH CHECK (tenant = current_tenant());
CREATE POLICY sync_versions ON versions FOR SELECT TO rag_sync USING (tenant = current_tenant());
CREATE POLICY sync_chunks ON chunks FOR SELECT TO rag_sync USING (tenant = current_tenant());
CREATE POLICY sync_jobs ON jobs TO rag_sync
    USING (tenant = current_tenant() AND kind = 'sync')
    WITH CHECK (tenant = current_tenant() AND kind = 'sync');
CREATE POLICY api_sync_status ON jobs FOR SELECT TO rag_api
    USING (tenant = current_tenant() AND kind = 'sync');
CREATE POLICY queue_sync ON jobs TO rag_queue
    USING (kind = 'sync') WITH CHECK (kind = 'sync');

GRANT SELECT ON chunks TO rag_api;
GRANT SELECT (id, tenant) ON apps TO rag_api;

CREATE POLICY api_read_chunks ON chunks FOR SELECT TO rag_api USING (tenant = current_tenant());
CREATE POLICY api_read_app_ids ON apps FOR SELECT TO rag_api USING (tenant = current_tenant());
CREATE POLICY api_enqueue_sync ON jobs FOR INSERT TO rag_api
    WITH CHECK (tenant = current_tenant() AND kind = 'sync');

COMMENT ON SCHEMA rag IS 'rag schema version 1';

COMMIT;
