import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "indexed_deploy", Path(__file__).resolve().parents[1] / "scripts" / "indexed_deploy.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)
from indexed_acl import schemas


class DeployTests(unittest.TestCase):
    def run_case(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            manifest, evidence = Path(directory) / "manifest.json", Path(directory) / "result.json"
            manifest.write_text(json.dumps({
                "siteUrl": "https://mngenvmcap566860.sharepoint.com/sites/test"
            }), encoding="utf-8")
            bundle = {**schemas(), "documents": {"value": [{"id": "one"}]}}
            created, state, calls = {}, {"assigned": False, "deletes": 0}, []

            def cli(*args):
                calls.append(args)
                if args == ("account", "show"):
                    return {"id": deploy.SUBSCRIPTION, "user": {"name": deploy.LOGIN}}
                if args[:3] == ("role", "assignment", "list"):
                    self.assertFalse("--all" in args and "--scope" in args)
                    return []
                if args[:3] == ("role", "assignment", "delete"):
                    self.assertIn(deploy.SCOPE + "/providers/Microsoft.Authorization/roleAssignments/",
                                  args[4])
                    if mode == "cleanup-error":
                        raise deploy.CliFailure("AuthorizationFailed")
                    state["assigned"] = False
                    state["deletes"] += 1
                    return None
                self.fail(f"Unexpected CLI operation: {args}")

            def rest(method, url, body=None, create_only=False):
                if url.startswith("https://graph.microsoft.com/"):
                    return {"id": deploy.ADMIN, "userPrincipalName": deploy.LOGIN}
                self.assertTrue(url.startswith("https://management.azure.com" + deploy.SCOPE))
                if method == "PUT":
                    if mode == "unsupported-scope":
                        raise deploy.CliFailure("InvalidResourceType")
                    state["assigned"] = True
                    state["assignment"] = {
                        "id": url.split("https://management.azure.com")[1].split("?")[0],
                        "properties": {**body["properties"], "scope": deploy.SCOPE},
                    }
                    if mode == "ambiguous-role-create":
                        raise deploy.CliFailure("TransportError after role creation")
                    return state["assignment"]
                self.assertEqual(method, "GET")
                if not state["assigned"]:
                    raise deploy.CliFailure("RoleAssignmentNotFound")
                return state["assignment"]

            def search(method, path, body=None, create_only=False):
                if path.endswith("?$select=name"):
                    collection = path.split("?")[0]
                    names = [key.split("/")[1] for key in created if key.startswith(collection + "/")]
                    if mode == "collision" and collection == "indexes":
                        names.append(deploy.INDEX)
                    return {"value": [{"name": name} for name in names]}
                if method == "PUT":
                    self.assertTrue(create_only)
                    created[path] = copy.deepcopy(body)
                    return created[path]
                if path.endswith("/docs/search.index"):
                    if mode == "upload-error":
                        raise deploy.CliFailure("BadRequest")
                    return {"value": [{"key": "one", "status": mode != "partial",
                                       "statusCode": 201 if mode != "partial" else 400}]}
                if path.endswith("/stats"):
                    return {"documentCount": 1}
                return created[path]

            with patch.object(deploy, "prepare", return_value=bundle), \
                 patch.object(deploy, "cli", side_effect=cli), \
                 patch.object(deploy, "rest", side_effect=rest), \
                 patch.object(deploy, "search", side_effect=search):
                if mode == "success":
                    result = deploy.deploy(manifest, evidence, delay=lambda _: None)
                    self.assertEqual(result["uploadedChunks"], 1)
                else:
                    with self.assertRaises((ValueError, deploy.CliFailure)):
                        deploy.deploy(manifest, evidence, delay=lambda _: None)
                self.assertEqual(state["assigned"], mode == "cleanup-error")
                if mode in ("success", "upload-error", "partial", "ambiguous-role-create"):
                    self.assertEqual(state["deletes"], 1)
                    self.assertTrue(json.loads(evidence.read_text())["cleanupVerifiedAbsent"])
                if mode == "unsupported-scope":
                    self.assertEqual(state["deletes"], 0)
                    result = json.loads(evidence.read_text())
                    self.assertTrue(result["cleanupVerifiedAbsent"])
                    self.assertEqual(result["uploadedChunks"], 0)
                if mode == "cleanup-error":
                    result = json.loads(evidence.read_text())
                    self.assertEqual(result["status"], "cleanup-failed")
                    self.assertIn("AuthorizationFailed", result["cleanupFailure"])
                    self.assertFalse(result["cleanupVerifiedAbsent"])
                if mode == "collision":
                    self.assertFalse(created)
                    self.assertFalse(evidence.exists())

    def test_success_removes_only_the_new_assignment(self):
        self.run_case("success")

    def test_upload_failure_and_partial_actions_always_clean_up(self):
        for mode in ("upload-error", "partial"):
            with self.subTest(mode=mode):
                self.run_case(mode)

    def test_unsupported_index_scope_stops_without_widening(self):
        self.run_case("unsupported-scope")

    def test_name_collision_prevents_all_writes(self):
        self.run_case("collision")

    def test_ambiguous_role_create_still_cleans_up_exact_id(self):
        self.run_case("ambiguous-role-create")

    def test_cleanup_failure_is_explicit_and_persisted(self):
        self.run_case("cleanup-error")

    def test_search_query_separator_preserves_api_version(self):
        with patch.object(deploy, "rest", return_value={}) as rest:
            deploy.search("GET", "indexes?$select=name")
            self.assertEqual(rest.call_args.args[1],
                             deploy.ENDPOINT + "/indexes?$select=name&api-version=" + deploy.VERSION)


if __name__ == "__main__":
    unittest.main()
