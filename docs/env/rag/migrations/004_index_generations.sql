BEGIN;
SET LOCAL search_path TO rag, pg_catalog;
SELECT pg_advisory_xact_lock(hashtext('rag schema migration'));
SELECT obj_description('rag'::regnamespace, 'pg_namespace') = 'rag schema version 3' AS apply \gset
\if :apply
CREATE TABLE index_generations (
    name text PRIMARY KEY,
    alias text NOT NULL,
    status text NOT NULL CHECK (status IN ('building', 'ready', 'active', 'archived')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX ON index_generations (alias) WHERE status = 'active';
CREATE TABLE index_generation_documents (
    generation text NOT NULL REFERENCES index_generations (name) ON DELETE CASCADE,
    tenant uuid NOT NULL,
    library uuid NOT NULL,
    document uuid NOT NULL,
    revision bigint NOT NULL CHECK (revision >= 0),
    PRIMARY KEY (generation, document),
    FOREIGN KEY (tenant, library, document) REFERENCES documents (tenant, library, id)
);
ALTER TABLE index_generations ENABLE ROW LEVEL SECURITY;
ALTER TABLE index_generation_documents ENABLE ROW LEVEL SECURITY;
COMMENT ON SCHEMA rag IS 'rag schema version 4';
\endif
COMMIT;
