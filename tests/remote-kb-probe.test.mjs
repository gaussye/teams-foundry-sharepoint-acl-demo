import assert from "node:assert/strict";
import test from "node:test";
import { buildRequest, checkEvidence, probe } from "../scripts/remote-kb-probe.mjs";

const NOW = 1_800_000_000_000;
const USER = "11111111-1111-4111-8111-111111111111";
const TENANT = "22222222-2222-4222-8222-222222222222";

function token(changes = {}) {
  const claims = {
    aud: "https://search.azure.com",
    oid: USER,
    tid: TENANT,
    exp: NOW / 1000 + 3600,
    scp: "user_impersonation",
    ...changes,
  };
  return [
    Buffer.from(JSON.stringify({ alg: "RS256" })).toString("base64url"),
    Buffer.from(JSON.stringify(claims)).toString("base64url"),
    "synthetic-not-a-valid-signature",
  ].join(".");
}

function options(changes = {}) {
  return {
    execute: true,
    endpoint: "https://synthetic-demo.search.windows.net",
    knowledgeBase: "synthetic-remote-kb",
    tenant: TENANT,
    expectedUser: USER,
    question: "Summarize the synthetic department plans.",
    expectedMarkers: ["PUBLIC_TEST", "FINANCE_TEST"],
    forbiddenMarkers: ["HR_TEST"],
    serviceToken: "synthetic-service-token",
    userToken: token(),
    ...changes,
  };
}

function payload(changes = {}) {
  return {
    response: [{ content: [{ type: "text", text: "PUBLIC_TEST FINANCE_TEST" }] }],
    references: [{ title: "PUBLIC_TEST", url: "https://example.invalid/demo" }],
    activity: [{ type: "remoteSharePoint", id: 0, error: null }],
    ...changes,
  };
}

test("per-request user token is in a header, never in the natural-language body", () => {
  const config = options();
  const request = buildRequest(config, NOW);
  assert.equal(request.init.headers.Authorization, "Bearer synthetic-service-token");
  assert.equal(request.init.headers["x-ms-query-source-authorization"], config.userToken);
  assert.equal(request.init.body.includes(config.userToken), false);
  assert.equal(request.init.redirect, "error");
  assert.match(request.url, /\/knowledgebases\/synthetic-remote-kb\/retrieve\?/);
});

for (const [name, changes, message] of [
  ["missing user token", { userToken: "" }, /Delegated user access token is required/],
  ["missing service token", { serviceToken: "" }, /Service access token is required/],
  ["expired token", { userToken: token({ exp: NOW / 1000 - 1 }) }, /expired/],
  ["token close to expiry", { userToken: token({ exp: NOW / 1000 + 10 }) }, /expiry/],
  ["future token", { userToken: token({ nbf: NOW / 1000 + 300 }) }, /not yet valid/],
  ["Graph token", { userToken: token({ aud: "https://graph.microsoft.com" }) }, /target Azure AI Search/],
  ["other user", { userToken: token({ oid: TENANT }) }, /expected user and tenant/],
  ["other tenant", { userToken: token({ tid: USER }) }, /expected user and tenant/],
  ["app-only token", { userToken: token({ scp: undefined, roles: ["Reader"], idtyp: "app" }) }, /delegated user/],
  ["external endpoint", { endpoint: "https://example.invalid" }, /Azure public-cloud Search/],
  ["HTTP endpoint", { endpoint: "http://synthetic-demo.search.windows.net" }, /HTTPS/],
  ["endpoint with credentials", { endpoint: "https://user:secret@synthetic-demo.search.windows.net" }, /HTTPS/],
  ["invalid KB name", { knowledgeBase: "../other-kb" }, /name is invalid/],
  ["empty question", { question: " " }, /question is required/],
  ["oversized question", { question: "a".repeat(1501) }, /1500/],
]) {
  test(`${name} is rejected before any network request`, async () => {
    let calls = 0;
    await assert.rejects(
      probe(options(changes), {
        now: NOW,
        fetchImpl: async () => {
          calls++;
          throw new Error("Network must not be called.");
        },
      }),
      message,
    );
    assert.equal(calls, 0);
  });
}

test("live retrieval is opt-in, with no automatic paid request", async () => {
  await assert.rejects(probe(options({ execute: false })), /requires --execute/);
});

test("model-free KB request uses one semantic intent and minimal extractive retrieval", () => {
  const config = options();
  const body = JSON.parse(buildRequest(config, NOW).init.body);
  assert.deepEqual(body.intents, [{ type: "semantic", search: config.question }]);
  assert.equal(body.messages, undefined);
  assert.deepEqual(body.retrievalReasoningEffort, { kind: "minimal" });
  assert.equal(body.outputMode, "extractiveData");
  assert.equal(body.includeActivity, true);
});

test("both expected authorized markers and source references are required", () => {
  assert.equal(checkEvidence(payload(), ["PUBLIC_TEST", "FINANCE_TEST"], ["HR_TEST"]).passed, true);
  assert.equal(checkEvidence(payload(), ["MISSING_TEST"], ["HR_TEST"]).passed, false);
  assert.throws(
    () => checkEvidence(payload({ references: [] }), ["PUBLIC_TEST"], ["HR_TEST"]),
    /nonempty source references/,
  );
});

test("forbidden title, URL or activity content cannot pass the marker check", () => {
  for (const changes of [
    { references: [{ title: "HR_TEST" }] },
    { references: [{ url: "https://example.invalid/HR_TEST" }] },
    { activity: [{ diagnostic: "HR_TEST" }] },
  ]) {
    assert.equal(
      checkEvidence(payload(changes), ["PUBLIC_TEST"], ["HR_TEST"]).passed,
      false,
    );
  }
});

test("invalid marker configuration is rejected before a paid request", async () => {
  let calls = 0;
  await assert.rejects(
    probe(options({ forbiddenMarkers: [] }), {
      now: NOW,
      fetchImpl: async () => { calls++; },
    }),
    /nonempty expected and forbidden/,
  );
  assert.equal(calls, 0);
});

test("overlapping allowed and forbidden markers are rejected", () => {
  assert.throws(
    () => checkEvidence(payload(), ["PUBLIC_TEST"], ["PUBLIC_TEST"]),
    /both expected and forbidden/,
  );
});

test("401/claims challenge causes one failure, not an app-only fallback or retry", async () => {
  let calls = 0;
  await assert.rejects(
    probe(options(), {
      now: NOW,
      fetchImpl: async () => {
        calls++;
        return new Response(null, {
          status: 401,
          headers: { "www-authenticate": 'Bearer error="insufficient_claims"' },
        });
      },
    }),
    /HTTP 401.*no fallback.*challenge present/,
  );
  assert.equal(calls, 1);
});

test("local simulated response produces marker evidence, not raw documents or tokens", async () => {
  const result = await probe(options(), {
    now: NOW,
    fetchImpl: async () => Response.json(payload()),
  });
  assert.equal(result.passed, true);
  assert.equal(result.referenceCount, 1);
  const output = JSON.stringify(result);
  assert.equal(output.includes("synthetic-service-token"), false);
  assert.equal(output.includes("example.invalid"), false);
  assert.equal(output.includes(token()), false);
});

test("HTTP 206 with all expected markers still fails without retry or reading partial content", async () => {
  let calls = 0;
  await assert.rejects(
    probe(options(), {
      now: NOW,
      fetchImpl: async () => {
        calls++;
        return {
          status: 206,
          headers: new Headers(),
          json: async () => { throw new Error("Partial content must not be inspected."); },
        };
      },
    }),
    /HTTP 206.*Partial retrieval/,
  );
  assert.equal(calls, 1);
});

for (const [name, changes, message] of [
  ["top-level error", { error: { message: "SYNTHETIC_ERROR_DETAILS" } }, /top-level error/],
  ["failed activity", { activity: [{ type: "remoteSharePoint", error: { code: "Forbidden" } }] }, /activity reports a failure/],
  ["missing activity", { activity: undefined }, /valid activity records/],
  ["empty activity", { activity: [] }, /valid activity records/],
  ["null activity record", { activity: [null] }, /valid activity records/],
  ["array activity record", { activity: [[]] }, /valid activity records/],
]) {
  test(`HTTP 200 with ${name} cannot pass despite authorized markers`, async () => {
    let calls = 0;
    await assert.rejects(
      probe(options(), {
        now: NOW,
        fetchImpl: async () => {
          calls++;
          return Response.json(payload(changes));
        },
      }),
      (error) => {
        assert.match(error.message, message);
        assert.equal(error.message.includes("SYNTHETIC_ERROR_DETAILS"), false);
        return true;
      },
    );
    assert.equal(calls, 1);
  });
}
