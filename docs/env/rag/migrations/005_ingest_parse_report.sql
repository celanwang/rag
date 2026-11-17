BEGIN;
SET LOCAL search_path TO rag, pg_catalog;
SELECT pg_advisory_xact_lock(hashtext('rag schema migration'));
SELECT obj_description('rag'::regnamespace, 'pg_namespace') = 'rag schema version 4' AS apply \gset
\if :apply
ALTER TABLE jobs ADD COLUMN parse_report jsonb CHECK (parse_report IS NULL OR kind = 'ingest');
GRANT UPDATE (parse_report) ON jobs TO rag_worker;
COMMENT ON SCHEMA rag IS 'rag schema version 5';
\endif
COMMIT;
