#!/usr/bin/env python3
"""Scan staged content or outgoing Git history without printing matched values."""
import argparse
import glob
import json
import os
from pathlib import Path
import re
import subprocess
import sys


PATTERNS = {
    "private-key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "provider-token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{25,}|xox[baprs]-[A-Za-z0-9-]{15,})"),
    "jwt": re.compile(rb"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}"),
    "credential-literal": re.compile(rb"(?im)[\"']?\b[A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|REFRESH_TOKEN|CLIENT_SECRET|PASSWORD|RELAY_TOKEN|WEBHOOK_TOKEN)[\"']?\s*[:=]\s*[\"'][A-Za-z0-9_+/=-]{16,}[\"']"),
}


def git(*args):
    return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)


def private_path(name):
    path = Path(name)
    return (any(p in {"secrets", "data", "spool", "config"} for p in path.parts[:-1])
            or path.name == "agents.json"
            or path.name.endswith((".sqlite", ".sqlite3", ".db", ".log", ".pem", ".key", ".bundle"))
            or (path.name.startswith((".env", "oauth.env")) and not path.name.endswith(".example")))


def load_policy(required):
    configured = os.environ.get("PUBLICATION_POLICY_FILE")
    if not configured:
        lookup = subprocess.run(["git", "config", "--get", "publication.policyFile"],
                                text=True, capture_output=True)
        configured = lookup.stdout.strip()
    if not configured:
        if required:
            raise ValueError("Private publication policy is required")
        return [], []
    policy = json.loads(Path(configured).read_text())
    if not isinstance(policy, dict):
        raise ValueError("Invalid private policy")
    expressions = policy.get("deny_patterns", [])
    secret_globs = policy.get("secret_globs", [])
    if not isinstance(expressions, list) or not all(isinstance(p, str) for p in expressions):
        raise ValueError("Invalid deny patterns")
    if not isinstance(secret_globs, list) or not all(isinstance(p, str) for p in secret_globs):
        raise ValueError("Invalid secret file patterns")
    patterns = [re.compile(p.encode(), re.I) for p in expressions]
    secrets = []
    for pattern in secret_globs:
        for name in glob.glob(pattern):
            path = Path(name)
            if path.is_file():
                value = path.read_bytes().strip()
                if len(value) >= 12 and b"\n" not in value:
                    secrets.append(value)
    return patterns, secrets


def staged_entries():
    for item in git("ls-files", "--stage", "-z").split(b"\0"):
        if not item:
            continue
        header, path = item.split(b"\t", 1)
        mode, oid, stage = header.split()
        if stage != b"0" or mode == b"160000":
            raise ValueError("Unmerged entries/submodules need separate publication review")
        yield oid.decode(), path.decode("utf-8")


def violations(content, patterns, secrets):
    matches = {label for label, pattern in PATTERNS.items() if pattern.search(content)}
    if any(pattern.search(content) for pattern in patterns):
        matches.add("private-site-pattern")
    if any(secret in content for secret in secrets):
        matches.add("known-secret-value")
    return matches


def history_entries(refs):
    commits = set()
    objects = {}
    for ref in refs:
        if ref.startswith("-"):
            raise ValueError("Invalid ref")
        commits.update(git("rev-list", ref).decode().splitlines())
        # Inspect each tree so a private path cannot hide behind a shared blob ID.
    seen_trees = set()
    for commit in sorted(commits):
        yield "message", commit, "commit-message"
        tree = git("rev-parse", commit + "^{tree}").decode().strip()
        if tree in seen_trees:
            continue
        seen_trees.add(tree)
        for item in git("ls-tree", "-r", "-z", tree).split(b"\0"):
            if not item:
                continue
            header, name = item.split(b"\t", 1)
            mode, kind, oid = header.split()
            if kind != b"blob":
                raise ValueError("Submodules need separate publication review")
            key = (oid.decode(), name.decode("utf-8"))
            objects[key] = None
    for oid, path in objects:
        yield "blob", oid, path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--ref", action="append")
    mode.add_argument("--pre-push", action="store_true")
    parser.add_argument("--require-policy", action="store_true")
    args = parser.parse_args(argv)
    try:
        os.chdir(git("rev-parse", "--show-toplevel").decode().strip())
        patterns, secrets = load_policy(args.require_policy)
        if args.staged:
            entries = (("blob", oid, path) for oid, path in staged_entries())
        else:
            refs = args.ref or []
            if args.pre_push:
                for line in sys.stdin:
                    fields = line.split()
                    if len(fields) != 4:
                        raise ValueError("Invalid pre-push input")
                    oid = fields[1]
                    if set(oid) != {"0"}:
                        refs.append(oid)
            entries = history_entries(refs)
        failures = set()
        checked = set()
        for kind, oid, path in entries:
            path_matches = violations(path.encode(), patterns, secrets)
            display_path = "<redacted-path>" if path_matches else path
            failures.update((label, display_path) for label in path_matches)
            if kind == "blob" and private_path(path):
                failures.add(("private-path", display_path))
            if (kind, oid) in checked:
                continue
            checked.add((kind, oid))
            if kind == "message":
                content = git("cat-file", "commit", oid).split(b"\n\n", 1)[1]
            else:
                if int(git("cat-file", "-s", oid)) > 2 * 1024 * 1024:
                    failures.add(("oversized-file", display_path))
                    continue
                content = git("cat-file", "blob", oid)
            failures.update((label, display_path) for label in violations(content, patterns, secrets))
        if failures:
            for category, path in sorted(failures):
                print("BLOCKED " + category + ": " + json.dumps(path, ensure_ascii=True), file=sys.stderr)
            return 1
        print("Publication check passed (" + str(len(checked)) + " unique objects)")
        return 0
    except (OSError, ValueError, re.error, subprocess.SubprocessError):
        print("Publication check failed: inspect configuration or repository; values suppressed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
