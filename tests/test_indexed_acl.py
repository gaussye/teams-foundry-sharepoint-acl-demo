import copy
import base64
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


SPEC = importlib.util.spec_from_file_location(
    "indexed_acl", Path(__file__).resolve().parents[1] / "scripts" / "indexed_acl.py")
acl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acl)
NOW = datetime(2026, 10, 25, 6, tzinfo=timezone.utc)
SITE = "https://example.sharepoint.com/sites/test"
GROUP = "11111111-1111-4111-8111-111111111111"
OWNER_GROUP = "22222222-2222-4222-8222-222222222222"
OWNER = "33333333-3333-4333-8333-333333333333"
CLAIM = f"c:0o.c|federateddirectoryclaimprovider|{OWNER_GROUP}_o"


def cap(url, body):
    return {"url": url, "observedAt": NOW.isoformat(), "body": body}


def fixture(directory):
    system = {"Id": 1073741823, "PrincipalType": 1, "LoginName": "SHAREPOINT\\system"}
    owner = {"Id": 6, "PrincipalType": 4, "LoginName": CLAIM}
    snapshot = {
        "version": 1, "siteUrl": SITE, "adminLogin": "i:0#.f|membership|admin@example.test",
        "currentUser": cap(SITE + "/_api/web/currentuser?$select=LoginName,IsSiteAdmin", {
            "LoginName": "i:0#.f|membership|admin@example.test", "IsSiteAdmin": True}),
        "siteAdmins": cap(SITE + "/_api/web/siteusers?$filter=IsSiteAdmin eq true",
                         [{"IsSiteAdmin": True, **owner}]),
        "spGroups": {"3": cap(SITE + "/_api/web/sitegroups/getbyid(3)/users",
                              {"value": [system, owner]})},
        "owners": {OWNER_GROUP: cap(
            f"https://graph.microsoft.com/v1.0/groups/{OWNER_GROUP}/owners",
            {"value": [{"id": OWNER, "@odata.type": "#microsoft.graph.user",
                        "userPrincipalName": "admin@example.test"}]})},
        "entraGroups": {GROUP: cap(
            f"https://graph.microsoft.com/v1.0/groups/{GROUP}?$select=id,securityEnabled,groupTypes",
            {"id": GROUP, "securityEnabled": True, "groupTypes": []})},
        "documents": [],
    }
    for number, (library, filename) in enumerate(acl.FILES.items(), 1):
        path = directory / filename
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", (
                '<w:document xmlns:w="' + acl.NS["w"] + '"><w:body><w:p><w:r><w:t>'
                + ("Synthetic evidence " * 180)
                + '</w:t></w:r></w:p></w:body></w:document>'))
        data = path.read_bytes()
        list_id = f"00000000-0000-4000-8000-{number:012d}"
        prefix = SITE + f"/_api/web/lists(guid'{list_id}')"
        grants = []
        for member, role in (
            ({"Id": 3, "PrincipalType": 8, "LoginName": "test Owners"}, "Full Control"),
            ({"Id": 12, "PrincipalType": 4, "LoginName": f"c:0t.c|tenant|{GROUP}"}, "Read"),
        ):
            high, low = acl.ROLES[role]
            grants.append({"Member": member, "RoleDefinitionBindings": [{
                "Name": role, "BasePermissions": {"High": high, "Low": low}}]})
        lib = cap(prefix + "?$expand=RoleAssignments", {
            "Title": library, "HasUniqueRoleAssignments": True, "RoleAssignments": grants})
        item = cap(prefix + "/items(1)?$expand=File", {
            "Id": 1, "FileRef": f"/sites/test/{library}/{filename}",
            "FileDirRef": f"/sites/test/{library}", "HasUniqueRoleAssignments": False,
            "File": {"ETag": '"1"', "UniqueId": list_id, "Length": str(len(data))}})
        snapshot["documents"].append({
            "library": library, "listId": list_id,
            "libraryBefore": lib, "libraryAfter": copy.deepcopy(lib),
            "itemBefore": item, "itemAfter": copy.deepcopy(item),
            "permissions": cap(
                f"https://graph.microsoft.com/v1.0/sites/site/lists/{list_id}/items/1/driveItem/permissions?",
                {"value": [{"roles": ["read"], "grantedToV2": {"group": {"id": GROUP}}},
                           {"roles": ["owner"], "grantedToV2": {"siteGroup": {"id": "3"}}}]}),
            "contentFile": filename, "contentSha256": hashlib.sha256(data).hexdigest(),
        })
    return snapshot


class IndexedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.snapshot = fixture(self.directory)

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self):
        return acl.prepare(self.snapshot, self.directory, NOW)

    def test_chunks_inherit_real_acl_and_owner_is_not_group(self):
        result = self.prepare()
        self.assertEqual(len(result["documents"]["value"]), 6)
        for chunk in result["documents"]["value"]:
            self.assertEqual(chunk["groupIds"], [GROUP])
            self.assertEqual(chunk["userIds"], [OWNER])
            self.assertNotIn(OWNER_GROUP, chunk["groupIds"])
            self.assertNotIn("all", json.dumps(chunk))
            self.assertEqual(len(chunk["snapshotSha256"]), 64)
        self.assertTrue(all(a["events"] == [
            {"principal": "SHAREPOINT\\system", "action": "no-Entra-query-grant"}
        ] for a in result["audit"]))

    def test_schema_is_permission_enforced_and_kb_is_isolated(self):
        result = self.prepare()
        self.assertEqual(result["index"]["permissionFilterOption"], "enabled")
        fields = {f["name"]: f for f in result["index"]["fields"]}
        for field in ("userIds", "groupIds"):
            self.assertTrue(fields[field]["filterable"])
            self.assertEqual(fields[field]["permissionFilter"], field)
        self.assertEqual(result["knowledgeBase"]["knowledgeSources"], [{"name": acl.SOURCE}])
        self.assertNotIn("models", result["knowledgeBase"])

    def test_modern_sharepoint_group_requires_matching_paired_identity(self):
        collection, web = GROUP, OWNER_GROUP
        for document in self.snapshot["documents"]:
            record = document["permissions"]
            record["url"] = record["url"].replace(
                "/sites/site/", f"/sites/example.sharepoint.com,{collection},{web}/")
            record["body"]["value"][1]["grantedToV2"]["sharePointGroup"] = {
                "principalId": "3",
                "id": base64.urlsafe_b64encode(f"{collection}_3".encode()).decode().rstrip("="),
                "title": "test Owners",
            }
        self.assertEqual(len(self.prepare()["documents"]["value"]), 6)
        original = copy.deepcopy(self.snapshot)
        for name, mutate in {
            "wrong-principal": lambda i: i["sharePointGroup"].update(principalId="4"),
            "wrong-site": lambda i: i["sharePointGroup"].update(id="other-site"),
            "unpaired": lambda i: i.pop("siteGroup"),
            "unknown-type": lambda i: i.update(unsupportedIdentity={}),
        }.items():
            with self.subTest(name=name):
                self.snapshot = copy.deepcopy(original)
                mutate(self.snapshot["documents"][0]["permissions"]["body"]["value"][1]["grantedToV2"])
                with self.assertRaises(ValueError):
                    self.prepare()

    def test_rejects_bad_captures(self):
        cases = {
            "summary": lambda s: s.pop("version"),
            "wrong-user": lambda s: s["currentUser"]["body"].update(IsSiteAdmin=False),
            "stale": lambda s: s["currentUser"].update(observedAt="2026-10-24T00:00:00Z"),
            "future": lambda s: s["currentUser"].update(observedAt="2026-10-26T00:00:00Z"),
            "naive": lambda s: s["currentUser"].update(observedAt="2026-10-25T06:00:00"),
            "missing-doc": lambda s: s["documents"].pop(),
            "paged-owners": lambda s: s["owners"][OWNER_GROUP]["body"].update(
                {"@odata.nextLink": "next"}),
            "unknown-owner": lambda s: s["owners"][OWNER_GROUP]["body"]["value"][0].update(
                {"@odata.type": "#microsoft.graph.servicePrincipal"}),
            "wrong-group": lambda s: s["entraGroups"][GROUP]["body"].update(securityEnabled=False),
            "unknown-principal": lambda s: s["spGroups"]["3"]["body"]["value"].append(
                {"Id": 77, "PrincipalType": 4, "LoginName": "unknown"}),
            "spoof-system": lambda s: s["spGroups"]["3"]["body"]["value"][0].update(Id=8),
            "cycle": lambda s: s["spGroups"]["3"]["body"]["value"].append(
                {"Id": 3, "PrincipalType": 8, "LoginName": "loop"}),
        }
        original = copy.deepcopy(self.snapshot)
        for name, mutate in cases.items():
            with self.subTest(name=name):
                self.snapshot = copy.deepcopy(original)
                mutate(self.snapshot)
                with self.assertRaises((ValueError, KeyError)):
                    self.prepare()

    def test_rejects_document_changes_or_unsupported_grants(self):
        original = copy.deepcopy(self.snapshot)
        cases = {
            "version": lambda d: d["itemAfter"]["body"]["File"].update(ETag='"2"'),
            "unique": lambda d: d["itemBefore"]["body"].update(HasUniqueRoleAssignments=True),
            "wrong-file": lambda d: d["itemBefore"]["body"].update(FileRef="/other"),
            "hash": lambda d: d.update(contentSha256="0" * 64),
            "path": lambda d: d.update(contentFile="..\\outside.docx"),
            "link": lambda d: d["permissions"]["body"]["value"][0].update(link={}),
            "unknown-graph": lambda d: d["permissions"]["body"]["value"][0].update(
                grantedToV2={"group": {"id": OWNER_GROUP}}),
            "paged": lambda d: d["permissions"]["body"].update({"@odata.nextLink": "next"}),
            "acl-change": lambda d: d["libraryAfter"]["body"]["RoleAssignments"].pop(),
            "custom-role": lambda d: d["libraryBefore"]["body"]["RoleAssignments"][0][
                "RoleDefinitionBindings"][0]["BasePermissions"].update(Low="1"),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                self.snapshot = copy.deepcopy(original)
                mutate(self.snapshot["documents"][2])
                with self.assertRaises((ValueError, KeyError)):
                    self.prepare()

    def test_atom_and_verbose_json_and_deferred_rejection(self):
        xml = ('<feed xmlns="' + acl.NS["a"] + '" xmlns:m="' + acl.NS["m"]
               + '" xmlns:d="' + acl.NS["d"] + '"><entry><content><m:properties>'
               '<d:Id m:type="Edm.Int32">3</d:Id>'
               '<d:IsSiteAdmin m:type="Edm.Boolean">true</d:IsSiteAdmin>'
               '</m:properties></content></entry></feed>')
        self.assertEqual(acl.unwrap(xml), [{"Id": 3, "IsSiteAdmin": True}])
        self.assertEqual(acl.rows({"d": {"results": [{"Id": 3}]}}), [{"Id": 3}])
        with self.assertRaises(ValueError):
            acl.unwrap(xml.replace("</feed>", '<link rel="next" href="next"/></feed>'))
        with self.assertRaises(ValueError):
            acl.unwrap({"d": {"RoleAssignments": {"__deferred": {"uri": "more"}}}})

    def test_empty_and_revision_docx_rejected(self):
        for body in ("", "<w:del><w:r><w:t>deleted</w:t></w:r></w:del>"):
            path = self.directory / "unsupported.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml",
                                f'<w:document xmlns:w="{acl.NS["w"]}">{body}</w:document>')
            with self.assertRaises(ValueError):
                acl.docx_text(path)

    def test_cli_writes_only_complete_bundle_and_refuses_overwrite(self):
        def refresh(value):
            if isinstance(value, dict):
                if "observedAt" in value:
                    value["observedAt"] = datetime.now(timezone.utc).isoformat()
                for child in value.values():
                    refresh(child)
            elif isinstance(value, list):
                for child in value:
                    refresh(child)

        refresh(self.snapshot)
        manifest = self.directory / "manifest.json"
        output = self.directory / "bundle.json"
        manifest.write_text(json.dumps(self.snapshot), encoding="utf-8")
        command = [sys.executable, "-B", str(Path(acl.__file__)), str(manifest), str(output)]
        first = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        original = output.read_bytes()
        self.assertEqual(len(json.loads(original)["documents"]["value"]), 6)
        again = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("Refusing to overwrite", again.stderr)
        self.assertEqual(output.read_bytes(), original)
        self.snapshot["documents"][-1]["contentSha256"] = "wrong"
        manifest.write_text(json.dumps(self.snapshot), encoding="utf-8")
        failed_output = self.directory / "failed.json"
        command[-1] = str(failed_output)
        failed = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(failed_output.exists())


if __name__ == "__main__":
    unittest.main()
