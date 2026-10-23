#!/usr/bin/env python3
"""Edit the reviewed, version-controlled PEP 592 yank policy."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from jsonschema import validate
from packaging.utils import canonicalize_name
from packaging.version import Version


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "repository" / "yanks.json"
SCHEMA_PATH = ROOT / "schemas" / "yanks.schema.json"


def update_policy(path: Path, action: str, name: str, version: str, reason: str = "") -> dict:
    canonical_name = canonicalize_name(name)
    normalized_version = str(Version(version))
    policy = json.loads(path.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validate(instance=policy, schema=schema)
    entries = {
        (canonicalize_name(item["name"]), str(Version(item["version"]))): item
        for item in policy["yanked"]
    }
    if len(entries) != len(policy["yanked"]):
        raise ValueError("yank policy contains duplicate package/version entries")
    key = (canonical_name, normalized_version)
    if action == "yank":
        if not reason.strip():
            raise ValueError("a non-empty reason is required when yanking a release")
        entries[key] = {"name": canonical_name, "version": normalized_version, "reason": reason.strip()}
    else:
        if key not in entries:
            raise ValueError(f"no yank entry exists for {canonical_name}=={normalized_version}")
        del entries[key]
    policy["yanked"] = [entries[item] for item in sorted(entries)]
    validate(instance=policy, schema=schema)

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", prefix=f".{path.name}.",
        suffix=".tmp", dir=path.parent, delete=False
    ) as output:
        temporary = Path(output.name)
        json.dump(policy, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return policy


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, default=DEFAULT_POLICY)
    subparsers = parser.add_subparsers(dest="action", required=True)
    yank = subparsers.add_parser("yank")
    yank.add_argument("name")
    yank.add_argument("version")
    yank.add_argument("--reason", required=True)
    unyank = subparsers.add_parser("unyank")
    unyank.add_argument("name")
    unyank.add_argument("version")
    args = parser.parse_args()
    policy = update_policy(
        args.file,
        args.action,
        args.name,
        args.version,
        getattr(args, "reason", ""),
    )
    print(f"Updated {args.file}: {len(policy['yanked'])} yanked release(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
