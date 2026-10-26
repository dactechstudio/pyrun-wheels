#!/usr/bin/env python3
"""Release gate for PyRun's universal, isolated Qt runtime AAR.

This verifier deliberately understands only the Phase 22 runtime contract.  It
does not accept a generic Android library: the archive must contain both ABIs,
the P4A Qt activity bridge, the pinned Python 3.14 runtime, Qt/PySide/Shiboken,
and ELF LOAD segments which are safe on Android devices using 16 KiB pages.
"""

from __future__ import annotations

import argparse
import hashlib
import io
from pathlib import Path, PurePath
import re
import subprocess
import tarfile
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from merge_qt_runtime_aars import EXPECTED_ABIS, _private_archive_files, _read_aar


ANDROID = "http://schemas.android.com/apk/res/android"
MIN_LOAD_ALIGNMENT = 16 * 1024
EXPECTED_MACHINE = {
    "arm64-v8a": "AArch64",
    "x86_64": "Advanced Micro Devices X86-64",
}
REQUIRED_METADATA = {
    "schemaVersion": "1",
    "qtVersion": "6.12.0",
    "pythonVersion": "3.14",
    "androidMinApi": "28",
    "abis": "arm64-v8a,x86_64",
    "bootstrapLibrary": "qtmain",
}
REQUIRED_QT_MODULES = ("Core", "Gui", "Widgets", "Qml", "Quick")


def _properties(payload: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in payload.decode("utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or key in result:
            raise ValueError("invalid Qt runtime metadata")
        result[key] = value
    return result


def _class_names(jar_payload: bytes) -> set[str]:
    with tempfile.NamedTemporaryFile(suffix=".jar") as handle:
        handle.write(jar_payload)
        handle.flush()
        with zipfile.ZipFile(handle.name) as jar:
            return {
                item.filename
                for item in jar.infolist()
                if not item.is_dir() and item.filename.endswith(".class")
            }


def _verify_manifest(payload: bytes) -> None:
    root = ET.fromstring(payload)
    if root.find("uses-sdk") is not None:
        raise ValueError("Qt runtime AAR must not raise the base app minimum SDK")
    if root.findall("uses-permission"):
        raise ValueError("Qt runtime AAR must not silently add Android permissions")
    application = root.find("application")
    if application is None:
        raise ValueError("Qt runtime manifest has no application element")
    if application.attrib.get(f"{{{ANDROID}}}name"):
        raise ValueError("Qt runtime AAR must not replace PyRun's Application")
    activities = [
        item
        for item in application.findall("activity")
        if item.attrib.get(f"{{{ANDROID}}}name") == "org.kivy.android.PythonActivity"
    ]
    if len(activities) != 1:
        raise ValueError("Qt runtime AAR must declare exactly one PythonActivity")
    activity = activities[0]
    if activity.attrib.get(f"{{{ANDROID}}}exported") != "false":
        raise ValueError("Qt PythonActivity must not be exported")
    if activity.attrib.get(f"{{{ANDROID}}}process") != ":python_qt":
        raise ValueError("Qt PythonActivity must run in :python_qt")
    if activity.findall("intent-filter"):
        raise ValueError("Qt PythonActivity must not own a launcher intent filter")
    app_lib_values = [
        item.attrib.get(f"{{{ANDROID}}}value")
        for item in activity.findall("meta-data")
        if item.attrib.get(f"{{{ANDROID}}}name") == "android.app.lib_name"
    ]
    if app_lib_values != ["qtmain"]:
        raise ValueError("Qt PythonActivity must load the non-conflicting libqtmain bootstrap")


def _verify_alignment(readelf: str, path: Path, abi: str) -> None:
    header = subprocess.run(
        [readelf, "-h", str(path)], check=True, capture_output=True, text=True
    ).stdout
    machine = re.search(r"^\s*Machine:\s*(.+)$", header, re.MULTILINE)
    if machine is None or machine.group(1).strip() != EXPECTED_MACHINE[abi]:
        raise ValueError(f"{path.name}: wrong ELF machine for {abi}")
    program_headers = subprocess.run(
        [readelf, "-lW", str(path)], check=True, capture_output=True, text=True
    ).stdout
    load_lines = [line.split() for line in program_headers.splitlines() if line.lstrip().startswith("LOAD ")]
    if not load_lines:
        raise ValueError(f"{path.name}: no ELF LOAD segments")
    if any(int(fields[-1], 16) < MIN_LOAD_ALIGNMENT for fields in load_lines):
        raise ValueError(f"{path.name}: ELF LOAD alignment is below 16 KiB")


def _verify_scientific_bundle(members: dict[str, bytes], abi: str) -> None:
    bundle_name = f"jni/{abi}/libpybundle.so"
    with tarfile.open(fileobj=io.BytesIO(members[bundle_name]), mode="r:gz") as archive:
        bundle_files = {entry.name for entry in archive if entry.isfile()}
    for package in ("numpy", "matplotlib"):
        if not any(name.endswith(f"/site-packages/{package}/__init__.py") for name in bundle_files):
            raise ValueError(f"Qt scientific runtime is missing {package} for {abi}")
    if not any(name.endswith("/site-packages/matplotlib/backends/backend_qtagg.py") for name in bundle_files):
        raise ValueError(f"Qt scientific runtime is missing QtAgg for {abi}")
    native_names = {
        name for name in members if name.startswith(f"jni/{abi}/") and name.endswith(".so")
    }
    combined_names = native_names | bundle_files
    for native_fragment in ("multiarray_umath", "ft2font"):
        if not any(native_fragment in name and name.endswith(".so") for name in combined_names):
            raise ValueError(f"Qt scientific runtime is missing {native_fragment} for {abi}")


def verify(path: Path, readelf: str, runner_path: Path, require_scientific: bool = False) -> dict[str, object]:
    members = _read_aar(path)
    resource_candidates = [
        name for name in ("res/values/libs.xml", "res/values/values.xml") if name in members
    ]
    for required in ("AndroidManifest.xml", "classes.jar", "pyrun-qt-runtime.properties"):
        if required not in members:
            raise ValueError(f"Qt runtime AAR is missing {required}")
    for name, payload in members.items():
        if name.startswith("res/") and name.endswith(".png") and not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError(f"Qt runtime AAR has an invalid PNG resource: {name}")
        if name.startswith("res/") and name.endswith((".jpg", ".jpeg")) and not payload.startswith(b"\xff\xd8\xff"):
            raise ValueError(f"Qt runtime AAR has an invalid JPEG resource: {name}")
    if len(resource_candidates) != 1:
        raise ValueError(
            "Qt runtime AAR must contain exactly one generated values resource "
            "(res/values/libs.xml or res/values/values.xml)"
        )
    license_names = {
        Path(name).name
        for name in members
        if name.startswith("META-INF/pyrun-qt-licenses/")
    }
    for required_license in ("LGPL-3.0-only.txt", "Qt-GPL-exception-1.0.txt"):
        if required_license not in license_names:
            raise ValueError(f"Qt runtime AAR is missing {required_license}")
    metadata = _properties(members["pyrun-qt-runtime.properties"])
    if metadata != REQUIRED_METADATA:
        raise ValueError("Qt runtime metadata differs from the reviewed contract")
    _verify_manifest(members["AndroidManifest.xml"])

    classes: set[str] = set()
    for name, payload in members.items():
        if name == "classes.jar" or (name.startswith("libs/") and name.endswith(".jar")):
            classes.update(_class_names(payload))
    for required_class in (
        "org/kivy/android/PythonActivity.class",
        "org/qtproject/qt/android/bindings/QtActivity.class",
    ):
        if required_class not in classes:
            raise ValueError(f"Qt runtime AAR is missing {required_class}")

    values = ET.fromstring(members[resource_candidates[0]])
    resource_text = "\n".join((item.text or "") for item in values.iter("item"))
    for abi in EXPECTED_ABIS:
        if abi not in resource_text:
            raise ValueError(f"Qt runtime resources do not describe {abi}")

    native_count = 0
    with tempfile.TemporaryDirectory(prefix="pyrun-qt-runtime-") as temporary:
        root = Path(temporary)
        for abi in EXPECTED_ABIS:
            prefix = f"jni/{abi}/"
            libraries = {
                name: payload for name, payload in members.items() if name.startswith(prefix) and name.endswith(".so")
            }
            names = {Path(name).name for name in libraries}
            required_fragments = {
                "libqtmain.so",
                "libpybundle.so",
                "libpython3.14.so",
                "libpyside6.abi3.so",
                "libshiboken6.abi3.so",
                "QtCore.abi3.so",
                "QtGui.abi3.so",
                "QtWidgets.abi3.so",
            }
            missing = sorted(required_fragments - names)
            if missing:
                raise ValueError(f"Qt runtime is missing {abi} libraries: {missing}")
            if "libmain.so" in names:
                raise ValueError(f"Qt runtime would collide with PyRun's SDL libmain.so for {abi}")
            if require_scientific:
                _verify_scientific_bundle(members, abi)
            for module in REQUIRED_QT_MODULES:
                if not any(name.startswith(f"libQt6{module}") for name in names):
                    raise ValueError(f"Qt runtime is missing Qt6{module} for {abi}")
            if not any("qtforandroid" in name for name in names):
                raise ValueError(f"Qt runtime is missing the Android platform plugin for {abi}")
            for index, (name, payload) in enumerate(sorted(libraries.items())):
                if Path(name).name == "libpybundle.so":
                    # python-for-android intentionally stores the Python
                    # bundle as gzip bytes under a .so name. It is unpacked
                    # by the bootstrap and is not an ELF object to inspect
                    # with readelf; reject malformed bundles explicitly.
                    if payload[:3] != b"\x1f\x8b\x08":
                        raise ValueError(f"Qt runtime has an invalid compressed Python bundle: {name}")
                    continue
                local = root / f"{abi}-{index}-{Path(name).name}"
                local.write_bytes(payload)
                _verify_alignment(readelf, local, abi)
            native_count += len(libraries)

    private_assets = {
        name for name in members if name in {"assets/private.tar", "assets/private.mp3"}
    }
    if not private_assets:
        raise ValueError("Qt runtime AAR is missing the bundled P4A private application")
    if len(private_assets) != 1:
        raise ValueError("Qt runtime AAR contains ambiguous P4A private applications")
    private_files = _private_archive_files(members[next(iter(private_assets))])
    bundled_runners = [
        payload for name, payload in private_files.items() if PurePath(name).name == "main.py"
    ]
    expected_runner = runner_path.read_bytes()
    if len(bundled_runners) != 1 or bundled_runners[0] != expected_runner:
        raise ValueError("Qt runtime does not contain the reviewed PyRun Qt entry point")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"sha256": digest, "nativeLibraryCount": native_count}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aar", required=True, type=Path)
    parser.add_argument("--readelf", required=True)
    parser.add_argument("--runner", required=True, type=Path)
    parser.add_argument("--require-scientific", action="store_true")
    args = parser.parse_args()
    result = verify(args.aar.resolve(), args.readelf, args.runner.resolve(), args.require_scientific)
    print(
        f"OK {args.aar.name} sha256={result['sha256']} "
        f"native={result['nativeLibraryCount']} abis={','.join(EXPECTED_ABIS)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
