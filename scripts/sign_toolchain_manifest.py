"""Sign and verify an Android compiler-pack manifest with the release key."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def validate_manifest(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schemaVersion") != 1 or value.get("profile") != "full":
        raise ValueError("not a full compiler-pack manifest")
    if value.get("abi") not in {"arm64_v8a", "x86_64"}:
        raise ValueError("compiler pack has an unsupported ABI")
    if int(value.get("androidMinApi", 0)) < 25:
        raise ValueError("compiler pack Android API is too old")
    tools = value.get("tools")
    if not isinstance(tools, list) or not tools:
        raise ValueError("compiler-pack tool records are missing")
    names = {item.get("name") for item in tools if isinstance(item, dict)}
    required = {"clang", "clang++", "ld.lld", "llvm-ar", "llvm-strip", "make", "m4", "pkg-config"}
    if not required.issubset(names):
        raise ValueError(f"compiler pack is missing required tools: {sorted(required - names)}")
    return value


def _private_key(value: str) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(value.encode("utf-8"), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("release key must be Ed25519")
    return key


def _public_key(path: Path) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("verification key must be Ed25519")
    return key


def sign(manifest: Path, signature: Path, private_key: str) -> None:
    validate_manifest(manifest)
    if signature.exists() or signature.is_symlink():
        raise FileExistsError(f"refusing to replace signature: {signature}")
    signature.parent.mkdir(parents=True, exist_ok=True)
    signature.write_bytes(_private_key(private_key).sign(manifest.read_bytes()))


def verify(manifest: Path, signature: Path, public_key: Path) -> None:
    validate_manifest(manifest)
    _public_key(public_key).verify(signature.read_bytes(), manifest.read_bytes())


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="mode", required=True)
    sign_parser = subparsers.add_parser("sign")
    sign_parser.add_argument("manifest", type=Path)
    sign_parser.add_argument("--private-key-env", required=True)
    sign_parser.add_argument("--signature", type=Path)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("manifest", type=Path)
    verify_parser.add_argument("--signature", required=True, type=Path)
    verify_parser.add_argument("--public-key", required=True, type=Path)
    args = parser.parse_args()
    if args.mode == "sign":
        private_key = os.environ.get(args.private_key_env, "")
        if not private_key:
            raise SystemExit(f"missing protected environment secret: {args.private_key_env}")
        signature = args.signature or args.manifest.with_suffix(args.manifest.suffix + ".sig")
        sign(args.manifest, signature, private_key)
        print(f"Signed compiler-pack manifest: {args.manifest}")
    else:
        verify(args.manifest, args.signature, args.public_key)
        print(f"Verified compiler-pack manifest: {args.manifest}")


if __name__ == "__main__":
    main()
