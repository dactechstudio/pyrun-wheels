#!/usr/bin/env python3
"""Cross-build the pinned SDL2 runtime libraries for a Pygame wheel ABI."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


ABI_TO_ANDROID = {"arm64_v8a": "arm64-v8a", "x86_64": "x86_64"}
SOURCE_DIRS = {
    "SDL": "SDL-release-2.32.10/SDL-release-2.32.10",
    "SDL_IMAGE": "SDL_image-release-2.8.12/SDL_image-release-2.8.12",
    "SDL_MIXER": "SDL_mixer-release-2.8.2/SDL_mixer-release-2.8.2",
    "SDL_TTF": "SDL_ttf-release-2.24.0/SDL_ttf-release-2.24.0",
}


def build(source_root: Path, build_dir: Path, ndk_root: Path, abi: str) -> Path:
    if abi not in ABI_TO_ANDROID:
        raise ValueError(f"unsupported PyRun ABI: {abi}")
    cmake_lists = Path(__file__).resolve().parents[1] / "recipes" / "pygame" / "CMakeLists.txt"
    toolchain = ndk_root / "build" / "cmake" / "android.toolchain.cmake"
    if not toolchain.is_file() or not cmake_lists.is_file():
        raise FileNotFoundError("Android NDK CMake toolchain or PyRun SDL CMake project is missing")
    if build_dir.exists():
        raise FileExistsError(f"refusing to reuse an existing native build directory: {build_dir}")

    command = [
        "cmake", "-S", str(cmake_lists.parent), "-B", str(build_dir),
        f"-DCMAKE_TOOLCHAIN_FILE={toolchain}",
        f"-DANDROID_ABI={ABI_TO_ANDROID[abi]}",
        "-DANDROID_PLATFORM=android-25",
        "-DANDROID_STL=c++_shared",
        "-DCMAKE_BUILD_TYPE=Release",
    ]
    if shutil.which("ninja"):
        command.extend(("-G", "Ninja"))
    for name, relative in SOURCE_DIRS.items():
        source = source_root / relative
        if not (source / "CMakeLists.txt").is_file():
            raise FileNotFoundError(f"pinned {name} source is missing: {source}")
        command.append(f"-DPYRUN_{name}_SOURCE_DIR={source}")
    subprocess.run(command, check=True)
    subprocess.run(["cmake", "--build", str(build_dir), "--config", "Release", "--parallel", "2"], check=True)
    expected = ["libSDL2.so", "libSDL2_image.so", "libSDL2_mixer.so", "libSDL2_ttf.so"]
    for name in expected:
        if not list(build_dir.rglob(name)):
            raise FileNotFoundError(f"native SDL build did not produce {name}")
    return build_dir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--build-dir", required=True, type=Path)
    parser.add_argument("--ndk-root", required=True, type=Path)
    parser.add_argument("--abi", required=True, choices=sorted(ABI_TO_ANDROID))
    args = parser.parse_args()
    result = build(args.source_root, args.build_dir, args.ndk_root, args.abi)
    print(f"Built pinned SDL2/Pygame native libraries in {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
