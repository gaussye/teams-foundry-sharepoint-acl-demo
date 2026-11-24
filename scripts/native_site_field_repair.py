"""Repair only native index site context using one metadata-only read and merge."""

from datetime import datetime, timezone
import json
from pathlib import Path
import time
import uuid

from native_agent_deploy import CLIENT
from native_indexer_experiment import (
    ADMIN, ENDPOINT, INDEX, MI_CLIENT, ROLE, SERVICE, SITE, SUBSCRIPTION,
    TENANT, LIBRARIES, cli, request, search, require,
)

FIELD = "NativeSharePointSiteUrl"


def validate_metadata(response):
    rows = response["value"]
    require(response["@odata.count"] == 3 and len(rows) == 3
            and not response.get("@odata.nextLink"), "Expected exactly three native records")
    require({r["metadata_spo_item_name"] for r in rows} == {x[3] for x in LIBRARIES},
            "Unexpected source file")
    require(len({r["id"] for r in rows}) == 3 and all(isinstance(r["id"], str) and r["id"] for r in rows),
            "Invalid or duplicate keys")
    return [{"@search.action": "merge", "id": r["id"], FIELD: SITE} for r in rows]


def repair(output):
    require(not output.exists(), "Existing repair evidence; no implicit retry")
    record = {"startedAt": datetime.now(timezone.utc).isoformat(), "status": "preflight",
              "metadataQueries": 0, "mergedRecords": 0, "temporaryAssignment": None,
              "cleanupVerifiedAbsent": False}
    def save():
        output.write_text(json.dumps(record, indent=2), encoding="utf-8")
    save()
    role_url = None
    try:
        index = search("GET", "indexes/" + INDEX)
        fields = {f["name"]: f for f in index["fields"]}
        require(FIELD not in fields and all(fields[k]["retrievable"] is False for k in
                                           ["userIds", "groupIds"]), "Unexpected schema")
        for suffix, *_ in LIBRARIES:
            cfg = search("GET", f"indexers/teams-acl-demo-sp-native-{suffix}-indexer")
            require(cfg["disabled"] is True and cfg["schedule"] is None, "Indexer enabled")
        record["indexBefore"] = index
        index.pop("@odata.context", None)
        index.pop("@odata.etag", None)
        index["fields"].append({"name": FIELD, "type": "Edm.String", "sharepointSiteUrl": True,
                                "retrievable": False})
        index["sharePointConnectorAppRegistration"] = {
            "applicationId": CLIENT, "tenantId": TENANT, "federatedCredentialId": MI_CLIENT}
        record["schemaUpdateAttempted"] = True; save()
        search("PUT", "indexes/" + INDEX, index)
        after = search("GET", "indexes/" + INDEX)
        require(next(f for f in after["fields"] if f["name"] == FIELD)["sharepointSiteUrl"] is True,
                "New site field marker failed")
        record["indexAfter"] = after; save()
        scope = SERVICE + "/indexes/" + INDEX
        assignment = scope + "/providers/Microsoft.Authorization/roleAssignments/" + str(uuid.uuid4())
        record["temporaryAssignment"] = assignment
        role_url = "https://management.azure.com" + assignment + "?api-version=2022-04-01"
        save()
        request("PUT", role_url, {"properties": {"principalId": ADMIN, "principalType": "User",
            "roleDefinitionId": f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/{ROLE}"}})
        require(request("GET", role_url)["properties"]["scope"].lower() == scope.lower(),
                "Temporary role scope differs")
        time.sleep(30)
        record["metadataQueries"] = 1; save()
        # Existing path field is non-retrievable; filename is the available source identifier.
        response = cli("rest", "--method", "POST", "--url",
                       ENDPOINT + f"/indexes/{INDEX}/docs/search?api-version=2026-10-01",
                       "--resource", "https://search.azure.com",
                       "--headers", "x-ms-enable-elevated-read=true", "Content-Type=application/json",
                       "--body", json.dumps({"search": "*", "select": "id,metadata_spo_item_name",
                                             "top": 3, "count": True}))
        payload = validate_metadata(response)
        record["metadata"] = response
        record["mergeAttempted"] = True; save()
        result = search("POST", f"indexes/{INDEX}/docs/search.index", {"value": payload})
        require(len(result["value"]) == 3 and all(r["status"] for r in result["value"]),
                "Some site backfills failed")
        record["mergeResults"] = result
        record["mergedRecords"] = 3
        record["status"] = "site-context-backfilled-without-content-or-acl-changes"
    except Exception as error:
        record["status"] = "failed-no-auto-retry"
        record["failure"] = str(error)
        raise
    finally:
        try:
            if role_url:
                cli("role", "assignment", "delete", "--ids", record["temporaryAssignment"],
                    "--subscription", SUBSCRIPTION)
                try:
                    request("GET", role_url)
                except RuntimeError as error:
                    require("RoleAssignmentNotFound" in str(error) or "ResourceNotFound" in str(error),
                            "Role absence not confirmed")
                    record["cleanupVerifiedAbsent"] = True
                else:
                    raise ValueError("Role remains")
        finally:
            record["completedAt"] = datetime.now(timezone.utc).isoformat(); save()
    return record


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--execute-approved", action="store_true")
    args = parser.parse_args()
    require(args.execute_approved, "Explicit execution required")
    repair(args.output)
