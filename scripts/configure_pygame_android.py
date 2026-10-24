#!/usr/bin/env python3
"""Generate Pygame's upstream Android Setup from the verified SDL build."""

from __future__ import annotations

import argparse
from pathlib import Path


def configure(pygame_source: Path, source_root: Path, native_build: Path, abi: str) -> Path:
    if abi not in {"arm64_v8a", "x86_64"}:
        raise ValueError(f"unsupported Android ABI for Pygame: {abi}")
    source_paths = {
        "sdl": source_root / "SDL-release-2.32.10" / "SDL-release-2.32.10",
        "image": source_root / "SDL_image-release-2.8.12" / "SDL_image-release-2.8.12",
        "mixer": source_root / "SDL_mixer-release-2.8.2" / "SDL_mixer-release-2.8.2",
        "ttf": source_root / "SDL_ttf-release-2.24.0" / "SDL_ttf-release-2.24.0",
        "png": source_root / "libpng-1.6.59" / "libpng-1.6.59",
        "jpeg": source_root / "libjpeg-turbo-3.2.0" / "libjpeg-turbo-3.2.0",
    }
    for name, path in source_paths.items():
        if not (path / "CMakeLists.txt").is_file():
            raise FileNotFoundError(f"pinned SDL {name} headers are missing: {path}")
    template_path = pygame_source / "buildconfig" / "Setup.Android.SDL2.in"
    if not template_path.is_file():
        raise FileNotFoundError(f"Pygame Android Setup template is missing: {template_path}")

    def cmake_output(name: str, filename: str) -> Path:
        matches = list(native_build.rglob(filename))
        if len(matches) != 1:
            raise FileNotFoundError(f"expected exactly one built {filename}, found {len(matches)}")
        return matches[0].parent

    values = {
        "sdl_includes": f"-I{source_paths['sdl'] / 'include'} -I{source_paths['sdl'] / 'include' / 'SDL2'} -L{cmake_output('sdl', 'libSDL2.so')}",
        "sdl_image_includes": f"-I{source_paths['image'] / 'include'} -L{cmake_output('image', 'libSDL2_image.so')}",
        "sdl_mixer_includes": f"-I{source_paths['mixer'] / 'include'} -L{cmake_output('mixer', 'libSDL2_mixer.so')}",
        "sdl_ttf_includes": f"-I{source_paths['ttf']} -I{source_paths['ttf'] / 'external' / 'freetype' / 'include'} -L{cmake_output('ttf', 'libSDL2_ttf.so')}",
        "jpeg_includes": f"-I{source_paths['jpeg'] / 'src'} -I{cmake_output('jpeg', 'libjpeg.a')} -L{cmake_output('jpeg', 'libjpeg.a')}",
        "png_includes": f"-I{source_paths['png']} -I{cmake_output('png', 'libpng16.a')} -L{cmake_output('png', 'libpng16.a')}",
        "freetype_includes": "",
    }
    text = template_path.read_text(encoding="utf-8")
    text = text.replace("JPEG = {jpeg_includes} -ljpeg", "JPEG = {jpeg_includes} -ljpeg -lm")
    text = text.replace("PNG = {png_includes} -lpng16", "PNG = {png_includes} -lpng16 -lz -lm")
    setup = text.format(**values)
    surface_line = "surface src_c/surface.c src_c/alphablit.c src_c/surface_fill.c $(SDL) $(DEBUG)"
    if setup.count(surface_line) != 1:
        raise ValueError("could not safely add Pygame's SIMD surface sources to Android Setup")
    simd_sources = "src_c/simd_blitters_sse2.c"
    if abi == "x86_64":
        simd_sources += " src_c/simd_blitters_avx2.c"
    setup = setup.replace(
        surface_line,
        f"surface src_c/surface.c src_c/alphablit.c src_c/surface_fill.c {simd_sources} $(SDL) $(DEBUG)",
    )
    expected_modules = {"base", "display", "event", "font", "imageext", "mixer", "mixer_music", "_sdl2.video"}
    found_modules = {line.split()[0] for line in setup.splitlines() if line.strip() and not line.lstrip().startswith("#") and "src_c" in line}
    missing = expected_modules - found_modules
    if missing:
        raise ValueError(f"generated Pygame Setup is missing required Android modules: {sorted(missing)}")
    output = pygame_source / "Setup"
    output.write_text(setup, encoding="utf-8", newline="\n")
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pygame-source", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--native-build", required=True, type=Path)
    parser.add_argument("--abi", required=True, choices=("arm64_v8a", "x86_64"))
    args = parser.parse_args()
    path = configure(args.pygame_source, args.source_root, args.native_build, args.abi)
    print(f"Generated reviewed Android Setup for Pygame: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
