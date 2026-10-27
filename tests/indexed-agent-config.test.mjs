import test from "node:test";
import assert from "node:assert/strict";
import {
  buildIndexedAgentConfiguration, buildIndexedBotConfiguration, buildIndexedPublication
} from "../scripts/indexed-agent-config.mjs";

const config = {
  accountName: "example-foundry", projectName: "teams-acl-demo",
  agentName: "teams-acl-demo-sp-indexed-agent",
  connectionName: "teams-acl-demo-sp-indexed-kb-mcp",
  knowledgeBaseName: "teams-acl-demo-sp-indexed-kb",
  searchEndpoint: "https://example-search.search.windows.net",
  location: "westus", modelDeployment: "gpt-4.1-mini"
};

test("Indexed agent reuses the project but only connects its own KB", () => {
  const built = buildIndexedAgentConfiguration(config);
  const tool = built.agent.definition.tools[0];
  assert.equal(built.projectEndpoint, "https://example-foundry.services.ai.azure.com/api/projects/teams-acl-demo");
  assert.equal(tool.project_connection_id, config.connectionName);
  assert.equal(tool.server_url, built.connection.properties.target);
  assert.match(tool.server_url, /\/teams-acl-demo-sp-indexed-kb\/mcp\?api-version=2026-08-01-preview$/);
  assert.deepEqual(tool.allowed_tools, ["knowledge_base_retrieve"]);
  assert.equal(built.agent.definition.tools.length, 1);
  assert.equal(built.agent.definition.model, "gpt-4.1-mini");
  assert.equal(tool.headers, undefined);
  assert.equal(built.agent.definition.structured_inputs, undefined);
  assert.equal(built.connection.properties.authType, "ProjectManagedIdentity");
  assert.equal(built.connection.properties.isSharedToAll, false);
  assert.equal(built.connection.properties.credentials, undefined);
});

test("Indexed config refuses Remote or mismatched resources", () => {
  for (const field of ["projectName", "agentName", "connectionName", "knowledgeBaseName"]) {
    assert.throws(() => buildIndexedAgentConfiguration({ ...config, [field]: "remote-other" }));
  }
});

test("Indexed Bot is separate, F0, single tenant and private at the Bot resource", () => {
  const clientId = "11111111-1111-4111-8111-111111111111";
  const tenantId = "22222222-2222-4222-8222-222222222222";
  const bot = buildIndexedBotConfiguration(
    buildIndexedAgentConfiguration(config).projectEndpoint, clientId, tenantId);
  assert.equal(bot.sku.name, "F0");
  assert.equal(bot.properties.msaAppType, "SingleTenant");
  assert.equal(bot.properties.publicNetworkAccess, "Disabled");
  assert.match(bot.properties.endpoint, /\/teams-acl-demo-sp-indexed-agent\/endpoint\//);
  assert.equal(bot.properties.msaAppId, clientId);
});

test("Shared publication never enables tenant-wide invocation or autopilot", () => {
  const id = "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/demo/providers/Microsoft.BotService/botServices/teams-acl-demo-sp-indexed-bot";
  const payload = buildIndexedPublication(id);
  assert.equal(payload.publishScope, "Shared");
  assert.equal(payload.publishAsAutopilot, false);
  assert.throws(() => buildIndexedPublication(id.replace("indexed", "remote")));
});
