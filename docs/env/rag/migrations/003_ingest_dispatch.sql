BEGIN;
SET LOCAL search_path TO rag, pg_catalog;
SELECT pg_advisory_xact_lock(hashtext('rag schema migration'));
SELECT obj_description('rag'::regnamespace, 'pg_namespace') = 'rag schema version 2' AS apply \gset
\if :apply
CREATE FUNCTION reserve_dispatch(job_kind text, batch integer)
RETURNS SETOF uuid
LANGUAGE sql SET search_path = pg_catalog, pg_temp
AS $$
    WITH due AS (
        SELECT j.id
        FROM rag.jobs AS j
        WHERE j.kind = job_kind AND (
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
ALTER FUNCTION reserve_dispatch(text, integer) OWNER TO rag_queue;
REVOKE ALL ON FUNCTION reserve_dispatch(text, integer) FROM PUBLIC;

CREATE OR REPLACE FUNCTION dispatch_sync(batch integer)
RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $$ SELECT * FROM rag.reserve_dispatch('sync', batch) $$;

CREATE FUNCTION dispatch_ingest(batch integer)
RETURNS SETOF uuid
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $$ SELECT * FROM rag.reserve_dispatch('ingest', batch) $$;
ALTER FUNCTION dispatch_ingest(integer) OWNER TO rag_queue;
REVOKE ALL ON FUNCTION dispatch_ingest(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION dispatch_ingest(integer) TO rag_worker;

CREATE FUNCTION claim_ingest(requested_job uuid, max_attempts integer, lease_seconds integer)
RETURNS TABLE (id uuid, tenant uuid, lease uuid)
LANGUAGE sql SECURITY DEFINER STRICT SET search_path = pg_catalog, pg_temp
AS $$
    SELECT * FROM rag.claim_job('ingest', max_attempts, lease_seconds, requested_job)
$$;
ALTER FUNCTION claim_ingest(uuid, integer, integer) OWNER TO rag_queue;
REVOKE ALL ON FUNCTION claim_ingest(uuid, integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_ingest(uuid, integer, integer) TO rag_worker;

COMMENT ON SCHEMA rag IS 'rag schema version 3';
\endif
COMMIT;
