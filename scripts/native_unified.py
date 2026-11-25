"""Unify the existing native demo's text and image sources, without expanding access."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen

from indexed_verify import matches
from native_chart_demo import configuration as chart_configuration, KS as CHART_KS
from native_chart_live import conditional_search_put, permission_signature, verify_identity
from native_agent_deploy import KS as TEXT_KS, KB
from native_indexer_experiment import (
    ENDPOINT, GRAPH, LIBRARIES, SITE, SITE_ID, VERSION, cli, require, request, search,
    status_evidence,
)

PREFIX = "teams-acl-demo-sp-native-unified"
INDEX = PREFIX + "-index"
DS = PREFIX + "-ds"
INDEXER = PREFIX + "-indexer"
SKILLSET = PREFIX + "-skillset"
KS = PREFIX + "-ks"
LEGACY_INDEXERS = tuple(
    f"teams-acl-demo-sp-native-{suffix}-indexer" for suffix in ("public", "finance", "hr")
) + tuple(
    f"teams-acl-demo-sp-native-charts-{department}-{extension}-indexer"
    for department in ("finance", "hr") for extension in ("pdf", "png")
)
EVIDENCE = Path(__file__).resolve().parents[1] / ".azure" / "indexed"
EXPECTED_FILES = {
    "TeamsAclDemoPublic": {"ACLDEMO_PUBLIC_20261024_P731.docx"},
    "TeamsAclDemoFinance": {
        "ACLDEMO_FINANCE_20261024_F482.docx",
        "NativeCharts20261125/prism-finance-embedded.pdf",
        "NativeCharts20261125/prism-finance-standalone.png",
    },
    "TeamsAclDemoHR": {
        "ACLDEMO_HR_20261024_H965.docx",
        "NativeCharts20261125/prism-hr-embedded.pdf",
        "NativeCharts20261125/prism-hr-standalone.png",
    },
}


def configuration():
    chart = chart_configuration(include_png_probe=True)
    index = deepcopy(chart["index"])
    index["name"] = INDEX
    index["fields"] = [f for f in index["fields"]
                       if f["name"] not in ("parentId", "pageNumber", "contentOffset")]
    index["semantic"]["defaultConfiguration"] = "native-unified-semantic"
    index["semantic"]["configurations"][0]["name"] = "native-unified-semantic"
    describe = deepcopy(chart["skillset"]["skills"][0])
    merge = {
        "@odata.type": "#Microsoft.Skills.Text.MergeSkill",
        "name": "merge-text-and-image-descriptions", "context": "/document",
        "insertPreTag": "\n\n[AI-generated image description]\n",
        "insertPostTag": "\n[End image description]\n\n",
        "inputs": [
            {"name": "text", "source": "/document/content"},
            {"name": "itemsToInsert", "source": "/document/normalized_images/*/description"},
            {"name": "offsets", "source": "/document/normalized_images/*/contentOffset"},
        ],
        "outputs": [{"name": "mergedText", "targetName": "unifiedContent"}],
    }
    source = deepcopy(chart["datasources"][0])
    source["name"] = DS
    source["container"] = {
        "name": "useQuery",
        "query": ";".join("includeLibrary=" + SITE + "/" + library
                          for _, library, _, _ in LIBRARIES),
    }
    indexer = deepcopy(chart["indexers"][0])
    indexer.update(name=INDEXER, dataSourceName=DS, targetIndexName=INDEX,
                   skillsetName=SKILLSET, disabled=True)
    indexer["parameters"]["configuration"]["indexedFileNameExtensions"] = ".docx,.pdf,.png"
    indexer["fieldMappings"] += [
        {"sourceFieldName": "metadata_user_ids", "targetFieldName": "userIds"},
        {"sourceFieldName": "metadata_group_ids", "targetFieldName": "groupIds"},
        {"sourceFieldName": "metadata_spo_site_url", "targetFieldName": "SharePointSiteUrl"},
        {"sourceFieldName": "metadata_spo_item_path", "targetFieldName": "metadata_spo_item_path"},
        {"sourceFieldName": "metadata_spo_item_weburi", "targetFieldName": "sourceUrl"},
    ]
    indexer["outputFieldMappings"] = [
        {"sourceFieldName": "/document/unifiedContent", "targetFieldName": "content"}]
    ks = deepcopy(chart["knowledgeSource"])
    ks["name"] = KS
    ks["searchIndexParameters"]["searchIndexName"] = INDEX
    ks["searchIndexParameters"]["semanticConfigurationName"] = "native-unified-semantic"
    ks["searchIndexParameters"]["sourceDataFields"] = [
        f for f in ks["searchIndexParameters"]["sourceDataFields"] if f["name"] != "pageNumber"]
    return {
        "index": index,
        "skillset": {"name": SKILLSET, "description": "Original text plus image descriptions; source ACLs.",
                     "skills": [describe, merge]},
        "datasource": source, "indexer": indexer, "knowledgeSource": ks,
    }


def kb_replacement(actual):
    require(actual.get("name") == KB and actual.get("models") == []
            and actual.get("outputMode") == "extractiveData"
            and actual.get("retrievalReasoningEffort") == {"kind": "minimal"},
            "Native KB retrieval settings changed")
    sources = actual.get("knowledgeSources", [])
    require(len(sources) == 2 and {s["name"] for s in sources} == {TEXT_KS, CHART_KS},
            "Expected exactly the two known native sources before migration")
    require(isinstance(actual.get("@odata.etag"), str) and actual["@odata.etag"],
            "Fresh KB ETag required")
    body = deepcopy(actual)
    etag = body.pop("@odata.etag")
    body.pop("@odata.context", None)
    body["knowledgeSources"] = [{"name": KS}]
    return body, etag


def verify_definition(actual, expected, kind):
    desired = deepcopy(expected)
    if kind == "datasource":
        # Search deliberately omits connection strings from GET responses.
        require(actual.get("credentials", {}).get("connectionString") is None,
                "Unexpected datasource credential visibility")
        desired["credentials"]["connectionString"] = None
    matches(actual, desired)


class Journal:
    def __init__(self, stage):
        self.path = EVIDENCE / f"native-unified-{stage}-20261125.json"
        require(not self.path.exists(), "Existing stage evidence; no blind replay")
        self.value = {"status": "in-progress", "stage": stage,
                      "startedAt": datetime.now(timezone.utc).isoformat(), "attempts": []}
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.value, indent=2), encoding="utf-8")

    def attempt(self, operation):
        self.value["attempts"].append(
            {"operation": operation, "at": datetime.now(timezone.utc).isoformat()})
        self.save()


def preflight(journal):
    config = configuration()
    journal.value["before"] = {}
    for collection in ("indexes", "skillsets", "datasources", "indexers", "knowledgesources"):
        listing = search("GET", collection + "?$select=name")
        require(not listing.get("@odata.nextLink")
                and not any(x["name"].startswith(PREFIX) for x in listing["value"]),
                "Unified object collision or incomplete listing")
    journal.value["before"]["kb"] = search("GET", "knowledgebases/" + KB)
    kb_replacement(journal.value["before"]["kb"])
    journal.value["before"]["indexers"] = [
        search("GET", "indexers/" + name) for name in LEGACY_INDEXERS]
    journal.value["sources"] = []
    for _, library, list_id, _ in LIBRARIES:
        drive = request("GET", GRAPH + f"sites/{SITE_ID}/lists/{list_id}/drive?$select=id,name,webUrl")
        require(drive["name"] == library and drive["webUrl"].lower() ==
                (SITE + "/" + library).lower(), "Unexpected source library")
        base = GRAPH + "drives/" + drive["id"]
        permissions = request("GET", base + "/root/permissions")
        signature = permission_signature(permissions)
        pending = [("root", "")]
        found = []
        while pending:
            item_id, prefix = pending.pop()
            path = "/root/children" if item_id == "root" else f"/items/{item_id}/children"
            listing = request("GET", base + path + "?$select=id,name,size,file,folder&$top=100")
            require(not listing.get("@odata.nextLink"), "Paged source inventory; stop")
            for item in listing["value"]:
                relative = prefix + item["name"]
                if "folder" in item:
                    require(relative == "NativeCharts20261125", "Unexpected folder in selected library")
                    pending.append((item["id"], relative + "/"))
                else:
                    require(relative in EXPECTED_FILES[library], "Unexpected file in selected library")
                    acl = request("GET", base + "/items/" + item["id"] + "/permissions")
                    require(permission_signature(acl) == signature, "Item ACL differs from library")
                    found.append({"path": relative, "id": item["id"], "size": item["size"],
                                  "permissions": acl})
        require({item["path"] for item in found} == EXPECTED_FILES[library],
                "Missing or unexpected approved source files")
        journal.value["sources"].append({"library": library, "drive": drive,
                                         "permissions": permissions, "files": found})
        journal.save()
    journal.value["definitions"] = config


def deploy(journal, resume=False):
    previous = json.loads((EVIDENCE / "native-unified-preflight-20261125.json").read_text(
        encoding="utf-8"))
    require(previous["status"] == "success", "Preflight incomplete")
    config = configuration()
    require(previous["definitions"] == config, "Definitions changed after preflight")
    retained = set()
    if resume:
        failed = json.loads((EVIDENCE / "native-unified-deploy-20261125.json").read_text(
            encoding="utf-8"))
        require(failed["status"] == "failed-no-automatic-retry"
                and failed["error"] == "schema.credentials.connectionString: mismatch"
                and [x["operation"] for x in failed["attempts"]] == [
                    "create-indexes/" + INDEX, "create-skillsets/" + SKILLSET,
                    "create-datasources/" + DS],
                "Only the specific redacted datasource readback failure may resume")
        retained = {"indexes", "skillsets", "datasources"}
        journal.value["previousFailureEvidence"] = "native-unified-deploy-20261125.json"
    for collection in ("indexes", "skillsets", "datasources", "indexers", "knowledgesources"):
        listing = search("GET", collection + "?$select=name")
        allowed = {"indexes": {INDEX}, "skillsets": {SKILLSET}, "datasources": {DS}}
        require(not listing.get("@odata.nextLink")
                and {x["name"] for x in listing["value"] if x["name"].startswith(PREFIX)}
                == (allowed[collection] if collection in retained else set()),
                "Unified object collision")
    journal.value["created"] = []
    for collection, key in (("indexes", "index"), ("skillsets", "skillset"),
                            ("datasources", "datasource"), ("indexers", "indexer")):
        body = config[key]
        path = collection + "/" + body["name"]
        if collection not in retained:
            journal.attempt("create-" + path)
            search("PUT", path, body)
        readback = search("GET", path)
        verify_definition(readback, body, key)
        journal.value["created"].append({"collection": collection, "readback": readback})
        journal.save()


def resume_deploy(journal):
    deploy(journal, resume=True)


def ingest(journal):
    deploy_path = EVIDENCE / "native-unified-resume-deploy-20261125.json"
    if not deploy_path.exists():
        deploy_path = EVIDENCE / "native-unified-deploy-20261125.json"
    prior = json.loads(deploy_path.read_text(
        encoding="utf-8"))
    require(prior["status"] == "success", "Deployment incomplete")
    config = configuration()
    actual = search("GET", "indexers/" + INDEXER)
    matches(actual, config["indexer"])
    state = search("GET", "indexers/" + INDEXER + "/status")
    require(not state.get("executionHistory") and not state.get("lastResult"),
            "Unified ingestion already started; no duplicate model work")
    enabled = deepcopy(actual)
    etag = enabled.pop("@odata.etag")
    enabled.pop("@odata.context", None)
    enabled["disabled"] = False
    journal.attempt("enable-unified-initial-ingestion")
    conditional_search_put("indexers/" + INDEXER, enabled, etag)
    # Keep enabled per user policy. Never POST Run in addition to the initial enable.
    deadline = time.monotonic() + 600
    while True:
        state = search("GET", "indexers/" + INDEXER + "/status")
        journal.value["execution"] = status_evidence(state)
        journal.save()
        last = state.get("lastResult")
        if last and last.get("endTime"):
            break
        require(time.monotonic() < deadline, "Observation deadline; don't resubmit ingestion")
        time.sleep(8)
    require(last["status"] == "success" and last["itemsProcessed"] == 7
            and last["itemsFailed"] == 0 and not last.get("errors"),
            "Unified seven-file ingestion failed; old KB remains unchanged")
    require(len(state.get("executionHistory", [])) == 1, "Unexpected extra unified execution")
    deadline = time.monotonic() + 60
    while True:
        stats = search("GET", "indexes/" + INDEX + "/stats")
        journal.value["stats"] = stats
        journal.save()
        if stats["documentCount"] == 7:
            break
        require(stats["documentCount"] < 7 and time.monotonic() < deadline,
                "Unified document count mismatch; don't switch KB")
        time.sleep(5)


def verify_ingest(journal):
    previous = json.loads((EVIDENCE / "native-unified-ingest-20261125.json").read_text(
        encoding="utf-8"))
    require(previous["status"] == "failed-no-automatic-retry"
            and previous["error"] == "Unified document count mismatch; don't switch KB",
            "Only delayed statistics after a completed successful run may be rechecked")
    journal.value["previousEvidence"] = "native-unified-ingest-20261125.json"
    state = search("GET", "indexers/" + INDEXER + "/status")
    last = state["lastResult"]
    require(len(state["executionHistory"]) == 1 and last["status"] == "success"
            and last["itemsProcessed"] == 7 and last["itemsFailed"] == 0
            and last["startTime"] == previous["execution"]["lastResult"]["startTime"],
            "Execution changed; no re-run or cutover")
    journal.value["execution"] = status_evidence(state)
    deadline = time.monotonic() + 300
    while True:
        journal.value["stats"] = search("GET", "indexes/" + INDEX + "/stats")
        journal.save()
        if journal.value["stats"]["documentCount"] == 7:
            break
        require(journal.value["stats"]["documentCount"] < 7 and time.monotonic() < deadline,
                "Seven records still unconfirmed; leave native KB unchanged")
        time.sleep(10)


def cutover(journal):
    verification_path = EVIDENCE / "native-unified-validate-content-20261125.json"
    require(verification_path.exists(), "Content validation required before KB cutover")
    verification = json.loads(verification_path.read_text(encoding="utf-8"))
    require(verification["status"] == "success"
            and verification.get("originalTextAndFourImageDescriptionsVerified") is True
            and verification.get("elevatedRead") is False,
            "Normal-user content validation incomplete; don't switch native KB")
    evidence_path = EVIDENCE / "native-unified-verify-ingest-20261125.json"
    if not evidence_path.exists():
        evidence_path = EVIDENCE / "native-unified-ingest-20261125.json"
    evidence = json.loads(evidence_path.read_text(
        encoding="utf-8"))
    require(evidence["status"] == "success" and evidence["stats"]["documentCount"] == 7,
            "Verified ingestion required before cutover")
    require(search("GET", "indexes/" + INDEX + "/stats")["documentCount"] == 7,
            "Unified count changed")
    config = configuration()
    for collection, key in (("indexes", "index"), ("skillsets", "skillset"),
                            ("datasources", "datasource")):
        verify_definition(search("GET", collection + "/" + config[key]["name"]), config[key], key)
    current = search("GET", "indexers/" + INDEXER)
    enabled = deepcopy(config["indexer"])
    enabled["disabled"] = False
    matches(current, enabled)
    kb = search("GET", "knowledgebases/" + KB)
    body, etag = kb_replacement(kb)
    inventory = search("GET", "knowledgesources?$select=name")
    require(not inventory.get("@odata.nextLink")
            and not any(x["name"] == KS for x in inventory["value"]), "KS collision")
    journal.attempt("create-unified-ks")
    search("PUT", "knowledgesources/" + KS, config["knowledgeSource"])
    matches(search("GET", "knowledgesources/" + KS), config["knowledgeSource"])
    journal.value["kbBefore"] = kb
    journal.attempt("switch-native-kb-to-single-unified-ks")
    conditional_search_put("knowledgebases/" + KB, body, etag)
    journal.value["kbAfter"] = search("GET", "knowledgebases/" + KB)
    matches(journal.value["kbAfter"], body)
    journal.save()
    journal.value["legacy"] = []
    for name in LEGACY_INDEXERS:
        actual = search("GET", "indexers/" + name)
        if not actual["disabled"]:
            paused = deepcopy(actual)
            version = paused.pop("@odata.etag")
            paused.pop("@odata.context", None)
            paused["disabled"] = True
            journal.attempt("pause-retained-legacy-" + name)
            conditional_search_put("indexers/" + name, paused, version)
        readback = search("GET", "indexers/" + name)
        require(readback["disabled"] is True, "Legacy pause not verified")
        journal.value["legacy"].append({"name": name, "disabled": readback["disabled"]})
        journal.save()


def validate_content(journal):
    """One normal admin-owned-content read, never elevated or an A/B impersonation."""
    root = Path(__file__).resolve().parents[1]
    query = {"search": "*", "top": 7, "count": True,
             "select": "metadata_spo_item_name,content,sourceUrl"}
    journal.value.update({"caller": "existing-admin-delegated-user",
                          "query": query, "elevatedRead": False, "abTest": False})
    token = cli("account", "get-access-token", "--resource", "https://search.azure.com")[
        "accessToken"]
    operation = Request(
        f"{ENDPOINT}/indexes/{INDEX}/docs/search?api-version={VERSION}",
        method="POST", data=json.dumps(query).encode("utf-8"),
        headers={"Authorization": "Bearer " + token,
                 "x-ms-query-source-authorization": token, "Content-Type": "application/json"})
    journal.attempt("normal-admin-content-verification-once")
    with urlopen(operation, timeout=120) as response:
        result = json.load(response)
    journal.value["result"] = result
    journal.save()
    require(result.get("@odata.count") == 7 and len(result["value"]) == 7,
            "Normal admin query did not return seven authorized documents; no elevated fallback")
    documents = {x["metadata_spo_item_name"]: x for x in result["value"]}
    require(set(documents) == {Path(p).name for names in EXPECTED_FILES.values() for p in names},
            "Unexpected or duplicated unified source names")
    for item in json.loads((root / "tests" / "fixtures" / "sharepoint-originals" /
                            "manifest.json").read_text(encoding="utf-8"))["files"]:
        content = documents[item["name"]]["content"]
        require(item["verificationCode"] in content and str(item["allocation"]) in content,
                "Original DOCX content lost during merging")
    for item in json.loads((root / "tests" / "fixtures" / "native-charts" /
                            "expected.json").read_text(encoding="utf-8"))["files"]:
        content = documents[item["name"]]["content"]
        require(item["marker"] in content and all(x in content for x in ("North", "Central", "South"))
                and "[AI-generated image description]" in content,
                "Image description lost during merging")
    journal.value["originalTextAndFourImageDescriptionsVerified"] = True


def rollback(journal):
    """Restore only this migration's KB/Indexer changes after unresolved validation."""
    failed = json.loads((EVIDENCE / "native-unified-validate-content-20261125.json").read_text(
        encoding="utf-8"))
    require(failed["status"] == "failed-no-automatic-retry"
            and failed.get("result", {}).get("@odata.count") == 0,
            "Rollback applies only to the recorded unresolved normal-user validation")
    migration = json.loads((EVIDENCE / "native-unified-cutover-20261125.json").read_text(
        encoding="utf-8"))
    require(migration["status"] == "success", "Migration state incomplete")
    prior = json.loads((EVIDENCE / "native-unified-preflight-20261125.json").read_text(
        encoding="utf-8"))["before"]
    actual = search("GET", "knowledgebases/" + KB)
    expected_current = deepcopy(migration["kbAfter"])
    expected_current.pop("@odata.context", None)
    expected_current.pop("@odata.etag", None)
    matches(actual, expected_current)
    restored = deepcopy(migration["kbBefore"])
    restored.pop("@odata.context", None)
    restored.pop("@odata.etag", None)
    journal.attempt("restore-native-kb-previous-two-sources")
    conditional_search_put("knowledgebases/" + KB, restored, actual["@odata.etag"])
    journal.value["restoredKb"] = search("GET", "knowledgebases/" + KB)
    matches(journal.value["restoredKb"], restored)
    journal.save()
    require({x["name"] for x in prior["indexers"]} == set(LEGACY_INDEXERS),
            "Unexpected legacy restore scope")
    journal.value["restoredIndexers"] = []
    for snapshot in prior["indexers"]:
        actual = search("GET", "indexers/" + snapshot["name"])
        expected = deepcopy(snapshot)
        expected.pop("@odata.context", None)
        expected.pop("@odata.etag", None)
        expected["disabled"] = actual["disabled"]
        matches(actual, expected)
        if actual["disabled"] != snapshot["disabled"]:
            body = deepcopy(actual)
            version = body.pop("@odata.etag")
            body.pop("@odata.context", None)
            body["disabled"] = snapshot["disabled"]
            journal.attempt("restore-legacy-enabled-state-" + snapshot["name"])
            conditional_search_put("indexers/" + snapshot["name"], body, version)
        readback = search("GET", "indexers/" + snapshot["name"])
        require(readback["disabled"] == snapshot["disabled"], "Restore state mismatch")
        journal.value["restoredIndexers"].append(
            {"name": readback["name"], "disabled": readback["disabled"]})
        journal.save()
    unified = search("GET", "indexers/" + INDEXER)
    paused = deepcopy(unified)
    version = paused.pop("@odata.etag")
    paused.pop("@odata.context", None)
    paused["disabled"] = True
    journal.attempt("pause-unverified-unified-indexer")
    conditional_search_put("indexers/" + INDEXER, paused, version)
    journal.value["unifiedIndexer"] = search("GET", "indexers/" + INDEXER)
    require(journal.value["unifiedIndexer"]["disabled"] is True, "Unified pause not verified")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=(
        "definitions", "preflight", "deploy", "resume-deploy", "ingest", "verify-ingest",
        "cutover", "validate-content", "rollback"))
    parser.add_argument("--execute-approved", action="store_true")
    args = parser.parse_args()
    if args.stage == "definitions":
        print(json.dumps(configuration(), indent=2))
        return
    require(args.execute_approved, "Explicit approved stage required")
    verify_identity()
    journal = Journal(args.stage)
    try:
        {"preflight": preflight, "deploy": deploy, "resume-deploy": resume_deploy, "ingest": ingest,
         "verify-ingest": verify_ingest, "cutover": cutover,
         "validate-content": validate_content, "rollback": rollback}[args.stage](journal)
        journal.value.update(status="success", completedAt=datetime.now(timezone.utc).isoformat())
        journal.save()
    except Exception as error:
        journal.value.update(status="failed-no-automatic-retry", error=str(error))
        journal.save()
        raise
    print(json.dumps({"status": "success", "evidence": str(journal.path)}))


if __name__ == "__main__":
    main()
