#!/usr/bin/env python3
"""Fetch a pinned Kivy source tree and apply the reviewed PyRun Android adaptations."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import stat
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


KIVY_REVISION = "20d74dcd30f143abbd1aa94c76bafc5bd934d5bd"
KIVY_ARCHIVE_URL = (
    "https://codeload.github.com/kivy/kivy/zip/" + KIVY_REVISION
)
KIVY_ARCHIVE_SHA256 = "0b9e566a43f184b17f184b704e7950e61069f9194b62e2533a2ad26f6cbfed78"
KIVY_ARCHIVE_NAME = "kivy-" + KIVY_REVISION + ".zip"
KIVY_ARCHIVE_ROOT = "kivy-" + KIVY_REVISION


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_relative(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or "\\" in name or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"unsafe Kivy archive member: {name!r}")
    return path


def extract_source(archive: Path, destination: Path) -> Path:
    source_root = destination / KIVY_ARCHIVE_ROOT
    source_root.mkdir(parents=True)
    with zipfile.ZipFile(archive) as source:
        members = []
        for member in source.infolist():
            if member.is_dir():
                continue
            relative = safe_relative(member.filename)
            unix_mode = member.external_attr >> 16
            if relative.parts[0] != KIVY_ARCHIVE_ROOT or stat.S_ISLNK(unix_mode):
                raise ValueError(f"unexpected Kivy source archive member: {member.filename!r}")
            members.append((member, relative.parts[1:]))
        if not members or not any(parts == ("setup.py",) for _, parts in members):
            raise ValueError("pinned Kivy source archive does not contain setup.py")
        for member, parts in members:
            if not parts:
                continue
            target = source_root.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(member) as origin, target.open("xb") as output:
                shutil.copyfileobj(origin, output, 64 * 1024)
    return source_root


def replace_once(path: Path, old: str, new: str, *, allow_already_patched: bool = True) -> None:
    text = path.read_text(encoding="utf-8")
    occurrences = text.count(old)
    if occurrences == 1:
        path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
    elif occurrences == 0 and allow_already_patched and new in text:
        return
    else:
        raise ValueError(f"{path}: expected one exact Android patch context, found {occurrences}")


def prepare_android_source(source_root: Path) -> None:
    setup = source_root / "setup.py"
    replace_once(
        setup,
        "if platform in ('ios', 'android'):\n    # NEVER use or declare cython on these platforms",
        "if platform == 'ios':\n    # PyRun compiles Android extensions from the pinned Cython sources",
    )

    pyproject = source_root / "pyproject.toml"
    for old, new in (
        ('"setuptools~=69.2.0"', '"setuptools==80.9.0"'),
        ('"wheel~=0.44.0"', '"wheel==0.45.1"'),
        ('"packaging~=24.0"', '"packaging==25.0"'),
        ('"cython>=0.29.1,<=3.0.11"', '"Cython==3.0.11"'),
    ):
        replace_once(pyproject, old, new)

    parser = source_root / "kivy" / "lang" / "parser.py"
    replace_once(
        parser,
        """                if isinstance(n, ast.Str):
                    # NOTE: required for python3.6
                    yield from cls.get_names_from_expression(n.s)
                else:
                    yield from cls.get_names_from_expression(n.value)
""",
        """                # Python 3.13 represents strings as ast.Constant.
                yield from cls.get_names_from_expression(n.value)
""",
    )

    # This pinned upstream declaration already carries ``nogil``. Keep it
    # intact; duplicating the modifier makes Cython reject the declaration.
    sdl_declarations = source_root / "kivy" / "lib" / "sdl2.pxi"
    if sdl_declarations.read_text(encoding="utf-8").count(
        "cdef void SDL_GL_SwapWindow(SDL_Window * window) nogil"
    ) != 1:
        raise ValueError("pinned SDL2 declarations do not expose the expected nogil swap function")
    sdl_window = source_root / "kivy" / "core" / "window" / "_window_sdl2.pyx"
    if "        with nogil:\n            SDL_GL_SwapWindow(self.win)" not in sdl_window.read_text(encoding="utf-8"):
        raise ValueError("pinned Kivy SDL window source no longer releases the GIL around swap")

    package_data_marker = """                [
                    'setupconfig.py',
                ] + binary_deps
"""
    package_data_replacement = """                [
                    'setupconfig.py',
                    'LICENSE.txt',
                ] + binary_deps
"""
    replace_once(setup, package_data_marker, package_data_replacement)

    # Kivy's Pyjnius Android clipboard implementation is tied to the
    # python-for-android activity/bootstrap classes. PyRun uses SDL2's own
    # clipboard provider instead, so don't probe the incompatible provider.
    clipboard = source_root / "kivy" / "core" / "clipboard" / "__init__.py"
    replace_once(
        clipboard,
        "if platform == 'android':\n    _clipboards.append(",
        "if platform == 'android' and not USE_SDL2:\n    _clipboards.append(",
    )
    license_source = source_root / "LICENSE"
    if not license_source.is_file():
        raise FileNotFoundError("pinned Kivy MIT license is missing")
    shutil.copyfile(license_source, source_root / "kivy" / "LICENSE.txt")


def prepare(output_dir: Path, cache_dir: Path) -> Path:
    output_dir = output_dir.resolve()
    cache_dir = cache_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"refusing to replace existing Kivy source directory: {output_dir}")
    output_dir.mkdir(parents=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive = cache_dir / KIVY_ARCHIVE_NAME
    try:
        if not archive.is_file() or sha256_file(archive) != KIVY_ARCHIVE_SHA256:
            temporary = archive.with_suffix(archive.suffix + ".download")
            temporary.unlink(missing_ok=True)
            request = urllib.request.Request(
                KIVY_ARCHIVE_URL,
                headers={"User-Agent": "PyRun-wheel-builder/1"},
            )
            with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as target:
                shutil.copyfileobj(response, target)
            if sha256_file(temporary) != KIVY_ARCHIVE_SHA256:
                temporary.unlink(missing_ok=True)
                raise ValueError("SHA-256 verification failed for the pinned Kivy archive")
            temporary.replace(archive)

        source_root = extract_source(archive, output_dir)
        prepare_android_source(source_root)
        return source_root
    except Exception:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--cache-dir", required=True, type=Path)
    args = parser.parse_args()
    source_root = prepare(args.output_dir, args.cache_dir)
    print(f"Prepared pinned Kivy source for PyRun Android builds at {source_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
