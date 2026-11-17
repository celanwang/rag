import argparse
from contextlib import contextmanager

import httpx
import psycopg

from .benchmark import BASE_URL, request, search, seed
from .retrieval import MODES
from .storage import DSN, ES_INDEX, Elasticsearch, Scope


def citation(client: httpx.Client, token: str, chunk_id: str, expected: int, library: str = "demo") -> None:
    response = client.get(f"/v1/citations/{chunk_id}", params={"knowledge_base_id": library},
                          headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == expected, response.text


def prohibit(result: dict, chunk_id: str, sentinel: str) -> None:
    assert not result["degraded"], result
    assert chunk_id not in str(result), result
    assert sentinel not in str(result), result


@contextmanager
def forged_hit(es: Elasticsearch, chunk_id: str, patch: dict):
    url = f"/{ES_INDEX}/_doc/{chunk_id}"
    source = es.client.get(url).raise_for_status().json()["_source"]
    es.client.put(url, params={"refresh": "true"}, json=source | patch).raise_for_status()
    try:
        yield source
    finally:
        es.client.put(url, params={"refresh": "true"}, json=source).raise_for_status()


def verify_rls() -> None:
    with psycopg.connect(DSN, autocommit=True) as connection:
        assert connection.execute("""
            SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user
        """).fetchone() == (False, False)
        for tenant in ("alpha", "beta"):
            with connection.transaction():
                connection.execute("SELECT set_config('rag.tenant_id', %s, true)", (tenant,))
                for table in ("documents", "chunks"):
                    actual = connection.execute(f"SELECT DISTINCT tenant_id FROM {table}").fetchall()
                    assert actual == [(tenant,)], actual
            for table in ("documents", "chunks"):
                count = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                assert count == 0, count


def check_isolation(client: httpx.Client, es: Elasticsearch) -> None:
    tenant_evidence = {}
    for mode in MODES:
        for tenant, expected, opposite in (
            ("alpha", "网关时钟", "Beta Trust Store"),
            ("beta", "Beta Trust Store", "网关时钟"),
        ):
            token = f"{tenant}-reader"
            result = search(client, token, "GW-4102", mode)
            assert not result["degraded"], result
            matches = [row for row in result["results"] if expected in row["body"]]
            assert matches and opposite not in str(result), result
            tenant_evidence[tenant] = matches[0]
            citation(client, token, matches[0]["chunk_id"], 200)

    alpha_id = tenant_evidence["alpha"]["chunk_id"]
    beta_id = tenant_evidence["beta"]["chunk_id"]
    citation(client, "beta-reader", alpha_id, 404)
    citation(client, "alpha-reader", beta_id, 404)
    assert tenant_evidence["alpha"]["document_id"] != tenant_evidence["beta"]["document_id"]

    auditor = search(client, "alpha-auditor", "AU-3368", "rerank")
    restricted_id = next(row["chunk_id"] for row in auditor["results"] if "BIOSEAL-9151" in row["body"])
    for mode in MODES:
        prohibit(search(client, "alpha-reader", "AU-3368", mode), restricted_id, "BIOSEAL-9151")
    citation(client, "alpha-auditor", restricted_id, 200)
    citation(client, "alpha-reader", restricted_id, 404)

    alpha_scope = Scope("alpha", "demo", "reader")
    for forbidden, patch, query, sentinel in (
        (restricted_id, {"public": True}, "BIOSEAL-9151", "BIOSEAL-9151"),
        (beta_id, {"tenant_id": "alpha"}, "GW-4102 Trust Store", "Beta Trust Store"),
    ):
        with forged_hit(es, forbidden, patch):
            assert forbidden in es.search(alpha_scope, query, 8), "Fixture must bypass the ES filter"
            for mode in ("keyword", "rrf", "rerank"):
                result = search(client, "alpha-reader", query, mode)
                assert result["debug"]["filtered"] >= 1, result
                prohibit(result, forbidden, sentinel)


def check_publication(client: httpx.Client, es: Elasticsearch) -> None:
    path = "/v1/knowledge-bases/ops/documents"
    token = "alpha-admin"

    def publish(marker: str, readers: list[str]) -> dict:
        return request(client, "POST", path, token, json={
            "filename": "poc-version.md", "content": f"# 测试\n\n版次标记 {marker}。", "readers": readers,
        }).json()

    first = publish("OLD-914", [])
    assert first == publish("OLD-914", []), "Replaying identical content must preserve the version"
    old = search(client, token, "OLD-914", "rerank", knowledge_base_id="ops")["results"][0]
    old_id = old["chunk_id"]
    citation(client, "alpha-admin", old_id, 200, "ops")
    citation(client, "alpha-admin", old_id, 404)
    citation(client, "alpha-reader", old_id, 403, "ops")
    assert client.post("/v1/retrieve", headers={"Authorization": "Bearer alpha-reader"}, json={
        "knowledge_base_id": "ops", "question": "OLD-914",
    }).status_code == 403
    old_source = es.client.get(f"/{ES_INDEX}/_doc/{old_id}").raise_for_status().json()["_source"]

    latest = publish("NEW-915", [])
    assert latest["keyword_ready"] and first["version_id"] != latest["version_id"]
    new = search(client, token, "NEW-915", "rerank", knowledge_base_id="ops")["results"][0]
    assert new["version_id"] == latest["version_id"] and "NEW-915" in new["body"]
    citation(client, token, old_id, 404, "ops")
    es.client.put(f"/{ES_INDEX}/_doc/{old_id}", params={"refresh": "true"}, json=old_source).raise_for_status()
    try:
        result = search(client, token, "OLD-914", "rerank", knowledge_base_id="ops")
        assert result["debug"]["filtered"] == 1, result
        prohibit(result, old_id, "OLD-914")
    finally:
        es.client.delete(f"/{ES_INDEX}/_doc/{old_id}", params={"refresh": "true"}).raise_for_status()

    latest_id = new["chunk_id"]
    revoked = publish("NEW-915", ["auditor"])
    assert revoked["keyword_ready"] and revoked["version_id"] == latest["version_id"]
    citation(client, token, latest_id, 404, "ops")
    with forged_hit(es, latest_id, {"public": True}):
        result = search(client, token, "NEW-915", "rerank", knowledge_base_id="ops")
        assert result["debug"]["filtered"] == 1, result
        prohibit(result, latest_id, "NEW-915")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run isolation, stale index, and publication assertions.")
    parser.add_argument("--url", default=BASE_URL)
    args = parser.parse_args()
    es = Elasticsearch()
    try:
        with httpx.Client(base_url=args.url, timeout=60, trust_env=False) as client:
            seed(client)
            assert client.post("/v1/retrieve", json={"question": "GW-4102"}).status_code == 401
            assert client.post("/v1/retrieve", headers={"Authorization": "Bearer alpha-reader"},
                               json={"question": "GW-4102", "tenant_id": "beta"}).status_code == 422
            verify_rls()
            print("PASS: tenant RLS on reused connection and rejected tenant forgery")
            check_isolation(client, es)
            print("PASS: tenant and document ACL across all retrieval modes and citations, including forged ES hits")
            check_publication(client, es)
            print("PASS: knowledge-base isolation, idempotency, version replacement, stale ES version and ACL revocation")
    finally:
        es.client.close()


if __name__ == "__main__":
    main()
