"""Execute a single approved native-chart stage; no retries, consent, keys or model fallback."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import time
from urllib.parse import quote
from urllib.request import Request, urlopen
import uuid

from indexed_agent_deploy import PROJECT_ID
from indexed_verify import matches
from native_chart_demo import (
    FOLDER, INDEX, KS, LIBRARIES, NATIVE_KB, PREFIX, configuration, knowledge_base_update,
)
from native_indexer_experiment import (
    ADMIN, ENDPOINT, GRAPH, LOGIN, MI, ROLE, SITE, SUBSCRIPTION, cli, request,
    VERSION, require, search, status_evidence,
)

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / ".azure" / "indexed"
GROUPS = {
    "finance": ("e22982f5-cd5d-457f-a313-8495825a6103",
                "cb1985cb-b3aa-49ee-8601-a159e2f6406f"),
    "hr": ("534ff5e2-de44-44bd-803b-787f1b3a3bf1",
           "373e6941-bcf1-4392-889f-a69611797bc1"),
}
PREFLIGHT = ROOT / ".azure" / "native-charts-preflight-20261125.json"
FIXTURES = ROOT / "tests" / "fixtures" / "native-charts"


def verify_identity():
    account = cli("account", "show")
    require(account["id"] == SUBSCRIPTION and account["user"]["name"].lower() == LOGIN.lower(),
            "Wrong admin/subscription")
    require(request("GET", GRAPH + "me?$select=id")["id"] == ADMIN, "Wrong Graph identity")


def permission_signature(response):
    require(not response.get("@odata.nextLink"), "Incomplete source ACL")
    return sorted(json.dumps({"identity": p["grantedToV2"], "roles": p["roles"]},
                             sort_keys=True) for p in response["value"])


def verify_root_acl(department, permissions):
    group_id, user_id = GROUPS[department]
    grants = permissions["value"]
    require(len(grants) == 3 and not permissions.get("@odata.nextLink"),
            "Source ACL shape changed; inspect it")
    require(any(p["grantedToV2"].get("group", {}).get("id") == group_id
                and p["roles"] == ["read"] for p in grants), "Expected source Read grant absent")
    for grant in grants:
        identity = grant["grantedToV2"]
        allowed = (
            identity.get("group", {}).get("id") == group_id and grant["roles"] == ["read"]
        ) or (
            identity.get("siteGroup", {}).get("id") == "3" and grant["roles"] == ["owner"]
        ) or (
            identity.get("siteUser", {}).get("loginName") ==
            "c:0o.c|federateddirectoryclaimprovider|f759edb3-b372-4617-8087-b12c053264a5_o"
            and grant["roles"] == ["owner"]
        )
        require(allowed and not grant.get("link"), "Unknown or broadened source grant")
    members = request("GET", GRAPH + f"groups/{group_id}/members?$select=id")
    require(not members.get("@odata.nextLink")
            and {m["id"] for m in members["value"]} == {user_id},
            "Source content group membership changed")


class Journal:
    def __init__(self, stage, resume_readonly_preflight=False,
                 resume_skill_expression_rejection=False):
        self.path = EVIDENCE / f"native-charts-{stage}-20261125.json"
        previous = None
        require(not (resume_readonly_preflight and resume_skill_expression_rejection),
                "Recovery modes cannot be combined")
        if resume_skill_expression_rejection:
            require(stage == "deploy" and self.path.exists(), "Only exact deployment may resume")
            previous = json.loads(self.path.read_text())
            require(previous["status"] == "failed-no-automatic-retry"
                    and [a["label"] for a in previous["attempts"]] == [
                        "create-indexes/" + INDEX, "create-skillsets/" + PREFIX + "-skillset"]
                    and [x["collection"] for x in previous["created"]] == ["indexes"]
                    and "Input 'userMessage' source is an invalid expression" in previous["failure"],
                    "Only recorded rejected skill expression with one Index created may resume")
        elif resume_readonly_preflight:
            require(stage == "upload" and self.path.exists(), "Only upload preflight may resume")
            previous = json.loads(self.path.read_text())
            require(previous["status"] == "failed-no-automatic-retry"
                    and previous["attempts"] == [] and previous.get("folders") == []
                    and previous.get("files") == [] and not previous.get("previousReadonlyFailure")
                    and "RemoteDisconnected" in previous.get("failure", ""),
                    "Only one recorded transport failure before any write may resume")
        else:
            require(not self.path.exists(), "Stage evidence exists; inspect it, never rerun blindly")
        self.value = {"stage": stage, "startedAt": datetime.now(timezone.utc).isoformat(),
                      "status": "in-progress", "attempts": []}
        if previous is not None:
            key = "previousRejectedDeployment" if resume_skill_expression_rejection else "previousReadonlyFailure"
            self.value[key] = previous
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.value, indent=2), encoding="utf-8")

    def attempt(self, label):
        self.value["attempts"].append(
            {"label": label, "at": datetime.now(timezone.utc).isoformat()})
        self.save()


def upload(journal):
    manifest = json.loads((FIXTURES / "expected.json").read_text())
    require(len(manifest["files"]) == 4, "Expected exactly four fixtures")
    preflight = json.loads(PREFLIGHT.read_text())
    journal.value["folders"], journal.value["files"] = [], []
    for source in preflight["sources"]:
        department, drive = source["department"], source["drive"]["id"]
        require(source["drive"]["name"] == LIBRARIES[department], "Unexpected source library")
        base = GRAPH + "drives/" + drive
        root_acl = request("GET", base + "/root/permissions")
        verify_root_acl(department, root_acl)
        children = request("GET", base + "/root/children?$select=id,name&$top=100")
        require(not children.get("@odata.nextLink")
                and not any(x["name"] == FOLDER for x in children["value"]),
                "Folder already exists or listing incomplete; stop before creating")
        journal.attempt("create-folder-" + department)
        folder = request("POST", base + "/root/children", {
            "name": FOLDER, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})
        journal.value["folders"].append({
            "department": department, "driveId": drive, "id": folder["id"],
            "webUrl": folder["webUrl"], "sourceAcl": root_acl})
        journal.save()
        folder_acl = request("GET", base + f"/items/{folder['id']}/permissions")
        require(permission_signature(folder_acl) == permission_signature(root_acl),
                "New folder effective ACL differs; no permission modification allowed")
        for item in [x for x in manifest["files"] if x["department"] == department]:
            path = FIXTURES / item["name"]
            payload = path.read_bytes()
            require(sha256(payload).hexdigest() == item["sha256"]
                    and len(payload) == item["bytes"], "Fixture integrity changed")
            token = cli("account", "get-access-token",
                        "--resource", "https://graph.microsoft.com")["accessToken"]
            url = base + f"/items/{folder['id']}:/{quote(item['name'], safe='')}:/content"
            url += "?@microsoft.graph.conflictBehavior=fail"
            journal.attempt("upload-" + item["name"])
            operation = Request(url, method="PUT", data=payload, headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/pdf" if item["format"] == "pdf" else "image/png"})
            with urlopen(operation, timeout=120) as response:
                require(response.status == 201, "Upload did not create a new item")
                result = json.load(response)
            saved = {"name": result["name"], "id": result["id"], "webUrl": result["webUrl"],
                     "size": result["size"], "department": department, "format": item["format"],
                     "driveId": drive, "sourceSha256": item["sha256"]}
            journal.value["files"].append(saved)
            journal.save()
            require(saved["name"] == item["name"] and saved["size"] == len(payload),
                    "Upload metadata mismatch")
            saved["permissions"] = request("GET", base + f"/items/{saved['id']}/permissions")
            require(permission_signature(saved["permissions"]) == permission_signature(root_acl),
                    "Uploaded item ACL differs from the approved library")
            saved["aclMatchesSource"] = True
            journal.save()
        items = request("GET", base + f"/items/{folder['id']}/children?$select=id,name,size")
        expected = {f["name"] for f in manifest["files"] if f["department"] == department}
        require(not items.get("@odata.nextLink")
                and {x["name"] for x in items["value"]} == expected,
                "Folder contains unexpected source content")


def deploy(journal):
    uploaded = json.loads((EVIDENCE / "native-charts-upload-20261125.json").read_text())
    require(uploaded["status"] == "success" and len(uploaded["files"]) == 4
            and all(f["aclMatchesSource"] for f in uploaded["files"]), "Source stage incomplete")
    config = configuration(include_png_probe=True)
    recovered = journal.value.get("previousRejectedDeployment")
    for collection in ("indexes", "skillsets", "datasources", "indexers", "knowledgesources"):
        listing = search("GET", collection + "?$select=name")
        allowed = {INDEX} if recovered and collection == "indexes" else set()
        require(not listing.get("@odata.nextLink")
                and {x["name"] for x in listing["value"] if x["name"].startswith(PREFIX)} == allowed,
                "Native chart resource collision; stop rather than overwrite")
    if recovered:
        matches(search("GET", "indexes/" + INDEX), config["index"])
    scope = PROJECT_ID.split("/projects/")[0]
    roles = cli("role", "assignment", "list", "--scope", scope, "--include-inherited",
                "--subscription", SUBSCRIPTION)
    require(any(r["principalId"] == MI
                and r["roleDefinitionName"] == "Cognitive Services OpenAI User"
                for r in roles), "Search MI model role not present; separate precise role step required")
    journal.value["created"] = deepcopy(recovered["created"]) if recovered else []
    for collection, definitions in (
        ("indexes", [config["index"]]), ("skillsets", [config["skillset"]]),
        ("datasources", config["datasources"]), ("indexers", config["indexers"]),
    ):
        for definition in definitions:
            if recovered and collection == "indexes":
                continue
            path = collection + "/" + definition["name"]
            journal.attempt("create-" + path)
            search("PUT", path, definition)
            actual = search("GET", path)
            require(actual["name"] == definition["name"], "Created object readback mismatch")
            if collection == "indexers":
                require(actual["disabled"] is True and not actual.get("schedule"),
                        "Indexer must remain disabled before approved bounded run")
            journal.value["created"].append({"collection": collection, "definition": actual})
            journal.save()


def conditional_search_put(path, body, etag):
    require(isinstance(etag, str) and etag and "\n" not in etag and "\r" not in etag,
            "Fresh valid ETag required")
    with tempfile.TemporaryDirectory(prefix="native-chart-update-") as directory:
        payload = Path(directory) / "body.json"
        payload.write_text(json.dumps(body), encoding="utf-8")
        return cli("rest", "--method", "PUT",
                   "--url", f"{ENDPOINT}/{path}?api-version={VERSION}",
                   "--resource", "https://search.azure.com", "--headers", "If-Match=" + etag,
                   "--body", "@" + str(payload))


def repair_run(journal):
    previous = json.loads((EVIDENCE / "native-charts-run-20261125.json").read_text())
    require(previous["status"] == "failed-no-automatic-retry"
            and previous["cleanupVerifiedAbsent"] is True and len(previous["runs"]) == 1,
            "Only the recorded first chart run failure may be repaired")
    failed = previous["runs"][0]
    last = failed["observed"]["lastResult"]
    require(failed["name"] == PREFIX + "-finance-pdf-indexer"
            and "parentId' is not a retrievable field" in last["errorMessage"]
            and last["itemsProcessed"] == 0 and last["itemsFailed"] == 0,
            "Unexpected failure; no approved extra-run recovery applies")
    expected = configuration(include_png_probe=True)
    baselines = {}
    for definition in expected["indexers"]:
        actual = search("GET", "indexers/" + definition["name"])
        state = search("GET", "indexers/" + definition["name"] + "/status")
        count = 1 if definition["name"] == failed["name"] else 0
        require(actual["disabled"] is True and len(state.get("executionHistory", [])) == count,
                "Indexer state changed since the specific repair approval")
        matches(actual, definition)
        if count:
            require(state["lastResult"]["startTime"] == last["startTime"],
                    "Another execution appeared; no more runs allowed")
        baselines[definition["name"]] = count
    require(search("GET", "indexes/" + INDEX + "/stats")["documentCount"] == 0,
            "Chart records appeared; inspect rather than repair blindly")
    actual = search("GET", "indexes/" + INDEX)
    before_expected = deepcopy(expected["index"])
    next(f for f in before_expected["fields"] if f["name"] == "parentId")["retrievable"] = False
    matches(actual, before_expected)
    updated = deepcopy(actual)
    etag = updated.pop("@odata.etag")
    updated.pop("@odata.context", None)
    next(f for f in updated["fields"] if f["name"] == "parentId")["retrievable"] = True
    journal.value["indexBefore"] = actual
    journal.attempt("repair-only-parentId-retrievable")
    conditional_search_put("indexes/" + INDEX, updated, etag)
    readback = search("GET", "indexes/" + INDEX)
    matches(readback, expected["index"])
    journal.value["indexAfter"] = readback
    journal.save()
    run(journal, baselines=baselines)


def run(journal, baselines=None):
    deployment = json.loads((EVIDENCE / "native-charts-deploy-20261125.json").read_text())
    require(deployment["status"] == "success", "Deployment stage incomplete")
    config = configuration(include_png_probe=True)
    scope = (f"/subscriptions/{SUBSCRIPTION}/resourceGroups/aks-test/providers/"
             f"Microsoft.Search/searchServices/search-service-ypy2/indexes/{INDEX}")
    assignment = scope + "/providers/Microsoft.Authorization/roleAssignments/" + str(uuid.uuid4())
    journal.value.update({"temporaryAssignment": assignment, "runs": [],
                          "cleanupVerifiedAbsent": False})
    journal.save()
    current = None
    try:
        journal.attempt("create-single-chart-index-temporary-role")
        result = request("PUT", "https://management.azure.com" + assignment
                         + "?api-version=2022-04-01", {"properties": {
                             "principalId": ADMIN, "principalType": "User",
                             "roleDefinitionId": f"/subscriptions/{SUBSCRIPTION}/providers/"
                             "Microsoft.Authorization/roleDefinitions/" + ROLE}})
        require(result["properties"]["scope"].lower() == scope.lower(), "Role scope mismatch")
        for definition in config["indexers"]:
            name = definition["name"]
            actual = search("GET", "indexers/" + name)
            history = search("GET", "indexers/" + name + "/status")
            require(actual["disabled"] is True and actual["targetIndexName"] == INDEX
                    and actual["skillsetName"] == config["skillset"]["name"],
                    "Indexer drift before run")
            baseline = 0 if baselines is None else baselines[name]
            require(len(history.get("executionHistory", [])) == baseline
                    and (baseline != 0 or not history.get("lastResult")),
                    "Indexer execution history changed; no automatic retry")
            previous_start = (history.get("lastResult") or {}).get("startTime")
            current = deepcopy(actual)
            for key in ("@odata.context", "@odata.etag"):
                current.pop(key, None)
            current["disabled"] = False
            journal.attempt("enable-once-" + name)
            search("PUT", "indexers/" + name, current)
            entry = {"name": name, "status": "waiting-for-initial-execution"}
            journal.value["runs"].append(entry)
            journal.save()
            if baseline:
                # Completed initial runs need an on-demand run; never reset or duplicate an observed run.
                after_enable = search("GET", "indexers/" + name + "/status")
                if (after_enable.get("lastResult") or {}).get("startTime") == previous_start:
                    journal.attempt("approved-single-extra-run-" + name)
                    search("POST", "indexers/" + name + "/run")
                    entry["explicitRunSubmitted"] = True
                    journal.save()
            deadline = time.monotonic() + 420
            while True:
                state = search("GET", "indexers/" + name + "/status")
                entry["observed"] = status_evidence(state)
                journal.save()
                last = state.get("lastResult")
                if last and last.get("endTime") and last.get("startTime") != previous_start:
                    break
                require(time.monotonic() < deadline, "Run observation deadline; no re-run allowed")
                time.sleep(8)
            current["disabled"] = True
            journal.attempt("disable-after-run-" + name)
            search("PUT", "indexers/" + name, current)
            require(search("GET", "indexers/" + name)["disabled"] is True,
                    "Indexer disabling not verified")
            current = None
            entry["status"] = last["status"]
            journal.save()
            require(len(state.get("executionHistory", [])) == baseline + 1,
                    "Unexpected execution count")
            # A rejected PNG is a compatibility result, not a reason to run it again.
            if last["status"] != "success" or last["itemsFailed"]:
                entry["compatibilityPassed"] = False
                require("-png-" in name, "PDF enrichment failed; stop without further paid work")
            else:
                entry["compatibilityPassed"] = last["itemsProcessed"] == 1
            journal.save()
        journal.value["stats"] = search("GET", "indexes/" + INDEX + "/stats")
    finally:
        cleanup_errors = []
        if current is not None:
            try:
                current["disabled"] = True
                journal.attempt("finally-disable-active-indexer")
                search("PUT", "indexers/" + current["name"], current)
                require(search("GET", "indexers/" + current["name"])["disabled"] is True,
                        "Finally disabling not verified")
            except Exception as error:
                cleanup_errors.append("Indexer disable: " + str(error))
        try:
            remaining = cli("role", "assignment", "list", "--scope", scope,
                            "--subscription", SUBSCRIPTION)
            if any(r["id"].lower() == assignment.lower() for r in remaining):
                journal.attempt("delete-exact-chart-index-temporary-role")
                request("DELETE", "https://management.azure.com" + assignment
                        + "?api-version=2022-04-01")
            remaining = cli("role", "assignment", "list", "--scope", scope,
                            "--subscription", SUBSCRIPTION)
            require(not any(r["id"].lower() == assignment.lower() for r in remaining),
                    "Temporary role remains")
            journal.value["cleanupVerifiedAbsent"] = True
        except Exception as error:
            cleanup_errors.append("Temporary role cleanup: " + str(error))
        journal.value["cleanupErrors"] = cleanup_errors
        journal.save()
        require(not cleanup_errors, "Cleanup failed: " + "; ".join(cleanup_errors))


def attach(journal):
    executed = json.loads((EVIDENCE / "native-charts-repair-run-20261125.json").read_text())
    require(executed["status"] == "success" and executed["cleanupVerifiedAbsent"] is True
            and len(executed["runs"]) == 4
            and all(r["compatibilityPassed"] for r in executed["runs"]),
            "All four sources must pass ingestion before changing native retrieval")
    config = configuration(include_png_probe=True)
    for definition in config["indexers"]:
        actual = search("GET", "indexers/" + definition["name"])
        require(actual["disabled"] is True, "Chart indexer still enabled")
    deadline = time.monotonic() + 60
    while True:
        stats = search("GET", "indexes/" + INDEX + "/stats")
        if stats["documentCount"] == 4:
            break
        require(stats["documentCount"] < 4 and time.monotonic() < deadline,
                "Expected four image records did not appear; do not attach incomplete evidence")
        time.sleep(5)
    journal.value["stats"] = stats
    inventory = search("GET", "knowledgesources?$select=name")
    require(not inventory.get("@odata.nextLink")
            and not any(item["name"] == KS for item in inventory["value"]), "KS collision")
    journal.attempt("create-native-chart-ks")
    search("PUT", "knowledgesources/" + KS, config["knowledgeSource"])
    readback = search("GET", "knowledgesources/" + KS)
    matches(readback, config["knowledgeSource"])
    journal.value["knowledgeSource"] = readback
    existing = search("GET", "knowledgebases/" + NATIVE_KB)
    update = knowledge_base_update(existing)
    journal.value["knowledgeBaseBefore"] = existing
    journal.attempt("append-chart-ks-to-existing-native-kb")
    conditional_search_put("knowledgebases/" + NATIVE_KB, update["body"], update["ifMatch"])
    actual = search("GET", "knowledgebases/" + NATIVE_KB)
    matches(actual, update["body"])
    journal.value["knowledgeBaseAfter"] = actual
    journal.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("upload", "deploy", "run", "repair-run", "attach"))
    parser.add_argument("--execute-approved", action="store_true")
    parser.add_argument("--resume-readonly-preflight", action="store_true")
    parser.add_argument("--resume-skill-expression-rejection", action="store_true")
    args = parser.parse_args()
    require(args.execute_approved, "Explicit approved execution is required")
    verify_identity()
    journal = Journal(args.stage, args.resume_readonly_preflight,
                      args.resume_skill_expression_rejection)
    try:
        {"upload": upload, "deploy": deploy, "run": run,
         "repair-run": repair_run, "attach": attach}[args.stage](journal)
        journal.value["status"] = "success"
    except Exception as error:
        journal.value.update({"status": "failed-no-automatic-retry",
                              "failure": str(error)})
        journal.save()
        raise
    journal.value["completedAt"] = datetime.now(timezone.utc).isoformat()
    journal.save()
    print(json.dumps({"status": journal.value["status"], "evidence": str(journal.path)}))


if __name__ == "__main__":
    main()
