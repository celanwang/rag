\set ON_ERROR_STOP on

BEGIN;
SET LOCAL search_path TO rag, pg_temp;

-- A sync job can carry the app and request that created the document revision.
ALTER TABLE jobs ADD COLUMN trace uuid;
ALTER TABLE jobs DROP CONSTRAINT jobs_source;
ALTER TABLE jobs ADD CONSTRAINT jobs_source CHECK (
    (kind = 'ingest' AND app IS NOT NULL AND revision IS NULL AND (
        (version IS NOT NULL AND purged IS NULL)
        OR (version IS NULL AND purged IS NOT NULL AND state IN ('done', 'failed'))
    ))
    OR (kind = 'sync' AND version IS NULL AND purged IS NULL AND revision IS NOT NULL AND request IS NULL)
);

COMMIT;
