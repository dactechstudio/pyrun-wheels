#!/usr/bin/env python3
"""Convert a kept python-for-android Qt distribution into a reusable AAR."""

from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile


ANDROID = "http://schemas.android.com/apk/res/android"
ET.register_namespace("android", ANDROID)
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"


def _android(name: str) -> str:
    return f"{{{ANDROID}}}{name}"


def _strip_manifest_sdk_and_permissions(payload: bytes) -> bytes:
    """Remove application-wide manifest restrictions from a generated AAR."""

    root = ET.fromstring(payload)
    restricted_tags = {"uses-sdk", "uses-permission", _android("uses-sdk"), _android("uses-permission")}
    for child in list(root):
        if child.tag in restricted_tags:
            root.remove(child)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def find_distribution(search_root: Path) -> Path:
    candidates = []
    for build_file in search_root.rglob("build.gradle"):
        try:
            content = build_file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        root = build_file.parent
        if (
            "com.android.application" in content
            and (root / "src" / "main" / "AndroidManifest.xml").is_file()
            and (root / "gradlew").is_file()
        ):
            candidates.append(root)
    if len(candidates) != 1:
        raise ValueError(f"expected one kept Qt Gradle distribution, found {len(candidates)}")
    return candidates[0]


def convert_manifest(path: Path) -> None:
    tree = ET.parse(path)
    root = tree.getroot()
    for attribute in ("versionCode", "versionName", "installLocation"):
        root.attrib.pop(_android(attribute), None)
    # The optional Qt runtime itself requires API 28, but the base IDE keeps its
    # existing minimum SDK. Runtime/UI code gates launching Qt on API 28; an AAR
    # must therefore not raise the minimum SDK of every PyRun installation.
    for uses_sdk in list(root.findall("uses-sdk")):
        root.remove(uses_sdk)
    # The runtime module must not silently broaden the base application's data
    # access. Feature-specific Android bridges request reviewed permissions in
    # the base manifest/UI when they are implemented.
    for permission in list(root.findall("uses-permission")):
        root.remove(permission)
    application = root.find("application")
    if application is None:
        raise ValueError("generated Qt manifest has no application element")
    for attribute in (
        "name",
        "label",
        "icon",
        "allowBackup",
        "fullBackupOnly",
        "fullBackupContent",
        "theme",
        "debuggable",
        "hardwareAccelerated",
    ):
        application.attrib.pop(_android(attribute), None)
    activities = application.findall("activity")
    python_activities = [
        activity for activity in activities
        if activity.attrib.get(_android("name")) == "org.kivy.android.PythonActivity"
    ]
    if len(python_activities) != 1:
        raise ValueError("generated Qt manifest must contain exactly one PythonActivity")
    for activity in activities:
        for intent_filter in list(activity.findall("intent-filter")):
            actions = {
                item.attrib.get(_android("name"))
                for item in intent_filter.findall("action")
            }
            categories = {
                item.attrib.get(_android("name"))
                for item in intent_filter.findall("category")
            }
            if "android.intent.action.MAIN" in actions or "android.intent.category.LAUNCHER" in categories:
                activity.remove(intent_filter)
    python_activity = python_activities[0]
    python_activity.set(_android("exported"), "false")
    python_activity.set(_android("process"), ":python_qt")
    app_lib_metadata = [
        item for item in python_activity.findall("meta-data")
        if item.attrib.get(_android("name")) == "android.app.lib_name"
    ]
    if len(app_lib_metadata) != 1:
        raise ValueError("generated Qt manifest must declare android.app.lib_name once")
    app_lib_metadata[0].set(_android("value"), "qtmain")
    tree.write(path, encoding="utf-8", xml_declaration=True)


def convert_build_file(path: Path) -> None:
    content = path.read_text(encoding="utf-8")
    converted, count = re.subn(
        r"apply\s+plugin:\s*['\"]com\.android\.application['\"]",
        "apply plugin: 'com.android.library'",
        content,
        count=1,
    )
    if count != 1:
        raise ValueError("could not convert generated Qt project to an Android library")
    converted = re.sub(r"^\s*applicationId\s+.+$", "", converted, flags=re.MULTILINE)
    path.write_text(converted, encoding="utf-8")


def _copy_entry(output: zipfile.ZipFile, name: str, payload: bytes, executable: bool = False) -> None:
    info = zipfile.ZipInfo(name, ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = ((0o755 if executable else 0o644) & 0xFFFF) << 16
    output.writestr(info, payload)


def normalized_resource_name(name: str, payload: bytes) -> str:
    """Correct the JPEG icon emitted with a .png suffix by P4A's Qt template."""

    if name == "res/mipmap/icon.png" and payload.startswith(JPEG_SIGNATURE):
        return "res/mipmap/icon.jpg"
    if name.startswith("res/") and name.endswith(".png") and not payload.startswith(PNG_SIGNATURE):
        raise ValueError(f"Qt AAR has an invalid PNG resource: {name}")
    return name


def embed_local_jars(
    source_aar: Path,
    distribution: Path,
    destination: Path,
    license_root: Path,
) -> None:
    jars = sorted((distribution / "src" / "main" / "libs").glob("*.jar"))
    names = {jar.name for jar in jars}
    if "Qt6AndroidBindings.jar" not in names:
        raise ValueError("generated Qt project is missing Qt6AndroidBindings.jar")
    with zipfile.ZipFile(source_aar) as source, zipfile.ZipFile(destination, "w") as output:
        existing = set()
        for member in sorted(source.infolist(), key=lambda item: item.filename):
            if member.is_dir():
                continue
            payload = source.read(member)
            name = re.sub(r"^(jni/[^/]+)/libmain(?:_[^/]+)?\.so$", r"\1/libqtmain.so", member.filename)
            name = normalized_resource_name(name, payload)
            if name in existing:
                raise ValueError(f"AAR contains duplicate member after Qt bootstrap rename: {name}")
            existing.add(name)
            if name == "AndroidManifest.xml":
                # Gradle may regenerate <uses-sdk> from the generated app
                # project even after convert_manifest() stripped the source
                # manifest.  Strip it at the AAR boundary as well so this
                # optional runtime cannot raise the host app's minSdk or add
                # permissions during manifest merge.
                payload = _strip_manifest_sdk_and_permissions(payload)
            _copy_entry(output, name, payload, bool(member.external_attr & (0o111 << 16)))
        for jar in jars:
            name = f"libs/{jar.name}"
            if name in existing:
                # Gradle's generated AAR is authoritative for deployment JARs
                # it already packages (for example Qt6Android.jar).  Local
                # deployment output can contain a different revision of the
                # same file, so always keep the generated member and omit the
                # duplicate rather than creating an invalid AAR.
                continue
            _copy_entry(output, name, jar.read_bytes())
        license_files = sorted(path for path in license_root.rglob("*") if path.is_file())
        if not license_files or len(license_files) > 500:
            raise ValueError("Qt for Python license inventory is missing or unexpectedly large")
        for license_file in license_files:
            relative = license_file.relative_to(license_root).as_posix()
            if any(part in ("", ".", "..") for part in PurePosixPath(relative).parts):
                raise ValueError(f"unsafe Qt license path: {relative!r}")
            if license_file.is_symlink() or license_file.stat().st_size > 2 * 1024 * 1024:
                raise ValueError(f"unsafe Qt license file: {relative!r}")
            name = f"META-INF/pyrun-qt-licenses/{relative}"
            if name in existing:
                raise ValueError(f"AAR already contains {name}")
            _copy_entry(output, name, license_file.read_bytes())


def assemble_distribution(distribution: Path, gradlew: Path) -> None:
    command = [
        str(gradlew),
        "--project-dir",
        str(distribution),
        "assembleRelease",
        "--no-daemon",
        "--console=plain",
    ]
    # The Android project copied into the P4A distribution may carry the
    # developer workstation's gradle.properties.  Pin Gradle to the JDK
    # selected by the CI runner so a stale org.gradle.java.home cannot point
    # at an unrelated Windows/Android Studio installation.
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        command.insert(1, f"-Dorg.gradle.java.home={java_home}")
    subprocess.run(command, cwd=distribution, check=True)


def ensure_local_settings(distribution: Path) -> None:
    """Prevent Gradle from discovering the host repository's settings file.

    python-for-android keeps the generated Gradle project intentionally small
    and does not emit a settings file.  When that project is nested under the
    PyRun checkout, Gradle walks up to the checkout's settings.gradle.kts and
    treats the generated distribution as an unrelated project.  A minimal
    local settings file makes the distribution self-contained for AAR builds.
    """
    if any((distribution / name).is_file() for name in ("settings.gradle", "settings.gradle.kts")):
        return
    (distribution / "settings.gradle").write_text(
        "rootProject.name = 'PyRunQtRuntime'\n",
        encoding="utf-8",
    )


def prepare(search_root: Path, output: Path, license_root: Path) -> Path:
    distribution = find_distribution(search_root)
    convert_build_file(distribution / "build.gradle")
    convert_manifest(distribution / "src" / "main" / "AndroidManifest.xml")
    gradlew = distribution / "gradlew"
    if os.name != "nt":
        gradlew.chmod(gradlew.stat().st_mode | 0o111)
    ensure_local_settings(distribution)
    assemble_distribution(distribution, gradlew)
    aars = list((distribution / "build" / "outputs" / "aar").glob("*.aar"))
    if len(aars) != 1:
        raise ValueError(f"expected one generated Qt AAR, found {len(aars)}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f"refusing to replace Qt runtime AAR: {output}")
    with tempfile.TemporaryDirectory(prefix="pyrun-qt-aar-") as temporary:
        staged = Path(temporary) / output.name
        embed_local_jars(aars[0], distribution, staged, license_root)
        shutil.move(staged, output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--search-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--license-root", required=True, type=Path)
    args = parser.parse_args()
    license_root = args.license_root.resolve()
    if not license_root.is_dir():
        raise ValueError(f"Qt for Python license directory is missing: {license_root}")
    result = prepare(args.search_root.resolve(), args.output.resolve(), license_root)
    print(f"Prepared isolated Qt runtime AAR: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
