#!/usr/bin/env python3
"""Verify the pinned official PySide6/Shiboken6 Android wheel pair.

The regular PyRun wheel verifier targets Chaquopy's CPython 3.13 wheel tag.
Qt for Python 6.12 intentionally uses a different, self-contained CPython 3.14
runtime, so it has a separate verifier and must never enter the ordinary pip
installation path.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import stat
import subprocess
import tempfile
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath

from packaging.utils import parse_wheel_filename


QT_VERSION = "6.12.0"
TARGET_PYTHON_SONAME = "libpython3.14.so"
MAX_MEMBERS = 100_000
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MIN_LOAD_ALIGNMENT = 16 * 1024

ARCHITECTURES = {
    "aarch64": {
        "qt_abi": "arm64-v8a",
        "machine": "AArch64",
    },
    "x86_64": {
        "qt_abi": "x86_64",
        "machine": "Advanced Micro Devices X86-64",
    },
}

EXPECTED_SHA256 = {
    "pyside6:aarch64": "2cb8352ae6cd091c9f5e11d7bc4ba3bf54088548f63cd804adfdffe5c97c5eb2",
    "pyside6:x86_64": "4f5cbfe5e0043aa027a322ff9e3881e705ed6f7d18e04ff2a430e7eeb5f44246",
    "shiboken6:aarch64": "dc5dc4ca86c88327575a9d338c678a8f81f859a25a4dc0e6a539e465ab881660",
    "shiboken6:x86_64": "8d5c45ecb0d86e77e9003f3a8e0589bc97fa206d523f2ee4c342fdf68806f02e",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > MAX_MEMBERS:
        raise ValueError("Qt wheel contains too many archive members")
    if sum(member.file_size for member in members) > MAX_UNCOMPRESSED_BYTES:
        raise ValueError("Qt wheel expands beyond the configured 2 GiB limit")
    seen: set[str] = set()
    for member in members:
        name = member.filename
        parts = PurePosixPath(name.rstrip("/")).parts
        mode = member.external_attr >> 16
        if (
            not name
            or name.startswith(("/", "\\"))
            or "\\" in name
            or "\x00" in name
            or ":" in name.split("/", 1)[0]
            or any(part in ("", ".", "..") for part in parts)
            or stat.S_ISLNK(mode)
        ):
            raise ValueError(f"unsafe Qt wheel member: {name!r}")
        if name in seen:
            raise ValueError(f"duplicate Qt wheel member: {name!r}")
        seen.add(name)
    return members


def _run_readelf(readelf: str, option: str, path: Path) -> str:
    return subprocess.run(
        [readelf, option, str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _verify_elf(
    readelf: str,
    path: Path,
    expected_machine: str,
    *,
    require_alignment: bool,
) -> tuple[str, bool]:
    header = _run_readelf(readelf, "-h", path)
    machine = re.search(r"^\s*Machine:\s*(.+)$", header, re.MULTILINE)
    if machine is None or machine.group(1).strip() != expected_machine:
        raise ValueError(f"{path.name}: ELF machine does not match {expected_machine}")
    program_headers = _run_readelf(readelf, "-lW", path)
    loads = [line.split() for line in program_headers.splitlines() if line.lstrip().startswith("LOAD ")]
    if not loads:
        raise ValueError(f"{path.name}: no ELF LOAD segments found")
    aligned = True
    for fields in loads:
        try:
            alignment = int(fields[-1], 16)
        except (IndexError, ValueError) as error:
            raise ValueError(f"{path.name}: could not parse ELF LOAD alignment") from error
        if alignment < MIN_LOAD_ALIGNMENT:
            aligned = False
    if require_alignment and not aligned:
        raise ValueError(f"{path.name}: LOAD alignment is below 16 KB")
    return _run_readelf(readelf, "-d", path), aligned


def _wheel_identity(path: Path, distribution: str, architecture: str) -> None:
    actual_distribution, version, _build, tags = parse_wheel_filename(path.name)
    expected_platform = f"android_{architecture}"
    if str(actual_distribution).lower().replace("_", "-") != distribution:
        raise ValueError(f"{path.name}: unexpected distribution")
    if str(version) != QT_VERSION:
        raise ValueError(f"{path.name}: expected Qt for Python {QT_VERSION}")
    if not any(
        tag.interpreter == "cp310" and tag.abi == "abi3" and tag.platform == expected_platform
        for tag in tags
    ):
        raise ValueError(f"{path.name}: expected cp310-abi3-{expected_platform}")


def verify_wheel(
    path: Path,
    distribution: str,
    architecture: str,
    readelf: str,
    *,
    check_all_elf: bool = True,
    require_alignment: bool = True,
    require_reviewed_upstream_digest: bool = True,
) -> dict[str, object]:
    if architecture not in ARCHITECTURES:
        raise ValueError(f"unsupported Qt Android architecture: {architecture}")
    _wheel_identity(path, distribution, architecture)
    expected_digest = EXPECTED_SHA256[f"{distribution}:{architecture}"]
    actual_digest = sha256_file(path)
    if require_reviewed_upstream_digest and actual_digest != expected_digest:
        raise ValueError(f"{path.name}: SHA-256 differs from the reviewed upstream artifact")

    abi = ARCHITECTURES[architecture]["qt_abi"]
    required = {
        "pyside6": {
            "PySide6/QtCore.abi3.so",
            "PySide6/QtGui.abi3.so",
            "PySide6/QtWidgets.abi3.so",
            "PySide6/QtQml.abi3.so",
            "PySide6/QtQuick.abi3.so",
            "PySide6/libpyside6.abi3.so",
            f"PySide6/Qt/lib/libQt6Core_{abi}.so",
            f"PySide6/Qt/lib/libQt6Gui_{abi}.so",
            f"PySide6/Qt/lib/libQt6Widgets_{abi}.so",
            f"PySide6/Qt/plugins/platforms/libplugins_platforms_qtforandroid_{abi}.so",
            "PySide6/jar/Qt6Android.jar",
            "PySide6/jar/Qt6AndroidBindings.jar",
        },
        "shiboken6": {
            "shiboken6/Shiboken.abi3.so",
            "shiboken6/libshiboken6.abi3.so",
        },
    }[distribution]

    with zipfile.ZipFile(path) as archive, tempfile.TemporaryDirectory(prefix="pyrun-qt-wheel-") as temp:
        members = safe_members(archive)
        names = {member.filename for member in members}
        missing = sorted(required - names)
        if missing:
            raise ValueError(f"{path.name}: required Android Qt files are missing: {missing}")
        metadata_name = next(
            (name for name in names if name.endswith(".dist-info/METADATA")),
            None,
        )
        wheel_name = next((name for name in names if name.endswith(".dist-info/WHEEL")), None)
        if metadata_name is None or wheel_name is None:
            raise ValueError(f"{path.name}: missing wheel metadata")
        metadata = Parser().parsestr(archive.read(metadata_name).decode("utf-8"))
        if metadata.get("Version") != QT_VERSION:
            raise ValueError(f"{path.name}: METADATA version differs")
        wheel_metadata = Parser().parsestr(archive.read(wheel_name).decode("utf-8"))
        if f"cp310-abi3-android_{architecture}" not in set(wheel_metadata.get_all("Tag", [])):
            raise ValueError(f"{path.name}: internal Android wheel tag differs")

        libraries = [member for member in members if member.filename.endswith(".so")]
        if not libraries:
            raise ValueError(f"{path.name}: no native libraries found")
        if not check_all_elf:
            libraries = [member for member in libraries if member.filename in required]
        linked_python = False
        unaligned_libraries: list[str] = []
        root = Path(temp)
        for index, member in enumerate(libraries):
            local = root / f"{index}-{Path(member.filename).name}"
            local.write_bytes(archive.read(member))
            dynamic, aligned = _verify_elf(
                readelf,
                local,
                str(ARCHITECTURES[architecture]["machine"]),
                require_alignment=require_alignment,
            )
            if not aligned:
                unaligned_libraries.append(member.filename)
            linked_python = linked_python or TARGET_PYTHON_SONAME in dynamic
        if not linked_python:
            raise ValueError(f"{path.name}: expected dependency on {TARGET_PYTHON_SONAME}")

    return {
        "distribution": distribution,
        "version": QT_VERSION,
        "architecture": architecture,
        "androidAbi": abi,
        "sha256": actual_digest,
        "nativeLibraryCount": len(libraries),
        "pythonSoname": TARGET_PYTHON_SONAME,
        "unalignedLibraries": tuple(unaligned_libraries),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pyside", required=True, type=Path)
    parser.add_argument("--shiboken", required=True, type=Path)
    parser.add_argument("--architecture", required=True, choices=sorted(ARCHITECTURES))
    parser.add_argument("--readelf", required=True)
    parser.add_argument("--required-elf-only", action="store_true")
    parser.add_argument(
        "--locally-built",
        action="store_true",
        help="accept a wheel built in the pinned Qt for Python source workflow",
    )
    parser.add_argument(
        "--allow-unaligned-upstream",
        action="store_true",
        help="report upstream 4 KB ELF files without accepting them as a release runtime",
    )
    args = parser.parse_args()
    for distribution, path in (("pyside6", args.pyside), ("shiboken6", args.shiboken)):
        result = verify_wheel(
            path,
            distribution,
            args.architecture,
            args.readelf,
            check_all_elf=not args.required_elf_only,
            require_alignment=not args.allow_unaligned_upstream,
            require_reviewed_upstream_digest=not args.locally_built,
        )
        print(
            f"OK {path.name} sha256={result['sha256']} "
            f"abi={result['androidAbi']} native={result['nativeLibraryCount']} "
            f"unaligned={len(result['unalignedLibraries'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
