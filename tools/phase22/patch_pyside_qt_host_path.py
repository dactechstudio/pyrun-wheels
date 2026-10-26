#!/usr/bin/env python3
"""Patch PySide's cross-build Qt probes to pass the host Qt path to CMake.

Qt's Android package is a cross-compiled target package.  Qt's CMake config
requires ``QT_HOST_PATH`` while PySide's pinned cross-build helper only passes
the target path to its small metadata probes.  The CI build supplies the host
path through ``PYRUN_QT_HOST_PATH`` so the patch remains deterministic and
does not hard-code a runner directory.
"""

from __future__ import annotations

import argparse
from pathlib import Path


MARKER = 'os.environ.get("PYRUN_QT_HOST_PATH")'
SHIBOKEN_MARKER = 'set(QT_HOST_PATH "$ENV{PYRUN_QT_HOST_PATH}" CACHE PATH'


def patch(source: Path) -> bool:
    qtinfo = source / "build_scripts" / "qtinfo.py"
    text = qtinfo.read_text(encoding="utf-8")
    changed = False

    if MARKER not in text:
        first = """            if self._qt_target_path:\n                cmake_cache_args.append((\"QFP_QT_TARGET_PATH\", self._qt_target_path))\n"""
        replacement = first + """            qt_host_path = os.environ.get(\"PYRUN_QT_HOST_PATH\")\n            if qt_host_path:\n                cmake_cache_args.append((\"QT_HOST_PATH\", qt_host_path))\n"""
        occurrences = text.count(first)
        if occurrences != 2:
            raise RuntimeError(f"unexpected Qt cache argument blocks: expected 2, found {occurrences}")
        text = text.replace(first, replacement)
        qtinfo.write_text(text, encoding="utf-8")
        changed = True

    shiboken_setup = source / "sources" / "shiboken6" / "cmake" / "ShibokenSetup.cmake"
    shiboken_text = shiboken_setup.read_text(encoding="utf-8")
    if SHIBOKEN_MARKER not in shiboken_text:
        needle = """shiboken_internal_detect_if_cross_building()\n\n# Note: For cross building, we rely on FindPython shipped with CMake 3.17+ to\n"""
        insertion = """shiboken_internal_detect_if_cross_building()\n\n# Qt's Android package requires the host Qt path before find_package(Qt6).\nif(SHIBOKEN_IS_CROSS_BUILD AND DEFINED ENV{PYRUN_QT_HOST_PATH})\n    set(QT_HOST_PATH \"$ENV{PYRUN_QT_HOST_PATH}\" CACHE PATH \"Host Qt installation\" FORCE)\nendif()\n\n# Note: For cross building, we rely on FindPython shipped with CMake 3.17+ to\n"""
        if shiboken_text.count(needle) != 1:
            raise RuntimeError("unexpected ShibokenSetup cross-build block")
        shiboken_setup.write_text(shiboken_text.replace(needle, insertion, 1), encoding="utf-8")
        changed = True

    pyside_tools_setup = source / "sources" / "pyside-tools" / "cmake" / "PySideToolsSetup.cmake"
    pyside_tools_text = pyside_tools_setup.read_text(encoding="utf-8")
    if SHIBOKEN_MARKER not in pyside_tools_text:
        needle = """pyside_tools_internal_detect_if_cross_building()\npyside_tools_internal_set_up_extra_dependency_paths()\n\nfind_package(Qt6 REQUIRED COMPONENTS Core HostInfo)\n"""
        insertion = """pyside_tools_internal_detect_if_cross_building()\npyside_tools_internal_set_up_extra_dependency_paths()\n\n# Qt's Android package requires the host Qt path before find_package(Qt6).\nif(CMAKE_CROSSCOMPILING AND DEFINED ENV{PYRUN_QT_HOST_PATH})\n    set(QT_HOST_PATH \"$ENV{PYRUN_QT_HOST_PATH}\" CACHE PATH \"Host Qt installation\" FORCE)\nendif()\n\nfind_package(Qt6 REQUIRED COMPONENTS Core HostInfo)\n"""
        if pyside_tools_text.count(needle) != 1:
            raise RuntimeError("unexpected PySideToolsSetup cross-build block")
        pyside_tools_setup.write_text(pyside_tools_text.replace(needle, insertion, 1), encoding="utf-8")
        changed = True

    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    changed = patch(args.source)
    print("patched PySide Qt host path probes" if changed else "PySide Qt host path probes already patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
