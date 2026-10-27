import test from "node:test";
import assert from "node:assert/strict";
import { captureIndexedSources } from "../scripts/indexed-browser-capture.mjs";

const siteUrl = "https://example.sharepoint.com/sites/test";
const group = "11111111-1111-4111-8111-111111111111";
const owner = {
  Id: 6, PrincipalType: 4,
  LoginName: `c:0o.c|federateddirectoryclaimprovider|${group}_o`
};
const config = {
  siteUrl, adminLogin: "i:0#.f|membership|admin@example.test",
  lists: {
    TeamsAclDemoPublic: "00000000-0000-4000-8000-000000000001",
    TeamsAclDemoFinance: "00000000-0000-4000-8000-000000000002",
    TeamsAclDemoHR: "00000000-0000-4000-8000-000000000003"
  }
};
const filenames = {
  TeamsAclDemoPublic: "ACLDEMO_PUBLIC_20261024_P731.docx",
  TeamsAclDemoFinance: "ACLDEMO_FINANCE_20261024_F482.docx",
  TeamsAclDemoHR: "ACLDEMO_HR_20261024_H965.docx"
};

function setup(t, change = () => {}) {
  const saved = { fetch: globalThis.fetch, location: globalThis.location };
  t.after(() => {
    globalThis.fetch = saved.fetch;
    if (saved.location === undefined) delete globalThis.location;
    else globalThis.location = saved.location;
  });
  globalThis.location = {
    origin: "https://example.sharepoint.com", pathname: "/sites/test/_api/web/currentuser"
  };
  const requests = [], counts = new Map();
  globalThis.fetch = async (url, options) => {
    assert.equal(options.method, "GET");
    assert.equal(options.credentials, "same-origin");
    assert.equal(options.redirect, "error");
    assert.equal(new URL(url).origin, globalThis.location.origin);
    requests.push(url);
    const path = new URL(url).pathname;
    const count = (counts.get(path) || 0) + 1;
    counts.set(path, count);
    let body;
    if (path.endsWith("/currentuser")) {
      body = { LoginName: config.adminLogin, IsSiteAdmin: true };
    } else if (path.endsWith("/siteusers")) {
      body = { value: [{ ...owner, IsSiteAdmin: true }] };
    } else if (path.endsWith("/users")) {
      body = { value: [
        { Id: 1073741823, PrincipalType: 1, LoginName: "SHAREPOINT\\system" }, owner
      ] };
    } else if (path.endsWith("/$value")) {
      assert.equal(options.headers["If-Match"], '"1"');
      return new Response(new Uint8Array([80, 75, 1, 2]));
    } else {
      const id = /lists\(guid'([^']+)'\)/.exec(path)?.[1];
      const library = Object.keys(config.lists).find(k => config.lists[k] === id);
      assert.ok(library);
      if (path.endsWith("/items(1)")) {
        body = { Id: 1, HasUniqueRoleAssignments: false,
          FileDirRef: `/sites/test/${library}`,
          FileRef: `/sites/test/${library}/${filenames[library]}`,
          File: { ETag: '"1"', UniqueId: id, Length: "4" } };
      } else {
        body = { Title: library, HasUniqueRoleAssignments: true, RoleAssignments: [
          { Member: { Id: 3, PrincipalType: 8, LoginName: "Owners" } },
          { Member: { Id: 12, PrincipalType: 4, LoginName: `c:0t.c|tenant|${group}` } }
        ] };
      }
    }
    change(body, path, count);
    return Response.json(body);
  };
  return requests;
}

test("captures exactly three contents once with source versions and raw ACLs", async t => {
  const requests = setup(t);
  const result = await captureIndexedSources(config);
  assert.equal(result.binary.length, 3);
  assert.equal(requests.filter(url => url.endsWith("/$value")).length, 3);
  assert.equal(result.snapshot.documents.length, 3);
  for (const doc of result.snapshot.documents) {
    assert.equal(doc.contentSha256.length, 64);
    assert.equal(doc.download.etag, '"1"');
    assert.deepEqual(doc.libraryBefore.body, doc.libraryAfter.body);
  }
});

test("wrong caller stops before content download", async t => {
  const requests = setup(t, (body, path) => {
    if (path.endsWith("/currentuser")) body.LoginName = "another-user";
  });
  await assert.rejects(captureIndexedSources(config), /Wrong source user/);
  assert.equal(requests.length, 1);
});

test("unknown source principal stops before content download", async t => {
  const requests = setup(t, body => {
    if (body.RoleAssignments) body.RoleAssignments[1].Member.LoginName = "unknown";
  });
  await assert.rejects(captureIndexedSources(config), /Unknown source ACL principal/);
  assert.equal(requests.filter(url => url.endsWith("/$value")).length, 0);
});

test("pagination stops without silently truncating permissions", async t => {
  const requests = setup(t, (body, path) => {
    if (path.endsWith("/users")) body["@odata.nextLink"] = "next";
  });
  await assert.rejects(captureIndexedSources(config), /Incomplete source response/);
  assert.equal(requests.filter(url => url.endsWith("/$value")).length, 0);
});

test("changed content invalidates capture, without a second download", async t => {
  const requests = setup(t, (body, path, count) => {
    if (path.endsWith("/items(1)") && count === 2) body.File.ETag = '"2"';
  });
  await assert.rejects(captureIndexedSources(config), /Source ACL\/content changed/);
  assert.equal(requests.filter(url => url.endsWith("/$value")).length, 3);
});

test("wrong page rejects before all network access", async t => {
  const requests = setup(t);
  globalThis.location.pathname = "/other";
  await assert.rejects(captureIndexedSources(config), /Wrong browser origin/);
  assert.equal(requests.length, 0);
});
