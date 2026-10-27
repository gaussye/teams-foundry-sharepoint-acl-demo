"""Deploy only the approved Indexed phase-1 increment; never run Remote IaC."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from indexed_acl import INDEX, SOURCE, KB, prepare, require

SUBSCRIPTION = "3456866f-6478-471f-8d59-a29a335d797a"
ADMIN = "46832239-f08b-4172-96a2-59fd65cf193a"
LOGIN = "admin@MngEnvMCAP566860.onmicrosoft.com"
SERVICE = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/aks-test"
    "/providers/Microsoft.Search/searchServices/search-service-ypy2"
)
SCOPE = SERVICE + "/indexes/" + INDEX
ROLE = "8ebe5a00-799e-43f5-93ac-243d3dce84a7"
ENDPOINT = "https://search-service-ypy2.search.windows.net"
VERSION = "2026-08-01-preview"
OBJECTS = (("indexes", INDEX, "index"), ("knowledgesources", SOURCE, "knowledgeSource"),
           ("knowledgebases", KB, "knowledgeBase"))


class CliFailure(RuntimeError):
    pass


def cli(*arguments):
    result = subprocess.run(
        [sys.executable, "-IBm", "azure.cli", *arguments, "--only-show-errors", "-o", "json"],
        capture_output=True, text=True, encoding="utf-8")
    if result.returncode:
        raise CliFailure(result.stderr.strip())
    return json.loads(result.stdout) if result.stdout.strip() else None


def rest(method, url, body=None, create_only=False, resource=None):
    arguments = ["rest", "--method", method, "--url", url]
    if resource:
        arguments += ["--resource", resource]
    elif url.startswith(ENDPOINT + "/"):
        arguments += ["--resource", "https://search.azure.com"]
    if create_only:
        arguments += ["--headers", "If-None-Match=*"]
    if body is None:
        return cli(*arguments)
    with tempfile.TemporaryDirectory(prefix="indexed-request-") as directory:
        path = Path(directory) / "body.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        return cli(*arguments, "--body", "@" + str(path))


def search(method, path, body=None, create_only=False):
    separator = "&" if "?" in path else "?"
    return rest(method, f"{ENDPOINT}/{path}{separator}api-version={VERSION}", body, create_only)


def names(collection):
    response = search("GET", collection + "?$select=name")
    require(not any("nextLink" in key for key in response), "Paged Search inventory")
    return sorted(item["name"] for item in response["value"])


def role_absent(role_url):
    try:
        rest("GET", role_url)
    except CliFailure as error:
        if any(code in str(error) for code in ("RoleAssignmentNotFound", "ResourceNotFound")):
            return True
        raise
    return False


def deploy(manifest, evidence, delay=time.sleep):
    require(not evidence.exists(), "Refusing to overwrite deployment evidence")
    snapshot = json.loads(manifest.read_text(encoding="utf-8"))
    require(snapshot["siteUrl"] == "https://mngenvmcap566860.sharepoint.com/sites/test",
            "Source site is outside the approved deployment scope")
    bundle = prepare(snapshot, manifest.parent)
    account = cli("account", "show")
    require(account["id"] == SUBSCRIPTION and account["user"]["name"].lower() == LOGIN.lower(),
            "Wrong Azure context")
    user = rest("GET", "https://graph.microsoft.com/v1.0/me?$select=id,userPrincipalName")
    require(user["id"] == ADMIN and user["userPrincipalName"].lower() == LOGIN.lower(),
            "Wrong admin identity")
    baseline = {collection: names(collection) for collection, _, _ in OBJECTS}
    for collection, name, _ in OBJECTS:
        require(name not in baseline[collection], f"Object already exists; refusing overwrite: {name}")
    roles = cli("role", "assignment", "list", "--scope", SCOPE,
                "--subscription", SUBSCRIPTION)
    require(not any(r["scope"].lower() == SCOPE.lower()
                    and r["principalId"].lower() == ADMIN and r["roleDefinitionId"].endswith(ROLE)
                    for r in roles), "A pre-existing contributor assignment must not be replaced")
    assignment_id = SCOPE + "/providers/Microsoft.Authorization/roleAssignments/" + str(uuid.uuid4())
    role_url = "https://management.azure.com" + assignment_id + "?api-version=2022-04-01"
    result = {
        "status": "executing", "createdObjects": [], "attemptedObjects": [],
        "assignmentId": None, "assignmentScope": None, "attemptedAssignmentId": assignment_id,
        "cleanupVerifiedAbsent": False, "uploadedChunks": 0, "uploadActions": [],
        "modelCalls": 0, "retrievalCalls": 0, "teamsMessages": 0, "remoteWrites": 0,
        "inventoryBefore": baseline, "readback": {},
    }

    def save():
        evidence.write_text(json.dumps(result, indent=2), encoding="utf-8")

    with evidence.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    attempted_role = False
    try:
        for collection, name, key in OBJECTS:
            result["attemptedObjects"].append(name)
            save()
            search("PUT", f"{collection}/{name}", bundle[key], create_only=True)
            result["createdObjects"].append(name)
            actual = search("GET", f"{collection}/{name}")
            result["readback"][key] = actual
            if key == "index":
                require(actual["permissionFilterOption"] == "enabled", "ACL enforcement not enabled")
                fields = {field["name"]: field for field in actual["fields"]}
                for field in ("userIds", "groupIds"):
                    require(fields[field]["permissionFilter"] == field
                            and fields[field]["filterable"] is True
                            and fields[field]["retrievable"] is False, "ACL field readback mismatch")
            elif key == "knowledgeSource":
                require(actual["kind"] == "searchIndex"
                        and actual["searchIndexParameters"]["searchIndexName"] == INDEX,
                        "Knowledge source is not isolated to the new index")
            else:
                require([source["name"] for source in actual["knowledgeSources"]] == [SOURCE]
                        and not actual.get("models")
                        and actual["retrievalReasoningEffort"]["kind"] == "minimal"
                        and actual["outputMode"] == "extractiveData", "Unexpected KB configuration")
            save()
        require(role_absent(role_url), "Generated assignment ID unexpectedly exists")
        attempted_role = True
        rest("PUT", role_url, {"properties": {
            "roleDefinitionId": f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/{ROLE}",
            "principalId": ADMIN, "principalType": "User",
        }})
        assigned = rest("GET", role_url)
        require(assigned["id"].lower() == assignment_id.lower()
                and assigned["properties"]["scope"].lower() == SCOPE.lower()
                and assigned["properties"]["principalId"].lower() == ADMIN
                and assigned["properties"]["roleDefinitionId"].endswith(ROLE),
                "Temporary assignment readback mismatch")
        result["assignmentId"], result["assignmentScope"] = assignment_id, SCOPE
        save()
        for attempt in range(13):
            prepare(snapshot, manifest.parent)
            try:
                uploaded = search("POST", f"indexes/{INDEX}/docs/search.index", bundle["documents"])
                break
            except CliFailure as error:
                if "Forbidden" not in str(error) or attempt == 12:
                    raise
                delay(5)
        actions = uploaded["value"]
        result["uploadActions"] = actions
        expected = {document["id"] for document in bundle["documents"]["value"]}
        require(len(actions) == len(expected) and {a["key"] for a in actions} == expected
                and all(a["status"] is True and a["statusCode"] in (200, 201) for a in actions),
                "Upload contains missing or failed document actions")
        result["uploadedChunks"] = len(actions)
        for attempt in range(13):
            stats = search("GET", f"indexes/{INDEX}/stats")
            if stats["documentCount"] == len(expected):
                break
            delay(5)
        require(stats["documentCount"] == len(expected), "Indexed document count mismatch")
        result["indexStats"] = stats
        result["inventoryAfter"] = {collection: names(collection) for collection, _, _ in OBJECTS}
        for collection, name, _ in OBJECTS:
            require(result["inventoryAfter"][collection] == sorted(baseline[collection] + [name]),
                    "Unexpected concurrent object inventory change")
        result["status"] = "uploaded"
    except (CliFailure, ValueError, KeyError) as error:
        result["failure"] = str(error)
        raise
    finally:
        if result["status"] == "executing":
            result["status"] = "failed"
        try:
            if attempted_role:
                if not role_absent(role_url):
                    cli("role", "assignment", "delete", "--ids", assignment_id,
                        "--subscription", SUBSCRIPTION)
                require(role_absent(role_url), "Temporary assignment still exists")
                result["cleanupVerifiedAbsent"] = True
        except (CliFailure, ValueError) as error:
            result["status"] = "cleanup-failed"
            result["cleanupFailure"] = str(error)
            raise
        finally:
            result["completedAt"] = datetime.now(timezone.utc).isoformat()
            save()
    require(result["cleanupVerifiedAbsent"], "Cleanup has not been verified")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    result = deploy(args.manifest, args.evidence)
    print(json.dumps({key: result[key] for key in (
        "status", "createdObjects", "uploadedChunks", "assignmentId", "cleanupVerifiedAbsent")}))


if __name__ == "__main__":
    main()
