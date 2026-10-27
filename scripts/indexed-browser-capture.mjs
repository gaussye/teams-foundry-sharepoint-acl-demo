export async function captureIndexedSources(config) {
  const files = {
    TeamsAclDemoPublic: "ACLDEMO_PUBLIC_20261024_P731.docx",
    TeamsAclDemoFinance: "ACLDEMO_FINANCE_20261024_F482.docx",
    TeamsAclDemoHR: "ACLDEMO_HR_20261024_H965.docx"
  };
  const fail = message => { throw new Error(message); };
  if (location.origin !== config.siteUrl.split("/sites/")[0] ||
      location.pathname !== "/sites/test/_api/web/currentuser" ||
      Object.keys(config.lists).sort().join() !== Object.keys(files).sort().join()) {
    fail("Wrong browser origin, page, or approved library scope");
  }
  const check = value => {
    if (!value || typeof value !== "object") return;
    if (["@odata.nextLink", "odata.nextLink", "__next", "__deferred"].some(k => k in value)) {
      fail("Incomplete source response; no content download");
    }
    Object.values(value).forEach(check);
  };
  const get = async path => {
    const url = config.siteUrl + "/_api/" + path;
    const response = await fetch(url, {
      method: "GET", credentials: "same-origin", redirect: "error", cache: "no-store",
      headers: { Accept: "application/json;odata=nometadata" }
    });
    if (!response.ok) fail(`Source GET failed: ${response.status}`);
    const body = await response.json();
    check(body);
    return { url, observedAt: new Date().toISOString(), body };
  };
  const items = record => {
    const value = record.body.value;
    if (!Array.isArray(value)) fail("Expected complete collection");
    return value;
  };
  const snapshot = {
    version: 1, siteUrl: config.siteUrl, adminLogin: config.adminLogin,
    spGroups: {}, owners: {}, entraGroups: {}, documents: []
  };
  snapshot.currentUser = await get("web/currentuser?$select=Id,LoginName,IsSiteAdmin");
  if (snapshot.currentUser.body.LoginName.toLowerCase() !== config.adminLogin.toLowerCase() ||
      snapshot.currentUser.body.IsSiteAdmin !== true) fail("Wrong source user");
  const adminsPath = "web/siteusers?$filter=IsSiteAdmin eq true&$select=Id,LoginName,PrincipalType,IsSiteAdmin";
  const membersPath = "web/sitegroups/getbyid(3)/users?$select=Id,LoginName,PrincipalType";
  snapshot.siteAdmins = await get(adminsPath);
  snapshot.spGroups["3"] = await get(membersPath);
  const libraryQuery = "?$select=Title,HasUniqueRoleAssignments,RoleAssignments/Member/Id,RoleAssignments/Member/LoginName,RoleAssignments/Member/PrincipalType,RoleAssignments/RoleDefinitionBindings/Name,RoleAssignments/RoleDefinitionBindings/BasePermissions&$expand=RoleAssignments/Member,RoleAssignments/RoleDefinitionBindings";
  const itemQuery = "/items(1)?$select=Id,FileRef,FileDirRef,HasUniqueRoleAssignments,File/ETag,File/UniqueId,File/Length&$expand=File";
  for (const [library, filename] of Object.entries(files)) {
    const listId = config.lists[library];
    if (!/^[0-9a-f-]{36}$/.test(listId)) fail("Invalid source list ID");
    const prefix = `web/lists(guid'${listId}')`;
    const libraryBefore = await get(prefix + libraryQuery);
    const itemBefore = await get(prefix + itemQuery);
    const lib = libraryBefore.body, item = itemBefore.body;
    if (lib.Title !== library || lib.HasUniqueRoleAssignments !== true ||
        item.Id !== 1 || item.FileRef !== `/sites/test/${library}/${filename}` ||
        item.FileDirRef !== `/sites/test/${library}` || item.HasUniqueRoleAssignments !== false ||
        !item.File?.ETag || !item.File.UniqueId || Number(item.File.Length) > 1000000) {
      fail("Unexpected library, file version, or inheritance");
    }
    if (!Array.isArray(lib.RoleAssignments) || lib.RoleAssignments.length !== 2) {
      fail("Unexpected source ACL shape");
    }
    const grants = lib.RoleAssignments.map(g => g.Member);
    if (!grants.some(g => g.PrincipalType === 8 && g.Id === 3) ||
        !grants.some(g => g.PrincipalType === 4 && /^c:0t\.c\|tenant\|[0-9a-f-]{36}$/.test(g.LoginName))) {
      fail("Unknown source ACL principal");
    }
    snapshot.documents.push({ library, listId, contentFile: filename,
      libraryBefore, itemBefore, prefix });
  }
  for (const member of [...items(snapshot.spGroups["3"]), ...items(snapshot.siteAdmins)]) {
    if (member.LoginName === "SHAREPOINT\\system" &&
        member.Id === 1073741823 && member.PrincipalType === 1) continue;
    if (member.PrincipalType !== 4 ||
        !/^c:0o\.c\|federateddirectoryclaimprovider\|[0-9a-f-]{36}_o$/.test(member.LoginName)) {
      fail("Unknown owner or site administrator principal");
    }
  }
  const binary = [];
  for (const document of snapshot.documents) {
    const item = document.itemBefore.body;
    const url = config.siteUrl + "/_api/web/GetFileByServerRelativeUrl('" + item.FileRef + "')/$value";
    const response = await fetch(url, {
      method: "GET", credentials: "same-origin", redirect: "error", cache: "no-store",
      headers: { "If-Match": item.File.ETag }
    });
    if (!response.ok) fail(`DOCX GET failed: ${response.status}`);
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.length !== Number(item.File.Length) || bytes[0] !== 80 || bytes[1] !== 75) {
      fail("DOCX bytes do not match source metadata");
    }
    const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
    document.contentSha256 = Array.from(digest, v => v.toString(16).padStart(2, "0")).join("");
    document.download = { url, observedAt: new Date().toISOString(),
      etag: item.File.ETag, sizeBytes: bytes.length };
    binary.push({ name: document.contentFile,
      base64: btoa(Array.from(bytes, v => String.fromCharCode(v)).join("")) });
  }
  for (const document of snapshot.documents) {
    document.libraryAfter = await get(document.prefix + libraryQuery);
    document.itemAfter = await get(document.prefix + itemQuery);
    if (JSON.stringify(document.libraryBefore.body) !== JSON.stringify(document.libraryAfter.body) ||
        JSON.stringify(document.itemBefore.body) !== JSON.stringify(document.itemAfter.body)) {
      fail("Source ACL/content changed during capture");
    }
    delete document.prefix;
  }
  const adminsAfter = await get(adminsPath), membersAfter = await get(membersPath);
  if (JSON.stringify(snapshot.siteAdmins.body) !== JSON.stringify(adminsAfter.body) ||
      JSON.stringify(snapshot.spGroups["3"].body) !== JSON.stringify(membersAfter.body)) {
    fail("Source owners or site administrators changed during capture");
  }
  snapshot.siteAdminsAfter = adminsAfter;
  snapshot.spGroupsAfter = { "3": membersAfter };
  return { snapshot, binary };
}
