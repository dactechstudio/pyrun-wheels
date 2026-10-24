#!/usr/bin/env python3
"""Point Kivy's Android extensions at the reviewed PyRun SDL2 build."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


ABI_NAMES = {"arm64_v8a", "x86_64"}
SOURCE_DIRS = {
    "sdl": ("SDL-release-2.32.10", "SDL-release-2.32.10", "include", "SDL.h"),
    "image": ("SDL_image-release-2.8.12", "SDL_image-release-2.8.12", "include", "SDL_image.h"),
    "mixer": ("SDL_mixer-release-2.8.2", "SDL_mixer-release-2.8.2", "include", "SDL_mixer.h"),
    # SDL_ttf 2.24.0 (the SDL2 branch) keeps SDL_ttf.h at its source root.
    "ttf": ("SDL_ttf-release-2.24.0", "SDL_ttf-release-2.24.0", ".", "SDL_ttf.h"),
}
LIBRARIES = ("libSDL2.so", "libSDL2_image.so", "libSDL2_mixer.so", "libSDL2_ttf.so")


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 1:
        path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
    elif count == 0 and new in text:
        return
    else:
        raise ValueError(f"{path}: expected one exact PyRun Android link context, found {count}")


def configure(kivy_source: Path, source_root: Path, native_build: Path, abi: str) -> str:
    if abi not in ABI_NAMES:
        raise ValueError(f"unsupported PyRun ABI for Kivy: {abi}")
    setup = kivy_source / "setup.py"
    if not setup.is_file():
        raise FileNotFoundError(f"prepared Kivy setup.py is missing: {setup}")

    search_paths = []
    for name, (parent, root, include, header) in SOURCE_DIRS.items():
        include_path = source_root / parent / root / include
        if not (include_path / header).is_file():
            raise FileNotFoundError(f"pinned SDL {name} header is missing: {include_path / header}")
        search_paths.append(include_path.resolve())

    library_paths = []
    for filename in LIBRARIES:
        matches = list(native_build.rglob(filename))
        if len(matches) != 1:
            raise FileNotFoundError(f"expected exactly one built {filename}, found {len(matches)}")
        library_paths.append(matches[0].parent.resolve())

    sdl = source_root / SOURCE_DIRS["sdl"][0] / SOURCE_DIRS["sdl"][1]
    freetype = source_root / "SDL_ttf-release-2.24.0" / "SDL_ttf-release-2.24.0" / "external" / "freetype" / "include"
    if freetype.is_dir():
        search_paths.append(freetype.resolve())
    for path in (sdl / "include",):
        if path.resolve() not in search_paths:
            search_paths.append(path.resolve())

    marker = "ext_modules = get_extensions_from_sources(sources)\n"
    replacement = marker + """if environ.get('PYRUN_ANDROID') == '1':
    for extension in ext_modules:
        extension.extra_link_args = list(extension.extra_link_args or [])
        extension.extra_link_args.append('-Wl,-z,max-page-size=16384')
"""
    _replace_once(setup, marker, replacement)

    paths = [str(path) for path in search_paths + library_paths]
    if any("\n" in path or "\r" in path for path in paths):
        raise ValueError("SDL build paths cannot contain newlines")
    return os.pathsep.join(paths)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kivy-source", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--native-build", required=True, type=Path)
    parser.add_argument("--abi", required=True, choices=sorted(ABI_NAMES))
    parser.add_argument("--github-env", type=Path)
    args = parser.parse_args()

    paths = configure(args.kivy_source, args.source_root, args.native_build, args.abi)
    if args.github_env:
        with args.github_env.open("a", encoding="utf-8", newline="\n") as output:
            output.write(f"KIVY_SDL2_PATH={paths}\n")
    print(f"Configured Kivy Android SDL paths for {args.abi}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
