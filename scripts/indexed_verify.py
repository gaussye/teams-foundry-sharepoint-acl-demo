"""Read-only verification of deployed phase-1 metadata, count, and role cleanup."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from indexed_acl import schemas, require
from indexed_deploy import (
    ADMIN, INDEX, LOGIN, OBJECTS, ROLE, SCOPE, SERVICE, SUBSCRIPTION,
    cli, rest, role_absent, search,
)


def matches(actual, expected, path="schema"):
    if isinstance(expected, dict):
        require(isinstance(actual, dict), f"{path}: expected object")
        for key, value in expected.items():
            require(key in actual, f"{path}: missing {key}")
            matches(actual[key], value, f"{path}.{key}")
    elif isinstance(expected, list):
        require(isinstance(actual, list) and len(actual) == len(expected),
                f"{path}: unexpected collection")
        for offset, (observed, desired) in enumerate(zip(actual, expected)):
            matches(observed, desired, f"{path}[{offset}]")
    else:
        require(type(actual) is type(expected) and actual == expected, f"{path}: mismatch")


def verify(deployment):
    require(deployment["status"] == "uploaded" and deployment["cleanupVerifiedAbsent"] is True,
            "Deployment evidence does not show successful upload and cleanup")
    account = cli("account", "show")
    require(account["id"] == SUBSCRIPTION and account["user"]["name"].lower() == LOGIN.lower(),
            "Wrong Azure context")
    identity = rest("GET", "https://graph.microsoft.com/v1.0/me?$select=id,userPrincipalName")
    require(identity["id"] == ADMIN and identity["userPrincipalName"].lower() == LOGIN.lower(),
            "Wrong Graph identity")
    desired = schemas()
    observed = {}
    for collection, name, key in OBJECTS:
        observed[key] = search("GET", f"{collection}/{name}")
        matches(observed[key], desired[key], key)
    require(not observed["knowledgeBase"].get("models"), "Unexpected KB model configuration")
    stats = search("GET", f"indexes/{INDEX}/stats")
    require(stats["documentCount"] == deployment["uploadedChunks"] == 3, "Wrong chunk count")
    require(stats["vectorIndexSize"] == 0, "Unexpected vector data")
    assignment = deployment["assignmentId"]
    require(assignment.startswith(SCOPE + "/providers/Microsoft.Authorization/roleAssignments/")
            and deployment["assignmentScope"] == SCOPE, "Unexpected assignment scope")
    require(role_absent("https://management.azure.com" + assignment + "?api-version=2022-04-01"),
            "Temporary assignment still exists")
    roles = cli("role", "assignment", "list", "--scope", SCOPE,
                "--include-inherited", "--subscription", SUBSCRIPTION)
    require(not any(role["principalId"].lower() == ADMIN
                    and role["roleDefinitionId"].lower().endswith(ROLE) for role in roles),
            "Admin still has an applicable Data Contributor assignment")
    service = cli("search", "service", "show", "--ids", SERVICE)
    require(service["sku"]["name"].lower() == "basic"
            and service["location"].replace(" ", "").lower() == "westus"
            and service["replicaCount"] == service["partitionCount"] == 1,
            "Search capacity differs from the approved configuration")
    remote = {}
    for collection, suffix in (("knowledgesources", "ks"), ("knowledgebases", "kb")):
        name = "teams-acl-demo-sp-remote-" + suffix
        remote[collection] = search("GET", collection + "/" + name)["name"]
        require(remote[collection] == name, "Remote metadata object missing")
    return {
        "verifiedAt": datetime.now(timezone.utc).isoformat(),
        "status": "read-only-checks-passed", "schemaChecks": list(observed),
        "stats": stats, "exactAssignmentAbsent": True,
        "adminHasApplicableDataContributor": False,
        "capacity": {"sku": "basic", "location": "West US", "replicas": 1, "partitions": 1},
        "remoteMetadataPresent": remote, "cloudWrites": 0,
        "modelCalls": 0, "retrievalCalls": 0, "teamsMessages": 0,
        "liveUserPermissionEnforcementTested": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deployment", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    require(not args.output.exists(), "Refusing to overwrite verification evidence")
    result = verify(json.loads(args.deployment.read_text(encoding="utf-8")))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
