import argparse
import json
import os
import re
from datetime import datetime
from secrets import token_urlsafe
from uuid import UUID

import psycopg

from .auth import key_digest
from .db import SCHEMA_OPTIONS


class ConfigurationError(Exception):
    pass


def short_code(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value):
        raise argparse.ArgumentTypeError("use 1 to 63 lowercase letters, digits or hyphens, starting with a letter or digit")
    return value


def display_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise argparse.ArgumentTypeError("name must not be blank")
    return name


def expires_at(value: str) -> datetime:
    try:
        expiry = datetime.fromisoformat(value)
        if expiry.utcoffset() is not None:
            return expiry
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("use an ISO 8601 timestamp with a time zone")


def tenant_id(conn: psycopg.Connection, code: str) -> UUID:
    row = conn.execute("SELECT id FROM tenants WHERE code = %s", (code,)).fetchone()
    if row is None:
        raise ConfigurationError("Tenant not found.")
    return row[0]


def create_tenant(conn: psycopg.Connection, args: argparse.Namespace) -> dict:
    row = conn.execute(
        "INSERT INTO tenants (code, name) VALUES (%s, %s) RETURNING id",
        (args.tenant, args.name),
    ).fetchone()
    return {"id": str(row[0])}


def register_app(conn: psycopg.Connection, args: argparse.Namespace) -> dict:
    tenant = tenant_id(conn, args.tenant)
    key = f"rag_{token_urlsafe(32)}"
    row = conn.execute(
        "INSERT INTO apps (tenant, name, digest, expires) VALUES (%s, %s, %s, %s) RETURNING id",
        (tenant, args.name, key_digest(key), args.expires),
    ).fetchone()
    return {"id": str(row[0]), "key": key}


def create_library(conn: psycopg.Connection, args: argparse.Namespace) -> dict:
    tenant = tenant_id(conn, args.tenant)
    row = conn.execute(
        "INSERT INTO libraries (tenant, code, name) VALUES (%s, %s, %s) RETURNING id",
        (tenant, args.code, args.name),
    ).fetchone()
    return {"id": str(row[0])}


def change_grant(conn: psycopg.Connection, args: argparse.Namespace) -> dict:
    tenant = tenant_id(conn, args.tenant)
    app = conn.execute(
        "SELECT id FROM apps WHERE tenant = %s AND id = %s", (tenant, args.app),
    ).fetchone()
    if app is None:
        raise ConfigurationError("Application not found in tenant.")

    library = conn.execute(
        "SELECT id FROM libraries WHERE tenant = %s AND code = %s", (tenant, args.code),
    ).fetchone()
    if library is None:
        raise ConfigurationError("Knowledge base not found in tenant.")

    params = (tenant, app[0], library[0], args.action)
    if args.command == "grant":
        conn.execute(
            "INSERT INTO grants (tenant, app, library, action) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            params,
        )
    else:
        conn.execute(
            "DELETE FROM grants WHERE tenant = %s AND app = %s AND library = %s AND action = %s",
            params,
        )
    return {"app": str(app[0]), "library": str(library[0]), "action": args.action}


def disable_app(conn: psycopg.Connection, args: argparse.Namespace) -> dict:
    tenant = tenant_id(conn, args.tenant)
    row = conn.execute(
        "UPDATE apps SET active = false WHERE tenant = %s AND id = %s RETURNING id",
        (tenant, args.app),
    ).fetchone()
    if row is None:
        raise ConfigurationError("Application not found in tenant.")
    return {"id": str(row[0]), "active": False}


def disable_tenant(conn: psycopg.Connection, args: argparse.Namespace) -> dict:
    row = conn.execute(
        "UPDATE tenants SET active = false WHERE code = %s RETURNING id", (args.tenant,),
    ).fetchone()
    if row is None:
        raise ConfigurationError("Tenant not found.")
    return {"id": str(row[0]), "active": False}


def command_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Configure RAG tenants, applications and knowledge bases.")
    commands = parser.add_subparsers(dest="command", required=True)

    tenant = commands.add_parser("create-tenant", help="Create a tenant")
    tenant.add_argument("tenant", type=short_code)
    tenant.add_argument("name", type=display_name)
    tenant.set_defaults(handler=create_tenant)

    app = commands.add_parser("register-app", help="Register an application and issue its API key")
    app.add_argument("tenant", type=short_code)
    app.add_argument("name", type=display_name)
    app.add_argument("--expires", type=expires_at)
    app.set_defaults(handler=register_app)

    library = commands.add_parser("create-library", help="Create a knowledge base")
    library.add_argument("tenant", type=short_code)
    library.add_argument("code", type=short_code)
    library.add_argument("name", type=display_name)
    library.set_defaults(handler=create_library)

    for verb in ("grant", "revoke"):
        permission = commands.add_parser(verb, help=f"{verb.capitalize()} an application permission")
        permission.add_argument("tenant", type=short_code)
        permission.add_argument("app", type=UUID)
        permission.add_argument("code", type=short_code)
        permission.add_argument("action", choices=("read", "write"))
        permission.set_defaults(handler=change_grant)

    inactive_app = commands.add_parser("disable-app", help="Disable an application's API key")
    inactive_app.add_argument("tenant", type=short_code)
    inactive_app.add_argument("app", type=UUID)
    inactive_app.set_defaults(handler=disable_app)

    inactive_tenant = commands.add_parser("disable-tenant", help="Disable a tenant and its application keys")
    inactive_tenant.add_argument("tenant", type=short_code)
    inactive_tenant.set_defaults(handler=disable_tenant)
    return parser


def main() -> None:
    parser = command_parser()
    args = parser.parse_args()
    dsn = os.environ.get("RAG_ADMIN_DATABASE_URL")
    if not dsn:
        parser.error("set RAG_ADMIN_DATABASE_URL before running the CLI")

    try:
        with psycopg.connect(dsn, connect_timeout=5, options=SCHEMA_OPTIONS) as conn:
            result = args.handler(conn, args)
    except ConfigurationError as error:
        parser.exit(1, f"{error}\n")
    except psycopg.errors.UniqueViolation:
        parser.exit(1, "A record with the same unique value already exists.\n")
    except psycopg.Error:
        parser.exit(1, "Database operation failed.\n")

    print(json.dumps(result))


if __name__ == "__main__":
    main()
