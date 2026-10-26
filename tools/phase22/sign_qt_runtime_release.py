#!/usr/bin/env python3
"""Create a signed, immutable manifest for a verified Qt runtime AAR."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import shutil

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ARTIFACT_NAME = "pyrun-qt-runtime-6.12.0-universal.aar"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sign(
    aar: Path,
    private_key_path: Path,
    public_key_path: Path,
    release_id: str,
    repository: str,
    revision: str,
    run_id: str,
    output: Path,
) -> Path:
    if not RELEASE_ID.fullmatch(release_id) or ".." in release_id:
        raise ValueError("invalid immutable Qt runtime release ID")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("source revision must be a full lowercase Git commit")
    if not run_id.isdecimal() or int(run_id) <= 0:
        raise ValueError("source workflow run ID must be a positive integer")
    if aar.name != ARTIFACT_NAME or not aar.is_file() or aar.stat().st_size > 1024 * 1024 * 1024:
        raise ValueError("invalid Qt runtime artifact")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"refusing to replace release files in {output}")
    output.mkdir(parents=True, exist_ok=True)

    private_key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("Qt runtime signing key must be Ed25519")
    configured_public = serialization.load_pem_public_key(public_key_path.read_bytes())
    derived_public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    configured_public_raw = configured_public.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    if derived_public != configured_public_raw:
        raise ValueError("private key does not match PyRun's pinned public key")

    artifact_sha256 = _sha256(aar)
    manifest = {
        "schemaVersion": 1,
        "releaseId": release_id,
        "runtime": {
            "name": "pyrun-qt-runtime",
            "version": "6.12.0",
            "pythonVersion": "3.14",
            "androidMinApi": 28,
            "abis": ["arm64-v8a", "x86_64"],
            "filename": ARTIFACT_NAME,
            "sha256": artifact_sha256,
            "size": aar.stat().st_size,
        },
        "source": {
            "repository": repository,
            "revision": revision,
            "workflowRunId": run_id,
        },
        "publicKeySha256": hashlib.sha256(configured_public_raw).hexdigest(),
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    signature = private_key.sign(manifest_bytes)
    if len(signature) != 64:
        raise ValueError("unexpected Ed25519 signature size")

    shutil.copyfile(aar, output / ARTIFACT_NAME)
    (output / "manifest.json").write_bytes(manifest_bytes)
    (output / "manifest.json.sig").write_bytes(signature)
    (output / f"{ARTIFACT_NAME}.sha256").write_text(
        f"{artifact_sha256}  {ARTIFACT_NAME}\n", encoding="ascii"
    )
    print(
        f"Signed Qt runtime release {release_id}: sha256={artifact_sha256} "
        f"publicKey={base64.b64encode(configured_public_raw).decode('ascii')}"
    )
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aar", required=True, type=Path)
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--public-key", required=True, type=Path)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    sign(
        args.aar.resolve(),
        args.private_key.resolve(),
        args.public_key.resolve(),
        args.release_id,
        args.repository,
        args.revision,
        args.run_id,
        args.output.resolve(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
