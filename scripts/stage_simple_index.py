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
RECIPES_DIR = ROOT / "recipes"
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


def load_recipes(recipe_schema: dict) -> dict[str, dict]:
    recipes: dict[str, dict] = {}
    for recipe_path in sorted(RECIPES_DIR.glob("*/recipe.json")):
        recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
        validate(instance=recipe, schema=recipe_schema)
        key = normalized_name(recipe["name"])
        if key in recipes:
            raise ValueError(f"duplicate recipe name: {key}")
        recipes[key] = {
            "data": recipe,
            "path": recipe_path,
            "root": recipe_path.parent,
            "directory": recipe_path.parent.name,
        }
    return recipes


def _stage_into(
    wheel_dir: Path,
    output_dir: Path,
    source_revision: str,
    yanks_file: Path,
) -> dict:
    recipe_schema = json.loads(RECIPE_SCHEMA_PATH.read_text(encoding="utf-8"))
    recipes = load_recipes(recipe_schema)
    yanks = json.loads(yanks_file.read_text(encoding="utf-8"))
    yanks_schema = json.loads(YANKS_SCHEMA_PATH.read_text(encoding="utf-8"))
    validate(instance=yanks, schema=yanks_schema)

    wheels = sorted(wheel_dir.glob("*.whl"))
    if not wheels:
        raise ValueError("no wheel artifacts found")
    groups: dict[str, list[Path]] = {}
    for wheel in wheels:
        distribution, _version, _build, _tags = parse_wheel_filename(wheel.name)
        key = normalized_name(str(distribution))
        if key not in recipes:
            raise ValueError(f"no reviewed build recipe for wheel {wheel.name}")
        groups.setdefault(key, []).append(wheel)

    staged_recipe_keys = {
        (key, str(Version(recipes[key]["data"]["version"])))
        for key in groups
    }
    yanked_versions: dict[tuple[str, str], str] = {}
    for item in yanks["yanked"]:
        key = (normalized_name(item["name"]), str(Version(item["version"])))
        if key in yanked_versions:
            raise ValueError(f"duplicate yank policy entry: {key[0]}=={key[1]}")
        yanked_versions[key] = item["reason"]
    unknown_yanks = set(yanked_versions) - staged_recipe_keys
    if unknown_yanks:
        raise ValueError(f"yank policy references package/version not staged: {sorted(unknown_yanks)}")

    toolchain = tomllib.loads((ROOT / "toolchain.lock").read_text(encoding="utf-8"))
    for key in groups:
        recipe = recipes[key]["data"]
        for field in (
            "ndk", "setuptools", "wheel", "cibuildwheel", "cmake", "cython",
            "kivy_cython", "ninja", "packaging",
        ):
            if field not in recipe["build"]:
                continue
            if recipe["build"][field] != str(toolchain[field]):
                raise ValueError(f"recipe {key} {field} version differs from toolchain.lock")
        if int(recipe["android_api_min"]) != int(toolchain["android_api"]):
            raise ValueError(f"recipe {key} Android API differs from toolchain.lock")

    android_api = max(int(recipes[key]["data"]["android_api_min"]) for key in groups)
    output_dir.mkdir(parents=True, exist_ok=False)
    package_dir = output_dir / "packages"
    package_dir.mkdir(parents=True, exist_ok=True)
    copied_toolchain = output_dir / "toolchain.lock"
    shutil.copyfile(ROOT / "toolchain.lock", copied_toolchain)

    artifacts: list[dict] = []
    package_links: dict[str, list[dict]] = {}
    for key in sorted(groups):
        entry = recipes[key]
        recipe = entry["data"]
        recipe_root = entry["root"].resolve(strict=True)
        recipe_path = entry["path"]
        package_version = str(Version(recipe["version"]))
        recipe_key = (key, package_version)
        package_artifacts: list[dict] = []
        seen_abis: set[str] = set()

        recipe_rel_dir = f"recipes/{entry['directory']}"
        recipe_output = output_dir / recipe_rel_dir
        recipe_output.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(recipe_path, recipe_output / recipe_path.name)
        recipe_hash = sha256_file(recipe_path)
        source_hash = sha256_tree(recipe_root)

        sbom_path = ROOT / recipe["sbom"]
        sbom_hash = sha256_file(sbom_path)
        sbom_output = output_dir / "sbom"
        sbom_output.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sbom_path, sbom_output / sbom_path.name)

        copied_patches: list[dict] = []
        for patch in recipe["patches"]:
            relative = PurePosixPath(patch["path"])
            if relative.is_absolute() or "\\" in patch["path"] or any(
                part in ("", ".", "..") for part in relative.parts
            ):
                raise ValueError(f"unsafe patch path: {patch['path']}")
            if relative.parts[0] in {"scripts", "recipes"}:
                patch_path = (ROOT / Path(*relative.parts)).resolve(strict=True)
                if not patch_path.is_relative_to(ROOT):
                    raise ValueError(f"patch path escapes repository: {patch['path']}")
                patch_manifest_path = relative.as_posix()
            else:
                patch_path = (recipe_root / Path(*relative.parts)).resolve(strict=True)
                patch_manifest_path = f"{recipe_rel_dir}/{relative.as_posix()}"
            if not patch_path.is_file():
                raise ValueError(f"patch path is not a file: {patch['path']}")
            if relative.parts[0] != "scripts" and not patch_path.is_relative_to(recipe_root):
                raise ValueError(f"patch path escapes recipe: {patch['path']}")
            actual_patch_hash = sha256_file(patch_path)
            if actual_patch_hash != patch["sha256"]:
                raise ValueError(f"patch hash mismatch: {patch['path']}")
            destination = output_dir / patch_manifest_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(patch_path, destination)
            copied_patches.append({"path": patch_manifest_path, "sha256": actual_patch_hash})

        for wheel in groups[key]:
            distribution, version, _build, tags = parse_wheel_filename(wheel.name)
            target_abis = {
                abi
                for abi in recipe["abis"]
                if any(
                    tag.interpreter == recipe["python_tag"].split("-")[0]
                    and tag.abi == recipe["python_tag"].split("-")[1]
                    and tag.platform == f"android_{recipe['android_api_min']}_{abi}"
                    for tag in tags
                )
            }
            if len(target_abis) != 1:
                raise ValueError(f"{wheel.name}: does not carry exactly one expected Android ABI tag")
            abi = target_abis.pop()
            if abi in seen_abis:
                raise ValueError(f"duplicate {recipe['name']} wheel for ABI {abi}")
            seen_abis.add(abi)
            if normalized_name(str(distribution)) != key:
                raise ValueError(f"unexpected package name in {wheel.name}")
            if str(version) != recipe["version"]:
                raise ValueError(f"unexpected package version in {wheel.name}")

            destination = package_dir / wheel.name
            shutil.copyfile(wheel, destination)
            source_url = recipe["source_url"].rstrip("/")
            if recipe.get("source_revision"):
                source_url = f"{source_url}/tree/{recipe['source_revision']}"
            else:
                source_url = f"{source_url}/tree/{source_revision}/recipes/{entry['directory']}"
            artifact = {
                "name": recipe["name"],
                "version": recipe["version"],
                "pythonTag": recipe["python_tag"],
                "abi": abi,
                "androidApi": int(recipe["android_api_min"]),
                "filename": wheel.name,
                "sha256": sha256_file(destination),
                "url": f"packages/{wheel.name}",
                "sourceUrl": source_url,
                "sourceTreeSha256": source_hash,
                "recipePath": f"{recipe_rel_dir}/recipe.json",
                "recipeSha256": recipe_hash,
                "patches": copied_patches,
                "license": recipe["license"],
                "sbomPath": f"sbom/{sbom_path.name}",
                "sbomSha256": sbom_hash,
                "playRuntimeInstallAllowed": False,
                "yanked": recipe_key in yanked_versions,
            }
            if recipe_key in yanked_versions:
                artifact["yankReason"] = yanked_versions[recipe_key]
            if recipe.get("source_revision"):
                artifact["upstreamRevision"] = recipe["source_revision"]
                artifact["upstreamSubmodules"] = recipe.get("source_submodules", {})
            artifacts.append(artifact)
            package_artifacts.append(artifact)

        if seen_abis != set(recipe["abis"]):
            raise ValueError(f"{recipe['name']}: missing ABI wheel(s): {sorted(set(recipe['abis']) - seen_abis)}")
        package_artifacts.sort(key=lambda item: item["abi"])
        package_links[key] = package_artifacts

    indexes: list[dict] = []
    root_links: list[str] = []
    for package_name, package_artifacts in sorted(package_links.items()):
        index_root = output_dir / "simple" / package_name
        index_root.mkdir(parents=True, exist_ok=True)
        links = []
        for item in package_artifacts:
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
        package_title = package_artifacts[0]["name"]
        index_path = index_root / "index.html"
        index_path.write_text(
            "<!doctype html>\n<html lang=\"en\">\n<meta charset=\"utf-8\">\n"
            f"<title>{html.escape(package_title)}</title>\n<body>\n"
            + "\n".join(links)
            + "\n</body>\n</html>\n",
            encoding="utf-8",
        )
        indexes.append(
            {
                "name": package_name,
                "path": f"simple/{package_name}/index.html",
                "sha256": sha256_file(index_path),
            }
        )
        root_links.append(f'<a href="simple/{package_name}/">{html.escape(package_title)}</a><br>')

    root_index = output_dir / "index.html"
    root_index.write_text(
        "<!doctype html>\n<html lang=\"en\"><meta charset=\"utf-8\">\n"
        "<title>PyRun Android wheels</title><h1>PyRun Android wheels</h1>\n"
        + "\n".join(root_links)
        + "\n</html>\n",
        encoding="utf-8",
    )
    indexes.insert(
        0,
        {"name": "root", "path": "index.html", "sha256": sha256_file(root_index)},
    )

    manifest = {
        "schemaVersion": 1,
        "sourceRevision": source_revision,
        "pythonVersion": "3.13",
        "androidMinApi": android_api,
        "ndkVersion": str(toolchain["ndk"]),
        "toolchainPath": "toolchain.lock",
        "toolchainSha256": sha256_file(copied_toolchain),
        "simpleIndexes": indexes,
        "artifacts": sorted(artifacts, key=lambda item: (normalized_name(item["name"]), item["abi"])),
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
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.stage-", dir=output_dir.parent))
    temporary.rmdir()
    try:
        manifest = _stage_into(wheel_dir, temporary, source_revision, yanks_file or YANKS_PATH)
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
