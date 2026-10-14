#!/usr/bin/env python3
"""Publish a local directory into a GitHub repository via the Git Data API.

Why the Git Data API instead of ``git push``: the sandbox has no stored git
credentials, and putting a token into a remote URL would leak it into
``.git/config``. Here the token is read from the ``GH_TOKEN`` environment
variable (or ``--token-file``) and never written to disk or printed.

    GH_TOKEN=... python tools/push_to_github.py --dir . --owner Alexkkkkk \
        --repo mount-file --message "..."

It creates one commit containing every included file (blobs -> tree ->
commit -> ref update) and prints the resulting commit URL.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"

EXCLUDE_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", "images",
                ".venv", "venv", "build", "dist"}
EXCLUDE_GLOBS = ("*.pyc", "*.bak", "*.img", "*.part", "*.tmp", "*.log", "*.bak.*")


def api(method: str, path: str, token: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "mount-file-push",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise SystemExit(f"GitHub API {method} {path} -> {exc.code}: {body[:600]}")


def collect(root: pathlib.Path, extra_excludes: list[str], include_binaries: bool):
    files: list[pathlib.Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        parts = set(rel.parts[:-1])
        if parts & EXCLUDE_DIRS:
            continue
        if any(fnmatch.fnmatch(rel.name, pat) for pat in EXCLUDE_GLOBS):
            continue
        if any(fnmatch.fnmatch(str(rel), pat) for pat in extra_excludes):
            continue
        if not include_binaries:
            try:
                path.read_text()
            except (UnicodeDecodeError, OSError):
                print(f"  skip (binary): {rel}", file=sys.stderr)
                continue
        files.append(rel)
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default=".")
    ap.add_argument("--owner", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", default="main")
    ap.add_argument("--message", required=True)
    ap.add_argument("--token-file")
    ap.add_argument("--exclude", action="append", default=[],
                    help="glob (relative to --dir) to skip, repeatable")
    ap.add_argument("--include-binaries", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("GH_TOKEN", "")
    if not token and args.token_file:
        token = pathlib.Path(args.token_file).read_text().strip()
    if not token:
        print("error: no token (set GH_TOKEN or pass --token-file)", file=sys.stderr)
        return 2

    root = pathlib.Path(args.dir).resolve()
    files = collect(root, args.exclude, args.include_binaries)
    if not files:
        print("error: nothing to push", file=sys.stderr)
        return 2

    status, ref = api("GET", f"/repos/{args.owner}/{args.repo}/git/ref/heads/{args.branch}", token)
    base_commit = ref["object"]["sha"]
    _, commit = api("GET", f"/repos/{args.owner}/{args.repo}/git/commits/{base_commit}", token)
    base_tree = commit["tree"]["sha"]
    print(f"base commit {base_commit[:10]} (tree {base_tree[:10]})")

    tree_entries = []
    for rel in files:
        raw = (root / rel).read_bytes()
        _, blob = api("POST", f"/repos/{args.owner}/{args.repo}/git/blobs", token, {
            "content": base64.b64encode(raw).decode(), "encoding": "base64"})
        tree_entries.append({"path": rel.as_posix(), "mode": "100644",
                             "type": "blob", "sha": blob["sha"]})
        print(f"  blob {rel.as_posix():<44} {len(raw):>7} bytes")

    _, tree = api("POST", f"/repos/{args.owner}/{args.repo}/git/trees", token,
                  {"base_tree": base_tree, "tree": tree_entries})
    _, new_commit = api("POST", f"/repos/{args.owner}/{args.repo}/git/commits", token,
                        {"message": args.message, "tree": tree["sha"],
                         "parents": [base_commit]})
    status, updated = api("PATCH", f"/repos/{args.owner}/{args.repo}/git/refs/heads/{args.branch}",
                          token, {"sha": new_commit["sha"], "force": False})

    print(f"\ncommit  : {new_commit['sha']}")
    print(f"url     : {new_commit['html_url']}")
    print(f"files   : {len(files)}")
    print(f"ref     : HTTP {status} -> {updated.get('object', {}).get('sha', '')[:10]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
