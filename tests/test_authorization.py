import json
import os
import subprocess
import sys
import unittest
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from rag.api import app
from rag.auth import LibraryForbidden, app_session, require_library


def cli(*args: str, expected: int = 0) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "rag.cli", *args], capture_output=True, text=True, check=False,
    )
    if result.returncode != expected:
        raise AssertionError(f"{args[0]} exited {result.returncode}: {result.stderr.strip()}")
    return json.loads(result.stdout) if expected == 0 else {}


class AuthorizationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api_dsn = os.environ["RAG_DATABASE_URL"]
        cls.admin_dsn = os.environ["RAG_ADMIN_DATABASE_URL"]
        suffix = uuid4().hex[:12]
        cls.alpha = f"alpha-{suffix}"
        cls.beta = f"beta-{suffix}"
        cls.alpha_id = cli("create-tenant", cls.alpha, "Alpha")["id"]
        cls.beta_id = cli("create-tenant", cls.beta, "Beta")["id"]
        cls.alpha_library = cli("create-library", cls.alpha, "demo", "Alpha demo")["id"]
        cls.beta_library = cli("create-library", cls.beta, "demo", "Beta demo")["id"]
        cli("create-library", cls.beta, "beta-private", "Beta private")
        cls.reader = cli("register-app", cls.alpha, "Alpha Reader")
        cls.writer = cli("register-app", cls.alpha, "Alpha Writer")
        cls.beta_reader = cli("register-app", cls.beta, "Beta Reader")
        cli("grant", cls.alpha, cls.reader["id"], "demo", "read")
        cli("grant", cls.alpha, cls.writer["id"], "demo", "write")
        cli("grant", cls.beta, cls.beta_reader["id"], "demo", "read")
        cls.client = cls.enterClassContext(TestClient(app))
        cls.pool = cls.enterClassContext(ConnectionPool(
            cls.api_dsn, min_size=1, max_size=1, open=False, kwargs={"autocommit": True},
        ))

    def read(self, key: str | None = None, code: str = "demo"):
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return self.client.get(f"/v1/knowledge-bases/{code}", headers=headers)

    def test_tenant_and_library_isolation(self):
        alpha = self.read(self.reader["key"])
        beta = self.read(self.beta_reader["key"])
        self.assertEqual(alpha.status_code, 200)
        self.assertEqual(alpha.json(), {"id": self.alpha_library, "code": "demo", "name": "Alpha demo"})
        self.assertEqual(beta.status_code, 200)
        self.assertEqual(beta.json(), {"id": self.beta_library, "code": "demo", "name": "Beta demo"})
        foreign = self.read(self.reader["key"], "beta-private")
        missing = self.read(self.reader["key"], "missing")
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign.json(), missing.json())
        self.assertEqual(self.read(self.beta_reader["key"], "beta-private").status_code, 403)

    def test_read_write_permissions_and_revocation(self):
        self.assertEqual(self.read(self.writer["key"]).status_code, 403)
        with app_session(self.pool, self.writer["key"]) as (conn, identity):
            self.assertEqual(str(require_library(conn, identity, "demo", "write").id), self.alpha_library)
        with self.assertRaises(LibraryForbidden), app_session(self.pool, self.reader["key"]) as (conn, identity):
            require_library(conn, identity, "demo", "write")

        temporary = cli("register-app", self.alpha, "Temporary")
        cli("grant", self.alpha, temporary["id"], "demo", "read")
        cli("grant", self.alpha, temporary["id"], "demo", "read")
        cli("grant", self.alpha, temporary["id"], "demo", "write")
        self.assertEqual(self.read(temporary["key"]).status_code, 200)
        cli("revoke", self.alpha, temporary["id"], "demo", "read")
        self.assertEqual(self.read(temporary["key"]).status_code, 403)
        with app_session(self.pool, temporary["key"]) as (conn, identity):
            require_library(conn, identity, "demo", "write")
        cli("revoke", self.alpha, temporary["id"], "demo", "write")
        with self.assertRaises(LibraryForbidden), app_session(self.pool, temporary["key"]) as (conn, identity):
            require_library(conn, identity, "demo", "write")

    def test_key_validity_and_disable(self):
        expired = cli("register-app", self.alpha, "Expired", "--expires", "2000-01-01T00:00:00Z")
        cli("grant", self.alpha, expired["id"], "demo", "read")
        for key in (None, "invalid-key", expired["key"]):
            with self.subTest(kind="missing" if key is None else "invalid"):
                response = self.read(key)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")

        disabled = cli("register-app", self.alpha, "Disabled")
        cli("grant", self.alpha, disabled["id"], "demo", "read")
        self.assertEqual(self.read(disabled["key"]).status_code, 200)
        cli("disable-app", self.alpha, disabled["id"])
        self.assertEqual(self.read(disabled["key"]).status_code, 401)

        tenant = f"disabled-{uuid4().hex[:12]}"
        cli("create-tenant", tenant, "Disabled tenant")
        cli("create-library", tenant, "demo", "Disabled demo")
        tenant_app = cli("register-app", tenant, "Reader")
        cli("grant", tenant, tenant_app["id"], "demo", "read")
        self.assertEqual(self.read(tenant_app["key"]).status_code, 200)
        cli("disable-tenant", tenant)
        self.assertEqual(self.read(tenant_app["key"]).status_code, 401)
        self.assertEqual(self.read(self.reader["key"]).status_code, 200)

    def test_database_role_and_pooled_tenant_context(self):
        with app_session(self.pool, self.reader["key"]) as (conn, _):
            pid = conn.info.backend_pid
            self.assertEqual(conn.execute("SELECT name FROM rag.libraries").fetchall(), [("Alpha demo",)])
            self.assertEqual(conn.execute("SELECT DISTINCT tenant::text FROM rag.grants").fetchall(), [(self.alpha_id,)])
        with self.pool.connection() as conn:
            self.assertEqual(conn.info.backend_pid, pid)
            self.assertEqual(conn.execute("SELECT name FROM rag.libraries").fetchall(), [])
            self.assertEqual(conn.execute("SELECT tenant FROM rag.grants").fetchall(), [])
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT digest FROM rag.apps")
        with app_session(self.pool, self.beta_reader["key"]) as (conn, _):
            self.assertEqual(conn.info.backend_pid, pid)
            self.assertEqual(
                {row[0] for row in conn.execute("SELECT name FROM rag.libraries")},
                {"Beta demo", "Beta private"},
            )

    def test_cross_tenant_grant_cannot_be_created(self):
        cli("grant", self.alpha, self.beta_reader["id"], "demo", "read", expected=1)
        with self.assertRaises(psycopg.errors.ForeignKeyViolation), psycopg.connect(self.admin_dsn) as conn:
            conn.execute(
                "INSERT INTO rag.grants (tenant, app, library, action) VALUES (%s, %s, %s, 'read')",
                (self.alpha_id, self.reader["id"], self.beta_library),
            )


if __name__ == "__main__":
    unittest.main()
