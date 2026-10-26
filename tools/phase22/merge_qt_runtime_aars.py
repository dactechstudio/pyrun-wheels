#!/usr/bin/env python3
"""Merge two single-ABI Qt runtime AARs into one deterministic AAR."""

from __future__ import annotations

import argparse
import io
import hashlib
import re
import stat
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
import xml.etree.ElementTree as ET
import zipfile


ZIP_TIME = (1980, 1, 1, 0, 0, 0)
EXPECTED_ABIS = ("arm64-v8a", "x86_64")
MAX_MEMBERS = 100_000
MAX_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024


def _read_aar(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS or sum(item.file_size for item in members) > MAX_UNCOMPRESSED_BYTES:
            raise ValueError(f"{path.name}: AAR exceeds safety limits")
        result = {}
        for item in members:
            if item.is_dir():
                continue
            name = item.filename
            parts = PurePosixPath(name).parts
            mode = item.external_attr >> 16
            if (
                not name
                or name.startswith(("/", "\\"))
                or "\\" in name
                or any(part in ("", ".", "..") for part in parts)
                or stat.S_ISLNK(mode)
                or name in result
            ):
                raise ValueError(f"{path.name}: unsafe or duplicate member {name!r}")
            result[name] = archive.read(item)
        return result


def _merge_values_xml(
    first: bytes,
    second: bytes,
    *,
    allowed_scalar_differences: frozenset[tuple[str, str | None]] = frozenset(),
) -> bytes:
    first_root = ET.fromstring(first)
    second_root = ET.fromstring(second)
    first_by_key = {(item.tag, item.attrib.get("name")): item for item in first_root}
    second_by_key = {(item.tag, item.attrib.get("name")): item for item in second_root}
    if set(first_by_key) != set(second_by_key):
        raise ValueError("Qt resource keys differ between ABI builds")
    for key, first_item in first_by_key.items():
        second_item = second_by_key[key]
        if first_item.tag == "array":
            values = []
            for item in list(first_item) + list(second_item):
                value = (item.text or "").strip()
                if value and value not in values:
                    values.append(value)
            for item in list(first_item):
                first_item.remove(item)
            for value in values:
                child = ET.SubElement(first_item, "item")
                child.text = value
        elif key in allowed_scalar_differences:
            # Generated P4A private_version is a content hash of the private
            # archive and is intentionally retained from the arm64 copy.
            continue
        elif ET.tostring(first_item) != ET.tostring(second_item):
            raise ValueError(f"Qt scalar resource differs between ABI builds: {key}")
    return ET.tostring(first_root, encoding="utf-8", xml_declaration=True)


def _normalize_private_recipe(name: str, content: bytes) -> bytes:
    """Normalize ABI-generated P4A recipe metadata before comparing archives.

    ``pyside6-android-deploy`` writes the selected wheel's absolute build path
    into the recipe and emits the module-copy statements in filesystem order.
    Both values legitimately vary between ABI jobs, while the recipe's logical
    module set must remain identical.  Keep the complete recipe comparison for
    every other file and canonicalize only these generated details.
    """

    if name not in {
        "deployment/recipes/PySide6/__init__.py",
        "deployment/recipes/shiboken6/__init__.py",
    }:
        return content
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content

    text = re.sub(
        r"(?m)^(\s*wheel_path\s*=\s*)['\"].*?['\"]\s*$",
        r"\1'<ABI-specific-wheel>'",
        text,
    )
    if name.endswith("/PySide6/__init__.py"):
        # The deploy helper adds one generic noqa marker around every copied
        # module; its placement follows the same nondeterministic filesystem
        # order as the copy statements and carries no runtime meaning.
        text = re.sub(r"(?m)^\s*# noqa: E999\s*\n?", "", text)
        lines = text.splitlines()
        kept: list[str] = []
        copy_blocks: list[str] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            if "shutil.copyfile(" not in line:
                kept.append(line)
                index += 1
                continue
            block = [line.strip()]
            index += 1
            while index < len(lines):
                block.append(lines[index].strip())
                if lines[index].rstrip().endswith(")"):
                    index += 1
                    break
                index += 1
            copy_blocks.append(" ".join(block))
        text = "\n".join(kept)
        text += "\n__PYRUN_CANONICAL_COPY_BLOCKS__\n"
        text += "\n".join(sorted(copy_blocks))
    return text.encode("utf-8")


def _private_archive_files(payload: bytes) -> dict[str, bytes]:
    """Return logical P4A private files while ignoring generated ABI metadata."""
    result: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
        members = archive.getmembers()
        if len(members) > MAX_MEMBERS:
            raise ValueError("P4A private archive contains too many members")
        for member in members:
            parts = PurePosixPath(member.name.rstrip("/")).parts
            if (
                not member.name
                or member.name.startswith(("/", "\\"))
                or "\\" in member.name
                or any(part in ("", ".", "..") for part in parts)
                or member.issym()
                or member.islnk()
            ):
                raise ValueError(f"unsafe P4A private archive member: {member.name!r}")
            if member.isdir():
                continue
            if not member.isfile() or member.name in result:
                raise ValueError(f"unsupported or duplicate P4A private member: {member.name!r}")
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"could not read P4A private member: {member.name!r}")
            content = source.read()
            if content.startswith(b"\x7fELF"):
                raise ValueError(f"native code must not be hidden in P4A assets: {member.name!r}")
            result[member.name] = _normalize_private_recipe(member.name, content)
    if sum(len(content) for content in result.values()) > MAX_UNCOMPRESSED_BYTES:
        raise ValueError("P4A private archive expands beyond the configured limit")
    return result


def _write_entry(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(name, ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, payload)


def merge(arm64_aar: Path, x86_64_aar: Path, output: Path) -> Path:
    if output.exists():
        raise FileExistsError(f"refusing to replace merged Qt runtime: {output}")
    first = _read_aar(arm64_aar)
    second = _read_aar(x86_64_aar)
    first_native = {name for name in first if name.startswith("jni/arm64-v8a/")}
    second_native = {name for name in second if name.startswith("jni/x86_64/")}
    if not first_native or not second_native:
        raise ValueError("single-ABI Qt AARs do not contain their expected jni directory")
    if any(name.startswith("jni/") and name not in first_native for name in first):
        raise ValueError("arm64 Qt AAR contains an unexpected ABI")
    if any(name.startswith("jni/") and name not in second_native for name in second):
        raise ValueError("x86_64 Qt AAR contains an unexpected ABI")

    values_name = "res/values/libs.xml"
    merged = {}
    common_names = (set(first) - first_native) | (set(second) - second_native)
    for name in sorted(common_names):
        left = first.get(name)
        right = second.get(name)
        if left is None or right is None:
            raise ValueError(f"Qt AAR common member is missing from one ABI: {name}")
        if name == values_name:
            merged[name] = _merge_values_xml(left, right)
        elif name.startswith("res/values/") and name.endswith(".xml"):
            merged[name] = _merge_values_xml(
                left,
                right,
                allowed_scalar_differences=frozenset({("string", "private_version")}),
            )
        elif name in {"assets/private.tar", "assets/private.mp3"}:
            if _private_archive_files(left) != _private_archive_files(right):
                raise ValueError("P4A private application differs between ABI builds")
            # Tar timestamps are not meaningful; retain the arm64 copy after
            # proving that both archives contain the exact same logical files.
            merged[name] = left
        elif left != right:
            raise ValueError(f"Qt AAR common member differs between ABI builds: {name}")
        else:
            merged[name] = left
    merged.update({name: first[name] for name in first_native})
    merged.update({name: second[name] for name in second_native})
    metadata = (
        "schemaVersion=1\n"
        "qtVersion=6.12.0\n"
        "pythonVersion=3.14\n"
        "androidMinApi=28\n"
        "abis=arm64-v8a,x86_64\n"
        "bootstrapLibrary=qtmain\n"
    ).encode("utf-8")
    merged["pyrun-qt-runtime.properties"] = metadata

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="pyrun-qt-runtime-", suffix=".aar", dir=output.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            for name in sorted(merged):
                _write_entry(archive, name, merged[name])
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm64", required=True, type=Path)
    parser.add_argument("--x86-64", required=True, type=Path, dest="x86_64")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = merge(args.arm64, args.x86_64, args.output)
    print(f"Merged Qt runtime AAR: {result} sha256={hashlib.sha256(result.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
