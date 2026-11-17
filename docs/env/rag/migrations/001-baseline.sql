\set ON_ERROR_STOP on

BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('rag.schema', 0));
SET LOCAL search_path TO rag, pg_temp;

SELECT obj_description('rag'::regnamespace, 'pg_namespace') IS NULL AS legacy
\gset
\if :legacy
DO $$
BEGIN
    IF to_regprocedure('rag.claim_sync(integer,integer)') IS NULL OR NOT EXISTS (
        SELECT 1 FROM pg_policy WHERE polrelid = to_regclass('rag.chunks') AND polname = 'api_read_chunks'
    ) THEN
        RAISE EXCEPTION 'Complete the legacy retrieval schema before upgrading to schema version 1.';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = 'rag.versions'::regclass AND attname = 'content' AND NOT attisdropped
    ) THEN
        RAISE EXCEPTION 'Run the original-file migration in the runbook before upgrading the schema.';
    END IF;
END
$$;

SELECT to_regprocedure('rag.retire_versions()') IS NULL AS add_retention,
       NOT EXISTS (
           SELECT 1 FROM pg_attribute
           WHERE attrelid = 'rag.jobs'::regclass AND attname = 'trace' AND NOT attisdropped
       ) AS add_audit
\gset

\if :add_retention
ALTER TABLE versions ADD COLUMN retired timestamptz;

-- Existing unpinned versions get a full retention window from the migration.
UPDATE versions AS v SET retired = statement_timestamp()
FROM documents AS d
WHERE (d.tenant, d.library, d.id) = (v.tenant, v.library, v.document)
  AND v.id IS DISTINCT FROM d.target AND v.id IS DISTINCT FROM d.published;

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
ALTER TABLE jobs ADD COLUMN purged uuid;
CREATE INDEX jobs_version_idx ON jobs (version) WHERE version IS NOT NULL;
\endif

\if :add_audit
ALTER TABLE jobs ADD COLUMN trace uuid;
ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_check3, DROP CONSTRAINT IF EXISTS jobs_source;
ALTER TABLE jobs ADD CONSTRAINT jobs_source CHECK (
    (kind = 'ingest' AND app IS NOT NULL AND revision IS NULL AND (
        (version IS NOT NULL AND purged IS NULL)
        OR (version IS NULL AND purged IS NOT NULL AND state IN ('done', 'failed'))
    ))
    OR (kind = 'sync' AND version IS NULL AND purged IS NULL AND revision IS NOT NULL AND request IS NULL)
);
\endif

COMMENT ON SCHEMA rag IS 'rag schema version 1';
\endif

COMMIT;
