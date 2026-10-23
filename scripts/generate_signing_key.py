#!/usr/bin/env python3
"""Create a new Ed25519 release keypair without placing the private key in Git."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PUBLIC_KEY = ROOT / "keys" / "pyrun-ed25519-public.pem"


def _write_new(path: Path, contents: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, mode)
    with os.fdopen(descriptor, "wb") as output:
        output.write(contents)
        output.flush()
        os.fsync(output.fileno())


def generate_keypair(
    private_key_path: Path,
    public_key_path: Path = DEFAULT_PUBLIC_KEY,
    repository_root: Path = ROOT,
) -> tuple[Path, Path]:
    root = repository_root.resolve(strict=True)
    requested_private_path = private_key_path.expanduser()
    requested_public_path = public_key_path.expanduser()
    if requested_private_path.is_symlink() or requested_public_path.is_symlink():
        raise ValueError("key output paths must not be symbolic links")
    private_path = requested_private_path.resolve()
    public_path = requested_public_path.resolve()
    key_directory = (root / "keys").resolve(strict=True)

    if private_path == root or root in private_path.parents:
        raise ValueError("private key must be stored outside the repository")
    if public_path.parent != key_directory:
        raise ValueError("public verification key must be stored directly in the repository keys/ directory")
    if private_path == public_path:
        raise ValueError("private and public key paths must be different")
    for path in (private_path, public_path):
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"refusing to replace existing key file: {path}")

    key = Ed25519PrivateKey.generate()
    private_bytes = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_bytes = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    private_created = False
    try:
        _write_new(private_path, private_bytes, 0o600)
        private_created = True
        _write_new(public_path, public_bytes, 0o644)
    except Exception:
        if private_created:
            private_path.unlink(missing_ok=True)
        raise
    return private_path, public_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--private-key",
        required=True,
        type=Path,
        help="path outside the repository for the private PEM key",
    )
    parser.add_argument(
        "--public-key",
        type=Path,
        default=DEFAULT_PUBLIC_KEY,
        help=f"public PEM path inside keys/ (default: {DEFAULT_PUBLIC_KEY})",
    )
    args = parser.parse_args()
    private_path, public_path = generate_keypair(args.private_key, args.public_key)
    print(f"Created Ed25519 public verification key: {public_path}")
    print(f"Created private signing key outside the repository: {private_path}")
    print("Keep the private key secret; add it only to the protected wheel-release environment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
