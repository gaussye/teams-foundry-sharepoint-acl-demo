import test from "node:test";
import assert from "node:assert/strict";
import {
  assertPersonalTeamsManifest,
  buildBotConfiguration,
  buildFoundryConfiguration
} from "../scripts/foundry-agent-config.mjs";

const config = {
  accountName: "example-foundry",
  projectName: "teams-acl-demo",
  agentName: "teams-acl-demo-sp-remote-agent",
  connectionName: "teams-acl-demo-sp-remote-kb-mcp",
  knowledgeBaseName: "teams-acl-demo-sp-remote-kb",
  searchEndpoint: "https://example-search.search.windows.net",
  location: "westus",
  modelDeployment: "gpt-4.1-mini"
};
const clientId = "11111111-1111-4111-8111-111111111111";
const tenantId = "22222222-2222-4222-8222-222222222222";
const manifest = () => ({
  bots: [{ botId: clientId, scopes: ["personal"] }],
  webApplicationInfo: { id: clientId },
  copilotAgents: { customEngineAgents: [{ id: clientId }] }
});

test("uses an isolated managed-identity project and the approved model", () => {
  const built = buildFoundryConfiguration(config);
  assert.deepEqual(built.project.identity, { type: "SystemAssigned" });
  assert.equal(built.agent.definition.kind, "prompt");
  assert.equal(built.agent.definition.model, "gpt-4.1-mini");
  assert.equal(built.projectEndpoint, "https://example-foundry.services.ai.azure.com/api/projects/teams-acl-demo");
});

test("connects only the Remote KB retrieve tool without static user credentials", () => {
  const built = buildFoundryConfiguration(config);
  assert.equal(built.connection.properties.authType, "ProjectManagedIdentity");
  assert.equal(built.connection.properties.target, built.agent.definition.tools[0].server_url);
  assert.equal(built.connection.properties.audience, "https://search.azure.com/");
  assert.equal(built.agent.definition.tools.length, 1);
  assert.deepEqual(built.agent.definition.tools[0].allowed_tools, ["knowledge_base_retrieve"]);
  assert.equal(built.agent.definition.tools[0].headers, undefined);
  assert.equal(built.agent.definition.structured_inputs, undefined);
  assert.equal(built.connection.properties.credentials, undefined);
});

for (const searchEndpoint of [
  "http://example-search.search.windows.net",
  "https://example.invalid",
  "https://user:password@example-search.search.windows.net",
  "https://example-search.search.windows.net/path",
  "https://example-search.search.windows.net/?token=example",
  "https://example-search.search.windows.net/#fragment"
]) {
  test(`rejects unapproved Search endpoint ${searchEndpoint}`, () => {
    assert.throws(() => buildFoundryConfiguration({ ...config, searchEndpoint }));
  });
}

test("rejects a different model, region, or injected resource name", () => {
  for (const change of [
    { location: "eastus" },
    { modelDeployment: "gpt-4o" },
    { projectName: "../other" },
    { agentName: "agent?api-version=other" }
  ]) {
    assert.throws(() => buildFoundryConfiguration({ ...config, ...change }));
  }
});

test("bot is single-tenant F0 and routes to this real Foundry agent", () => {
  const built = buildFoundryConfiguration(config);
  const bot = buildBotConfiguration(built.projectEndpoint, config.agentName, clientId, tenantId);
  assert.equal(bot.sku.name, "F0");
  assert.equal(bot.properties.msaAppType, "SingleTenant");
  assert.equal(bot.properties.publicNetworkAccess, "Disabled");
  assert.equal(bot.properties.endpoint, `${built.projectEndpoint}/agents/${config.agentName}/endpoint/protocols/activityProtocol?api-version=v1`);
  const url = new URL(bot.properties.endpoint);
  assert.equal(url.pathname, `/api/projects/${config.projectName}/agents/${config.agentName}/endpoint/protocols/activityProtocol`);
  assert.deepEqual([...url.searchParams], [["api-version", "v1"]]);
  assert.equal(bot.properties.msaAppId, clientId);
  assert.equal(bot.properties.msaAppTenantId, tenantId);
  assert.throws(() => buildBotConfiguration("https://example.invalid/api/projects/test", config.agentName, clientId, tenantId));
  assert.throws(() => buildBotConfiguration(built.projectEndpoint, config.agentName, "not-an-id", tenantId));
});

test("accepts only the expected personal-only generated agent package", () => {
  assert.equal(assertPersonalTeamsManifest(manifest(), clientId), true);
});

test("rejects channel scopes, other identities, extra bots and added permissions", () => {
  const changes = [
    value => { value.bots[0].scopes.push("groupChat"); },
    value => { value.bots[0].scopes = ["team"]; },
    value => { value.bots[0].botId = tenantId; },
    value => { value.bots.push({ botId: clientId, scopes: ["personal"] }); },
    value => { value.webApplicationInfo.id = tenantId; },
    value => { value.copilotAgents.customEngineAgents[0].id = tenantId; },
    value => { value.authorization = { permissions: {} }; }
  ];
  for (const change of changes) {
    const value = manifest();
    change(value);
    assert.throws(() => assertPersonalTeamsManifest(value, clientId));
  }
});
