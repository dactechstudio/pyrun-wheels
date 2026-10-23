#!/usr/bin/env python3
"""Fetch pinned upstream OpenCV-Python sources and apply PyRun Android build adaptations."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


OPENCV_PYTHON_URL = "https://github.com/opencv/opencv-python.git"
OPENCV_PYTHON_REVISION = "255564a37c4a23275485e827abbe19031347d458"
OPENCV_REVISION = "31b0eeea0b44b370fd0712312df4214d4ae1b158"


def run(*command: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return result.stdout.strip()


def replace_once(path: Path, old: str, new: str, *, count: int = 1) -> None:
    source = path.read_text(encoding="utf-8")
    actual = source.count(old)
    if actual != count:
        raise ValueError(f"{path}: expected {count} patch context(s), found {actual}")
    path.write_text(source.replace(old, new), encoding="utf-8", newline="\n")


def prepare(destination: Path) -> None:
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"refusing to replace existing source directory: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    run("git", "clone", "--no-checkout", OPENCV_PYTHON_URL, str(destination))
    run("git", "checkout", "--detach", OPENCV_PYTHON_REVISION, cwd=destination)
    run("git", "submodule", "update", "--init", "--depth", "1", "opencv", cwd=destination)

    actual_wrapper_revision = run("git", "rev-parse", "HEAD", cwd=destination)
    actual_opencv_revision = run("git", "-C", "opencv", "rev-parse", "HEAD", cwd=destination)
    if actual_wrapper_revision != OPENCV_PYTHON_REVISION:
        raise ValueError(f"unexpected opencv-python source revision: {actual_wrapper_revision}")
    if actual_opencv_revision != OPENCV_REVISION:
        raise ValueError(f"unexpected OpenCV submodule revision: {actual_opencv_revision}")

    replace_once(
        destination / "pyproject.toml",
        '  "numpy>=2.0.0;  python_version>=\'3.9\'",',
        '  "numpy==1.26.2; python_version>=\'3.9\'",',
    )
    replace_once(
        destination / "pyproject.toml",
        '  "scikit-build>=0.14.0",',
        '  "cmake==3.31.6",\n  "scikit-build==0.18.1",',
    )

    replace_once(
        destination / "opencv" / "modules" / "python" / "CMakeLists.txt",
        "if(ANDROID OR APPLE_FRAMEWORK OR WINRT)",
        "if(APPLE_FRAMEWORK OR WINRT) # PyRun: enable Python bindings for Android",
    )

    detect_python = destination / "opencv" / "cmake" / "OpenCVDetectPython.cmake"
    replace_once(
        detect_python,
        "if(NOT ANDROID AND NOT APPLE_FRAMEWORK)",
        "if(NOT APPLE_FRAMEWORK) # PyRun: discover target Python for Android",
    )
    replace_once(
        detect_python,
        "if(NOT ANDROID AND NOT IOS AND NOT XROS)",
        "if(NOT IOS AND NOT XROS) # PyRun: discover target packages and NumPy",
        count=2,
    )
    common_cmake = destination / "opencv" / "modules" / "python" / "common.cmake"
    replace_once(
        common_cmake,
        'string(REPLACE "-Wl,--no-undefined" "" CMAKE_MODULE_LINKER_FLAGS "${CMAKE_MODULE_LINKER_FLAGS}")',
        'string(REPLACE "-Wl,--no-undefined-version" "" CMAKE_MODULE_LINKER_FLAGS "${CMAKE_MODULE_LINKER_FLAGS}") # PyRun: Android linker compatibility\n'
        'string(REPLACE "-Wl,--no-undefined" "" CMAKE_MODULE_LINKER_FLAGS "${CMAKE_MODULE_LINKER_FLAGS}")',
    )

    setup = destination / "setup.py"
    replace_once(
        setup,
        '            ("etc" if os.name == "nt" else "share/opencv4") + r"/haarcascades/.*\\.xml"',
        '            ("etc" if os.name == "nt" else ("sdk/etc" if "CIBUILDWHEEL_BUILD_IDENTIFIER" in os.environ else "share/opencv4")) + r"/haarcascades/.*\\.xml"',
    )
    replace_once(
        setup,
        "import platform\nfrom skbuild import cmaker, setup",
        "import platform\nimport numpy\nfrom skbuild import cmaker, setup",
    )
    replace_once(
        setup,
        "    if build_headless:\n",
        "    if \"CIBUILDWHEEL_BUILD_IDENTIFIER\" in os.environ:\n"
        "        cmake_args.extend([\n"
        '            "-DCMAKE_TOOLCHAIN_FILE=" + os.environ["CMAKE_TOOLCHAIN_FILE"],\n'
        '            "-DBUILD_ANDROID_PROJECTS=OFF",\n'
        '            "-DBUILD_ANDROID_EXAMPLES=OFF",\n'
        '            "-DBUILD_opencv_python2=OFF",\n'
        '            "-DBUILD_opencv_python3=ON",\n'
        '            "-DPYTHON3_NUMPY_INCLUDE_DIRS=" + numpy.get_include(),\n'
        '            "-DOPENCV_FORCE_PYTHON_LIBS=ON",\n'
        '            "-DWITH_OPENCL=OFF",\n'
        '            "-DWITH_TBB=OFF",\n'
        '            "-DWITH_IPP=OFF",\n'
        '            "-DWITH_ITT=OFF",\n'
        '            "-DWITH_V4L=OFF",\n'
        '            "-DWITH_GSTREAMER=OFF",\n'
        '            "-DWITH_FFMPEG=OFF",\n'
        '            "-DWITH_GTK=OFF",\n'
        '            "-DWITH_QT=OFF",\n'
        '            "-DWITH_OPENEXR=OFF",\n'
        '            "-DWITH_WEBP=OFF",\n'
        '            "-DWITH_TIFF=OFF",\n'
        '            "-DWITH_JASPER=OFF",\n'
        "        ])\n\n"
        "    if build_headless:\n",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", required=True, type=Path)
    args = parser.parse_args()
    prepare(args.source_dir)
    print(f"Prepared pinned OpenCV-Python sources at {args.source_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
