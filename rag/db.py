from uuid import UUID

import psycopg

SCHEMA_OPTIONS = "-c search_path=pg_catalog,rag,pg_temp"


def set_tenant(conn: psycopg.Connection, tenant: UUID) -> None:
    conn.execute("SELECT set_config('rag.tenant_id', %s, true)", (str(tenant),))
