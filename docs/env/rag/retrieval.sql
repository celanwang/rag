\set ON_ERROR_STOP on

BEGIN;
SET LOCAL search_path TO rag, pg_temp;

GRANT SELECT ON chunks TO rag_api;
GRANT SELECT (id, tenant) ON apps TO rag_api;
GRANT UPDATE (readers, revision, deleted) ON documents TO rag_api;

CREATE POLICY api_read_chunks ON chunks FOR SELECT TO rag_api USING (tenant = current_tenant());
CREATE POLICY api_read_app_ids ON apps FOR SELECT TO rag_api USING (tenant = current_tenant());
CREATE POLICY api_enqueue_sync ON jobs FOR INSERT TO rag_api
    WITH CHECK (tenant = current_tenant() AND kind = 'sync');

COMMIT;
