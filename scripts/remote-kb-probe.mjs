import { parseArgs } from "node:util";
import { pathToFileURL } from "node:url";

const API_VERSION = "2026-08-01-preview";
const GUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function required(value, name) {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`${name} is required.`);
  }
  return value.trim();
}

export function buildRequest(options, now = Date.now()) {
  const endpoint = new URL(required(options.endpoint, "Search endpoint"));
  if (
    endpoint.protocol !== "https:" ||
    !/^[a-z0-9-]+\.search\.windows\.net$/i.test(endpoint.hostname) ||
    endpoint.port ||
    endpoint.username ||
    endpoint.password ||
    endpoint.pathname !== "/" ||
    endpoint.search ||
    endpoint.hash
  ) {
    throw new Error("Use an HTTPS Azure public-cloud Search service endpoint.");
  }
  const knowledgeBase = required(options.knowledgeBase, "Knowledge base");
  if (!/^[a-z0-9][a-z0-9-]{0,127}$/.test(knowledgeBase)) {
    throw new Error("Knowledge base name is invalid.");
  }
  const question = required(options.question, "Synthetic test question");
  if (question.length > 1500) {
    throw new Error("Synthetic test question must not exceed 1500 characters.");
  }
  const serviceToken = required(options.serviceToken, "Service access token");
  const userToken = required(options.userToken, "Delegated user access token");
  const expectedUser = required(options.expectedUser, "Expected user object ID");
  const tenant = required(options.tenant, "Expected tenant ID");
  if (!GUID.test(expectedUser) || !GUID.test(tenant)) {
    throw new Error("Expected user and tenant must be Entra object IDs.");
  }
  if (/\s/.test(serviceToken) || /\s/.test(userToken)) {
    throw new Error("Provide raw access tokens without a Bearer prefix.");
  }

  let claims;
  try {
    const parts = userToken.split(".");
    if (parts.length !== 3 || !parts.every(Boolean)) {
      throw new Error("Invalid JWT structure.");
    }
    claims = JSON.parse(Buffer.from(parts[1], "base64url").toString("utf8"));
  } catch {
    throw new Error("Delegated user access token must have a readable JWT payload.");
  }
  // Diagnostic guards only. Search must validate the actual token signature.
  if (!claims || typeof claims !== "object" || Array.isArray(claims)) {
    throw new Error("Delegated user token payload is invalid.");
  }
  if (
    typeof claims.exp !== "number" ||
    !Number.isFinite(claims.exp) ||
    claims.exp <= now / 1000 + 30
  ) {
    throw new Error("Delegated user token is expired or too close to expiry.");
  }
  if (
    claims.nbf !== undefined &&
    (typeof claims.nbf !== "number" || !Number.isFinite(claims.nbf) || claims.nbf > now / 1000)
  ) {
    throw new Error("Delegated user token is not yet valid.");
  }
  if (!["https://search.azure.com", "https://search.azure.com/"].includes(claims.aud)) {
    throw new Error("Delegated user token must target Azure AI Search, not Graph or the bot.");
  }
  if (
    typeof claims.oid !== "string" ||
    typeof claims.tid !== "string" ||
    claims.oid.toLowerCase() !== expectedUser.toLowerCase() ||
    claims.tid.toLowerCase() !== tenant.toLowerCase()
  ) {
    throw new Error("Delegated user token does not match the expected user and tenant.");
  }
  if (typeof claims.scp !== "string" || !claims.scp.trim() || claims.idtyp === "app") {
    throw new Error("A delegated user token is required; app-only fallback is forbidden.");
  }

  return {
    url: `${endpoint.origin}/knowledgebases/${knowledgeBase}/retrieve?api-version=${API_VERSION}`,
    init: {
      method: "POST",
      redirect: "error",
      headers: {
        Authorization: `Bearer ${serviceToken}`,
        "x-ms-query-source-authorization": userToken,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        intents: [{ type: "semantic", search: question }],
        retrievalReasoningEffort: { kind: "minimal" },
        outputMode: "extractiveData",
        includeActivity: true,
      }),
    },
  };
}

export function checkEvidence(payload, expectedMarkers, forbiddenMarkers) {
  if (
    !Array.isArray(expectedMarkers) ||
    !Array.isArray(forbiddenMarkers) ||
    expectedMarkers.length === 0 ||
    forbiddenMarkers.length === 0 ||
    [...expectedMarkers, ...forbiddenMarkers].some(
      (marker) => typeof marker !== "string" || !marker.trim(),
    )
  ) {
    throw new Error("Specify nonempty expected and forbidden synthetic markers.");
  }
  if (expectedMarkers.some((marker) => forbiddenMarkers.includes(marker))) {
    throw new Error("A marker cannot be both expected and forbidden.");
  }
  if (
    !payload ||
    !Array.isArray(payload.response) ||
    !Array.isArray(payload.references) ||
    payload.references.length === 0
  ) {
    throw new Error("Retrieval must return response and nonempty source references.");
  }
  const responseAndReferences = JSON.stringify({
    response: payload.response,
    references: payload.references,
  });
  const allOutput = JSON.stringify(payload);
  const expected = expectedMarkers.map((marker) => ({
    marker,
    found: responseAndReferences.includes(marker),
  }));
  const forbidden = forbiddenMarkers.map((marker) => ({
    marker,
    found: allOutput.includes(marker),
  }));
  return {
    passed: expected.every((item) => item.found) && forbidden.every((item) => !item.found),
    referenceCount: payload.references.length,
    expected,
    forbidden,
  };
}

export async function probe(options, { fetchImpl = fetch, now = Date.now() } = {}) {
  if (options.execute !== true) {
    throw new Error("Live retrieval requires --execute and separately approved licensing and charges.");
  }
  // Validate the evidence configuration before any potentially billable request.
  checkEvidence(
    { response: [], references: [{}] },
    options.expectedMarkers,
    options.forbiddenMarkers,
  );
  const request = buildRequest(options, now);
  const response = await fetchImpl(request.url, {
    ...request.init,
    signal: AbortSignal.timeout(60_000),
  });
  if (response.status !== 200) {
    const challenge = response.headers.get("www-authenticate");
    const reason =
      response.status === 401 || response.status === 403
        ? "Authorization failed. Complete any required consent or CA sign-in challenge; no fallback attempted."
        : response.status === 206
          ? "Partial retrieval is not a passing result; no automatic retry attempted."
          : "Retrieval failed; no automatic retry attempted.";
    throw new Error(`HTTP ${response.status}. ${reason}${challenge ? " Authentication challenge present." : ""}`);
  }
  const payload = await response.json();
  if (payload?.error != null) {
    throw new Error("Retrieval returned a top-level error; result rejected.");
  }
  if (
    !Array.isArray(payload?.activity) ||
    payload.activity.length === 0 ||
    payload.activity.some(
      (activity) => !activity || typeof activity !== "object" || Array.isArray(activity),
    )
  ) {
    throw new Error("Retrieval must return nonempty valid activity records.");
  }
  if (payload.activity.some((activity) => activity.error != null)) {
    throw new Error("Retrieval activity reports a failure; partial evidence rejected.");
  }
  return {
    test: "remote-kb-synthetic-marker-probe",
    expectedUser: options.expectedUser,
    ...checkEvidence(payload, options.expectedMarkers, options.forbiddenMarkers),
  };
}

async function main() {
  const { values } = parseArgs({
    options: {
      help: { type: "boolean" },
      execute: { type: "boolean", default: false },
      endpoint: { type: "string" },
      kb: { type: "string" },
      tenant: { type: "string" },
      user: { type: "string" },
      question: { type: "string" },
      expected: { type: "string", multiple: true },
      forbidden: { type: "string", multiple: true },
    },
  });
  if (values.help) {
    console.log(`Remote SharePoint KB probe (Azure public cloud; no resources are created).
Requires approved licensing, an existing demo remote KB, and real delegated A/B tokens.
Tokens must be supplied through SEARCH_SERVICE_ACCESS_TOKEN and SEARCH_USER_ACCESS_TOKEN.
--execute --endpoint URL --kb NAME --tenant ID --user OBJECT_ID --question TEXT
--expected PUBLIC_MARKER --expected OWN_GROUP_MARKER --forbidden OTHER_GROUP_MARKER
Run separately for A and B. Output contains synthetic marker evidence, not document text.
Local JWT checks are diagnostic, not signature verification. This is not a Teams test.`);
    return;
  }
  const result = await probe({
    execute: values.execute,
    endpoint: values.endpoint,
    knowledgeBase: values.kb,
    tenant: values.tenant,
    expectedUser: values.user,
    question: values.question,
    expectedMarkers: values.expected,
    forbiddenMarkers: values.forbidden,
    serviceToken: process.env.SEARCH_SERVICE_ACCESS_TOKEN,
    userToken: process.env.SEARCH_USER_ACCESS_TOKEN,
  });
  console.log(JSON.stringify(result, null, 2));
  if (!result.passed) process.exitCode = 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
