#!/usr/bin/env python3
"""Copy a verified, signed repository snapshot to a new release directory."""

from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

try:
    from .sign_manifest import validate_manifest, verify_signature
except ImportError:
    from sign_manifest import validate_manifest, verify_signature


def promote(snapshot: Path, destination: Path, public_key: Path) -> None:
    if snapshot.is_symlink():
        raise ValueError("snapshot must not be a symbolic link")
    snapshot = snapshot.resolve(strict=True)
    if not snapshot.is_dir():
        raise ValueError("snapshot must be a real directory")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"refusing to overwrite existing release directory: {destination}")
    for item in snapshot.rglob("*"):
        if item.is_symlink():
            raise ValueError(f"snapshot contains a symbolic link: {item.relative_to(snapshot)}")

    manifest_path = snapshot / "manifest.json"
    validate_manifest(manifest_path)
    signature = snapshot / "manifest.json.sig"
    if not signature.is_file():
        raise ValueError("signed manifest is required before promotion")
    verify_signature(manifest_path, public_key.resolve(strict=True), signature)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.promote-", dir=destination.parent))
    temporary.rmdir()
    try:
        shutil.copytree(snapshot, temporary)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"release destination appeared during promotion: {destination}")
        temporary.rename(destination)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path, help="immutable signed snapshot to promote or roll back to")
    parser.add_argument("destination", type=Path, help="new, previously unused deployment directory")
    parser.add_argument("--public-key", required=True, type=Path)
    args = parser.parse_args()
    promote(args.snapshot, args.destination, args.public_key)
    print(f"Verified and copied snapshot {args.snapshot} -> {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
