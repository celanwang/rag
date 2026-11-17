BEGIN;
CREATE EXTENSION vector;

CREATE ROLE rag_app LOGIN PASSWORD 'poc-app' NOSUPERUSER NOBYPASSRLS;

CREATE TABLE documents (
    tenant_id text NOT NULL,
    knowledge_base_id text NOT NULL,
    document_id uuid NOT NULL,
    filename text NOT NULL,
    fingerprint text NOT NULL,
    published_version uuid NOT NULL,
    readers text[] NOT NULL,
    PRIMARY KEY (tenant_id, knowledge_base_id, document_id)
);

CREATE TABLE chunks (
    tenant_id text NOT NULL,
    knowledge_base_id text NOT NULL,
    document_id uuid NOT NULL,
    version_id uuid NOT NULL,
    chunk_id uuid NOT NULL,
    heading text NOT NULL,
    body text NOT NULL,
    line_start integer NOT NULL,
    line_end integer NOT NULL CHECK (line_end >= line_start),
    embedding vector(512) NOT NULL,
    PRIMARY KEY (tenant_id, knowledge_base_id, chunk_id),
    FOREIGN KEY (tenant_id, knowledge_base_id, document_id)
        REFERENCES documents (tenant_id, knowledge_base_id, document_id)
);

CREATE INDEX chunks_by_document_version
    ON chunks (tenant_id, knowledge_base_id, document_id, version_id);

ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;
CREATE POLICY document_tenant ON documents
    USING (tenant_id = current_setting('rag.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('rag.tenant_id', true));

ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks FORCE ROW LEVEL SECURITY;
CREATE POLICY chunk_tenant ON chunks
    USING (tenant_id = current_setting('rag.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('rag.tenant_id', true));

GRANT SELECT, INSERT, UPDATE ON documents TO rag_app;
GRANT SELECT, INSERT ON chunks TO rag_app;
COMMIT;
