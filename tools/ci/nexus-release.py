#!/usr/bin/env python3
"""Prepare and verify Nexus mirrors of published GitHub release assets. No API writes."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid


REPOSITORY = "humangenome/ValheimOne"
MOD_PAGE_ID = "3571"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Unexpected API redirect; credentials were not forwarded")


def nexus(path, payload=None):
    key = os.environ.get("NEXUSMODS_API_KEY", "")
    if not key:
        raise RuntimeError("NEXUSMODS_API_KEY is missing; configure the repository secret")
    request = urllib.request.Request(
        "https://api.nexusmods.com" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"apikey": key, "Accept": "application/json", "Content-Type": "application/json",
                 "User-Agent": "ValheimOne-release-sync"},
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=45) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Nexus API returned HTTP {error.code} for {path}") from None


def github_release():
    result = subprocess.run(
        ["gh", "api", f"repos/{REPOSITORY}/releases/latest"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def release_version(release, requested=""):
    tag = release.get("tag_name", "")
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("Expected a stable vMAJOR.MINOR.PATCH release")
    if release.get("draft", True) or release.get("prerelease", True) or not release.get("published_at"):
        raise ValueError("Only a published stable release can be mirrored")
    if requested and requested != tag:
        raise ValueError("Requested release is no longer Latest; refusing to downgrade Nexus")
    return tag[1:]


def verify_assets(folder, release, version):
    names = [f"ValheimOne-{version}.zip", f"ValheimOne-full-{version}.zip"]
    sums_name = f"SHA256SUMS-{version}.txt"
    assets = {a["name"]: a for a in release["assets"]}
    expected = {}
    for line in (folder / sums_name).read_text().splitlines():
        match = re.fullmatch(r"([a-f0-9]{64})\s+\*?([^/\\]+)", line)
        if not match or match[2] in expected:
            raise ValueError("Malformed or duplicate release checksum entry")
        expected[match[2]] = match[1]
    if set(expected) != set(names):
        raise ValueError("Release checksum manifest must name exactly the two release zips")
    for name in [sums_name, *names]:
        data = (folder / name).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        asset = assets[name]
        if asset.get("digest") != "sha256:" + digest or asset["size"] != len(data):
            raise ValueError(f"GitHub asset digest/size mismatch: {name}")
        if name in expected and expected[name] != digest:
            raise ValueError(f"Release checksum mismatch: {name}")
    return names


def select_files(files):
    selected = {}
    for file in files:
        name = file.get("name", "")
        for kind, pattern in (
            ("plugin", r"ValheimOne(?: \d+\.\d+\.\d+)?"),
            ("full", r"ValheimOne Full(?: \d+\.\d+\.\d+)?"),
        ):
            if re.fullmatch(pattern, name, re.IGNORECASE):
                if kind in selected:
                    raise ValueError(f"Multiple Nexus {kind} file groups; review their API IDs")
                selected[kind] = str(file["id"])
    if set(selected) != {"plugin", "full"}:
        raise ValueError("Expected the existing plugin and Full file groups on Nexus")
    for file_id in selected.values():
        if not re.fullmatch(r"[A-Za-z0-9-]+", file_id):
            raise ValueError("Unexpected Nexus file ID")
    return selected


def existing_version(versions, version):
    target = tuple(map(int, version.split(".")))
    for item in versions:
        live_version = item.get("version", "")
        if item.get("category") == "main" and re.fullmatch(r"\d+\.\d+\.\d+", live_version):
            if tuple(map(int, live_version.split("."))) > target:
                raise ValueError("Nexus already has a newer active version; refusing to downgrade")
    matches = [v for v in versions if v.get("version") == version]
    if len(matches) > 1:
        raise ValueError("Duplicate versions on Nexus; review instead of uploading another copy")
    if matches and matches[0].get("category") != "main":
        raise ValueError("Matching Nexus version is not an active main file; manual review required")
    return matches[0] if matches else None


def verify_indexed_hash(folder, filename, current):
    # Nexus's legacy hash index identifies the actual stored archive. SHA-256 above
    # remains the release integrity check; this MD5 lookup is only index reconciliation.
    digest = hashlib.md5((folder / filename).read_bytes(), usedforsecurity=False).hexdigest()
    matches = nexus(f"/v1/games/valheim/mods/md5_search/{digest}.json")
    if not any(
        str(m.get("mod", {}).get("mod_id")) == MOD_PAGE_ID
        and str(m.get("file_details", {}).get("file_id")) == str(current["game_scoped_id"])
        for m in matches
    ):
        raise ValueError("Nexus has not indexed the expected archive; do not upload a duplicate")


def public_download_status(game_id):
    # The legacy v1 mod metadata can lag behind the live page after a v3 upload.
    # GraphQL is a read-only query for the page version and per-file scan status.
    return nexus("/v2/graphql", {
        "query": """query($mod:ID!,$game:ID!) {
            mod(modId:$mod,gameId:$game) { name version status }
            modFiles(modId:$mod,gameId:$game) {
                fileId version category primary manager requirementsAlert scannedV2
            }
        }""",
        "variables": {"mod": MOD_PAGE_ID, "game": str(game_id)},
    })


def verify_public_downloads(response, version, expected):
    if set(expected) != {"plugin", "full"}:
        raise ValueError("Both Nexus download versions must be present")
    if response.get("errors"):
        raise ValueError("Nexus page/scan query failed; download status is unverified")
    data = response.get("data") or {}
    mod = data.get("mod") or {}
    if mod.get("name") != "ValheimOne" or mod.get("status") != "published":
        raise ValueError("Nexus page is not the expected published mod")
    if mod.get("version") != version:
        raise ValueError("Nexus page version does not match GitHub Latest")
    for kind, current in expected.items():
        matches = [f for f in (data.get("modFiles") or [])
                   if str(f.get("fileId")) == str(current["game_scoped_id"])]
        if len(matches) != 1:
            raise ValueError(f"Nexus {kind} download status is missing or ambiguous")
        file = matches[0]
        if file.get("version") != version or file.get("category") != "MAIN":
            raise ValueError(f"Nexus {kind} download is not the expected active version")
        # Legacy manager=1 disables mod-manager downloads; 0 enables them.
        if (file.get("primary") != int(kind == "plugin")
                or file.get("manager") != int(kind == "full")
                or file.get("requirementsAlert") != int(kind == "plugin")):
            raise ValueError(f"Nexus {kind} primary/download/requirements controls do not match")
        status = file.get("scannedV2")
        if status == "QUARANTINED":
            raise ValueError(f"Nexus {kind} archive is quarantined; moderator review required. Do not reupload.")
        if status not in ("VERIFIED", "INTERNALLY_VERIFIED", "MANUALLY_VERIFIED"):
            raise ValueError(f"Nexus {kind} archive has not passed its scan; download remains unverified")
        print(f"Nexus {kind}: scan cleared and download controls verified")


def write_outputs(values):
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with open(target, "a") as stream:
        for key, value in values.items():
            delimiter = "value_" + uuid.uuid4().hex
            stream.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["assets", "prepare", "verify"])
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    release = github_release()
    version = release_version(release, os.environ.get("RELEASE_TAG", ""))
    folder = args.directory
    folder.mkdir(parents=True, exist_ok=True)
    if args.mode != "verify":
        for name in [f"ValheimOne-{version}.zip", f"ValheimOne-full-{version}.zip", f"SHA256SUMS-{version}.txt"]:
            subprocess.run([
                "gh", "release", "download", release["tag_name"], "--repo", REPOSITORY,
                "--dir", str(folder), "--pattern", name, "--clobber",
            ], check=True, stdout=subprocess.DEVNULL)
    names = verify_assets(folder, release, version)
    if args.mode == "assets":
        print(f"GitHub {release['tag_name']}: both archives and checksum manifest verified")
        return
    mod = nexus(f"/v3/games/valheim/mods/{MOD_PAGE_ID}")["data"]
    if str(mod["game_scoped_id"]) != MOD_PAGE_ID or mod.get("name") != "ValheimOne":
        raise ValueError("Unexpected Nexus mod identity")
    mod_id = str(mod["id"])
    if not re.fullmatch(r"[A-Za-z0-9-]+", mod_id):
        raise ValueError("Unexpected Nexus mod ID")
    files = select_files(nexus(f"/v3/mods/{mod_id}/files")["data"]["mod_files"])
    outputs = {"version": version, "mod_id": mod_id, "notes": release["body"]}
    current_files = {}
    for kind, filename in zip(["plugin", "full"], names):
        versions = nexus(f"/v3/mod-files/{files[kind]}/versions")["data"]["versions"]
        current = existing_version(versions, version)
        outputs[kind + "_id"] = files[kind]
        outputs[kind + "_upload"] = "false" if current else "true"
        if current:
            current_files[kind] = current
            if bool(current.get("is_primary")) != (kind == "plugin"):
                raise ValueError("Plugin-only must be the primary Nexus download")
        elif args.mode == "verify":
            raise ValueError(f"Nexus {kind} version is missing")
        print(f"Nexus {kind}: {'version present' if current else 'upload required'}")
    # Quarantined files can disappear from the legacy hash index. Report the
    # actual moderation state before that lookup can obscure it with a 404.
    if len(current_files) == 2:
        verify_public_downloads(public_download_status(mod["game_id"]), version, current_files)
    for kind, filename in zip(["plugin", "full"], names):
        if kind in current_files:
            verify_indexed_hash(folder, filename, current_files[kind])
            print(f"Nexus {kind}: hash-index verified")
    if args.mode == "verify":
        changelogs = nexus(f"/v1/games/valheim/mods/{MOD_PAGE_ID}/changelogs.json")
        if version not in changelogs:
            raise ValueError("Nexus release changelog is missing")
        print("Nexus versions, archive index, primary download, page version and changelog verified")
        print("Scan state verified. Check both public download buttons and download the archives normally.")
    write_outputs(outputs)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        # Do not include API response bodies, request headers, or credentials in CI logs.
        print(f"Nexus release check failed: {error}", file=sys.stderr)
        sys.exit(1)
