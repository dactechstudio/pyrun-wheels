#!/usr/bin/env python3
"""Verify Kivy Android extensions, SDL linkage, package data, and ELF alignment."""

from __future__ import annotations

import argparse
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
from packaging.utils import parse_wheel_filename

try:
    from .verify_android_wheels import verify_wheel
except ImportError:  # Support direct execution as ``python scripts/verify_kivy_wheel.py``.
    from verify_android_wheels import verify_wheel


REQUIRED_MODULES = {
    "kivy/core/window/_window_sdl2",
    "kivy/core/image/_img_sdl2",
    "kivy/core/text/_text_sdl2",
    "kivy/core/audio/audio_sdl2",
    "kivy/graphics/cgl_backend/cgl_sdl2",
}
REQUIRED_LINKS = {
    "kivy/core/window/_window_sdl2": {"libSDL2.so"},
    "kivy/core/image/_img_sdl2": {"libSDL2.so", "libSDL2_image.so"},
    "kivy/core/text/_text_sdl2": {"libSDL2.so", "libSDL2_ttf.so"},
    "kivy/core/audio/audio_sdl2": {"libSDL2.so", "libSDL2_mixer.so"},
    "kivy/graphics/cgl_backend/cgl_sdl2": {"libSDL2.so"},
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
        if relative.startswith("kivy/"):
            result[relative] = name
    return result


def verify_kivy(wheel: Path, abi: str, android_api: int, readelf: str) -> None:
    verify_wheel(wheel, abi, android_api, readelf)
    with zipfile.ZipFile(wheel) as archive:
        members = archive.namelist()
        modules = extension_modules(members)
        missing = REQUIRED_MODULES - modules.keys()
        if missing:
            raise ValueError(f"{wheel.name}: missing required Kivy SDL2 modules: {sorted(missing)}")
        if "kivy/LICENSE.txt" not in members:
            raise ValueError(f"{wheel.name}: missing Kivy's MIT license notice")
        if "kivy/data/fonts/Roboto-Regular.ttf" not in members:
            raise ValueError(f"{wheel.name}: missing the bundled Roboto regular font")
        bundled_sdl = [
            name for name in members
            if Path(name).name.lower() in {
                "libsdl2.so", "libsdl2_image.so", "libsdl2_mixer.so", "libsdl2_ttf.so"
            }
        ]
        if bundled_sdl:
            raise ValueError(f"{wheel.name}: SDL libraries must be shared with the APK: {bundled_sdl}")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            needed_by_module = {}
            for module, member in modules.items():
                library = root / (module.replace("/", "_") + ".so")
                library.write_bytes(archive.read(member))
                dynamic = subprocess.run(
                    [readelf, "-d", str(library)], check=True, capture_output=True, text=True
                ).stdout
                needed_by_module[module] = set(re.findall(r"Shared library: \[(.+?)\]", dynamic))
            for module, expected in REQUIRED_LINKS.items():
                missing_libraries = expected - needed_by_module.get(module, set())
                if missing_libraries:
                    raise ValueError(
                        f"{wheel.name}: {module} does not link to its PyRun SDL libraries: "
                        f"{sorted(missing_libraries)}"
                    )


def normalize_wheel_filename(wheel: Path, abi: str, android_api: int) -> Path:
    distribution, version, _build, tags = parse_wheel_filename(wheel.name)
    if str(distribution).lower().replace("_", "-") != "kivy" or str(version) != "2.3.1":
        raise ValueError(f"{wheel.name}: unexpected Kivy wheel identity")
    platform = f"android_{android_api}_{abi}"
    if not any(tag.interpreter == "cp313" and tag.abi == "cp313" and tag.platform == platform for tag in tags):
        raise ValueError(f"{wheel.name}: expected cp313-cp313-{platform}")
    canonical = wheel.with_name(f"kivy-2.3.1-cp313-cp313-{platform}.whl")
    if canonical != wheel:
        if canonical.exists():
            raise FileExistsError(f"refusing to replace an existing normalized Kivy wheel: {canonical}")
        wheel.rename(canonical)
    return canonical


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheels", nargs="+", type=Path)
    parser.add_argument("--expected-abi", choices=("arm64_v8a", "x86_64"), required=True)
    parser.add_argument("--android-api", type=int, default=25)
    parser.add_argument("--readelf", required=True)
    args = parser.parse_args()
    for wheel in args.wheels:
        normalized = normalize_wheel_filename(wheel, args.expected_abi, args.android_api)
        verify_kivy(normalized, args.expected_abi, args.android_api, args.readelf)
        print(f"{normalized.name}: Kivy modules, SDL linkage and Android ELF layout verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
