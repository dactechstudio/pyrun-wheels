#!/usr/bin/env python3
"""Verify Android wheel metadata, safe ZIP members, ABI, hash, and ELF alignment."""

from __future__ import annotations

import argparse
import base64
import csv
import io
import hashlib
import re
import stat
import subprocess
import tempfile
import zipfile
from email.parser import Parser
from pathlib import Path

from packaging.utils import parse_wheel_filename
from packaging.specifiers import SpecifierSet
from packaging.version import Version


EXPECTED_MACHINE = {
    "arm64_v8a": "AArch64",
    "x86_64": "Advanced Micro Devices X86-64",
}
MIN_LOAD_ALIGNMENT = 16 * 1024
MAX_WHEEL_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_WHEEL_MEMBERS = 100_000


def safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > MAX_WHEEL_MEMBERS:
        raise ValueError("wheel contains too many archive members")
    if sum(member.file_size for member in members) > MAX_WHEEL_UNCOMPRESSED_BYTES:
        raise ValueError("wheel expands beyond the configured 1 GiB limit")
    seen: set[str] = set()
    for member in members:
        name = member.filename
        mode = member.external_attr >> 16
        if (
            not name
            or name.startswith(("/", "\\"))
            or "\\" in name
            or "\x00" in name
            or ":" in name.split("/", 1)[0]
            or any(part in ("", ".", "..") for part in name.rstrip("/").split("/"))
            or stat.S_ISLNK(mode)
        ):
            raise ValueError(f"unsafe wheel member: {name!r}")
        if name in seen:
            raise ValueError(f"duplicate wheel member: {name!r}")
        seen.add(name)
    return members


def check_load_alignment(readelf: str, library: Path) -> None:
    output = subprocess.run(
        [readelf, "-lW", str(library)], check=True, capture_output=True, text=True
    ).stdout
    loads = [line.split() for line in output.splitlines() if line.lstrip().startswith("LOAD ")]
    if not loads:
        raise ValueError(f"no ELF LOAD segments found in {library.name}")
    for fields in loads:
        try:
            alignment = int(fields[-1], 16)
        except (IndexError, ValueError) as error:
            raise ValueError(f"could not parse ELF LOAD alignment in {library.name}") from error
        if alignment < MIN_LOAD_ALIGNMENT:
            raise ValueError(
                f"{library.name} has LOAD alignment {alignment}, below 16 KB"
            )


def verify_wheel(
    wheel: Path,
    expected_abi: str,
    android_api: int,
    readelf: str,
) -> str:
    distribution, version, _build, tags = parse_wheel_filename(wheel.name)
    platform_tag = f"android_{android_api}_{expected_abi}"
    if not any(
        tag.interpreter == "cp313"
        and tag.abi == "cp313"
        and tag.platform == platform_tag
        for tag in tags
    ):
        raise ValueError(f"{wheel.name}: expected cp313-cp313-{platform_tag}")

    digest_builder = hashlib.sha256()
    with wheel.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest_builder.update(block)
    digest = digest_builder.hexdigest()
    with zipfile.ZipFile(wheel) as archive, tempfile.TemporaryDirectory() as temp:
        members = safe_members(archive)
        metadata_name = next(
            (item.filename for item in members if item.filename.endswith(".dist-info/METADATA")),
            None,
        )
        wheel_metadata_name = next(
            (item.filename for item in members if item.filename.endswith(".dist-info/WHEEL")),
            None,
        )
        record_name = next(
            (item.filename for item in members if item.filename.endswith(".dist-info/RECORD")),
            None,
        )
        if metadata_name is None or wheel_metadata_name is None or record_name is None:
            raise ValueError(f"{wheel.name}: missing METADATA, WHEEL, or RECORD")
        metadata = Parser().parsestr(archive.read(metadata_name).decode("utf-8"))
        if str(distribution).lower().replace("_", "-") != str(metadata["Name"]).lower().replace("_", "-"):
            raise ValueError(f"{wheel.name}: filename and METADATA package names differ")
        if str(version) != str(metadata["Version"]):
            raise ValueError(f"{wheel.name}: filename and METADATA versions differ")
        requires_python = metadata.get("Requires-Python")
        if not requires_python or Version("3.13") not in SpecifierSet(requires_python):
            raise ValueError(f"{wheel.name}: Requires-Python does not include CPython 3.13")
        wheel_metadata = Parser().parsestr(archive.read(wheel_metadata_name).decode("utf-8"))
        internal_tags = set(wheel_metadata.get_all("Tag", []))
        expected_tag = f"cp313-cp313-{platform_tag}"
        if expected_tag not in internal_tags:
            raise ValueError(f"{wheel.name}: internal WHEEL tags differ from its filename")

        record_text = archive.read(record_name).decode("utf-8")
        record_rows = csv.reader(io.StringIO(record_text, newline=""))
        record_entries = {row[0]: row[1:] for row in record_rows if len(row) == 3}
        for member in members:
            if member.filename == record_name or member.is_dir():
                continue
            row = record_entries.get(member.filename)
            if row is None or not row[0] or not row[1]:
                raise ValueError(f"{wheel.name}: RECORD is missing integrity data for {member.filename}")
            algorithm, separator, expected_digest = row[0].partition("=")
            if algorithm != "sha256" or not separator:
                raise ValueError(f"{wheel.name}: unsupported RECORD hash for {member.filename}")
            actual_digest = base64.urlsafe_b64encode(
                hashlib.sha256(archive.read(member)).digest()
            ).decode("ascii").rstrip("=")
            if actual_digest != expected_digest or int(row[1]) != member.file_size:
                raise ValueError(f"{wheel.name}: RECORD integrity check failed for {member.filename}")

        libraries = [item for item in members if item.filename.endswith(".so")]
        if not libraries:
            raise ValueError(f"{wheel.name}: expected at least one native shared library")
        root = Path(temp)
        for index, item in enumerate(libraries):
            library = root / f"{index}-{Path(item.filename).name}"
            library.write_bytes(archive.read(item))
            header = subprocess.run(
                [readelf, "-h", str(library)], check=True, capture_output=True, text=True
            ).stdout
            machine = re.search(r"^\s*Machine:\s*(.+)$", header, re.MULTILINE)
            if machine is None or machine.group(1).strip() != EXPECTED_MACHINE[expected_abi]:
                raise ValueError(f"{item.filename}: ELF machine does not match {expected_abi}")
            check_load_alignment(readelf, library)
    return digest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheels", nargs="+", type=Path)
    parser.add_argument("--expected-abi", choices=sorted(EXPECTED_MACHINE))
    parser.add_argument("--android-api", type=int, default=25)
    parser.add_argument("--readelf", required=True)
    args = parser.parse_args()
    if args.expected_abi and len(args.wheels) != 1:
        parser.error("--expected-abi requires exactly one wheel")
    for wheel in args.wheels:
        expected_abi = args.expected_abi
        if expected_abi is None:
            _distribution, _version, _build, tags = parse_wheel_filename(wheel.name)
            candidates = {
                abi
                for abi in EXPECTED_MACHINE
                if any(tag.platform == f"android_{args.android_api}_{abi}" for tag in tags)
            }
            if len(candidates) != 1:
                raise ValueError(f"{wheel.name}: cannot uniquely determine target ABI")
            expected_abi = candidates.pop()
        digest = verify_wheel(wheel, expected_abi, args.android_api, args.readelf)
        print(f"OK {wheel.name} sha256={digest} abi={expected_abi} api={args.android_api}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
