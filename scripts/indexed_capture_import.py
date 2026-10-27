"""Import a browser capture and resolve its ACLs using the existing Azure CLI identity."""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

# Azure CLI's embedded Python omits the script directory from sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from indexed_acl import FILES, complete, guid, prepare, require


def graph(path):
    url = "https://graph.microsoft.com/v1.0/" + path
    result = subprocess.run([
        sys.executable, "-IBm", "azure.cli", "rest", "--method", "GET",
        "--url", url, "--only-show-errors", "-o", "json",
    ], capture_output=True, text=True, encoding="utf-8")
    require(result.returncode == 0, f"Graph GET failed: {result.stderr.strip()}")
    body = complete(json.loads(result.stdout))
    return {"url": url, "observedAt": datetime.now(timezone.utc).isoformat(), "body": body}


def import_capture(archive, destination, graph_site_id, admin_object_id):
    require(not destination.exists(), "Refusing to overwrite a capture directory")
    value = json.loads(archive.read_text(encoding="utf-8"))
    snapshot = value["snapshot"]
    require(snapshot["version"] == 1, "Unknown capture version")
    require(graph_site_id.split(",")[0] == snapshot["siteUrl"].split("/")[2],
            "Graph site belongs to a different host")
    require(len(snapshot["documents"]) == 3
            and {d["library"] for d in snapshot["documents"]} == set(FILES),
            "Expected exactly the approved three libraries")
    require(len(value["binary"]) == 3
            and {v["name"] for v in value["binary"]} == set(FILES.values()),
            "Expected exactly the approved three DOCX downloads")
    identity = graph("me?$select=id,userPrincipalName")["body"]
    require(identity["id"] == guid(admin_object_id)
            and snapshot["adminLogin"].lower()
            == "i:0#.f|membership|" + identity["userPrincipalName"].lower(),
            "Wrong Graph caller; do not authenticate with a replacement identity")
    group_ids, owner_ids = set(), set()
    principals = list(snapshot["siteAdmins"]["body"]["value"])
    for record in snapshot["spGroups"].values():
        principals.extend(record["body"]["value"])
    for document in snapshot["documents"]:
        principals.extend(g["Member"] for g in document["libraryBefore"]["body"]["RoleAssignments"])
        list_id = guid(document["listId"])
        permissions = graph(
            f"sites/{graph_site_id}/lists/{list_id}/items/1/driveItem/permissions"
            "?$select=id,roles,grantedToV2,grantedToIdentitiesV2,inheritedFrom,link")
        require(not any("link" in p for p in permissions["body"]["value"]),
                "Sharing link detected; reject capture without persisting link secrets")
        document["permissions"] = permissions
    for principal in principals:
        security = re.fullmatch(r"c:0t\.c\|tenant\|([0-9a-f-]{36})", principal["LoginName"])
        owners = re.fullmatch(
            r"c:0o\.c\|federateddirectoryclaimprovider\|([0-9a-f-]{36})_o",
            principal["LoginName"])
        if principal["PrincipalType"] == 4 and security:
            group_ids.add(guid(security[1]))
        elif principal["PrincipalType"] == 4 and owners:
            owner_ids.add(guid(owners[1]))
        else:
            require(principal["PrincipalType"] == 8 or (
                principal["LoginName"] == "SHAREPOINT\\system"
                and principal["PrincipalType"] == 1 and principal["Id"] == 1073741823),
                "Unknown source principal; reject the entire capture")
    for group in sorted(group_ids):
        snapshot["entraGroups"][group] = graph(
            f"groups/{group}?$select=id,securityEnabled,mailEnabled,groupTypes")
    for group in sorted(owner_ids):
        snapshot["owners"][group] = graph(f"groups/{group}/owners?$select=id,userPrincipalName")
    decoded = {}
    for binary in value["binary"]:
        data = base64.b64decode(binary["base64"], validate=True)
        document = next(d for d in snapshot["documents"] if d["contentFile"] == binary["name"])
        require(hashlib.sha256(data).hexdigest() == document["contentSha256"]
                and len(data) == document["download"]["sizeBytes"],
                "Exported content hash/receipt mismatch")
        decoded[binary["name"]] = data
    destination.mkdir()
    for name, data in decoded.items():
        with (destination / name).open("xb") as stream:
            stream.write(data)
    with (destination / "manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(snapshot, stream, indent=2)
    result = prepare(snapshot, destination)
    with (destination / "bundle.json").open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps({
        "sourceDocuments": len(decoded), "sourceBytes": sum(map(len, decoded.values())),
        "chunks": len(result["documents"]["value"]), "cloudWrites": 0,
    }))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--graph-site-id", required=True)
    parser.add_argument("--admin-object-id", required=True)
    args = parser.parse_args()
    import_capture(args.archive, args.destination, args.graph_site_id, args.admin_object_id)


if __name__ == "__main__":
    main()
