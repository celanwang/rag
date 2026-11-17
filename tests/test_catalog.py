import unittest

from ingestion_fixture import IngestionFixture
from test_authorization import cli


class CatalogTest(IngestionFixture, unittest.TestCase):
    @staticmethod
    def auth(key):
        return {"Authorization": f"Bearer {key}"}

    def test_catalog_resolves_tenant_from_key_and_lists_only_read_grants(self):
        path = "/v1/knowledge-bases"
        self.assertEqual(self.client.get(path).status_code, 401)
        cli("create-library", self.alpha, "internal", "Internal")
        alpha = self.client.get(path, headers=self.auth(self.reader)).json()
        self.assertEqual(alpha["tenant_id"], self.alpha_id)
        self.assertEqual([item["code"] for item in alpha["libraries"]], ["shop"])
        beta = self.client.get(path, headers=self.auth(self.beta_writer["key"])).json()
        self.assertEqual(beta, {"tenant_id": self.beta_id, "libraries": []})
        self.assertEqual(self.client.get(f"{path}/shop/documents", headers=self.auth(self.beta_writer["key"])).status_code, 403)

    def test_document_list_filters_tenant_current_version_and_readers(self):
        route = "/v1/knowledge-bases/shop/documents"
        read = self.auth(self.reader)
        write = self.auth(self.writer["key"])
        before = self.accepted()
        self.process(before)
        other = self.accepted(request="shipping", file="alpha/shipping.txt")
        self.process(other)
        beta = self.accepted(file="beta/returns.md", key=self.beta_writer["key"])
        self.process(beta)
        new = self.accepted(request="updated", document=before["document_id"])
        self.assertEqual(self.client.get(route, params={"q": "RETURNS"}, headers=read).json()["items"][0]["version_id"],
                         before["version_id"])
        self.process(new)

        page = self.client.get(route, params={"limit": 1}, headers=read).json()
        self.assertTrue(page["has_more"])
        self.assertEqual([(row["document_id"], row["version_id"], row["version"]) for row in page["items"]],
                         [(before["document_id"], new["version_id"], 2)])
        next_page = self.client.get(route, params={"offset": 1, "limit": 1}, headers=read).json()
        self.assertFalse(next_page["has_more"])
        self.assertEqual([row["document_id"] for row in next_page["items"]], [other["document_id"]])
        self.assertEqual(self.client.get(route, params={"q": "missing"}, headers=read).json(),
                         {"items": [], "has_more": False})

        deny = self.client.patch(f"/v1/documents/{before['document_id']}/readers", json={"readers": []}, headers=write)
        self.assertEqual(deny.status_code, 200)
        deleted = self.client.delete(f"/v1/documents/{other['document_id']}", headers=write)
        self.assertEqual(deleted.status_code, 202)
        self.assertEqual(self.client.get(route, headers=read).json(), {"items": [], "has_more": False})
