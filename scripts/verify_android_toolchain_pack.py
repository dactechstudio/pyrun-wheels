"""Verify an extracted Android compiler pack and its Ed25519 signature."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from scripts.sign_toolchain_manifest import verify as verify_signature
from scripts.sign_toolchain_manifest import validate_manifest


def verify_pack(root: Path, public_key: Path) -> dict:
    root = root.resolve(strict=True)
    manifest = root / "manifest.json"
    signature = root / "manifest.json.sig"
    value = validate_manifest(manifest)
    verify_signature(manifest, signature, public_key)
    for tool in value["tools"]:
        relative = Path(tool["path"])
        if relative.is_absolute() or ".." in relative.parts or "\\" in tool["path"]:
            raise ValueError(f"unsafe tool path: {tool['path']}")
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root) or path.is_symlink() or not path.is_file():
            raise ValueError(f"tool is not a regular file inside the pack: {tool['name']}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != tool["sha256"]:
            raise ValueError(f"tool digest mismatch: {tool['name']}")
        if not os.access(path, os.X_OK):
            raise ValueError(f"tool is not executable: {tool['name']}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--public-key", required=True, type=Path)
    args = parser.parse_args()
    value = verify_pack(args.root, args.public_key)
    print(f"OK compiler pack abi={value['abi']} version={value['version']} tools={len(value['tools'])}")


if __name__ == "__main__":
    main()
