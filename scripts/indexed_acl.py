"""Offline, fail-closed preparation for the three-document Indexed POC.

Inputs are trusted operator captures, not the historical observation summary.
This module never authenticates, downloads, changes roles, or calls Search.
"""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid
import xml.etree.ElementTree as ET
import zipfile


FILES = {
    "TeamsAclDemoPublic": "ACLDEMO_PUBLIC_20261024_P731.docx",
    "TeamsAclDemoFinance": "ACLDEMO_FINANCE_20261024_F482.docx",
    "TeamsAclDemoHR": "ACLDEMO_HR_20261024_H965.docx",
}
INDEX = "teams-acl-demo-sp-indexed-index"
SOURCE = "teams-acl-demo-sp-indexed-ks"
KB = "teams-acl-demo-sp-indexed-kb"
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
    "d": "http://schemas.microsoft.com/ado/2007/08/dataservices",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
}
ROLES = {
    "Read": ("176", "138612833"),
    "Full Control": ("2147483647", "4294967295"),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def guid(value):
    require(isinstance(value, str), "Missing Entra object ID")
    parsed = str(uuid.UUID(value))
    require(parsed == value.lower(), "Expected canonical GUID")
    require(parsed != str(uuid.UUID(int=0)), "Empty GUID")
    return parsed


def complete(value):
    if isinstance(value, dict):
        require(not any(key in value for key in (
            "@odata.nextLink", "odata.nextLink", "__next", "__deferred"
        )), "Incomplete or deferred response")
        for child in value.values():
            complete(child)
    elif isinstance(value, list):
        for child in value:
            complete(child)
    return value


def xml_value(element):
    kind = element.get(f"{{{NS['m']}}}type", "")
    if element.get(f"{{{NS['m']}}}null") == "true":
        return None
    if len(element):
        return {child.tag.split("}")[-1]: xml_value(child) for child in element}
    text = element.text or ""
    if kind == "Edm.Boolean":
        require(text in ("true", "false"), "Invalid XML boolean")
        return text == "true"
    if kind in ("Edm.Int32", "Edm.Int64"):
        return int(text)
    return text


def atom(element):
    require(not element.findall(".//a:link[@rel='next']", NS), "Paged XML response")
    if element.tag == f"{{{NS['a']}}}feed":
        return [atom(entry) for entry in element.findall("a:entry", NS)]
    require(element.tag == f"{{{NS['a']}}}entry", "Expected Atom entry or feed")
    props = element.find("a:content/m:properties", NS)
    require(props is not None, "Missing OData properties")
    result = xml_value(props)
    for link in element.findall("a:link", NS):
        inline = link.find("m:inline", NS)
        if inline is not None:
            require(len(inline) == 1, "Missing expanded relationship")
            result[link.attrib["title"]] = atom(inline[0])
    return result


def unwrap(body):
    if isinstance(body, str):
        require("<!DOCTYPE" not in body.upper() and "<!ENTITY" not in body.upper(),
                "XML declarations are not supported")
        return complete(atom(ET.fromstring(body)))
    complete(body)
    if isinstance(body, dict) and "d" in body:
        body = body["d"]
    if isinstance(body, dict) and "results" in body:
        return body["results"]
    if isinstance(body, dict) and "value" in body:
        return body["value"]
    return body


def rows(value):
    value = unwrap(value)
    require(isinstance(value, list), "Expected a fully expanded collection")
    return value


def capture(record, prefix, now):
    require(record["url"].startswith(prefix), "Capture source does not match")
    observed = datetime.fromisoformat(record["observedAt"].replace("Z", "+00:00"))
    require(observed.tzinfo is not None, "Capture time must include timezone")
    require(0 <= (now - observed).total_seconds() <= 900, "Stale/future capture")
    return unwrap(record["body"])


class Resolver:
    def __init__(self, snapshot, now):
        self.snapshot, self.now = snapshot, now
        self.audit = []
        self.users, self.groups = set(), set()

    def principal(self, principal, stack=()):
        login, kind = principal["LoginName"], principal["PrincipalType"]
        if login == "SHAREPOINT\\system":
            require(kind == 1 and principal["Id"] == 1073741823,
                    "Unrecognized system principal")
            self.audit.append({"principal": login, "action": "no-Entra-query-grant"})
            return
        if kind == 8:
            group = str(principal["Id"])
            require(group not in stack, "SharePoint group cycle")
            record = self.snapshot["spGroups"][group]
            members = capture(record, self.snapshot["siteUrl"]
                              + f"/_api/web/sitegroups/getbyid({group})/users", self.now)
            require(isinstance(members, list) and members, "Empty/missing SP group")
            for member in members:
                self.principal(member, (*stack, group))
            return
        owners = re.fullmatch(
            r"c:0o\.c\|federateddirectoryclaimprovider\|([0-9a-f-]{36})_o", login)
        security = re.fullmatch(r"c:0t\.c\|tenant\|([0-9a-f-]{36})", login)
        if kind == 4 and owners:
            group = guid(owners[1])
            values = capture(self.snapshot["owners"][group],
                             f"https://graph.microsoft.com/v1.0/groups/{group}/owners",
                             self.now)
            require(isinstance(values, list) and values, "Owners are unresolved")
            for owner in values:
                require(owner.get("@odata.type") == "#microsoft.graph.user"
                        and owner.get("userPrincipalName"), "Non-user/unknown owner")
                self.users.add(guid(owner["id"]))
            return
        if kind == 4 and security:
            group = guid(security[1])
            value = capture(self.snapshot["entraGroups"][group],
                            f"https://graph.microsoft.com/v1.0/groups/{group}?", self.now)
            require(guid(value["id"]) == group and value["securityEnabled"] is True
                    and not value["groupTypes"], "Unsupported content group")
            self.groups.add(group)
            return
        raise ValueError("Unsupported source principal; reject entire document")


def docx_text(path):
    with zipfile.ZipFile(path) as archive:
        require(len(archive.namelist()) == len(set(archive.namelist())),
                "Duplicate ZIP entries")
        require(sum(info.file_size for info in archive.infolist()) <= 8_000_000,
                "DOCX exceeds POC extraction limit")
        raw = archive.read("word/document.xml")
        require(b"<!DOCTYPE" not in raw.upper() and b"<!ENTITY" not in raw.upper(),
                "Unsupported document XML")
        root = ET.fromstring(raw)
        require(not any(root.findall(f".//w:{tag}", NS) for tag in (
            "ins", "del", "moveFrom", "moveTo", "altChunk", "drawing", "object"
        )), "Unsupported revisions or non-text content")
        paragraphs = ["".join(node.text or "" for node in p.findall(".//w:t", NS)).strip()
                      for p in root.findall(".//w:p", NS)]
        text = "\n".join(p for p in paragraphs if p)
        require(bool(text), "Empty DOCX")
        return text


def schemas():
    fields = [
        {"name": "id", "type": "Edm.String", "key": True, "filterable": True},
        {"name": "title", "type": "Edm.String", "searchable": True},
        {"name": "content", "type": "Edm.String", "searchable": True},
        {"name": "sourceUrl", "type": "Edm.String"},
        {"name": "contentSha256", "type": "Edm.String"},
        {"name": "snapshotSha256", "type": "Edm.String"},
    ]
    fields += [{"name": name, "type": "Collection(Edm.String)", "filterable": True,
                "retrievable": False, "permissionFilter": name}
               for name in ("userIds", "groupIds")]
    return {
        "index": {"name": INDEX, "fields": fields, "permissionFilterOption": "enabled",
                  "semantic": {"defaultConfiguration": "acl-semantic", "configurations": [{
                      "name": "acl-semantic", "prioritizedFields": {
                          "titleField": {"fieldName": "title"},
                          "prioritizedContentFields": [{"fieldName": "content"}],
                      }}]}},
        "knowledgeSource": {"name": SOURCE, "kind": "searchIndex",
                            "searchIndexParameters": {
                                "searchIndexName": INDEX,
                                "semanticConfigurationName": "acl-semantic",
                                "searchFields": [{"name": "content"}],
                                "sourceDataFields": [{"name": name} for name in
                                                     ("id", "title", "content", "sourceUrl")]}},
        "knowledgeBase": {"name": KB, "knowledgeSources": [{"name": SOURCE}],
                          "retrievalReasoningEffort": {"kind": "minimal"},
                          "outputMode": "extractiveData"},
    }


def prepare(snapshot, directory, now=None):
    now = now or datetime.now(timezone.utc)
    require(snapshot.get("version") == 1, "Not a raw capture manifest")
    site = snapshot["siteUrl"]
    require(re.fullmatch(r"https://[a-z0-9-]+\.sharepoint\.com/sites/test", site),
            "Expected approved SharePoint test site")
    current = capture(snapshot["currentUser"], site + "/_api/web/currentuser?", now)
    require(current["LoginName"].lower() == snapshot["adminLogin"].lower()
            and current["IsSiteAdmin"] is True, "Wrong source caller")
    documents = snapshot["documents"]
    require(len(documents) == 3 and {d["library"] for d in documents} == set(FILES),
            "Exactly the three approved libraries are required")
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    output, audit = [], []
    for document in documents:
        library = document["library"]
        filename = FILES[library]
        list_id = guid(document["listId"])
        prefix = site + f"/_api/web/lists(guid'{list_id}')"
        before = capture(document["libraryBefore"], prefix + "?", now)
        after = capture(document["libraryAfter"], prefix + "?", now)
        require(before == after and before["Title"] == library
                and before["HasUniqueRoleAssignments"] is True,
                "Library ACL changed or inheritance unsupported")
        item = capture(document["itemBefore"], prefix + "/items(1)?", now)
        later = capture(document["itemAfter"], prefix + "/items(1)?", now)
        expected_ref = f"/sites/test/{library}/{filename}"
        require(item == later and item["Id"] == 1 and item["FileRef"] == expected_ref
                and item["FileDirRef"] == f"/sites/test/{library}"
                and item["HasUniqueRoleAssignments"] is False
                and bool(item["File"]["ETag"]) and bool(item["File"]["UniqueId"]),
                "Content changed, wrong source, or unsupported file inheritance")
        permissions = capture(document["permissions"],
                              "https://graph.microsoft.com/v1.0/sites/", now)
        require(f"/lists/{list_id}/items/1/driveItem/permissions?"
                in document["permissions"]["url"], "Wrong permissions source")
        require(isinstance(permissions, list) and permissions, "Missing permissions")
        for permission in permissions:
            require(not any(key in permission for key in (
                "link", "invitation", "grantedToIdentitiesV2", "grantedToIdentities"
            )) and permission["roles"] in (["read"], ["write"], ["owner"])
                    and bool(permission.get("grantedToV2")),
                    "Sharing link or unsupported Graph grant")
        resolver = Resolver(snapshot, now)
        grants = rows(before["RoleAssignments"])
        require(bool(grants), "No source role assignments")
        for grant in grants:
            bindings = rows(grant["RoleDefinitionBindings"])
            require(bool(bindings), "Missing role definitions")
            for role in bindings:
                require(role["Name"] in ROLES and (
                    str(role["BasePermissions"]["High"]),
                    str(role["BasePermissions"]["Low"])) == ROLES[role["Name"]],
                    "Unsupported role definition")
            resolver.principal(grant["Member"])
        admins = capture(snapshot["siteAdmins"],
                         site + "/_api/web/siteusers?$filter=IsSiteAdmin", now)
        require(isinstance(admins, list) and admins, "Missing site administrator capture")
        for admin in admins:
            require(admin["IsSiteAdmin"] is True, "Invalid administrator capture")
            resolver.principal(admin)
        source_members = [grant["Member"] for grant in grants] + admins
        for permission in permissions:
            identity = permission["grantedToV2"]
            require(set(identity).issubset({
                "group", "siteGroup", "siteUser", "user", "sharePointGroup"
            }),
                    "Unknown Graph identity type")
            matches = []
            if "sharePointGroup" in identity:
                paired = identity.get("siteGroup")
                modern = identity["sharePointGroup"]
                site_parts = document["permissions"]["url"].split("/sites/", 1)[1].split("/")[0].split(",")
                require(len(site_parts) == 3 and site_parts[0] == site.split("/")[2],
                        "Unexpected Graph site identity")
                site_collection = guid(site_parts[1])
                guid(site_parts[2])
                require(paired and str(modern.get("principalId")) == str(paired.get("id")),
                        "Unpaired SharePoint group identity")
                expected_id = base64.urlsafe_b64encode(
                    f"{site_collection}_{paired['id']}".encode()).decode().rstrip("=")
                matches.append(modern.get("id") == expected_id)
            if "siteGroup" in identity:
                matches.append(any(
                    member["PrincipalType"] == 8
                    and str(member["Id"]) == str(identity["siteGroup"]["id"])
                    for member in source_members))
            if "siteUser" in identity:
                matches.append(any(
                    member["LoginName"] == identity["siteUser"].get("loginName")
                    for member in source_members))
            if "group" in identity:
                group = guid(identity["group"]["id"])
                paired_owner = identity.get("siteUser", {}).get("loginName") == (
                    f"c:0o.c|federateddirectoryclaimprovider|{group}_o")
                matches.append(group in resolver.groups or (
                    paired_owner and group in snapshot["owners"]))
            if "user" in identity:
                matches.append(guid(identity["user"]["id"]) in resolver.users)
            require(matches and all(matches),
                    "Graph grant not explained by effective source ACL")
        require(0 < len(resolver.users) + len(resolver.groups)
                and max(len(resolver.users), len(resolver.groups)) <= 1000,
                "Empty ACL or Search ACL limit exceeded")
        path = (directory / document["contentFile"]).resolve()
        require(path.is_relative_to(directory.resolve()) and path.name == filename,
                "Content path outside capture directory")
        data = path.read_bytes()
        content_hash = hashlib.sha256(data).hexdigest()
        require(content_hash == document["contentSha256"]
                and len(data) == int(item["File"]["Length"]), "Content hash/size mismatch")
        text = docx_text(path)
        for offset in range(0, len(text), 2000):
            output.append({
                "@search.action": "upload",
                "id": f"{list_id}-1-{offset // 2000}",
                "title": filename, "content": text[offset:offset + 2000],
                "sourceUrl": site.split("/sites/")[0] + expected_ref,
                "contentSha256": content_hash, "snapshotSha256": digest,
                "userIds": sorted(resolver.users), "groupIds": sorted(resolver.groups),
            })
        audit.append({"file": filename, "events": resolver.audit})
    return {"documents": {"value": output}, "audit": audit, **schemas()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    require(not args.output.exists(), "Refusing to overwrite an existing bundle")
    snapshot = json.loads(args.manifest.read_text(encoding="utf-8"))
    result = prepare(snapshot, args.manifest.parent)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    print(f"Prepared {len(result['documents']['value'])} chunks; no network calls made.")


if __name__ == "__main__":
    main()
