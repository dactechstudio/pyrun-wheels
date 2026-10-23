#!/usr/bin/env python3
"""Create a deterministic local Simple Repository snapshot and manifest."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import tempfile
import tomllib
from pathlib import Path, PurePosixPath

from jsonschema import validate
from packaging.utils import parse_wheel_filename
from packaging.version import Version


ROOT = Path(__file__).resolve().parents[1]
RECIPE_DIR = ROOT / "recipes" / "native_probe"
RECIPE_PATH = RECIPE_DIR / "recipe.json"
SCHEMA_PATH = ROOT / "schemas" / "repository-manifest.schema.json"
RECIPE_SCHEMA_PATH = ROOT / "schemas" / "build-recipe.schema.json"
YANKS_PATH = ROOT / "repository" / "yanks.json"
YANKS_SCHEMA_PATH = ROOT / "schemas" / "yanks.schema.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_tree(root: Path) -> str:
    digest = hashlib.sha256()
    excluded = {"build", "dist", "__pycache__", ".pytest_cache"}
    paths = sorted(root.rglob("*"))
    symlinks = [path for path in paths if path.is_symlink()]
    if symlinks:
        raise ValueError(f"source tree contains a symbolic link: {symlinks[0].relative_to(root)}")
    for path in (item for item in paths if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if any(part in excluded for part in Path(relative).parts):
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
        digest.update(b"\n")
    return digest.hexdigest()


def normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _stage_into(
    wheel_dir: Path,
    output_dir: Path,
    source_revision: str,
    yanks_file: Path,
) -> dict:
    recipe = json.loads(RECIPE_PATH.read_text(encoding="utf-8"))
    recipe_schema = json.loads(RECIPE_SCHEMA_PATH.read_text(encoding="utf-8"))
    validate(instance=recipe, schema=recipe_schema)
    yanks = json.loads(yanks_file.read_text(encoding="utf-8"))
    yanks_schema = json.loads(YANKS_SCHEMA_PATH.read_text(encoding="utf-8"))
    validate(instance=yanks, schema=yanks_schema)
    yanked_versions: dict[tuple[str, str], str] = {}
    for item in yanks["yanked"]:
        key = (normalized_name(item["name"]), str(Version(item["version"])))
        if key in yanked_versions:
            raise ValueError(f"duplicate yank policy entry: {key[0]}=={key[1]}")
        yanked_versions[key] = item["reason"]
    recipe_key = (normalized_name(recipe["name"]), str(Version(recipe["version"])))
    unknown_yanks = set(yanked_versions) - {recipe_key}
    if unknown_yanks:
        raise ValueError(f"yank policy references package/version not staged: {sorted(unknown_yanks)}")
    toolchain = tomllib.loads((ROOT / "toolchain.lock").read_text(encoding="utf-8"))
    if recipe["build"]["ndk"] != str(toolchain["ndk"]):
        raise ValueError("recipe NDK version differs from toolchain.lock")
    if recipe["build"]["setuptools"] != str(toolchain["setuptools"]):
        raise ValueError("recipe setuptools version differs from toolchain.lock")
    if recipe["build"]["wheel"] != str(toolchain["wheel"]):
        raise ValueError("recipe wheel version differs from toolchain.lock")
    if recipe["build"]["cibuildwheel"] != str(toolchain["cibuildwheel"]):
        raise ValueError("recipe cibuildwheel version differs from toolchain.lock")
    if int(recipe["android_api_min"]) != int(toolchain["android_api"]):
        raise ValueError("recipe Android API differs from toolchain.lock")
    python_version = "3.13"
    android_api = int(recipe["android_api_min"])
    output_dir.mkdir(parents=True, exist_ok=False)
    package_dir = output_dir / "packages"
    package_dir.mkdir(parents=True, exist_ok=True)
    source_hash = sha256_tree(RECIPE_DIR)
    recipe_hash = sha256_file(RECIPE_PATH)
    sbom_path = ROOT / recipe["sbom"]
    sbom_hash = sha256_file(sbom_path)
    recipe_output = output_dir / "recipes" / "native_probe"
    recipe_output.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(RECIPE_PATH, recipe_output / RECIPE_PATH.name)
    sbom_output = output_dir / "sbom"
    sbom_output.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sbom_path, sbom_output / sbom_path.name)
    toolchain_path = ROOT / "toolchain.lock"
    shutil.copyfile(toolchain_path, output_dir / toolchain_path.name)
    copied_patches: list[dict] = []
    recipe_root = RECIPE_DIR.resolve(strict=True)
    for patch in recipe["patches"]:
        relative = PurePosixPath(patch["path"])
        if relative.is_absolute() or "\\" in patch["path"] or any(part in ("", ".", "..") for part in relative.parts):
            raise ValueError(f"unsafe patch path: {patch['path']}")
        patch_path = (recipe_root / Path(*relative.parts)).resolve(strict=True)
        if not patch_path.is_file() or not patch_path.is_relative_to(recipe_root):
            raise ValueError(f"patch path escapes recipe or is not a file: {patch['path']}")
        actual_patch_hash = sha256_file(patch_path)
        if actual_patch_hash != patch["sha256"]:
            raise ValueError(f"patch hash mismatch: {patch['path']}")
        destination = recipe_output / Path(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(patch_path, destination)
        copied_patches.append(
            {
                "path": f"recipes/native_probe/{patch['path']}",
                "sha256": actual_patch_hash,
            }
        )
    artifacts = []

    wheels = sorted(wheel_dir.glob("*.whl"))
    if len(wheels) != len(recipe["abis"]):
        raise ValueError(f"expected {len(recipe['abis'])} wheel(s), found {len(wheels)}")
    seen_abis: set[str] = set()
    for wheel in wheels:
        distribution, version, _build, tags = parse_wheel_filename(wheel.name)
        target_abis = {
            abi
            for abi in recipe["abis"]
            if any(
                tag.interpreter == recipe["python_tag"].split("-")[0]
                and tag.abi == recipe["python_tag"].split("-")[1]
                and tag.platform == f"android_{android_api}_{abi}"
                for tag in tags
            )
        }
        if len(target_abis) != 1:
            raise ValueError(f"{wheel.name}: does not carry exactly one expected Android ABI tag")
        abi = target_abis.pop()
        if abi in seen_abis:
            raise ValueError(f"duplicate wheel for ABI {abi}")
        seen_abis.add(abi)
        if normalized_name(str(distribution)) != normalized_name(recipe["name"]):
            raise ValueError(f"unexpected package name in {wheel.name}")
        if str(version) != recipe["version"]:
            raise ValueError(f"unexpected package version in {wheel.name}")
        destination = package_dir / wheel.name
        shutil.copyfile(wheel, destination)
        artifact_record = {
                "name": recipe["name"],
                "version": recipe["version"],
                "pythonTag": recipe["python_tag"],
                "abi": abi,
                "androidApi": android_api,
                "filename": wheel.name,
                "sha256": sha256_file(destination),
                "url": f"packages/{wheel.name}",
                "sourceUrl": f"{recipe['source_url'].rstrip('/')}/tree/{source_revision}/recipes/native_probe",
                "sourceTreeSha256": source_hash,
                "recipePath": "recipes/native_probe/recipe.json",
                "recipeSha256": recipe_hash,
                "patches": copied_patches,
                "license": recipe["license"],
                "sbomPath": f"sbom/{sbom_path.name}",
                "sbomSha256": sbom_hash,
                "playRuntimeInstallAllowed": False,
                "yanked": recipe_key in yanked_versions,
            }
        if recipe_key in yanked_versions:
            artifact_record["yankReason"] = yanked_versions[recipe_key]
        artifacts.append(artifact_record)

    if seen_abis != set(recipe["abis"]):
        raise ValueError(f"missing ABI wheel(s): {sorted(set(recipe['abis']) - seen_abis)}")
    artifacts.sort(key=lambda item: item["abi"])
    index_root = output_dir / "simple" / normalized_name(recipe["name"])
    index_root.mkdir(parents=True, exist_ok=True)
    links = []
    for item in artifacts:
        yanked_attribute = (
            f' data-yanked="{html.escape(item["yankReason"], quote=True)}"'
            if item["yanked"]
            else ""
        )
        links.append(
            f'  <a href="../../{html.escape(item["url"], quote=True)}#sha256={item["sha256"]}"'
            f'{yanked_attribute} data-requires-python="&gt;=3.13,&lt;3.14">'
            f'{html.escape(item["filename"])}</a><br>'
        )
    index_html = (
        "<!doctype html>\n<html lang=\"en\">\n<meta charset=\"utf-8\">\n"
        f"<title>{html.escape(recipe['name'])}</title>\n<body>\n"
        + "\n".join(links)
        + "\n</body>\n</html>\n"
    )
    index_path = index_root / "index.html"
    index_path.write_text(index_html, encoding="utf-8")
    output_dir.joinpath("index.html").write_text(
        "<!doctype html>\n<html lang=\"en\"><meta charset=\"utf-8\">\n"
        "<title>PyRun Android wheels</title><h1>PyRun Android wheels</h1>\n"
        f"<a href=\"simple/{normalized_name(recipe['name'])}/\">{html.escape(recipe['name'])}</a>\n"
        "</html>\n",
        encoding="utf-8",
    )

    manifest = {
        "schemaVersion": 1,
        "sourceRevision": source_revision,
        "pythonVersion": python_version,
        "androidMinApi": android_api,
        "ndkVersion": recipe["build"]["ndk"],
        "toolchainPath": "toolchain.lock",
        "toolchainSha256": sha256_file(output_dir / "toolchain.lock"),
        "simpleIndexes": [
            {
                "name": "root",
                "path": "index.html",
                "sha256": sha256_file(output_dir / "index.html"),
            },
            {
                "name": normalized_name(recipe["name"]),
                "path": f"simple/{normalized_name(recipe['name'])}/index.html",
                "sha256": sha256_file(index_path),
            }
        ],
        "artifacts": artifacts,
    }
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validate(instance=manifest, schema=schema)
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    return manifest


def stage(
    wheel_dir: Path,
    output_dir: Path,
    source_revision: str,
    yanks_file: Path | None = None,
) -> dict:
    if output_dir.exists():
        raise FileExistsError(f"refusing to replace existing snapshot: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.stage-", dir=output_dir.parent)
    )
    temporary.rmdir()
    try:
        manifest = _stage_into(
            wheel_dir,
            temporary,
            source_revision,
            yanks_file or YANKS_PATH,
        )
        if output_dir.exists():
            raise FileExistsError(f"snapshot destination appeared during staging: {output_dir}")
        temporary.rename(output_dir)
        return manifest
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--yanks-file", type=Path, default=YANKS_PATH)
    args = parser.parse_args()
    manifest = stage(args.wheel_dir, args.output_dir, args.source_revision, args.yanks_file)
    print(f"Staged {len(manifest['artifacts'])} verified wheel records for {manifest['sourceRevision']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
