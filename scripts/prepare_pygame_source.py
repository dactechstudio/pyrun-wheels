#!/usr/bin/env python3
"""Fetch and checksum-verify the upstream Pygame and SDL2 source stack."""

from __future__ import annotations

import argparse
import hashlib
import posixpath
import shutil
import stat
import tarfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


SOURCES = {
    "pygame": (
        "https://files.pythonhosted.org/packages/49/cc/08bba60f00541f62aaa252ce0cfbd60aebd04616c0b9574f755b583e45ae/pygame-2.6.1.tar.gz",
        "56fb02ead529cee00d415c3e007f75e0780c655909aaa8e8bf616ee09c9feb1f",
        "pygame-2.6.1.tar.gz",
    ),
    "sdl": (
        "https://github.com/libsdl-org/SDL/archive/refs/tags/release-2.32.10.zip",
        "7a3c207b8509edc487d658df357ad764cd852d68fe248d307b25c0741d52fdf0",
        "SDL-release-2.32.10.zip",
    ),
    "sdl_image": (
        "https://github.com/libsdl-org/SDL_image/archive/refs/tags/release-2.8.12.zip",
        "d8de703e04b168b7136d08d01d33750ffb11cd450b388ec7d3f7e5a431b168ea",
        "SDL_image-release-2.8.12.zip",
    ),
    "sdl_mixer": (
        "https://github.com/libsdl-org/SDL_mixer/archive/refs/tags/release-2.8.2.zip",
        "f5414517da77ce84ee1cd1fe6f0d05448dd98558f0f93d3c2862ce1aab30014a",
        "SDL_mixer-release-2.8.2.zip",
    ),
    "sdl_ttf": (
        "https://github.com/libsdl-org/SDL_ttf/archive/refs/tags/release-2.24.0.zip",
        "056fefe27b8399be85d83ec9ac37e659093e00f39d596c73f6fa3fd0c969ec59",
        "SDL_ttf-release-2.24.0.zip",
    ),
    "freetype": (
        "https://download.savannah.gnu.org/releases/freetype/freetype-2.13.3.tar.gz",
        "5c3a8e78f7b24c20b25b54ee575d6daa40007a5f4eea2845861c3409b3021747",
        "freetype-2.13.3.tar.gz",
    ),
    "libpng": (
        "https://codeload.github.com/pnggroup/libpng/zip/refs/tags/v1.6.59",
        "6fd97a1e5e2ac55cec0a538a9800a351eb3844086736291844ebd12db72ba433",
        "libpng-1.6.59.zip",
    ),
    "libjpeg_turbo": (
        "https://codeload.github.com/libjpeg-turbo/libjpeg-turbo/zip/refs/tags/3.2.0",
        "1b4c2cb6cba1ca5e53b9e85eeeab02154cab4b70894c96e97a9c747a26d19d63",
        "libjpeg-turbo-3.2.0.zip",
    ),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_relative(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or "\\" in name or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"unsafe source archive member: {name!r}")
    return path


def _extract_tar(archive: Path, destination: Path, expected_root: str) -> None:
    with tarfile.open(archive, "r:gz") as source:
        for member in source.getmembers():
            relative = _safe_relative(member.name)
            if relative.parts[0] != expected_root or member.issym() or member.islnk() or member.isdev():
                raise ValueError(f"unexpected Pygame source archive member: {member.name!r}")
        source.extractall(destination, filter="data")


def _extract_zip(archive: Path, destination: Path, expected_root: str) -> None:
    with zipfile.ZipFile(archive) as source:
        links = []
        regular_members = []
        for member in source.infolist():
            relative = _safe_relative(member.filename.rstrip("/"))
            unix_mode = member.external_attr >> 16
            if (
                relative.parts[0] != expected_root
                or (member.is_dir() and member.file_size != 0)
            ):
                raise ValueError(f"unexpected SDL source archive member: {member.filename!r}")
            if stat.S_ISLNK(unix_mode):
                links.append((member, relative))
            else:
                regular_members.append(member)
        source.extractall(destination, members=regular_members)
        pending = links.copy()
        while pending:
            unresolved = []
            for member, relative in pending:
                target_text = source.read(member).decode("utf-8").strip()
                target_path = posixpath.normpath(
                    posixpath.join(relative.parent.as_posix(), target_text)
                )
                target_relative = _safe_relative(target_path)
                if target_relative.parts[0] != expected_root:
                    raise ValueError(f"SDL source link escapes its source root: {member.filename!r}")
                link = destination.joinpath(*relative.parts)
                target = destination.joinpath(*target_relative.parts)
                if not target.exists():
                    unresolved.append((member, relative))
                    continue
                if target.is_dir():
                    shutil.copytree(target, link)
                else:
                    shutil.copyfile(target, link)
            if len(unresolved) == len(pending):
                names = [member.filename for member, _relative in unresolved]
                raise ValueError(f"SDL source link targets are unresolved: {names}")
            pending = unresolved


def add_android_license_notices(pygame_source: Path) -> None:
    """Keep Pygame and all statically linked notices in the Android wheel."""
    package_root = pygame_source / "src_py"
    licenses = {
        "LICENSE.txt": pygame_source / "docs" / "LGPL.txt",
        "LICENSE.sdl_gfx.txt": pygame_source / "docs" / "licenses" / "LICENSE.sdl_gfx.txt",
    }
    for filename, source in licenses.items():
        if not source.is_file():
            raise ValueError(f"upstream Pygame license file is missing: {source}")
        shutil.copyfile(source, package_root / filename)

    setup_path = pygame_source / "setup.py"
    setup_text = setup_path.read_text(encoding="utf-8")
    stripped_files = '"src_py/pygame_icon_mac.bmp"])]'
    if setup_text.count(stripped_files) != 1:
        raise ValueError("could not safely add required Pygame license notices to the Android wheel")
    setup_text = setup_text.replace(
        stripped_files,
        '"src_py/pygame_icon_mac.bmp",\n'
        '                              "src_py/LICENSE.txt",\n'
        '                              "src_py/LICENSE.sdl_gfx.txt",\n'
        '                              "src_py/LICENSE.libpng.txt",\n'
        '                              "src_py/LICENSE.libjpeg-turbo.txt"])]',
    )
    avx_spawn = "        distutils.ccompiler.spawn(cmd, dry_run=self.dry_run, **kwargs)"
    if setup_text.count(avx_spawn) != 1:
        raise ValueError("could not safely adapt Pygame's AVX2 compiler hook to Python 3.13")
    setup_text = setup_text.replace(
        avx_spawn,
        "        self.__spawn(cmd, dry_run=self.dry_run, **kwargs)",
    )
    setup_marker = "for e in extensions:\n    # Only define the ARM_NEON defines if they have been enabled at build time."
    if setup_text.count(setup_marker) != 1:
        raise ValueError("could not safely add Android ELF alignment flags to Pygame extensions")
    setup_text = setup_text.replace(
        setup_marker,
        "if os.environ.get('PYGAME_ANDROID') == '1':\n"
        "    for extension in extensions:\n"
        "        extension.extra_link_args.append('-Wl,-z,max-page-size=16384')\n\n"
        + setup_marker,
    )
    setup_path.write_text(setup_text, encoding="utf-8", newline="\n")


def add_native_license_notices(
    pygame_source: Path, libpng_source: Path, libjpeg_source: Path
) -> None:
    notices = {
        "LICENSE.libpng.txt": libpng_source / "LICENSE",
        "LICENSE.libjpeg-turbo.txt": libjpeg_source / "LICENSE.md",
    }
    package_root = pygame_source / "src_py"
    for filename, source in notices.items():
        if not source.is_file():
            raise ValueError(f"required native dependency license file is missing: {source}")
        shutil.copyfile(source, package_root / filename)


def prepare(output_dir: Path, cache_dir: Path) -> Path:
    if output_dir.exists():
        raise FileExistsError(f"refusing to replace existing source directory: {output_dir}")
    output_dir.mkdir(parents=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    roots = {
        "pygame": "pygame-2.6.1",
        "sdl": "SDL-release-2.32.10",
        "sdl_image": "SDL_image-release-2.8.12",
        "sdl_mixer": "SDL_mixer-release-2.8.2",
        "sdl_ttf": "SDL_ttf-release-2.24.0",
        "freetype": "freetype-2.13.3",
        "libpng": "libpng-1.6.59",
        "libjpeg_turbo": "libjpeg-turbo-3.2.0",
    }
    try:
        for name, (url, expected_hash, archive_name) in SOURCES.items():
            archive = cache_dir / archive_name
            if not archive.is_file() or sha256_file(archive) != expected_hash:
                temporary = archive.with_suffix(archive.suffix + ".download")
                temporary.unlink(missing_ok=True)
                request = urllib.request.Request(url, headers={"User-Agent": "PyRun-wheel-builder/1"})
                with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as target:
                    shutil.copyfileobj(response, target)
                if sha256_file(temporary) != expected_hash:
                    temporary.unlink(missing_ok=True)
                    raise ValueError(f"SHA-256 verification failed for {archive_name}")
                temporary.replace(archive)

            if name in ("pygame", "freetype"):
                source_root = output_dir / roots[name]
                source_root.mkdir()
                _extract_tar(archive, source_root, roots[name])
            else:
                source_root = output_dir / roots[name]
                source_root.mkdir()
                _extract_zip(archive, source_root, roots[name])

        pygame_source = output_dir / roots["pygame"] / roots["pygame"]
        add_android_license_notices(pygame_source)
        libpng_source = output_dir / roots["libpng"] / roots["libpng"]
        libjpeg_source = output_dir / roots["libjpeg_turbo"] / roots["libjpeg_turbo"]
        add_native_license_notices(pygame_source, libpng_source, libjpeg_source)

        freetype_source = output_dir / roots["freetype"] / roots["freetype"]
        ttf_external = output_dir / roots["sdl_ttf"] / roots["sdl_ttf"] / "external" / "freetype"
        if ttf_external.exists():
            shutil.rmtree(ttf_external)
        shutil.copytree(freetype_source, ttf_external)

        required = {
            pygame_source / "buildconfig" / "Setup.Android.SDL2.in",
            output_dir / roots["sdl"] / roots["sdl"] / "CMakeLists.txt",
            output_dir / roots["sdl_image"] / roots["sdl_image"] / "CMakeLists.txt",
            output_dir / roots["sdl_mixer"] / roots["sdl_mixer"] / "CMakeLists.txt",
            output_dir / roots["sdl_ttf"] / roots["sdl_ttf"] / "CMakeLists.txt",
            ttf_external / "CMakeLists.txt",
            libpng_source / "CMakeLists.txt",
            libpng_source / "LICENSE",
            libjpeg_source / "CMakeLists.txt",
            libjpeg_source / "LICENSE.md",
        }
        missing = sorted(path.as_posix() for path in required if not path.is_file())
        if missing:
            raise ValueError(f"upstream sources are incomplete: {missing}")
        return pygame_source
    except Exception:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--cache-dir", required=True, type=Path)
    args = parser.parse_args()
    pygame_source = prepare(args.output_dir, args.cache_dir)
    print(f"Prepared checksum-verified Pygame source at {pygame_source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
