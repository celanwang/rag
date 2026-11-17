\set ON_ERROR_STOP on

BEGIN;
SET LOCAL search_path TO rag, pg_temp;

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

-- Keep upload receipts and idempotency keys after removing the original version.
ALTER TABLE jobs ADD COLUMN purged uuid;
ALTER TABLE jobs DROP CONSTRAINT jobs_check3;
ALTER TABLE jobs ADD CONSTRAINT jobs_source CHECK (
    (kind = 'ingest' AND app IS NOT NULL AND revision IS NULL AND (
        (version IS NOT NULL AND purged IS NULL)
        OR (version IS NULL AND purged IS NOT NULL AND state IN ('done', 'failed'))
    ))
    OR (kind = 'sync' AND app IS NULL AND version IS NULL AND purged IS NULL
        AND revision IS NOT NULL AND request IS NULL)
);
CREATE INDEX jobs_version_idx ON jobs (version) WHERE version IS NOT NULL;

COMMIT;
