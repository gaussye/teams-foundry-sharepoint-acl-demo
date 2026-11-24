import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from native_indexer_experiment import (
    configuration, INDEX, MI_CLIENT, LIBRARIES, verify_consent_authority,
    resume_identity, SITE_ID, ADMIN, APP, verify_existing_indexer, status_evidence,
)


class NativeConfigurationTests(unittest.TestCase):
    def test_acl_enabled_but_hidden_and_no_query_group_credentials(self):
        result = configuration("11111111-1111-1111-1111-111111111111")
        self.assertEqual(result["index"]["permissionFilterOption"], "enabled")
        fields = {r["name"]: r for r in result["index"]["fields"]}
        for name in ("userIds", "groupIds"):
            self.assertEqual(fields[name]["permissionFilter"], name)
            self.assertTrue(fields[name]["filterable"])
            self.assertFalse(fields[name]["retrievable"])
        self.assertNotIn("sharePointConnectorAppRegistration", result["index"])

    def test_three_exact_libraries_and_disabled_no_model_indexers(self):
        result = configuration("11111111-1111-1111-1111-111111111111")
        self.assertEqual(len(result["datasources"]), 3)
        for source, indexer, (_, library, _, _) in zip(
                result["datasources"], result["indexers"], LIBRARIES):
            self.assertEqual(source["container"]["name"], "useQuery")
            self.assertTrue(source["container"]["query"].endswith("/" + library))
            connection = source["credentials"]["connectionString"]
            self.assertIn("FederatedCredentialApplicationId=" + MI_CLIENT, connection)
            self.assertNotIn("Secret", connection)
            self.assertTrue(indexer["disabled"])
            self.assertEqual(indexer["targetIndexName"], INDEX)
            self.assertNotIn("schedule", indexer)
            self.assertNotIn("skillsetName", indexer)
            mappings = {m["sourceFieldName"]: m["targetFieldName"]
                        for m in indexer["fieldMappings"]}
            self.assertEqual(mappings["metadata_group_ids"], "groupIds")
            self.assertEqual(mappings["metadata_user_ids"], "userIds")

    def test_invalid_app_id_rejected(self):
        with self.assertRaises(ValueError):
            configuration("injected;ApplicationSecret=bad")

    def test_reader_or_resource_scoped_admin_cannot_consent_graph_roles(self):
        for role, scope in [("Global Reader", "/"), ("Cloud Application Administrator", "/"),
                            ("Global Administrator", "/applications/specific-app")]:
            with self.subTest(role=role, scope=scope), self.assertRaises(ValueError):
                verify_consent_authority({"value": [{
                    "roleDefinition": {"displayName": role}, "directoryScopeId": scope}]})

    def test_verified_active_admin_role_passes(self):
        verify_consent_authority({"value": [{
            "roleDefinition": {"displayName": "Privileged Role Administrator"},
            "directoryScopeId": "/"}]})

    def test_resume_rejects_wrong_site_or_broader_grant_before_network(self):
        previous = {"status": "failed-no-automatic-retry", "temporaryAssignment": None,
                    "runs": [], "attemptedWrites": ["test-site-read"],
                    "application": {"id": "app", "appId": "client"}}
        grant = {"status": "created-via-graph-explorer", "httpStatusObserved": 201,
                 "roles": ["read"], "siteId": SITE_ID, "adminObjectId": ADMIN,
                 "applicationDisplayName": APP, "applicationClientId": "client"}
        for override in [{"siteId": "other-site"}, {"roles": ["fullcontrol"]},
                         {"applicationClientId": "different"}]:
            with self.subTest(override=override), patch("native_indexer_experiment.request") as network:
                with self.assertRaises(ValueError):
                    resume_identity(previous, {**grant, **override}, {})
                network.assert_not_called()

    def test_existing_indexer_recovery_accepts_normalized_mappings_not_scope_drift(self):
        import copy
        expected = configuration("11111111-1111-1111-1111-111111111111")["indexers"][0]
        actual = copy.deepcopy(expected)
        for mapping in actual["fieldMappings"]:
            mapping.setdefault("mappingFunction", None)
        verify_existing_indexer(actual, expected)
        actual["targetIndexName"] = "unrelated-index"
        with self.assertRaises(ValueError):
            verify_existing_indexer(actual, expected)

    def test_execution_summary_omits_source_tracking_tokens(self):
        self.assertEqual(status_evidence({"lastResult": {
            "status": "success", "finalTrackingState": "opaque",
            "initialTrackingState": "opaque", "itemsProcessed": 1}}),
            {"lastResult": {"status": "success", "itemsProcessed": 1}})


if __name__ == "__main__":
    unittest.main()
