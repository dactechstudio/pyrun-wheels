#!/usr/bin/env python3
"""Validate Pygame module coverage and SDL linkage in an Android wheel."""

from __future__ import annotations

import argparse
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

try:
    from .verify_android_wheels import verify_wheel
except ImportError:  # Support direct execution as ``python scripts/verify_pygame_wheel.py``.
    from verify_android_wheels import verify_wheel


REQUIRED_MODULES = {
    "pygame/base",
    "pygame/color",
    "pygame/constants",
    "pygame/display",
    "pygame/event",
    "pygame/key",
    "pygame/mouse",
    "pygame/rect",
    "pygame/rwobject",
    "pygame/surface",
    "pygame/surflock",
    "pygame/time",
    "pygame/joystick",
    "pygame/draw",
    "pygame/image",
    "pygame/transform",
    "pygame/mask",
    "pygame/bufferproxy",
    "pygame/pixelarray",
    "pygame/math",
    "pygame/pixelcopy",
    "pygame/newbuffer",
    "pygame/scrap",
    "pygame/gfxdraw",
    "pygame/_sprite",
    "pygame/font",
    "pygame/imageext",
    "pygame/mixer",
    "pygame/mixer_music",
    "pygame/_sdl2/sdl2",
    "pygame/_sdl2/video",
    "pygame/_sdl2/audio",
    "pygame/_sdl2/touch",
    "pygame/_sdl2/controller",
}
REQUIRED_LINKS = {
    "pygame/font": {"libSDL2.so", "libSDL2_ttf.so"},
    "pygame/imageext": {"libSDL2.so", "libSDL2_image.so"},
    "pygame/mixer": {"libSDL2.so", "libSDL2_mixer.so"},
    "pygame/mixer_music": {"libSDL2.so", "libSDL2_mixer.so"},
}


def extension_modules(names: list[str]) -> dict[str, str]:
    result = {}
    for name in names:
        if not name.endswith(".so"):
            continue
        relative = name.removesuffix(".so")
        for suffix in (".cpython-313", ".abi3", ".cpython-312", ".cpython-311"):
            if suffix in relative:
                relative = relative.split(suffix, 1)[0]
                break
        if relative.startswith("pygame/"):
            result[relative] = name
    return result


def verify_pygame(wheel: Path, abi: str, android_api: int, readelf: str) -> None:
    verify_wheel(wheel, abi, android_api, readelf)
    with zipfile.ZipFile(wheel) as archive:
        members = archive.namelist()
        for notice in (
            "LICENSE.txt",
            "LICENSE.sdl_gfx.txt",
            "LICENSE.libpng.txt",
            "LICENSE.libjpeg-turbo.txt",
        ):
            if not any(name == f"pygame/{notice}" or name.endswith(f"/data/pygame/{notice}") for name in members):
                raise ValueError(f"{wheel.name}: missing required Pygame license notice {notice}")
        modules = extension_modules(archive.namelist())
        missing = REQUIRED_MODULES - modules.keys()
        if missing:
            raise ValueError(f"{wheel.name}: missing required Pygame modules: {sorted(missing)}")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            needed_by_module = {}
            for module, member in modules.items():
                library = root / (module.replace("/", "_") + ".so")
                library.write_bytes(archive.read(member))
                dynamic = subprocess.run(
                    [readelf, "-d", str(library)], check=True, capture_output=True, text=True
                ).stdout
                needed_by_module[module] = set(
                    re.findall(r"Shared library: \[(.+?)\]", dynamic)
                )
            for module, expected in REQUIRED_LINKS.items():
                missing_libraries = expected - needed_by_module.get(module, set())
                if missing_libraries:
                    raise ValueError(
                        f"{wheel.name}: {module} is not linked to its matching SDL libraries: "
                        f"{sorted(missing_libraries)}"
                    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheels", nargs="+", type=Path)
    parser.add_argument("--expected-abi", choices=("arm64_v8a", "x86_64"), required=True)
    parser.add_argument("--android-api", type=int, default=25)
    parser.add_argument("--readelf", required=True)
    args = parser.parse_args()
    for wheel in args.wheels:
        verify_pygame(wheel, args.expected_abi, args.android_api, args.readelf)
        print(f"{wheel.name}: Pygame modules and SDL linkage verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
