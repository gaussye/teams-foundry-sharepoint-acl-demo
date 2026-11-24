import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from native_agent_deploy import definition, configuration, INDEX, CLIENT, MI_CLIENT, semantic_definition


class NativeAgentTests(unittest.TestCase):
    def test_query_identity_delta_preserves_hidden_acl(self):
        old = {"name": INDEX, "permissionFilterOption": "enabled", "fields": [
            {"name": "userIds", "retrievable": False}, {"name": "groupIds", "retrievable": False},
            {"name": "SharePointSiteUrl", "retrievable": False}]}
        new = definition(old)
        self.assertEqual(new["sharePointConnectorAppRegistration"]["applicationId"], CLIENT)
        self.assertEqual(new["sharePointConnectorAppRegistration"]["federatedCredentialId"], MI_CLIENT)
        self.assertTrue(new["fields"][2]["sharepointSiteUrl"])
        self.assertNotIn("sharePointConnectorAppRegistration", old)
        for field in new["fields"]:
            self.assertFalse(field["retrievable"])

    def test_native_tool_uses_only_native_kb_and_no_tenant_share(self):
        cfg = configuration()
        self.assertFalse(cfg["connection"]["properties"]["isSharedToAll"])
        self.assertEqual(cfg["connection"]["properties"]["authType"], "ProjectManagedIdentity")
        self.assertIn("teams-acl-demo-sp-native-kb/mcp", cfg["agent"]["definition"]["tools"][0]["server_url"])
        self.assertEqual(cfg["agent"]["definition"]["model"], "gpt-4.1-mini")

    def test_semantic_change_reuses_fields_and_rejects_unsearchable_title(self):
        old = {"name": INDEX, "fields": [{"name": n, "type": "Edm.String", "searchable": True}
                                       for n in ["content","metadata_spo_item_name"]]}
        new = semantic_definition(old)
        self.assertEqual(new["fields"], old["fields"])
        self.assertEqual(new["semantic"]["defaultConfiguration"], "native-acl-semantic")
        old["fields"][1]["searchable"] = False
        with self.assertRaises(ValueError):
            semantic_definition(old)


if __name__ == "__main__":
    unittest.main()
