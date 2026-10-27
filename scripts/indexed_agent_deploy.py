"""Create the approved Indexed Agent and publish it with Shared/RBAC scope only."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from indexed_acl import require
from indexed_deploy import ADMIN, LOGIN, SUBSCRIPTION, cli, rest
from indexed_verify import matches, verify

PROJECT_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-admin-3283"
    "/providers/Microsoft.CognitiveServices/accounts/admin-3283-resource/projects/teams-acl-demo"
)
PROJECT = "https://admin-3283-resource.services.ai.azure.com/api/projects/teams-acl-demo"
AGENT = "teams-acl-demo-sp-indexed-agent"
CONNECTION = "teams-acl-demo-sp-indexed-kb-mcp"
REMOTE = "teams-acl-demo-sp-remote-agent"
BOT_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-admin-3283"
    "/providers/Microsoft.BotService/botServices/teams-acl-demo-sp-indexed-bot"
)
TENANT = "72c10632-ee48-4c08-a16a-e6cea1dbb7ef"
USERS = {"cb1985cb-b3aa-49ee-8601-a159e2f6406f", "373e6941-bcf1-4392-889f-a69611797bc1"}
CONSUMER = "eed3b665-ab3a-47b6-8f48-c9382fb1dad6"


def foundry(method, path, body=None):
    return rest(method, PROJECT + "/" + path + "?api-version=v1", body,
                resource="https://ai.azure.com")


def generated(operation, values):
    script = (
        "import fs from 'node:fs';"
        "import {buildIndexedAgentConfiguration,buildIndexedBotConfiguration,"
        "buildIndexedPublication} from './scripts/indexed-agent-config.mjs';"
        "const v=JSON.parse(fs.readFileSync(0,'utf8'));"
        f"console.log(JSON.stringify({operation}));"
    )
    response = subprocess.run(["node", "--input-type=module", "-e", script],
                              input=json.dumps(values), capture_output=True, text=True, check=True)
    return json.loads(response.stdout)


def connection_resume(path):
    if path is None:
        return None
    previous = json.loads(path.read_text(encoding="utf-8"))
    require(previous.get("status") == "failed-no-automatic-retry"
            and previous.get("created") == [CONNECTION]
            and previous.get("agentName") == AGENT
            and previous.get("project") == PROJECT
            and previous.get("failure") == "connection.properties.isSharedToAll: mismatch",
            "Only the recorded connection-only readback failure can be resumed")
    return previous


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase1_evidence", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--resume-connection-evidence", type=Path)
    args = parser.parse_args()
    require(not args.output.exists(), "Refusing to overwrite Agent deployment evidence")
    previous = connection_resume(args.resume_connection_evidence)
    verification = verify(json.loads(args.phase1_evidence.read_text(encoding="utf-8")))
    account = cli("account", "show")
    require(account["tenantId"] == TENANT and account["user"]["name"].lower() == LOGIN.lower(),
            "Unexpected tenant/caller")
    roles = cli("role", "assignment", "list", "--scope", PROJECT_ID,
                "--subscription", SUBSCRIPTION)
    consumers = {role["principalId"].lower() for role in roles
                 if role["roleDefinitionId"].lower().endswith(CONSUMER)}
    require(consumers == USERS, "Project consumer assignments differ from approved A/B")
    agents = foundry("GET", "agents")
    require(agents["has_more"] is False and all(item["name"] != AGENT for item in agents["data"]),
            "Agent name collision or incomplete inventory")
    connection_url = "https://management.azure.com" + PROJECT_ID + "/connections"
    connections = rest("GET", connection_url + "?api-version=2025-10-01-preview")
    existing_connections = [item for item in connections["value"] if item["name"] == CONNECTION]
    require(not connections.get("nextLink")
            and len(existing_connections) == (1 if previous else 0),
            "Connection name collision or incomplete inventory")
    bots = cli("resource", "list", "--resource-group", "rg-admin-3283",
               "--resource-type", "Microsoft.BotService/botServices",
               "--subscription", SUBSCRIPTION)
    require(not any(bot["id"].lower() == BOT_ID.lower() for bot in bots), "Bot name collision")
    remote_before = foundry("GET", "agents/" + REMOTE)
    if previous:
        require(remote_before == previous["remoteBefore"], "Remote changed since interrupted deployment")
    config = generated("buildIndexedAgentConfiguration(v)", {
        "accountName": "admin-3283-resource", "projectName": "teams-acl-demo",
        "agentName": AGENT, "connectionName": CONNECTION,
        "knowledgeBaseName": "teams-acl-demo-sp-indexed-kb",
        "searchEndpoint": "https://search-service-ypy2.search.windows.net",
        "location": "westus", "modelDeployment": "gpt-4.1-mini",
    })
    evidence = {
        "status": "executing", "startedAt": datetime.now(timezone.utc).isoformat(),
        "project": PROJECT, "agentName": AGENT, "connectionName": CONNECTION,
        "remoteBefore": remote_before, "phase1Readback": verification, "created": [],
        "publishScope": "Shared", "teamsMessagesSent": 0, "modelInvocations": 0,
        "newRoleAssignments": 0, "automaticEvaluation": "unavailable-no-Foundry-MCP-and-outside-two-message-budget",
    }
    if previous:
        evidence["resumedFrom"] = str(args.resume_connection_evidence)
        evidence["reused"] = [CONNECTION]

    def save():
        args.output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")

    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(evidence, stream, indent=2)
    try:
        if previous is None:
            rest("PUT", connection_url + "/" + CONNECTION + "?api-version=2025-10-01-preview",
                 config["connection"], create_only=True)
            evidence["created"].append(CONNECTION)
        actual_connection = rest("GET", connection_url + "/" + CONNECTION
                                 + "?api-version=2025-10-01-preview")
        matches(actual_connection, config["connection"], "connection")
        evidence["connection"] = actual_connection
        save()
        foundry("POST", "agents", config["agent"])
        evidence["created"].append(AGENT)
        agent = foundry("GET", "agents/" + AGENT)
        latest = agent["versions"]["latest"]
        expected_definition = json.loads(json.dumps(config["agent"]["definition"]))
        expected_definition["tools"][0]["allowed_tools"] = {
            "tool_names": ["knowledge_base_retrieve"]
        }
        matches(latest["definition"], expected_definition, "agent.definition")
        evidence["agent"] = agent
        save()
        client_id = agent["instance_identity"]["client_id"]
        bot = generated("buildIndexedBotConfiguration(v.endpoint,v.clientId,v.tenantId)", {
            "endpoint": PROJECT, "clientId": client_id, "tenantId": TENANT,
        })
        rest("PUT", "https://management.azure.com" + BOT_ID + "?api-version=2022-09-15",
             bot, create_only=True)
        evidence["created"].append(BOT_ID)
        bot_actual = rest("GET", "https://management.azure.com" + BOT_ID + "?api-version=2022-09-15")
        matches(bot_actual, bot, "bot")
        evidence["bot"] = bot_actual
        save()
        rest("PUT", "https://management.azure.com" + BOT_ID
             + "/channels/MsTeamsChannel?api-version=2022-09-15",
             {"location": "global", "properties": {
                 "channelName": "MsTeamsChannel", "properties": {"isEnabled": True},
             }}, create_only=True)
        evidence["created"].append("MsTeamsChannel")
        publish = generated("buildIndexedPublication(v.botId)", {"botId": BOT_ID})
        evidence["publicationRequest"] = publish
        save()
        evidence["publicationResponse"] = foundry(
            "POST", "agents/" + AGENT + "/microsoft365/publish", publish)
        save()
        published = foundry("GET", "agents/" + AGENT)
        schemes = published["agent_endpoint"]["authorization_schemes"]
        require(any(scheme["type"] == "BotServiceRbac" for scheme in schemes)
                and not any(scheme["type"] == "BotServiceTenant" for scheme in schemes),
                "Published endpoint does not retain restricted RBAC authorization")
        evidence["publishedAgent"] = published
        remote_after = foundry("GET", "agents/" + REMOTE)
        require(remote_before == remote_after, "Remote definition changed during this operation")
        evidence["remoteUnchanged"] = True
        evidence["status"] = "published-awaiting-real-Teams-tests"
    except (ValueError, RuntimeError, KeyError, subprocess.CalledProcessError) as error:
        evidence["status"] = "failed-no-automatic-retry"
        evidence["failure"] = str(error)
        raise
    finally:
        evidence["completedAt"] = datetime.now(timezone.utc).isoformat()
        save()
    print(json.dumps({key: evidence[key] for key in (
        "status", "agentName", "created", "publicationResponse", "remoteUnchanged")}, indent=2))


if __name__ == "__main__":
    main()
