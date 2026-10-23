#!/usr/bin/env python3
"""Require two independent wheel outputs to be byte-for-byte identical."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def wheel_hashes(directory: Path) -> dict[str, str]:
    if not directory.is_dir():
        raise ValueError(f"wheel directory does not exist: {directory}")
    wheels = sorted(path for path in directory.iterdir() if path.is_file() and path.suffix == ".whl")
    if not wheels:
        raise ValueError(f"no wheels found in {directory}")
    result = {}
    for wheel in wheels:
        digest_builder = hashlib.sha256()
        with wheel.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest_builder.update(block)
        digest = digest_builder.hexdigest()
        if wheel.name in result:
            raise ValueError(f"duplicate wheel output: {wheel.name}")
        result[wheel.name] = digest
    return result


def compare(first: Path, second: Path) -> None:
    first_hashes = wheel_hashes(first)
    second_hashes = wheel_hashes(second)
    if first_hashes != second_hashes:
        raise ValueError(f"wheel outputs differ: first={first_hashes}; second={second_hashes}")
    for filename, digest in sorted(first_hashes.items()):
        print(f"REPRODUCIBLE {filename} sha256={digest}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    args = parser.parse_args()
    compare(args.first, args.second)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
