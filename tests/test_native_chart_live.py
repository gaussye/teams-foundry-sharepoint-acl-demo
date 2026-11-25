from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from native_chart_demo import INDEX, configuration
from native_chart_live import (
    GROUPS, Journal, attach, conditional_search_put, permission_signature, repair_run, run,
    verify_root_acl,
)


class NativeChartLiveGuardsTests(unittest.TestCase):
    def permissions(self):
        return {"value": [
            {"grantedToV2": {"group": {"id": GROUPS["finance"][0]}}, "roles": ["read"]},
            {"grantedToV2": {"siteGroup": {"id": "3"}}, "roles": ["owner"]},
            {"grantedToV2": {"siteUser": {"loginName":
                "c:0o.c|federateddirectoryclaimprovider|f759edb3-b372-4617-8087-b12c053264a5_o"}},
             "roles": ["owner"]},
        ]}

    def test_unknown_link_or_broader_source_acl_stops_before_group_lookup(self):
        for change in (
            lambda p: p["value"][0].update(roles=["write"]),
            lambda p: p["value"][1].update(link={"scope": "anonymous"}),
            lambda p: p.update({"@odata.nextLink": "next"}),
        ):
            permissions = self.permissions()
            change(permissions)
            with self.subTest(permissions=permissions), patch("native_chart_live.request") as network:
                with self.assertRaises(ValueError):
                    verify_root_acl("finance", permissions)
                network.assert_not_called()

    def test_source_membership_is_fresh_and_not_silently_truncated(self):
        for members in (
            {"value": [{"id": GROUPS["hr"][1]}]},
            {"value": [{"id": GROUPS["finance"][1]}], "@odata.nextLink": "next"},
        ):
            with patch("native_chart_live.request", return_value=members):
                with self.assertRaises(ValueError):
                    verify_root_acl("finance", self.permissions())
        with patch("native_chart_live.request",
                   return_value={"value": [{"id": GROUPS["finance"][1]}]}) as network:
            verify_root_acl("finance", self.permissions())
            network.assert_called_once()

    def test_effective_acl_comparison_ignores_response_order_not_roles_or_principals(self):
        first = self.permissions()
        second = deepcopy(first)
        second["value"].reverse()
        second["value"][0]["inheritedFrom"] = {"id": "parent"}
        self.assertEqual(permission_signature(first), permission_signature(second))
        second["value"][0]["roles"] = ["read"]
        self.assertNotEqual(permission_signature(first), permission_signature(second))

    def test_only_one_no_write_transport_failure_can_resume_without_losing_history(self):
        previous = {
            "status": "failed-no-automatic-retry", "attempts": [], "folders": [], "files": [],
            "failure": "RemoteDisconnected",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "native-charts-upload-20261125.json"
            for override in ({"attempts": [{"label": "create-folder"}]},
                             {"previousReadonlyFailure": {"failure": "old"}}):
                path.write_text(json.dumps({**previous, **override}))
                with patch("native_chart_live.EVIDENCE", Path(directory)):
                    with self.assertRaises(ValueError):
                        Journal("upload", resume_readonly_preflight=True)
            path.write_text(json.dumps(previous))
            with patch("native_chart_live.EVIDENCE", Path(directory)):
                journal = Journal("upload", resume_readonly_preflight=True)
            self.assertEqual(journal.value["previousReadonlyFailure"], previous)

    def test_role_cleanup_still_runs_when_active_indexer_disabling_fails(self):
        scope = ("/subscriptions/3456866f-6478-471f-8d59-a29a335d797a/resourceGroups/"
                 "aks-test/providers/Microsoft.Search/searchServices/search-service-ypy2/indexes/"
                 + INDEX)
        assignment = scope + "/providers/Microsoft.Authorization/roleAssignments/fixed"
        definition = configuration(include_png_probe=True)["indexers"][0]

        def fake_search(method, path, body=None):
            if method == "PUT":
                raise RuntimeError("indexer PUT transport failure")
            return {"executionHistory": [], "lastResult": None} if path.endswith("/status") else definition

        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory)
            (evidence / "native-charts-deploy-20261125.json").write_text('{"status":"success"}')
            with patch("native_chart_live.EVIDENCE", evidence):
                journal = Journal("run")
                with patch("native_chart_live.uuid.uuid4", return_value="fixed"), \
                        patch("native_chart_live.search", side_effect=fake_search), \
                        patch("native_chart_live.request",
                              return_value={"properties": {"scope": scope}}) as network, \
                        patch("native_chart_live.cli", side_effect=[[{"id": assignment}], []]):
                    with self.assertRaisesRegex(ValueError, "Cleanup failed"):
                        run(journal)
                self.assertTrue(journal.value["cleanupVerifiedAbsent"])
                self.assertTrue(journal.value["cleanupErrors"])
                self.assertTrue(any(call.args[0] == "DELETE" for call in network.call_args_list))

    def test_repair_rejects_unrelated_or_processed_failure_before_cloud_calls(self):
        baseline = {
            "status": "failed-no-automatic-retry", "cleanupVerifiedAbsent": True,
            "runs": [{"name": "teams-acl-demo-sp-native-charts-finance-pdf-indexer",
                      "observed": {"lastResult": {
                          "errorMessage": "Invalid expression: 'parentId' is not a retrievable field",
                          "itemsProcessed": 0, "itemsFailed": 0}}}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "native-charts-run-20261125.json"
            for override in ({"errorMessage": "some other error"}, {"itemsProcessed": 1}):
                changed = deepcopy(baseline)
                changed["runs"][0]["observed"]["lastResult"].update(override)
                path.write_text(json.dumps(changed))
                with patch("native_chart_live.EVIDENCE", Path(directory)), \
                        patch("native_chart_live.search") as network:
                    with self.assertRaises(ValueError):
                        repair_run(None)
                    network.assert_not_called()

    def test_conditional_update_requires_a_valid_fresh_etag(self):
        for etag in (None, "", '"etag"\r\nunexpected'):
            with patch("native_chart_live.cli") as network:
                with self.assertRaises(ValueError):
                    conditional_search_put("indexes/" + INDEX, {}, etag)
                network.assert_not_called()

    def test_partial_or_failed_image_ingestion_cannot_be_attached_to_live_native_kb(self):
        baseline = {"status": "success", "cleanupVerifiedAbsent": True,
                    "runs": [{"compatibilityPassed": True} for _ in range(4)]}
        cases = []
        missing = deepcopy(baseline)
        missing["runs"].pop()
        cases.append(missing)
        failed = deepcopy(baseline)
        failed["runs"][1]["compatibilityPassed"] = False
        cases.append(failed)
        unclean = deepcopy(baseline)
        unclean["cleanupVerifiedAbsent"] = False
        cases.append(unclean)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "native-charts-repair-run-20261125.json"
            for value in cases:
                path.write_text(json.dumps(value))
                with patch("native_chart_live.EVIDENCE", Path(directory)), \
                        patch("native_chart_live.search") as network:
                    with self.assertRaises(ValueError):
                        attach(None)
                    network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
