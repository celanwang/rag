\set ON_ERROR_STOP on
\getenv sync_password RAG_SYNC_PASSWORD

BEGIN;
SET LOCAL search_path TO rag, pg_temp;

CREATE ROLE rag_sync LOGIN PASSWORD :'sync_password' NOSUPERUSER NOBYPASSRLS;
GRANT USAGE ON SCHEMA rag TO rag_sync;
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

CREATE OR REPLACE FUNCTION claim_ingest(max_attempts integer, lease_seconds integer)
RETURNS TABLE (id uuid, tenant uuid, lease uuid)
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT * FROM rag.claim_job('ingest', max_attempts, lease_seconds)
$$;

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

COMMIT;
