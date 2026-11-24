"""Bounded native SharePoint ingestion experiment; not a general deployment tool."""

import argparse
from datetime import datetime, timezone
import json
import locale
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

from indexed_deploy import ADMIN, ENDPOINT, LOGIN, SERVICE, SUBSCRIPTION

TENANT = "72c10632-ee48-4c08-a16a-e6cea1dbb7ef"
MI = "2f5a902a-2160-4107-93ef-7cd20f88760d"
MI_CLIENT = "1daa1e7b-aa19-4620-9916-6f40452252e8"
APP = "teams-acl-demo-sp-native-ingest"
INDEX = "teams-acl-demo-sp-native-index"
VERSION = "2026-10-01"
SITE = "https://mngenvmcap566860.sharepoint.com/sites/test"
SITE_ID = ("mngenvmcap566860.sharepoint.com,adf14383-204a-43a2-abe9-5d1459ef094d,"
           "2a9a1e9e-9870-49ae-8d39-5391d0e5b711")
GRAPH = "https://graph.microsoft.com/v1.0/"
ROLE = "8ebe5a00-799e-43f5-93ac-243d3dce84a7"
LIBRARIES = (
    ("public", "TeamsAclDemoPublic", "d243dec2-749a-439f-843a-d0843f2ab725",
     "ACLDEMO_PUBLIC_20261024_P731.docx"),
    ("finance", "TeamsAclDemoFinance", "bd70f90e-d1c0-45ad-ba2b-c42617fd4918",
     "ACLDEMO_FINANCE_20261024_F482.docx"),
    ("hr", "TeamsAclDemoHR", "e8902302-fdbb-4da1-95da-fe2492eefe2c",
     "ACLDEMO_HR_20261024_H965.docx"),
)
PERMISSIONS = (
    ("00000003-0000-0000-c000-000000000000", "Files.Read.All"),
    ("00000003-0000-0000-c000-000000000000", "Sites.Selected"),
    ("00000003-0000-0ff1-ce00-000000000000", "Sites.Selected"),
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def cli(*args):
    executable = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / (
        r"Microsoft SDKs\Azure\CLI2\python.exe")
    response = subprocess.run(
        [str(executable), "-IBm", "azure.cli", *args, "--only-show-errors", "-o", "json"],
        capture_output=True, timeout=120)
    if response.returncode:
        raise RuntimeError(response.stderr.decode("utf-8", errors="replace").strip())
    if not response.stdout.strip():
        return None
    try:
        text = response.stdout.decode("utf-8")
    except UnicodeDecodeError:
        text = response.stdout.decode(locale.getencoding())
    return json.loads(text)


def request(method, url, body=None):
    args = ["rest", "--method", method, "--url", url]
    if url.startswith(ENDPOINT):
        args += ["--resource", "https://search.azure.com"]
    if body is None:
        return cli(*args)
    with tempfile.TemporaryDirectory(prefix="native-indexer-") as directory:
        path = Path(directory) / "request.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        return cli(*args, "--body", "@" + str(path))


def search(method, path, body=None):
    separator = "&" if "?" in path else "?"
    return request(method, f"{ENDPOINT}/{path}{separator}api-version={VERSION}", body)


def configuration(app_id):
    require(str(uuid.UUID(app_id)) == app_id, "Invalid ingestion client ID")
    fields = [
        {"name": "id", "type": "Edm.String", "key": True},
        {"name": "content", "type": "Edm.String", "searchable": True},
        {"name": "metadata_spo_item_name", "type": "Edm.String"},
        {"name": "metadata_spo_item_path", "type": "Edm.String", "retrievable": False},
        {"name": "SharePointSiteUrl", "type": "Edm.String", "retrievable": False},
    ] + [{"name": name, "type": "Collection(Edm.String)", "permissionFilter": name,
          "filterable": True, "retrievable": False} for name in ("userIds", "groupIds")]
    index = {"name": INDEX, "fields": fields, "permissionFilterOption": "enabled"}
    # Query-time site-group registration requires separate permissions not approved here.
    connection = (f"SharePointOnlineEndpoint={SITE};ApplicationId={app_id};"
                  f"FederatedCredentialApplicationId={MI_CLIENT};TenantId={TENANT}")
    sources, indexers = [], []
    for suffix, library, _, _ in LIBRARIES:
        name = f"teams-acl-demo-sp-native-{suffix}"
        sources.append({
            "name": name + "-ds", "type": "sharepoint",
            "indexerPermissionOptions": ["userIds", "groupIds"],
            "credentials": {"connectionString": connection},
            "container": {"name": "useQuery", "query": f"includeLibrary={SITE}/{library}"},
        })
        indexers.append({
            "name": name + "-indexer", "dataSourceName": name + "-ds",
            "targetIndexName": INDEX, "disabled": True,
            "parameters": {"maxFailedItems": 0, "maxFailedItemsPerBatch": 0,
                           "configuration": {"indexedFileNameExtensions": ".docx",
                                             "dataToExtract": "contentAndMetadata"}},
            "fieldMappings": [
                {"sourceFieldName": "metadata_spo_site_library_item_id",
                 "targetFieldName": "id", "mappingFunction": {"name": "base64Encode"}},
                {"sourceFieldName": "metadata_user_ids", "targetFieldName": "userIds"},
                {"sourceFieldName": "metadata_group_ids", "targetFieldName": "groupIds"},
                {"sourceFieldName": "metadata_spo_site_url", "targetFieldName": "SharePointSiteUrl"},
            ],
        })
    return {"index": index, "datasources": sources, "indexers": indexers}


def verify_consent_authority(assignments):
    require(not assignments.get("@odata.nextLink"), "Consent role response is paged")
    roles = {r["roleDefinition"]["displayName"] for r in assignments["value"]
             if r["directoryScopeId"] == "/"}
    require(bool(roles & {"Global Administrator", "Privileged Role Administrator"}),
            "No confirmed active tenant-wide role to consent Microsoft Graph application permissions; "
            "stop before writes. Group/custom/PIM authority requires separate confirmation.")


def resume_identity(previous, site_grant, resources):
    require(previous["status"] == "failed-no-automatic-retry"
            and previous["temporaryAssignment"] is None and previous["runs"] == []
            and previous["attemptedWrites"][-1] == "test-site-read",
            "Only exact site-grant failure can resume")
    require(site_grant["status"] == "created-via-graph-explorer"
            and site_grant["httpStatusObserved"] == 201 and site_grant["roles"] == ["read"]
            and site_grant["siteId"] == SITE_ID and site_grant["adminObjectId"] == ADMIN
            and site_grant["applicationDisplayName"] == APP
            and site_grant["applicationClientId"] == previous["application"]["appId"],
            "Site grant evidence scope differs")
    application = request("GET", GRAPH + "applications/" + previous["application"]["id"]
                          + "?$select=id,appId,displayName,requiredResourceAccess")
    require(application["appId"] == previous["application"]["appId"]
            and application["displayName"] == APP, "Resume app mismatch")
    expected = {(aid, p["id"]) for aid, r in resources.items() for p in r["permissions"]}
    require({(r["resourceAppId"], p["id"]) for r in application["requiredResourceAccess"]
             for p in r["resourceAccess"]} == expected, "Resume app permissions drift")
    principal = request("GET", GRAPH + "servicePrincipals/" + previous["servicePrincipalId"]
                        + "?$select=id,appId")
    require(principal["appId"] == application["appId"], "Resume service principal mismatch")
    return application, principal


def verify_existing_indexer(actual, expected):
    def mappings(items):
        return [(m["sourceFieldName"], m["targetFieldName"],
                 (m.get("mappingFunction") or {}).get("name")) for m in items]
    require(actual["name"] == expected["name"] and actual["disabled"] is True
            and actual["dataSourceName"] == expected["dataSourceName"]
            and actual["targetIndexName"] == INDEX and not actual.get("schedule")
            and not actual.get("skillsetName")
            and mappings(actual["fieldMappings"]) == mappings(expected["fieldMappings"])
            and actual["parameters"]["configuration"] == expected["parameters"]["configuration"]
            and actual["parameters"]["maxFailedItems"] == 0
            and actual["parameters"]["maxFailedItemsPerBatch"] == 0,
            "Existing indexer differs from isolated configuration")


def status_evidence(value):
    """Exclude opaque source continuation state from diagnostic output."""
    if isinstance(value, dict):
        return {k: status_evidence(v) for k, v in value.items()
                if k not in {"initialTrackingState", "finalTrackingState"}}
    if isinstance(value, list):
        return [status_evidence(v) for v in value]
    return value


def run_existing(output, deployment_path):
    require(not output.exists(), "Refusing run evidence overwrite")
    previous = json.loads(deployment_path.read_text(encoding="utf-8"))
    require(previous["cleanupVerifiedAbsent"] is True and previous["status"] == "failed-no-automatic-retry"
            and "disabled and cannot be run" in previous["failure"],
            "Only the recorded disabled run rejection may recover")
    definitions = configuration(previous["application"]["appId"])
    evidence = {"startedAt": datetime.now(timezone.utc).isoformat(), "runs": [],
                "status": "verifying-existing-config", "cleanupVerifiedAbsent": False,
                "resumedFrom": str(deployment_path), "modelCalls": 0, "retrievalCalls": 0,
                "teamsMessages": 0}
    assignment = None
    current = None
    def save():
        output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    save()
    try:
        require(cli("account", "show")["user"]["name"].lower() == LOGIN.lower(), "Wrong caller")
        require(request("GET", GRAPH + "me?$select=id")["id"] == ADMIN, "Wrong Graph caller")
        for definition, source in zip(definitions["indexers"], definitions["datasources"]):
            actual = search("GET", "indexers/" + definition["name"])
            verify_existing_indexer(actual, definition)
            ds = search("GET", "datasources/" + source["name"])
            require(ds["type"] == "sharepoint" and ds["container"] == source["container"]
                    and ds["indexerPermissionOptions"] == ["userIds", "groupIds"],
                    "Data source scope drift")
            history = search("GET", "indexers/" + definition["name"] + "/status")
            require(not history.get("executionHistory") and not history.get("lastResult"),
                    "Source has already run; not authorized to repeat")
        scope = SERVICE + "/indexes/" + INDEX
        assignment = scope + "/providers/Microsoft.Authorization/roleAssignments/" + str(uuid.uuid4())
        role_url = "https://management.azure.com" + assignment + "?api-version=2022-04-01"
        evidence["temporaryAssignment"] = assignment
        save()
        request("PUT", role_url, {"properties": {"principalId": ADMIN, "principalType": "User",
                "roleDefinitionId": f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/{ROLE}"}})
        assigned = request("GET", role_url)
        require(assigned["properties"]["scope"].lower() == scope.lower(), "Scope mismatch")
        for definition in definitions["indexers"]:
            current = search("GET", "indexers/" + definition["name"])
            current["disabled"] = False
            current.pop("@odata.context", None)
            evidence["runs"].append({"name": definition["name"], "enableAttemptedAt":
                                    datetime.now(timezone.utc).isoformat()})
            save()
            search("PUT", "indexers/" + current["name"], current)
            # Observe before POST: a service-triggered initial run also consumes the one-run allowance.
            state = search("GET", "indexers/" + current["name"] + "/status")
            if not state.get("lastResult") and not state.get("executionHistory"):
                evidence["runs"][-1]["runPostAttempted"] = True
                save()
                search("POST", "indexers/" + current["name"] + "/run")
            else:
                evidence["runs"][-1]["serviceTriggered"] = True
            deadline = time.monotonic() + 600
            while True:
                state = search("GET", "indexers/" + current["name"] + "/status")
                evidence["runs"][-1]["statusReadback"] = status_evidence(state)
                save()
                latest = state.get("lastResult")
                if latest and latest.get("endTime"):
                    break
                require(time.monotonic() < deadline, "Execution deadline; no retry")
                time.sleep(10)
            current["disabled"] = True
            search("PUT", "indexers/" + current["name"], current)
            require(search("GET", "indexers/" + current["name"])["disabled"] is True,
                    "Indexer disabling not verified")
            current = None
            require(latest["status"] == "success" and latest["itemsFailed"] == 0
                    and latest["itemsProcessed"] == 1, "Native source run failed; no site permission escalation")
        # Indexer completion and storage statistics can become visible at different times.
        deadline = time.monotonic() + 300
        while True:
            evidence["indexStats"] = search("GET", "indexes/" + INDEX + "/stats")
            save()
            if evidence["indexStats"]["documentCount"] == 3 or time.monotonic() >= deadline:
                break
            time.sleep(15)
        require(evidence["indexStats"]["documentCount"] == 3, "Native count mismatch")
        evidence["status"] = "native-ingestion-completed-query-acl-unverified"
    except Exception as error:
        evidence["failure"] = str(error)
        evidence["status"] = "failed-no-automatic-retry"
        raise
    finally:
        cleanup_errors = []
        if current is not None:
            try:
                current["disabled"] = True
                search("PUT", "indexers/" + current["name"], current)
            except Exception as error:
                cleanup_errors.append(str(error))
        if assignment:
            try:
                cli("role", "assignment", "delete", "--ids", assignment, "--subscription", SUBSCRIPTION)
                try:
                    request("GET", role_url)
                except RuntimeError as error:
                    require("RoleAssignmentNotFound" in str(error) or "ResourceNotFound" in str(error),
                            "Role absence unverified")
                    evidence["cleanupVerifiedAbsent"] = True
                else:
                    raise ValueError("Role remains")
            except Exception as error:
                cleanup_errors.append(str(error))
        if cleanup_errors:
            evidence["cleanupErrors"] = cleanup_errors
            evidence["status"] = "cleanup-failed"
        evidence["completedAt"] = datetime.now(timezone.utc).isoformat()
        save()
        require(not cleanup_errors, "Cleanup failed; inspect evidence")
    return evidence


def run(output, previous_path=None, site_grant_path=None):
    require(not output.exists(), "Refusing evidence overwrite or automatic resume")
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence = {"startedAt": datetime.now(timezone.utc).isoformat(), "status": "preflight",
                "created": [], "attemptedWrites": [], "runs": [],
                "temporaryAssignment": None, "cleanupVerifiedAbsent": False,
                "modelCalls": 0, "retrievalCalls": 0, "teamsMessages": 0}

    def save():
        output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")

    def write(label, method, url, body):
        evidence["attemptedWrites"].append(label)
        save()
        result = request(method, url, body)
        evidence["created"].append(label)
        save()
        return result

    save()
    role_attempted = False
    role_url = None
    active_indexer = None
    try:
        context = cli("account", "show")
        require(context["id"] == SUBSCRIPTION and context["user"]["name"].lower() == LOGIN.lower(),
                "Unexpected Azure context")
        require(request("GET", GRAPH + "me?$select=id")["id"] == ADMIN, "Unexpected Graph caller")
        assignments = request(
            "GET", GRAPH + "roleManagement/directory/roleAssignments"
            f"?$filter=principalId eq '{ADMIN}'"
            "&$expand=roleDefinition($select=displayName)&$select=roleDefinitionId,directoryScopeId")
        evidence["consentAuthorityReadback"] = assignments
        verify_consent_authority(assignments)
        service = request("GET", "https://management.azure.com" + SERVICE + "?api-version=2025-05-01")
        require(service["identity"]["principalId"] == MI and service["sku"]["name"] == "basic",
                "Unexpected Search identity/capacity")
        mi = request("GET", GRAPH + f"servicePrincipals/{MI}?$select=id,appId")
        require(mi["appId"] == MI_CLIENT, "Unexpected Search MI client ID")
        apps = request("GET", GRAPH + f"applications?$filter=displayName eq '{APP}'&$select=id,appId")
        require(not apps.get("@odata.nextLink")
                and len(apps["value"]) == (1 if previous_path else 0), "App collision or paging")
        definitions = configuration(str(uuid.uuid4()))
        for collection, expected in [("indexes", [INDEX]),
                                     ("datasources", [d["name"] for d in definitions["datasources"]]),
                                     ("indexers", [d["name"] for d in definitions["indexers"]])]:
            inventory = search("GET", collection + "?$select=name")
            require(not inventory.get("@odata.nextLink"), "Paged inventory")
            require(not set(expected) & {r["name"] for r in inventory["value"]}, "Name collision")
        source_metadata = []
        for _, library, lid, filename in LIBRARIES:
            items = request("GET", GRAPH + f"sites/{SITE_ID}/lists/{lid}/items"
                            "?$select=id&$expand=driveItem($select=name,size)&$top=5")
            require(not items.get("@odata.nextLink") and len(items["value"]) == 1
                    and items["value"][0]["driveItem"]["name"] == filename,
                    "Library scope no longer exactly one approved synthetic document")
            source_metadata.append({"library": library, "items": items["value"]})
        evidence["sourceMetadata"] = source_metadata
        resources = {}
        for resource_app, permission in PERMISSIONS:
            response = request("GET", GRAPH + "servicePrincipals"
                               f"?$filter=appId eq '{resource_app}'&$select=id,appRoles")
            require(len(response["value"]) == 1 and not response.get("@odata.nextLink"),
                    "Ambiguous permission resource")
            sp = response["value"][0]
            roles = [r for r in sp["appRoles"] if r["value"] == permission
                     and r["isEnabled"] and "Application" in r["allowedMemberTypes"]]
            require(len(roles) == 1, "Permission definition unavailable")
            resources.setdefault(resource_app, {"id": sp["id"], "permissions": []})[
                "permissions"].append({"id": roles[0]["id"], "type": "Role"})
        if previous_path:
            require(site_grant_path is not None, "Site-grant evidence required for resume")
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
            site_grant = json.loads(site_grant_path.read_text(encoding="utf-8"))
            application, principal = resume_identity(previous, site_grant, resources)
            evidence["resumedFrom"] = str(previous_path)
            evidence["siteGrantReportedEvidence"] = site_grant
            try:
                evidence["siteGrantReadback"] = request(
                    "GET", GRAPH + f"sites/{SITE_ID}/permissions/" + site_grant["returnedPermissionId"])
                require(evidence["siteGrantReadback"]["roles"] == ["read"], "Site grant drift")
            except RuntimeError as error:
                require("Forbidden" in str(error), "Unexpected site-grant readback error")
                evidence["siteGrantReadbackUnavailable"] = str(error)
                # Actual native source access must verify the reported grant; no privilege fallback.
        else:
            application = write("ingestion-app", "POST", GRAPH + "applications", {
                "displayName": APP, "signInAudience": "AzureADMyOrg",
                "requiredResourceAccess": [{"resourceAppId": aid, "resourceAccess": r["permissions"]}
                                           for aid, r in resources.items()]})
            principal = write("ingestion-service-principal", "POST", GRAPH + "servicePrincipals",
                              {"appId": application["appId"]})
        evidence["application"] = {"id": application["id"], "appId": application["appId"]}
        app_object, client = application["id"], application["appId"]
        evidence["servicePrincipalId"] = principal["id"]
        fic = {"name": "search-system-mi", "issuer": f"https://login.microsoftonline.com/{TENANT}/v2.0",
               "subject": MI, "audiences": ["api://AzureADTokenExchange"]}
        if not previous_path:
            write("federated-credential", "POST", GRAPH + f"applications/{app_object}/federatedIdentityCredentials", fic)
            for resource in resources.values():
                for permission in resource["permissions"]:
                    write("application-consent-" + permission["id"], "POST",
                          GRAPH + f"servicePrincipals/{principal['id']}/appRoleAssignments", {
                              "principalId": principal["id"], "resourceId": resource["id"],
                              "appRoleId": permission["id"]})
            site_grant = write("test-site-read", "POST", GRAPH + f"sites/{SITE_ID}/permissions", {
                "roles": ["read"], "grantedToIdentities": [
                    {"application": {"id": client, "displayName": APP}}]})
        evidence["siteGrant"] = site_grant
        evidence["federatedCredentialReadback"] = request(
            "GET", GRAPH + f"applications/{app_object}/federatedIdentityCredentials")
        evidence["consentReadback"] = request(
            "GET", GRAPH + f"servicePrincipals/{principal['id']}/appRoleAssignments")
        require(site_grant["roles"] == ["read"], "Site role unexpectedly broader")
        credentials = evidence["federatedCredentialReadback"]["value"]
        require(len(credentials) == 1 and all(credentials[0][k] == v for k, v in fic.items()),
                "Federated trust readback differs")
        consented = evidence["consentReadback"]["value"]
        expected_roles = {(r["id"], p["id"]) for r in resources.values() for p in r["permissions"]}
        require({(c["resourceId"], c["appRoleId"]) for c in consented} == expected_roles,
                "Application consent readback differs")
        definitions = configuration(client)
        write(INDEX, "POST", ENDPOINT + f"/indexes?api-version={VERSION}", definitions["index"])
        scope = SERVICE + "/indexes/" + INDEX
        assignment = scope + "/providers/Microsoft.Authorization/roleAssignments/" + str(uuid.uuid4())
        role_url = "https://management.azure.com" + assignment + "?api-version=2022-04-01"
        evidence["temporaryAssignment"] = assignment
        role_attempted = True
        write("temporary-index-uploader", "PUT", role_url, {"properties": {
            "principalId": ADMIN, "principalType": "User",
            "roleDefinitionId": f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/{ROLE}"}})
        role = request("GET", role_url)
        require(role["properties"]["scope"].lower() == scope.lower(), "Role scope differs")
        for definition in definitions["datasources"]:
            write(definition["name"], "POST", ENDPOINT + f"/datasources?api-version={VERSION}", definition)
        for definition in definitions["indexers"]:
            write(definition["name"], "POST", ENDPOINT + f"/indexers?api-version={VERSION}", definition)
        evidence["indexReadback"] = search("GET", f"indexes/{INDEX}")
        save()
        for definition in definitions["indexers"]:
            name = definition["name"]
            require(search("GET", f"indexers/{name}")["disabled"] is True, "Indexer unexpectedly enabled")
            active_indexer = {**definition, "disabled": False}
            evidence["runs"].append({"name": name, "enableAttemptedAt": datetime.now(timezone.utc).isoformat()})
            save()
            search("PUT", f"indexers/{name}", active_indexer)
            status = search("GET", f"indexers/{name}/status")
            if not status.get("lastResult") and not status.get("executionHistory"):
                evidence["runs"][-1]["runPostAttempted"] = True
                save()
                search("POST", f"indexers/{name}/run")
            else:
                evidence["runs"][-1]["serviceTriggered"] = True
            deadline = time.monotonic() + 600
            while True:
                status = search("GET", f"indexers/{name}/status")
                evidence["runs"][-1]["statusReadback"] = status_evidence(status)
                save()
                latest = status.get("lastResult")
                if latest and latest.get("endTime"):
                    break
                require(time.monotonic() < deadline, "Native run deadline reached; inspect status before any retry")
                time.sleep(10)
            search("PUT", f"indexers/{name}", definition)
            require(search("GET", f"indexers/{name}")["disabled"] is True,
                    "Indexer disabling not verified")
            active_indexer = None
            require(latest["status"] == "success" and latest["itemsFailed"] == 0
                    and latest["itemsProcessed"] == 1, "Native ingestion did not process one file successfully")
        deadline = time.monotonic() + 300
        while True:
            evidence["indexStats"] = search("GET", f"indexes/{INDEX}/stats")
            save()
            if evidence["indexStats"]["documentCount"] == 3 or time.monotonic() >= deadline:
                break
            time.sleep(15)
        require(evidence["indexStats"]["documentCount"] == 3, "Expected three native records")
        evidence["status"] = "native-ingestion-completed-query-acl-unverified"
    except Exception as error:
        evidence["status"] = "failed-no-automatic-retry"
        evidence["failure"] = str(error)
        raise
    finally:
        try:
            if active_indexer is not None:
                active_indexer["disabled"] = True
                search("PUT", "indexers/" + active_indexer["name"], active_indexer)
        except Exception as error:
            evidence["disableFailure"] = str(error)
            evidence["status"] = "cleanup-failed"
        try:
            if role_attempted:
                try:
                    request("GET", role_url)
                except RuntimeError as error:
                    require("RoleAssignmentNotFound" in str(error) or "ResourceNotFound" in str(error),
                            "Cannot determine temporary role existence")
                else:
                    cli("role", "assignment", "delete", "--ids", evidence["temporaryAssignment"],
                        "--subscription", SUBSCRIPTION)
                try:
                    request("GET", role_url)
                except RuntimeError as error:
                    require("RoleAssignmentNotFound" in str(error) or "ResourceNotFound" in str(error),
                            "Role absence not proven")
                    evidence["cleanupVerifiedAbsent"] = True
                else:
                    raise ValueError("Temporary role remains")
        except Exception as cleanup:
            evidence["cleanupFailure"] = str(cleanup)
            evidence["status"] = "cleanup-failed"
            raise
        finally:
            evidence["completedAt"] = datetime.now(timezone.utc).isoformat()
            save()
    require("disableFailure" not in evidence, "Indexer cleanup failed; inspect evidence")
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--execute-approved", action="store_true")
    parser.add_argument("--resume-site-grant-failure", type=Path)
    parser.add_argument("--site-grant-evidence", type=Path)
    parser.add_argument("--resume-disabled-run", type=Path)
    args = parser.parse_args()
    require(args.execute_approved, "Execution must be explicit; no default cloud writes")
    if args.resume_disabled_run:
        require(not args.resume_site_grant_failure and not args.site_grant_evidence,
                "Cannot combine recovery modes")
        run_existing(args.evidence, args.resume_disabled_run)
    else:
        run(args.evidence, args.resume_site_grant_failure, args.site_grant_evidence)
