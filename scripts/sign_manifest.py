#!/usr/bin/env python3
"""Sign or verify an exact repository manifest using Ed25519."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path, PurePosixPath

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from jsonschema import validate


ROOT = Path(__file__).resolve().parents[1]


def validate_manifest(manifest_path: Path) -> dict:
    manifest_path = manifest_path.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schema = json.loads((ROOT / "schemas" / "repository-manifest.schema.json").read_text(encoding="utf-8"))
    validate(instance=manifest, schema=schema)
    manifest_root = manifest_path.parent.resolve(strict=True)

    def resolve_artifact(relative_path: str) -> Path:
        relative = PurePosixPath(relative_path)
        if "\\" in relative_path or relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
            raise ValueError(f"unsafe manifest artifact path: {relative_path}")
        resolved = manifest_root.joinpath(*relative.parts).resolve(strict=True)
        if not resolved.is_relative_to(manifest_root) or not resolved.is_file():
            raise ValueError(f"manifest artifact escapes repository root: {relative_path}")
        return resolved

    def verify_hash(relative_path: str, expected_digest: str) -> None:
        artifact_path = resolve_artifact(relative_path)
        digest = hashlib.sha256()
        with artifact_path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != expected_digest:
            raise ValueError(f"manifest hash mismatch: {relative_path}")

    for artifact in manifest["artifacts"]:
        verify_hash(artifact["url"], artifact["sha256"])
        recipe_path = resolve_artifact(artifact["recipePath"])
        verify_hash(artifact["recipePath"], artifact["recipeSha256"])
        recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
        if recipe.get("release_status") not in (None, "approved"):
            raise ValueError(
                f"recipe is not approved for publication: {recipe.get('name', artifact['name'])}"
            )
        verify_hash(artifact["sbomPath"], artifact["sbomSha256"])
        for patch in artifact["patches"]:
            verify_hash(patch["path"], patch["sha256"])
    for index in manifest["simpleIndexes"]:
        verify_hash(index["path"], index["sha256"])
    verify_hash(manifest["toolchainPath"], manifest["toolchainSha256"])
    return manifest


def verify_signature(manifest: Path, public_key: Path, signature: Path) -> None:
    key = serialization.load_pem_public_key(public_key.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("repository verification key must be Ed25519")
    try:
        key.verify(signature.read_bytes(), manifest.read_bytes())
    except InvalidSignature as error:
        raise ValueError("manifest signature verification failed") from error


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="mode", required=True)
    sign = subparsers.add_parser("sign")
    sign.add_argument("manifest", type=Path)
    key_source = sign.add_mutually_exclusive_group(required=True)
    key_source.add_argument("--private-key", type=Path)
    key_source.add_argument(
        "--private-key-env",
        help="read an unencrypted PEM private key from this environment variable",
    )
    sign.add_argument("--signature", type=Path)
    verify = subparsers.add_parser("verify")
    verify.add_argument("manifest", type=Path)
    verify.add_argument("--public-key", required=True, type=Path)
    verify.add_argument("--signature", required=True, type=Path)
    args = parser.parse_args()
    validate_manifest(args.manifest)

    if args.mode == "sign":
        signature = args.signature or args.manifest.with_suffix(args.manifest.suffix + ".sig")
        if signature.exists() or signature.is_symlink():
            raise FileExistsError(f"refusing to replace existing signature: {signature}")
        if args.private_key is not None:
            private_key_bytes = args.private_key.read_bytes()
        else:
            private_key_value = os.environ.get(args.private_key_env)
            if not private_key_value:
                raise ValueError(f"environment variable {args.private_key_env} is not set")
            private_key_bytes = private_key_value.encode("utf-8")
        key = serialization.load_pem_private_key(private_key_bytes, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("repository signing key must be Ed25519")
        signature.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix="pyrun-signature-", dir=signature.parent, delete=False) as output:
            temporary = Path(output.name)
        try:
            temporary.write_bytes(key.sign(args.manifest.read_bytes()))
            if signature.exists() or signature.is_symlink():
                raise FileExistsError(f"signature appeared during signing: {signature}")
            temporary.replace(signature)
        finally:
            temporary.unlink(missing_ok=True)
        print(f"Signed {args.manifest.name} -> {signature.name}")
        return 0

    verify_signature(args.manifest, args.public_key, args.signature)
    print(f"Verified {args.manifest.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
