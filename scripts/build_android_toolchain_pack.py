"""Build a relocatable Android-host compiler pack from pinned Termux packages.

The reference application's compiler archive is not redistributed here.  This
builder consumes the public Termux package index, records every package and
digest, and emits a compact pack containing Android-hosted Clang/LLVM/Flang
plus Make, m4 and pkg-config. CMake, Ninja and the standalone binutils package
are intentionally left out of this runtime profile; native recipes can use the
Android NDK build tools. The generated manifest is signed by the protected
release workflow after this script completes.

The pack is intentionally installed under PyRun's private files directory. A
small wrapper supplies resource/sysroot paths because Termux binaries are
compiled with the normal Termux prefix. No network or shell is used by the
Android runtime; all network access happens in this release builder.
"""

from __future__ import annotations

import argparse
import bz2
import hashlib
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import tempfile
import urllib.request
import gzip


ABI_TO_TERMUX = {"arm64_v8a": "aarch64", "x86_64": "x86_64"}
ANDROID_PREFIX = "/data/user/0/com.dactechstudio.pythonrunner/files/toolchain"
DEFAULT_REPOSITORY = "https://packages.termux.dev/apt/termux-main"
REQUIRED_PACKAGES = (
    "clang",
    "flang",
    "lld",
    "llvm",
    "make",
    "m4",
    "pkg-config",
)
TOOL_NAMES = (
    "clang",
    "clang++",
    "ld.lld",
    "llvm-ar",
    "llvm-strip",
    "make",
    "m4",
    "pkg-config",
    "flang",
)
PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.-]*$")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stanzas(data: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for raw in re.split(r"\n\s*\n", data):
        fields: dict[str, str] = {}
        current = None
        for line in raw.splitlines():
            if line.startswith((" ", "\t")) and current:
                fields[current] += "\n" + line.strip()
                continue
            if ": " not in line:
                continue
            current, value = line.split(": ", 1)
            fields[current] = value
        if fields.get("Package") and fields.get("Filename"):
            records.append(fields)
    return records


def _dependency_names(value: str) -> list[str]:
    result: list[str] = []
    # Alternatives are intentionally resolved to the first package available
    # in the pinned repository. Version constraints are enforced by the
    # repository's index selection, not silently ignored by a second source.
    for group in value.split(","):
        candidate = group.split("|", 1)[0].strip()
        candidate = re.sub(r"\s*\([^)]*\)", "", candidate)
        candidate = candidate.split(":", 1)[0].strip()
        if candidate and PACKAGE_NAME_RE.fullmatch(candidate):
            result.append(candidate)
    return result


def load_index(repository: str, arch: str, cache_dir: Path) -> dict[str, dict[str, str]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / f"Packages-{arch}.gz"
    if not cache.exists():
        url = f"{repository.rstrip('/')}/dists/stable/main/binary-{arch}/Packages.gz"
        with urllib.request.urlopen(url, timeout=60) as response, cache.open("wb") as output:
            shutil.copyfileobj(response, output)
    import io

    with gzip.open(cache, "rt", encoding="utf-8") as source:
        records = _stanzas(source.read())
    packages: dict[str, dict[str, str]] = {}
    for record in records:
        name = record["Package"]
        packages.setdefault(name, record)
    return packages


def resolve_packages(index: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    pending = list(REQUIRED_PACKAGES)
    resolved: dict[str, dict[str, str]] = {}
    while pending:
        name = pending.pop(0)
        if name in resolved:
            continue
        record = index.get(name)
        if record is None:
            raise RuntimeError(f"Termux package is not present in the pinned index: {name}")
        resolved[name] = record
        for dependency in _dependency_names(record.get("Depends", "")):
            if dependency not in resolved:
                pending.append(dependency)
    return [resolved[name] for name in sorted(resolved)]


def download_package(record: dict[str, str], repository: str, cache_dir: Path) -> Path:
    filename = Path(record["Filename"]).name
    target = cache_dir / filename
    if not target.exists() or sha256(target) != record["SHA256"]:
        target.parent.mkdir(parents=True, exist_ok=True)
        url = f"{repository.rstrip('/')}/{record['Filename']}"
        with urllib.request.urlopen(url, timeout=120) as response, target.open("wb") as output:
            shutil.copyfileobj(response, output)
        if sha256(target) != record["SHA256"]:
            raise RuntimeError(f"downloaded package checksum mismatch: {filename}")
    return target


def _data_tar(deb: Path) -> bytes:
    data = deb.read_bytes()
    if not data.startswith(b"!<arch>\n"):
        raise RuntimeError(f"not a Debian archive: {deb.name}")
    offset = 8
    while offset < len(data):
        header = data[offset : offset + 60]
        offset += 60
        if len(header) != 60:
            raise RuntimeError(f"truncated Debian archive: {deb.name}")
        name = header[:16].decode("ascii").strip().rstrip("/")
        size = int(header[48:58].decode("ascii").strip())
        payload = data[offset : offset + size]
        offset += size + size % 2
        if not name.startswith("data.tar"):
            continue
        if name.endswith(".xz"):
            return lzma.decompress(payload)
        if name.endswith(".gz"):
            return gzip.decompress(payload)
        if name.endswith(".bz2"):
            return bz2.decompress(payload)
        raise RuntimeError(f"unsupported Debian compression in {deb.name}: {name}")
    raise RuntimeError(f"data member missing from Debian archive: {deb.name}")


def _safe_pack_path(name: str) -> Path | None:
    normalized = name.lstrip("./")
    prefix = "data/data/com.termux/files/usr/"
    if normalized == "data/data/com.termux/files/usr":
        return Path(".")
    if not normalized.startswith(prefix):
        return None
    relative = PurePosixPath(normalized[len(prefix) :])
    if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        return None
    # Documentation/manpage packages are not needed by an in-app compiler and
    # several Termux split packages intentionally own overlapping compressed
    # manpages. Keep the runtime pack deterministic and record package metadata
    # separately instead of shipping those duplicate files.
    if relative.parts[:2] in (("share", "man"), ("share", "info"), ("share", "doc")):
        return None
    return Path(*relative.parts)


def extract_deb(deb: Path, root: Path) -> None:
    with tarfile.open(fileobj=__import__("io").BytesIO(_data_tar(deb)), mode="r:") as archive:
        for member in archive.getmembers():
            relative = _safe_pack_path(member.name)
            if relative is None:
                continue
            destination = (root / relative).resolve()
            if not destination.is_relative_to(root.resolve()):
                raise RuntimeError(f"archive path escapes pack root: {member.name}")
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if member.issym():
                linkname = member.linkname
                termux_prefix = "/data/data/com.termux/files/usr/"
                if linkname.startswith(termux_prefix):
                    target = root / Path(*PurePosixPath(linkname[len(termux_prefix) :]).parts)
                    linkname = os.path.relpath(target, destination.parent)
                elif linkname.startswith("/"):
                    raise RuntimeError(f"absolute link escapes pack root: {member.name} -> {linkname}")
                resolved_link = (destination.parent / linkname).resolve()
                if not resolved_link.is_relative_to(root.resolve()):
                    raise RuntimeError(f"relative link escapes pack root: {member.name} -> {linkname}")
                if destination.exists() or destination.is_symlink():
                    if destination.is_symlink() and os.readlink(destination) == linkname:
                        continue
                    raise RuntimeError(f"conflicting symlink in pack: {relative}")
                destination.symlink_to(linkname)
                continue
            if member.islnk():
                target_relative = _safe_pack_path(member.linkname)
                if target_relative is None:
                    raise RuntimeError(f"invalid hardlink target in pack: {member.name} -> {member.linkname}")
                target = (root / target_relative).resolve()
                if not target.is_relative_to(root.resolve()) or not target.exists():
                    raise RuntimeError(f"invalid hardlink in pack: {member.name}")
                if destination.exists():
                    continue
                os.link(target, destination)
                continue
            if not member.isfile():
                continue
            payload = archive.extractfile(member).read()  # type: ignore[union-attr]
            if destination.is_symlink():
                # A few split Termux packages replace a symlink with the
                # concrete header/library at the same path. Keep the concrete
                # package payload; never follow a package symlink while
                # writing into the pack.
                destination.unlink()
            if destination.exists():
                if destination.read_bytes() != payload:
                    raise RuntimeError(f"conflicting file in pack: {relative}")
            else:
                destination.write_bytes(payload)
            destination.chmod(stat.S_IMODE(member.mode) or 0o644)


def _real_binary(root: Path, names: tuple[str, ...]) -> Path:
    for name in names:
        candidate = root / "bin" / name
        if not candidate.exists() and not candidate.is_symlink():
            continue
        resolved = candidate.resolve()
        if resolved.is_file() and resolved.is_relative_to(root.resolve() / "bin"):
            return resolved
    raise RuntimeError(f"compiler executable missing after extraction: {names}")


def _write_wrapper(
    path: Path,
    target: str,
    resource_dir: str | None = None,
    extra: str = "",
    argument_prefix: str = "",
    target_triple: str | None = None,
    cxx_include: bool = False,
    passthrough_arg: str | None = None,
    include_root: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    target_flags = ""
    if target_triple:
        abi_include = target_triple.removesuffix("24")
        target_flags = (
            f'--target={target_triple} --sysroot="$PYRUN_TOOLCHAIN_ROOT" '
            + (f'-isystem "$PYRUN_TOOLCHAIN_ROOT/include/c++/v1" ' if cxx_include else "")
            + (f'-isystem "$PYRUN_TOOLCHAIN_ROOT/include/{abi_include}" ' if include_root else "")
            + ( '-isystem "$PYRUN_TOOLCHAIN_ROOT/include" ' if include_root else "")
        )
    passthrough = (
        f'\nif [ "${{1:-}}" = "{passthrough_arg}" ]; then\n'
        f'  exec "$PYRUN_TOOLCHAIN_ROOT/bin/{target}" "$@"\n'
        "fi\n"
        if passthrough_arg
        else ""
    )
    resource = (
        f'{passthrough}\nexec "$PYRUN_TOOLCHAIN_ROOT/bin/{target}" {argument_prefix}{target_flags}-resource-dir '
        f'"$PYRUN_TOOLCHAIN_ROOT/{resource_dir}" '
        + (f'-isystem "$PYRUN_TOOLCHAIN_ROOT/include" ' if include_root else "")
        + f'-L "$PYRUN_TOOLCHAIN_ROOT/lib" "$@"\n'
        if resource_dir
        else f'{passthrough}\nexec "$PYRUN_TOOLCHAIN_ROOT/bin/{target}" {argument_prefix}{target_flags}"$@"\n'
    )
    text = "#!/system/bin/sh\nset -eu\n: \"${PYRUN_TOOLCHAIN_ROOT:?toolchain root is not configured}\"\n" + extra + resource
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _create_wrappers(root: Path, abi: str) -> dict[str, str]:
    clang = _real_binary(root, ("clang-21", "clang"))
    clang_cpp = _real_binary(root, ("clang++-21", "clang++"))
    flang = _real_binary(root, ("flang-new-21", "flang-21", "flang-new", "flang"))
    resource = next((p for p in (root / "lib" / "clang").iterdir() if p.is_dir()), None)
    if resource is None:
        raise RuntimeError("LLVM resource directory missing from pack")
    resource_relative = resource.relative_to(root).as_posix()
    # Termux exposes compiler entry points as symlinks (for example clang ->
    # clang-21).  Writing a wrapper through that symlink would overwrite the
    # real compiler binary and make the published pack recursively invoke its
    # own wrapper.  Preserve the resolved binary before replacing the entry
    # point with our Android-aware wrapper.
    def preserve_target(wrapper_name: str, target: Path) -> Path:
        wrapper = root / "bin" / wrapper_name
        resolved_wrapper = wrapper.resolve() if wrapper.exists() or wrapper.is_symlink() else wrapper
        target = target.resolve()
        if resolved_wrapper == target:
            # A symlink entry point can be removed without moving the real
            # binary.  Keeping its original basename is important for Flang:
            # the driver selects its frontend mode from argv[0].
            if wrapper.is_symlink():
                wrapper.unlink()
                return target
            preserved = root / "bin" / f"{wrapper_name}.real"
            if not preserved.exists():
                shutil.move(target, preserved)
            return preserved
        return target

    clang = preserve_target("clang", clang)
    clang_cpp = preserve_target("clang++", clang_cpp)
    flang = preserve_target("flang", flang)
    target_triple = {
        "arm64_v8a": "aarch64-linux-android24",
        "x86_64": "x86_64-linux-android24",
    }[abi]
    _write_wrapper(
        root / "bin" / "clang",
        clang.name,
        resource_relative,
        'export PREFIX="$PYRUN_TOOLCHAIN_ROOT"\n',
        target_triple=target_triple,
    )
    cpp_mode = "--driver-mode=g++ " if clang_cpp == clang else ""
    _write_wrapper(
        root / "bin" / "clang++",
        clang_cpp.name,
        resource_relative,
        'export PREFIX="$PYRUN_TOOLCHAIN_ROOT"\n',
        argument_prefix=f"{cpp_mode}-lc++_shared ",
        target_triple=target_triple,
        cxx_include=True,
    )
    _write_wrapper(
        root / "bin" / "flang",
        flang.name,
        resource_relative,
        'export PREFIX="$PYRUN_TOOLCHAIN_ROOT"\n',
        target_triple=target_triple,
        passthrough_arg="-fc1",
        include_root=False,
    )
    pkg_config = root / "bin" / "pkg-config"
    real_pkg = root / "bin" / "pkg-config.real"
    if pkg_config.is_symlink():
        target = pkg_config.resolve()
        pkg_config.unlink()
        if target.exists() and target != real_pkg:
            target.rename(real_pkg)
    elif pkg_config.exists():
        pkg_config.rename(real_pkg)
    _write_wrapper(
        pkg_config,
        real_pkg.name,
        extra='export PREFIX="$PYRUN_TOOLCHAIN_ROOT"\nexport PKG_CONFIG_LIBDIR="$PYRUN_TOOLCHAIN_ROOT/lib/pkgconfig:$PYRUN_TOOLCHAIN_ROOT/share/pkgconfig"\n',
    )
    tools = {
        "clang": "bin/clang",
        "clang++": "bin/clang++",
        "flang": "bin/flang",
        "ld.lld": "bin/ld.lld",
        "llvm-ar": "bin/llvm-ar",
        "llvm-strip": "bin/llvm-strip",
        "make": "bin/make",
        "m4": "bin/m4",
        "pkg-config": "bin/pkg-config",
    }
    for name, relative in tools.items():
        if not (root / relative).exists() and not (root / relative).is_symlink():
            raise RuntimeError(f"required tool missing after extraction: {name}")
    return tools


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _prune_runtime(root: Path, abi: str) -> None:
    """Remove Termux build-time payload that the in-app compilers never use.

    LLVM packages contain their own developer toolchain: optimizers, language
    servers, MLIR/Flang static libraries, sanitizer runtimes and generator
    headers. None is needed to compile a user C/C++/Fortran source with the
    three exposed drivers. Keeping the pruning policy here makes the archive
    deterministic and leaves the package provenance in ``metadata`` intact.
    """
    bin_dir = root / "bin"
    keep_bin = {
        "clang", "clang-21", "clang++", "clang++-21",
        "flang", "flang-21", "flang-new", "flang-new-21",
        "lld", "ld.lld", "llvm-ar", "llvm-strip", "llvm-objcopy",
        "make", "m4", "pkg-config",
    }
    for child in bin_dir.iterdir():
        if child.name not in keep_bin:
            _remove_path(child)

    include_dir = root / "include"
    for name in ("clang", "clang-c", "clang-tidy", "flang", "llvm", "llvm-c", "mlir", "mlir-c", "python3.14"):
        candidate = include_dir / name
        if candidate.exists() or candidate.is_symlink():
            _remove_path(candidate)
    target_include = {
        "arm64_v8a": "aarch64-linux-android",
        "x86_64": "x86_64-linux-android",
    }[abi]
    for child in include_dir.iterdir():
        if child.name.endswith("-linux-android") and child.name != target_include:
            _remove_path(child)

    lib_dir = root / "lib"
    for child in list(lib_dir.iterdir()):
        if child.name in {"cmake", "libear", "libscanbuild", "python3.14"}:
            _remove_path(child)
            continue
        if child.is_file() and child.suffix == ".a":
            # Required by the Android clang driver for the default link line.
            # Keep this small runtime archive while pruning optional
            # sanitizer/compiler build payload.
            keep = child.name in {
                "libunwind.a",
                "libflang_rt.runtime.a",
                "libc++_static.a",
            }
            keep = keep or child.name.startswith("libclang_rt.builtins-")
            if not keep:
                _remove_path(child)

    share_dir = root / "share"
    for child in list(share_dir.iterdir()):
        if child.name != "pkgconfig":
            _remove_path(child)

    # Pruning must never leave a broken symlink in the published archive.
    for child in root.rglob("*"):
        if child.is_symlink() and not child.exists():
            child.unlink()


def _manifest(root: Path, abi: str, version: str, packages: list[dict[str, str]], tools: dict[str, str]) -> dict:
    records = []
    for name, relative in sorted(tools.items()):
        path = root / relative
        records.append({
            "name": name,
            "path": relative,
            "sha256": sha256(path),
            "executable": True,
        })
    return {
        "schemaVersion": 1,
        "profile": "full",
        "version": version,
        "abi": abi,
        "androidMinApi": 25,
        "installPrefix": ANDROID_PREFIX,
        "requiredTools": ["clang", "clang++", "ld.lld", "llvm-ar", "llvm-strip", "make", "m4", "pkg-config"],
        "fortranTools": ["flang"],
        "tools": records,
        "source": {
            "repository": DEFAULT_REPOSITORY,
            "architecture": ABI_TO_TERMUX[abi],
            "packages": [
                {key: record[key] for key in ("Package", "Version", "Architecture", "Filename", "SHA256", "Homepage") if key in record}
                for record in packages
            ],
        },
        "proof": [
            {"language": "c", "source": "proof/hello.c", "command": "clang proof/hello.c -o proof/hello-c"},
            {"language": "c++", "source": "proof/hello.cpp", "command": "clang++ proof/hello.cpp -o proof/hello-cpp"},
            {"language": "fortran", "source": "proof/hello.f90", "command": "flang proof/hello.f90 -o proof/hello-fortran"},
        ],
    }


def build(abi: str, output: Path, version: str, repository: str, cache_dir: Path) -> dict:
    if abi not in ABI_TO_TERMUX:
        raise ValueError(f"unsupported ABI: {abi}")
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"refusing to replace output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"pyrun-toolchain-{abi}-"))
    try:
        index = load_index(repository, ABI_TO_TERMUX[abi], cache_dir)
        packages = resolve_packages(index)
        package_cache = cache_dir / "debs" / ABI_TO_TERMUX[abi]
        root = work / "pack"
        root.mkdir()
        for record in packages:
            extract_deb(download_package(record, repository, package_cache), root)
        _prune_runtime(root, abi)
        tools = _create_wrappers(root, abi)
        (root / "proof").mkdir()
        (root / "proof" / "hello.c").write_text("#include <stdio.h>\nint main(void) { puts(\"pyrun-c-ok\"); return 0; }\n", encoding="utf-8")
        (root / "proof" / "hello.cpp").write_text("#include <iostream>\nint main() { std::cout << \"pyrun-cpp-ok\\n\"; }\n", encoding="utf-8")
        (root / "proof" / "hello.f90").write_text("program hello\nprint *, 'pyrun-fortran-ok'\nend program hello\n", encoding="utf-8")
        metadata = root / "metadata"
        metadata.mkdir()
        (metadata / "packages.json").write_text(json.dumps(packages, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (metadata / "LICENSES.txt").write_text(
            "PyRun compiler pack sources are Termux packages. See each package's Homepage and the upstream Termux license files.\n"
            "The pack is not the proprietary reference archive; it is built from the pinned public index recorded in packages.json.\n",
            encoding="utf-8",
        )
        manifest = _manifest(root, abi, version, packages, tools)
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with tarfile.open(output, mode="w:xz", preset=6) as archive:
            for child in sorted(root.iterdir(), key=lambda path: path.name):
                archive.add(child, arcname=child.name, recursive=True)
        return manifest
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--abi", required=True, choices=sorted(ABI_TO_TERMUX))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--cache-dir", type=Path, default=Path(".toolchain-cache"))
    args = parser.parse_args()
    manifest = build(args.abi, args.output, args.version, args.repository, args.cache_dir)
    print(f"Built Android compiler pack {args.abi} {manifest['version']} -> {args.output}")
    print(f"Packages: {len(manifest['source']['packages'])}; tools: {len(manifest['tools'])}")


if __name__ == "__main__":
    main()
