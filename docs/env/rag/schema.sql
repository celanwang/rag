\set ON_ERROR_STOP on
\getenv api_password RAG_API_PASSWORD
\getenv admin_password RAG_ADMIN_PASSWORD

BEGIN;

CREATE ROLE rag_api LOGIN PASSWORD :'api_password' NOSUPERUSER NOBYPASSRLS;
CREATE ROLE rag_admin LOGIN PASSWORD :'admin_password' NOSUPERUSER NOBYPASSRLS;
CREATE ROLE rag_auth NOLOGIN NOSUPERUSER NOBYPASSRLS;

CREATE SCHEMA rag;
SET LOCAL search_path TO rag, pg_temp;
GRANT USAGE ON SCHEMA rag TO rag_api, rag_admin, rag_auth;

-- Store tenant codes and activation status.
CREATE TABLE tenants (
    id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code    text NOT NULL UNIQUE CHECK (code ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    name    text NOT NULL,
    active  boolean NOT NULL DEFAULT true
);

-- Bind an application and its API key digest to a tenant.
CREATE TABLE apps (
    id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant   uuid NOT NULL REFERENCES tenants (id),
    name     text NOT NULL,
    digest   bytea NOT NULL UNIQUE CHECK (octet_length(digest) = 32),
    expires  timestamptz,
    active   boolean NOT NULL DEFAULT true,
    UNIQUE (tenant, id)
);

-- Store each tenant's knowledge bases.
CREATE TABLE libraries (
    id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant    uuid NOT NULL REFERENCES tenants (id),
    code      text NOT NULL CHECK (code ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    name      text NOT NULL,
    settings  jsonb NOT NULL DEFAULT '{}',
    UNIQUE (tenant, code),
    UNIQUE (tenant, id)
);

-- Store independent read and write grants within the same tenant.
CREATE TABLE grants (
    tenant   uuid NOT NULL,
    app      uuid NOT NULL,
    library  uuid NOT NULL,
    action   text NOT NULL CHECK (action IN ('read', 'write')),
    PRIMARY KEY (tenant, app, library, action),
    FOREIGN KEY (tenant, app) REFERENCES apps (tenant, id),
    FOREIGN KEY (tenant, library) REFERENCES libraries (tenant, id)
);

ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
ALTER TABLE apps ENABLE ROW LEVEL SECURITY;
ALTER TABLE apps FORCE ROW LEVEL SECURITY;
ALTER TABLE libraries ENABLE ROW LEVEL SECURITY;
ALTER TABLE libraries FORCE ROW LEVEL SECURITY;
ALTER TABLE grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE grants FORCE ROW LEVEL SECURITY;

-- The CLI provisions tenants and grants with a separate database role.
GRANT SELECT (id, code), INSERT, UPDATE (active) ON tenants TO rag_admin;
GRANT SELECT (id, tenant), INSERT, UPDATE (active) ON apps TO rag_admin;
GRANT SELECT (id, tenant, code), INSERT ON libraries TO rag_admin;
GRANT SELECT, INSERT, DELETE ON grants TO rag_admin;
CREATE POLICY tenant_admin ON tenants TO rag_admin USING (true) WITH CHECK (true);
CREATE POLICY app_admin ON apps TO rag_admin USING (true) WITH CHECK (true);
CREATE POLICY library_admin ON libraries TO rag_admin USING (true) WITH CHECK (true);
CREATE POLICY grant_admin ON grants TO rag_admin USING (true) WITH CHECK (true);

GRANT SELECT (id, tenant, code, name) ON libraries TO rag_api;
GRANT SELECT ON grants TO rag_api;
CREATE POLICY library_scope ON libraries FOR SELECT TO rag_api
    USING (tenant = NULLIF(current_setting('rag.tenant_id', true), '')::uuid);
CREATE POLICY grant_scope ON grants FOR SELECT TO rag_api
    USING (tenant = NULLIF(current_setting('rag.tenant_id', true), '')::uuid);

-- Resolve a key before a tenant is known. Only the function owner can read digests.
GRANT SELECT (id, active) ON tenants TO rag_auth;
GRANT SELECT (id, tenant, digest, active, expires) ON apps TO rag_auth;
CREATE POLICY tenant_auth ON tenants FOR SELECT TO rag_auth USING (true);
CREATE POLICY app_auth ON apps FOR SELECT TO rag_auth USING (true);

CREATE FUNCTION resolve_key(key_digest bytea)
RETURNS TABLE (app uuid, tenant uuid)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT a.id, a.tenant
    FROM rag.apps AS a
    JOIN rag.tenants AS t ON t.id = a.tenant
    WHERE a.digest = key_digest
      AND a.active AND t.active
      AND (a.expires IS NULL OR a.expires > statement_timestamp())
$$;

ALTER FUNCTION resolve_key(bytea) OWNER TO rag_auth;
REVOKE ALL ON FUNCTION resolve_key(bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_key(bytea) TO rag_api;

COMMIT;
