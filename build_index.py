#!/usr/bin/env python3
"""
build_index.py

Iterates every repo in a GitHub organization, reads each repo's plugin.json
(for name/description/author/icon/homepage/permissions) and update.json
(for version/versionCode/download/changelog), resolves the icon path
relative to the repo root, and writes a combined index.json catalog.

Env vars required:
    GH_TOKEN   - GitHub token with read access to the org's repos
    ORG        - GitHub organization login (e.g. "Dioxamine-plugins-repo")

Optional env vars:
    REGISTRY_REPO     - repo this script lives in; skipped when building index (default: "registry")
    OUTPUT_PATH        - where to write index.json (default: "index.json")
    PLUGIN_JSON_PATH   - manifest filename at repo root (default: "plugin.json")
    UPDATE_JSON_PATH   - update-info filename at repo root (default: "update.json")
"""

import base64
import json
import os
import sys
import time
from typing import Any, Optional

import urllib.request
import urllib.error

API_ROOT = "https://api.github.com"

GH_TOKEN = os.environ.get("GH_TOKEN")
ORG = os.environ.get("ORG")
REGISTRY_REPO = os.environ.get("REGISTRY_REPO", "registry")
OUTPUT_PATH = os.environ.get("OUTPUT_PATH", "index.json")
PLUGIN_JSON_PATH = os.environ.get("PLUGIN_JSON_PATH", "plugin.json")
UPDATE_JSON_PATH = os.environ.get("UPDATE_JSON_PATH", "update.json")

if not GH_TOKEN or not ORG:
    print("ERROR: GH_TOKEN and ORG environment variables are required", file=sys.stderr)
    sys.exit(1)


def gh_request(path: str) -> Any:
    url = path if path.startswith("http") else f"{API_ROOT}{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {GH_TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def list_org_repos(org: str) -> list[dict]:
    repos = []
    page = 1
    while True:
        batch = gh_request(f"/orgs/{org}/repos?per_page=100&page={page}&type=public")
        if not batch:
            break
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return repos


def fetch_file_bytes(owner: str, repo: str, path: str) -> Optional[bytes]:
    """Fetch raw bytes of a file from the repo's default branch via contents API."""
    data = gh_request(f"/repos/{owner}/{repo}/contents/{path}")
    if data is None or "content" not in data:
        return None
    return base64.b64decode(data["content"])


def fetch_json_file(owner: str, repo: str, path: str) -> Optional[dict]:
    raw = fetch_file_bytes(owner, repo, path)
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as e:
        print(f"  WARN: {repo}: invalid JSON in {path} ({e})", file=sys.stderr)
        return None


def resolve_icon_path(icon_field: str) -> str:
    """
    plugin.json's "icon" field is relative to repo root (e.g. "icon.png",
    "assets/icon.png", "./icon.png"). Normalize it to a clean repo-root-relative path.
    """
    path = icon_field.strip().lstrip("./")
    return path


def fetch_icon_base64(owner: str, repo: str, icon_field: str) -> Optional[str]:
    icon_path = resolve_icon_path(icon_field)
    raw = fetch_file_bytes(owner, repo, icon_path)
    if raw is None:
        print(f"  WARN: {repo}: icon not found at '{icon_path}'", file=sys.stderr)
        return None
    ext = icon_path.rsplit(".", 1)[-1].lower() if "." in icon_path else "png"
    mime = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "svg": "image/svg+xml",
    }.get(ext, "image/png")
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def build_entry(owner: str, repo_name: str) -> Optional[dict]:
    manifest = fetch_json_file(owner, repo_name, PLUGIN_JSON_PATH)
    if manifest is None:
        print(f"  SKIP: {repo_name} (no {PLUGIN_JSON_PATH})")
        return None

    manifest_required = ["id", "name"]
    missing = [f for f in manifest_required if f not in manifest]
    if missing:
        print(f"  SKIP: {repo_name} ({PLUGIN_JSON_PATH} missing fields: {missing})", file=sys.stderr)
        return None

    update_info = fetch_json_file(owner, repo_name, UPDATE_JSON_PATH)
    if update_info is None:
        print(f"  SKIP: {repo_name} (no {UPDATE_JSON_PATH})")
        return None

    update_required = ["version", "versionCode", "download"]
    missing = [f for f in update_required if f not in update_info]
    if missing:
        print(f"  SKIP: {repo_name} ({UPDATE_JSON_PATH} missing fields: {missing})", file=sys.stderr)
        return None

    icon_data_uri = None
    icon_field = manifest.get("icon")
    if icon_field:
        icon_data_uri = fetch_icon_base64(owner, repo_name, icon_field)

    entry = {
        "id": manifest["id"],
        "name": manifest["name"],
        "description": manifest.get("description", ""),
        "author": manifest.get("author", ""),
        "icon": icon_data_uri,
        "homepage": manifest.get("homepage", f"https://github.com/{owner}/{repo_name}"),
        "permissions": manifest.get("permissions", {}),
        "version": update_info["version"],
        "versionCode": update_info["versionCode"],
        "download": update_info["download"],
        "changelog": update_info.get("changelog"),
        "updateUrl": f"https://raw.githubusercontent.com/{owner}/{repo_name}/main/{UPDATE_JSON_PATH}",
        "repo": f"https://github.com/{owner}/{repo_name}",
    }
    print(f"  OK:   {repo_name} v{entry['version']} ({entry['versionCode']})")
    return entry


def main() -> None:
    print(f"Fetching repos for org '{ORG}'...")
    repos = list_org_repos(ORG)
    print(f"Found {len(repos)} repos.\n")

    entries = []
    for repo in repos:
        repo_name = repo["name"]
        if repo_name == REGISTRY_REPO:
            continue
        if repo.get("archived"):
            print(f"  SKIP: {repo_name} (archived)")
            continue

        try:
            entry = build_entry(ORG, repo_name)
            if entry:
                entries.append(entry)
        except Exception as e:
            print(f"  ERROR: {repo_name}: {e}", file=sys.stderr)

        time.sleep(0.3)

    entries.sort(key=lambda e: e["name"].lower())

    index = {
        "schemaVersion": 1,
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(entries),
        "plugins": entries,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)

    print(f"\nWrote {len(entries)} plugins to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
