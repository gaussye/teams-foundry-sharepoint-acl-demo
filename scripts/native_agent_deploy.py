"""Create only the approved native SharePoint end-to-end demo increment."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from native_indexer_experiment import (
    ADMIN, APP, ENDPOINT, GRAPH, INDEX, LOGIN, MI_CLIENT, SUBSCRIPTION,
    TENANT, cli, request, search, require, verify_consent_authority,
)
from indexed_agent_deploy import PROJECT, PROJECT_ID
from indexed_verify import matches

CLIENT = "7effa646-d029-4073-b39a-1f11946e757e"
APP_OBJECT = "28852a30-8f69-436c-b3dc-8b345a072def"
PRINCIPAL = "695720c7-acf0-4f87-8dfd-a96c7e6476e3"
KS = "teams-acl-demo-sp-native-ks"
KB = "teams-acl-demo-sp-native-kb"
CONNECTION = KB + "-mcp"
AGENT = "teams-acl-demo-sp-native-agent"
BOT = "teams-acl-demo-sp-native-bot"
BOT_ID = PROJECT_ID.split("/providers/")[0] + "/providers/Microsoft.BotService/botServices/" + BOT
USER_READ = "df021288-bdef-4463-88db-98f22de89214"
SP_RESOURCE = "0b1ad636-fda8-41e1-9375-f1b41cb1b6be"
SP_APP = "00000003-0000-0ff1-ce00-000000000000"


def definition(index):
    fields = {r["name"]: r for r in index["fields"]}
    require(index["name"] == INDEX and index["permissionFilterOption"] == "enabled",
            "Wrong native index")
    require(all(fields[k]["retrievable"] is False for k in ("userIds", "groupIds")),
            "ACL field visibility changed")
    updated = json.loads(json.dumps(index))
    updated.pop("@odata.context", None)
    updated.pop("@odata.etag", None)
    updated["sharePointConnectorAppRegistration"] = {
        "applicationId": CLIENT, "tenantId": TENANT, "federatedCredentialId": MI_CLIENT}
    for field in updated["fields"]:
        if field["name"] == "SharePointSiteUrl":
            field["sharepointSiteUrl"] = True
    return updated


def semantic_definition(index):
    fields = {f["name"]: f for f in index["fields"]}
    require(index["name"] == INDEX and all(fields[n]["searchable"] is True
            and fields[n]["type"] == "Edm.String" for n in ["content","metadata_spo_item_name"]),
            "Existing native semantic fields incompatible; do not change schema")
    result = json.loads(json.dumps(index))
    result.pop("@odata.context", None)
    result.pop("@odata.etag", None)
    result["semantic"] = {"defaultConfiguration": "native-acl-semantic", "configurations": [
        {"name": "native-acl-semantic", "prioritizedFields": {
            "titleField": {"fieldName": "metadata_spo_item_name"},
            "prioritizedContentFields": [{"fieldName": "content"}]}}]}
    return result


def foundry(method, path, body=None):
    args = ["rest", "--method", method, "--url", PROJECT + "/" + path + "?api-version=v1",
            "--resource", "https://ai.azure.com"]
    if body is None:
        return cli(*args)
    import tempfile
    with tempfile.TemporaryDirectory(prefix="native-foundry-") as directory:
        path = Path(directory) / "body.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        return cli(*args, "--body", "@" + str(path))


def configuration():
    script = """
import {buildFoundryConfiguration,buildBotConfiguration} from './scripts/foundry-agent-config.mjs';
const config=buildFoundryConfiguration({
accountName:'admin-3283-resource', projectName:'teams-acl-demo',
agentName:'teams-acl-demo-sp-native-agent', connectionName:'teams-acl-demo-sp-native-kb-mcp',
knowledgeBaseName:'teams-acl-demo-sp-native-kb', searchEndpoint:'https://search-service-ypy2.search.windows.net',
location:'westus',modelDeployment:'gpt-4.1-mini'});
config.connection.properties.isSharedToAll=false;
config.agent.description='Native SharePoint ACL pilot for A/B private Teams chats.';
config.agent.definition.instructions+='\\nUse only the native SharePoint KB. Call knowledge_base_retrieve once per question. Never retry or use elevated read or administrator identity in place of the user.';
console.log(JSON.stringify(config));
"""
    result = subprocess.run(["node", "--input-type=module", "-e", script],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def deploy(output, resume_path=None, repair_path=None):
    require(not output.exists(), "Evidence exists; inspect partial state before recovery")
    evidence = {"startedAt": datetime.now(timezone.utc).isoformat(), "status": "preflight",
                "created": [], "attempted": [], "testQuestionsSent": 0, "testQuestionLimit": 2,
                "modelCalls": 0, "retrievalCalls": 0, "temporaryRoles": []}
    def save():
        output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    def write(label, method, url, body):
        evidence["attempted"].append(label)
        save()
        result = request(method, url, body)
        evidence["created"].append(label)
        save()
        return result
    save()
    try:
        account = cli("account", "show")
        require(account["id"] == SUBSCRIPTION and account["user"]["name"].lower() == LOGIN.lower(),
                "Wrong Azure identity")
        require(request("GET", GRAPH + "me?$select=id")["id"] == ADMIN, "Wrong Graph user")
        roles = request("GET", GRAPH + f"roleManagement/directory/roleAssignments?$filter=principalId eq '{ADMIN}'"
                        "&$expand=roleDefinition($select=displayName)&$select=roleDefinitionId,directoryScopeId")
        verify_consent_authority(roles)
        app = request("GET", GRAPH + f"applications/{APP_OBJECT}?$select=id,appId,requiredResourceAccess")
        require(app["appId"] == CLIENT, "Wrong native ingestion app")
        index = search("GET", "indexes/" + INDEX)
        require(search("GET", "indexes/" + INDEX + "/stats")["documentCount"] == 3,
                "Native count changed")
        evidence["indexBefore"] = index
        for coll, name in [("knowledgesources", KS), ("knowledgebases", KB)]:
            inv = search("GET", coll + "?$select=name")
            require(not any(r["name"] == name for r in inv["value"]), "Search collision")
        agents = foundry("GET", "agents")
        require(not agents["has_more"] and not any(a["name"] == AGENT for a in agents["data"]),
                "Agent collision/incomplete inventory")
        conn_url = "https://management.azure.com" + PROJECT_ID + "/connections"
        conns = request("GET", conn_url + "?api-version=2025-10-01-preview")
        require(not conns.get("nextLink") and not any(c["name"] == CONNECTION for c in conns["value"]),
                "Connection collision")
        bots = cli("resource", "list", "--resource-group", "rg-admin-3283",
                   "--resource-type", "Microsoft.BotService/botServices", "--subscription", SUBSCRIPTION)
        require(not any(b["id"].lower() == BOT_ID.lower() for b in bots), "Bot collision")
        evidence["oldAgentsBefore"] = {n: foundry("GET", "agents/" + n) for n in
                                      ["teams-acl-demo-sp-indexed-agent", "teams-acl-demo-sp-remote-agent"]}
        scopes = cli("role", "assignment", "list", "--scope", PROJECT_ID,
                     "--subscription", SUBSCRIPTION)
        consumers = {r["principalId"] for r in scopes if r["roleDefinitionId"].endswith(
            "eed3b665-ab3a-47b6-8f48-c9382fb1dad6")}
        require(consumers == {"cb1985cb-b3aa-49ee-8601-a159e2f6406f",
                              "373e6941-bcf1-4392-889f-a69611797bc1"}, "Consumer scope changed")
        previous = json.loads(resume_path.read_text(encoding="utf-8")) if resume_path else None
        if previous:
            require(previous["status"] == "failed-no-automatic-retry"
                    and ((not repair_path and previous["created"] == ["native-app-SharePoint-User.Read.All"]
                          and previous["attempted"][-1] == "native-SharePoint-User.Read.All-consent")
                         or (repair_path and previous["created"] == ["native-SharePoint-User.Read.All-consent"]
                             and "CannotChangeSharepointSiteUrl" in previous["failure"])
                         or (repair_path and previous["created"] == []
                             and previous["attempted"] == [KS] and "does not have semantic" in previous["failure"])),
                    "Only exact known interruption can resume")
            evidence["resumedFrom"] = str(resume_path)
        new_access = json.loads(json.dumps(app["requiredResourceAccess"]))
        surface = next(a for a in new_access if a["resourceAppId"] == SP_APP)
        require(any(p["id"] == USER_READ for p in surface["resourceAccess"]) == bool(previous),
                "Unexpected application permission state")
        if not previous:
            surface["resourceAccess"].append({"id": USER_READ, "type": "Role"})
            write("native-app-SharePoint-User.Read.All", "PATCH", GRAPH + "applications/" + APP_OBJECT,
                  {"requiredResourceAccess": new_access})
        grants = request("GET", GRAPH + f"servicePrincipals/{PRINCIPAL}/appRoleAssignments")
        existing = [r for r in grants["value"] if r["appRoleId"] == USER_READ and r["resourceId"] == SP_RESOURCE]
        require(len(existing) == (1 if repair_path else 0), "Unexpected consent state")
        if not repair_path:
            consent = write("native-SharePoint-User.Read.All-consent", "POST",
                            GRAPH + f"servicePrincipals/{PRINCIPAL}/appRoleAssignments",
                            {"principalId": PRINCIPAL, "resourceId": SP_RESOURCE, "appRoleId": USER_READ})
            evidence["newConsent"] = consent
        evidence["consentsReadback"] = request("GET", GRAPH + f"servicePrincipals/{PRINCIPAL}/appRoleAssignments")
        require(any(r["appRoleId"] == USER_READ and r["resourceId"] == SP_RESOURCE
                    for r in evidence["consentsReadback"]["value"]), "Consent readback failed")
        if repair_path:
            repaired = json.loads(repair_path.read_text(encoding="utf-8"))
            require(repaired["mergedRecords"] == 3 and repaired["cleanupVerifiedAbsent"] is True,
                    "Site repair incomplete")
            require(index.get("sharePointConnectorAppRegistration") == {
                "applicationId": CLIENT, "tenantId": TENANT, "federatedCredentialId": MI_CLIENT}
                and any(f["name"] == "NativeSharePointSiteUrl" and f["sharepointSiteUrl"] is True
                        for f in index["fields"]), "Site query registration drift")
            actual = index
            evidence["repairEvidence"] = str(repair_path)
        else:
            updated = definition(index)
            write("native-index-query-identity", "PUT", ENDPOINT + "/indexes/" + INDEX + "?api-version=2026-10-01", updated)
            actual = search("GET", "indexes/" + INDEX)
            matches(actual, updated, "native index")
        evidence["indexAfter"] = actual
        source = {"name": KS, "kind": "searchIndex", "searchIndexParameters": {
            "searchIndexName": INDEX, "semanticConfigurationName": "native-acl-semantic",
            "searchFields": [{"name": "content"}],
            "sourceDataFields": [{"name": n} for n in ["id","metadata_spo_item_name","content"]]}}
        write(KS, "POST", ENDPOINT + "/knowledgesources?api-version=2026-10-01", source)
        kb = {"name": KB, "knowledgeSources": [{"name": KS}], "outputMode": "extractiveData",
              "retrievalReasoningEffort": {"kind": "minimal"}}
        write(KB, "POST", ENDPOINT + "/knowledgebases?api-version=2026-10-01", kb)
        evidence["ks"] = search("GET", "knowledgesources/" + KS)
        evidence["kb"] = search("GET", "knowledgebases/" + KB)
        matches(evidence["ks"], source, "native ks")
        matches(evidence["kb"], kb, "native kb")
        config = configuration()
        write(CONNECTION, "PUT", conn_url + "/" + CONNECTION + "?api-version=2025-10-01-preview", config["connection"])
        evidence["connection"] = request("GET", conn_url + "/" + CONNECTION + "?api-version=2025-10-01-preview")
        matches(evidence["connection"], config["connection"], "native connection")
        evidence["attempted"].append(AGENT); save()
        foundry("POST", "agents", config["agent"]); evidence["created"].append(AGENT)
        agent = foundry("GET", "agents/" + AGENT)
        evidence["agent"] = agent; save()
        latest = agent["versions"]["latest"]
        identity = agent.get("instance_identity") or latest["instance_identity"]
        bot = {"location": "global", "kind": "azurebot", "sku": {"name": "F0"},
               "properties": {"displayName": "Teams SharePoint ACL Demo Native",
                              "endpoint": PROJECT + "/agents/" + AGENT + "/endpoint/protocols/activityProtocol?api-version=v1",
                              "msaAppId": identity["client_id"], "msaAppTenantId": TENANT,
                              "msaAppType": "SingleTenant", "publicNetworkAccess": "Disabled"}}
        write(BOT, "PUT", "https://management.azure.com" + BOT_ID + "?api-version=2022-09-15", bot)
        evidence["bot"] = request("GET", "https://management.azure.com" + BOT_ID + "?api-version=2022-09-15")
        matches(evidence["bot"], bot, "native bot")
        write("native-MsTeamsChannel", "PUT", "https://management.azure.com" + BOT_ID
              + "/channels/MsTeamsChannel?api-version=2022-09-15", {"location": "global",
                "properties": {"channelName": "MsTeamsChannel", "properties": {"isEnabled": True}}})
        publication = {"agentDisplayName": "Teams SharePoint ACL Demo Native", "botServiceArmId": BOT_ID,
                       "publishScope": "Shared", "publishAsAutopilot": False, "appVersion": "1.0.0",
                       "shortDescription": "Native SharePoint ACL pilot for two users.",
                       "fullDescription": "Retrieve native indexed synthetic evidence in A/B private chats.",
                       "developerName": "ACL Demo",
                       "developerWebsiteUrl": "https://github.com/gaussye/teams-foundry-sharepoint-acl-demo",
                       "privacyUrl": "https://privacy.microsoft.com/privacystatement",
                       "termsOfUseUrl": "https://www.microsoft.com/servicesagreement"}
        evidence["attempted"].append("native-Shared-publish"); save()
        evidence["publication"] = foundry("POST", "agents/" + AGENT + "/microsoft365/publish", publication)
        published = foundry("GET", "agents/" + AGENT)
        evidence["publishedAgent"] = published
        schemes = published["agent_endpoint"]["authorization_schemes"]
        require(any(s["type"] == "BotServiceRbac" for s in schemes)
                and not any(s["type"] == "BotServiceTenant" for s in schemes), "Unexpected publication scope")
        require(all(foundry("GET","agents/"+n) == previous for n, previous in evidence["oldAgentsBefore"].items()),
                "Old agents changed")
        evidence["status"] = "published-awaiting-two-real-private-chat-tests"
    except Exception as error:
        evidence["status"] = "failed-no-automatic-retry"
        evidence["failure"] = str(error)
        raise
    finally:
        evidence["completedAt"] = datetime.now(timezone.utc).isoformat()
        save()
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--execute-approved", action="store_true")
    parser.add_argument("--resume-app-only", type=Path)
    parser.add_argument("--repaired-site-evidence", type=Path)
    args = parser.parse_args()
    require(args.execute_approved, "No implicit cloud execution")
    deploy(args.output, args.resume_app_only, args.repaired_site_evidence)
