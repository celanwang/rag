"""Provision two users and a shared local model in a dedicated RAGFlow PoC."""
import argparse
import base64
import json
import os
import secrets
import subprocess
from pathlib import Path
from urllib.parse import quote

import httpx

from .ragflow_baseline import RF_URL, data
from .ragflow_embedding import MODEL_ID

FACTORY = "OpenAI-API-Compatible"
INSTANCE = "rag-poc-local"
ADMIN_URL = "http://127.0.0.1:19381"
MODEL_ENDPOINT = "http://host.docker.internal:18090/v1"


def private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
        json.dump(value, file, indent=2)
        file.write("\n")


def encrypted_password(public_key: Path, password: str) -> str:
    result = subprocess.run(
        ["openssl", "pkeyutl", "-encrypt", "-pubin", "-inkey", str(public_key),
         "-pkeyopt", "rsa_padding_mode:pkcs1"],
        input=base64.b64encode(password.encode()), stdout=subprocess.PIPE, check=True,
    )
    return base64.b64encode(result.stdout).decode()


def login(client: httpx.Client, endpoint: str, email: str, password: str, public_key: Path) -> dict:
    response = client.post(endpoint, json={"email": email, "password": encrypted_password(public_key, password)})
    result = data(response)
    authorization = response.headers.get("Authorization")
    return {"Authorization": authorization or f"Bearer {result['access_token']}"}


def configure_model(client: httpx.Client, headers: dict, embedding_key: str) -> str:
    providers = data(client.get("/api/v1/providers", headers=headers))
    if not any(provider["name"] == FACTORY for provider in providers):
        data(client.put("/api/v1/providers", headers=headers, json={"provider_name": FACTORY}))
    instances = f"/api/v1/providers/{quote(FACTORY, safe='')}/instances"
    configured = data(client.get(instances, headers=headers))
    if not any(row["instance_name"] == INSTANCE for row in configured):
        data(client.post(instances, headers=headers, json={
            "instance_name": INSTANCE, "api_key": embedding_key,
            "base_url": MODEL_ENDPOINT,
            "model_info": [{"model_name": MODEL_ID, "model_type": ["embedding"], "max_tokens": 512}],
        }))
    return f"{MODEL_ID}@{INSTANCE}@{FACTORY}"


def issue_key(client: httpx.Client, headers: dict) -> str:
    result = data(client.post("/api/v1/system/keys", headers=headers, json={}))
    return result["token"]


def provision(admin: httpx.Client, api: httpx.Client, args) -> dict:
    admin_auth = login(admin, "/api/v1/admin/login", "admin@ragflow.io",
                       args.admin_password.read_text().strip(), args.public_key)
    users = data(admin.get("/api/v1/admin/users", headers=admin_auth))
    known = {user["email"] for user in users}
    if args.accounts.exists():
        accounts = json.loads(args.accounts.read_text())
    else:
        accounts = {tenant: secrets.token_urlsafe(24) for tenant in ("alpha", "beta")}
        private_json(args.accounts, accounts)
    result = json.loads(args.out.read_text()) if args.out.exists() else {}
    for tenant, password in accounts.items():
        email = f"rag-poc-{tenant}@example.test"
        if email not in known:
            data(admin.post("/api/v1/admin/users", headers=admin_auth, json={
                "username": email, "password": encrypted_password(args.public_key, password),
            }))
        headers = login(api, "/api/v1/auth/login", email, password, args.public_key)
        model = configure_model(api, headers, args.embedding_key.read_text().strip())
        config = result.setdefault(tenant, {})
        config["embedding_model"] = model
        if "key" not in config:
            config["key"] = issue_key(api, headers)
        if tenant == "alpha" and "auditor_key" not in config:
            config["auditor_key"] = issue_key(api, headers)
        private_json(args.out, result)
        print(f"Provisioned RAGFlow tenant {tenant} and local embedding model", flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ragflow-url", default=RF_URL)
    parser.add_argument("--admin-url", default=ADMIN_URL)
    parser.add_argument("--admin-password", type=Path, default=Path(".poc/ragflow-admin-password"))
    parser.add_argument("--public-key", type=Path, default=Path(".poc/ragflow-public.pem"))
    parser.add_argument("--embedding-key", type=Path, default=Path(".poc/ragflow-embedding-key"))
    parser.add_argument("--accounts", type=Path, default=Path(".poc/ragflow-accounts.json"))
    parser.add_argument("--out", type=Path, default=Path(".poc/ragflow-credentials.json"))
    args = parser.parse_args()
    with (
        httpx.Client(base_url=args.admin_url, timeout=60, trust_env=False) as admin,
        httpx.Client(base_url=args.ragflow_url, timeout=60, trust_env=False) as api,
    ):
        provision(admin, api, args)


if __name__ == "__main__":
    main()
