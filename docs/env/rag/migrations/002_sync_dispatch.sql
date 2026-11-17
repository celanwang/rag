BEGIN;
SET LOCAL search_path TO rag, pg_catalog;
SELECT pg_advisory_xact_lock(hashtext('rag schema migration'));
SELECT obj_description('rag'::regnamespace, 'pg_namespace') = 'rag schema version 1' AS apply \gset
\if :apply
ALTER TABLE jobs ADD COLUMN dispatch_after timestamptz;
GRANT SELECT (dispatch_after), UPDATE (dispatch_after) ON jobs TO rag_queue;

CREATE FUNCTION claim_job(job_kind text, max_attempts integer, lease_seconds integer, requested_job uuid)
RETURNS TABLE (id uuid, tenant uuid, lease uuid)
LANGUAGE sql
SET search_path = pg_catalog, pg_temp
AS $$
    WITH next_job AS (
        SELECT j.id
        FROM rag.jobs AS j
        WHERE j.kind = job_kind AND (requested_job IS NULL OR j.id = requested_job) AND (
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
        dispatch_after = NULL,
        error = CASE WHEN j.attempts >= max_attempts THEN 'lease_expired' END
    FROM next_job
    WHERE j.id = next_job.id
    RETURNING j.id, j.tenant, j.lease
$$;

ALTER FUNCTION claim_job(text, integer, integer, uuid) OWNER TO rag_queue;
REVOKE ALL ON FUNCTION claim_job(text, integer, integer, uuid) FROM PUBLIC;

CREATE OR REPLACE FUNCTION claim_job(job_kind text, max_attempts integer, lease_seconds integer)
RETURNS TABLE (id uuid, tenant uuid, lease uuid)
LANGUAGE sql SET search_path = pg_catalog, pg_temp
AS $$
    SELECT * FROM rag.claim_job(job_kind, max_attempts, lease_seconds, NULL)
$$;

CREATE FUNCTION claim_sync(requested_job uuid, max_attempts integer, lease_seconds integer)
RETURNS TABLE (id uuid, tenant uuid, lease uuid)
LANGUAGE sql SECURITY DEFINER STRICT SET search_path = pg_catalog, pg_temp
AS $$
    SELECT * FROM rag.claim_job('sync', max_attempts, lease_seconds, requested_job)
$$;
ALTER FUNCTION claim_sync(uuid, integer, integer) OWNER TO rag_queue;
REVOKE ALL ON FUNCTION claim_sync(uuid, integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_sync(uuid, integer, integer) TO rag_sync;

CREATE FUNCTION dispatch_sync(batch integer)
RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $$
    WITH due AS (
        SELECT j.id
        FROM rag.jobs AS j
        WHERE j.kind = 'sync' AND (
            (j.state IN ('queued', 'retry') AND j.available <= statement_timestamp())
            OR (j.state = 'running' AND j.deadline <= statement_timestamp())
        )
        AND (j.dispatch_after IS NULL OR j.dispatch_after <= statement_timestamp())
        ORDER BY CASE WHEN j.state = 'running' THEN j.deadline ELSE j.available END, j.id
        LIMIT least(greatest(batch, 1), 100)
        FOR UPDATE SKIP LOCKED
    )
    UPDATE rag.jobs AS j SET dispatch_after = clock_timestamp() + interval '5 seconds'
    FROM due WHERE j.id = due.id
    RETURNING j.id
$$;
ALTER FUNCTION dispatch_sync(integer) OWNER TO rag_queue;
REVOKE ALL ON FUNCTION dispatch_sync(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION dispatch_sync(integer) TO rag_sync;

COMMENT ON SCHEMA rag IS 'rag schema version 2';
\endif
COMMIT;
